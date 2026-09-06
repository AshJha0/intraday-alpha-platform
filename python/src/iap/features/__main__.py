"""Feature pipeline: replay normalized data -> feature + label parquet files.

Usage (from python/ with PYTHONPATH=src):

    python3 -m iap.features [--data-dir ../data/normalized]
                            [--out-dir ../data/features]
                            [--configs ../configs]
                            [--registry-out ../data/reference/feature_registry.json]
                            [--cadence-ms 100]
                            [--files eq_20260824.normalized.iap1 ...]

For every normalized input file (sorted by name = trading-day order; a fresh
engine per day, session profiles carried across days per instrument), the
pipeline replays events through the FeatureEngine and writes, per instrument:

    data/features/features_<instrument_id>.parquet

with columns: instrument_id, exchange_ts, one float64 column per registered
feature (NaN where invalid), a packed validity bitset, and per-horizon label
columns label_mid_<h> / label_cost_<h> / label_valid_<h> / label_reason_<h>
(event-time forward returns, mid-to-mid and cost-adjusted; two-pointer
sweep, no lookahead; the reason bitmask explains every invalid label —
iap.labels.LabelReason).

A summary JSON is written to data/features/features_summary.json: rows,
valid fraction per family, registry hash, and the data-quality facts a
researcher needs BEFORE trusting a row — ``crossed_frac`` (share of
emissions whose merged book is crossed, i.e. a stale LP quote),
``mean_row_gap_ns`` / ``median_sample_gap_ns``, the pinned
``label_max_age_ns`` freshness bound, ``label_valid_frac_by_horizon``,
``label_zero_frac_by_horizon`` (a 500 ms FX label that is 0 in 99 % of rows
is a sampling artefact, not evidence) and the invalid-label reason counts.

Everything is deterministic: sorted file order, sorted instrument iteration,
event-time only.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from array import array
from pathlib import Path
from typing import Dict, List

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from iap.core.codec import read_iap1, read_jsonl
from iap.features.context import build_contexts
from iap.features.engine import FeatureEngine
from iap.features.registry import build_registry, registry_hash, write_registry
from iap.features.spec import FAMILY_ORDER
from iap.labels.labels import (
    HORIZON_ORDER,
    LabelReason,
    MidSeries,
    compute_labels,
    max_sample_age,
)

_REPO = Path(__file__).resolve().parents[4]


class _InstrumentBuffer:
    """Accumulates emitted rows for one instrument within one input file."""

    __slots__ = ("ts", "values", "bits", "series", "last_event_ts",
                 "last_refresh_seq")

    def __init__(self) -> None:
        self.ts: List[int] = []
        self.values = array("d")
        self.bits = bytearray()
        self.series = MidSeries()
        self.last_event_ts = 0
        self.last_refresh_seq = 0


def _load_events(path: Path):
    if path.suffix == ".iap1":
        return read_iap1(path)
    return read_jsonl(path)


def _flush_file(
    writers: Dict[int, pq.ParquetWriter],
    out_dir: Path,
    schema: pa.Schema,
    buffers: Dict[int, _InstrumentBuffer],
    names: List[str],
    fam_valid: Dict[int, np.ndarray],
    row_counts: Dict[int, int],
    label_stats: Dict[int, dict],
) -> None:
    """Write one row group per instrument for the just-processed file."""
    nfeat = len(names)
    nbytes = (nfeat + 7) // 8
    for iid in sorted(buffers):
        buf = buffers[iid]
        rows = len(buf.ts)
        if rows == 0:
            continue
        vals = np.frombuffer(buf.values, dtype=np.float64).reshape(rows, nfeat)
        packed = np.frombuffer(bytes(buf.bits), dtype=np.uint8).reshape(rows, nbytes)
        validity = np.unpackbits(packed, axis=1, bitorder="little")[:, :nfeat]
        fam_valid.setdefault(iid, np.zeros(nfeat, dtype=np.int64))
        fam_valid[iid] += validity.sum(axis=0, dtype=np.int64)
        row_counts[iid] = row_counts.get(iid, 0) + rows

        max_age = max_sample_age(buf.series)
        labels = compute_labels(buf.ts, buf.series, buf.last_event_ts,
                                max_age_ns=max_age)
        # Data-quality facts a researcher must see before trusting a row
        # (RESEARCH round-3): how many emissions came from a CROSSED merged
        # book (a stale LP quote makes spread_ticks < 0), how far apart the
        # rows are, and how many labels are exact zeros / why they are
        # invalid.
        ts_arr = np.asarray(buf.ts, dtype=np.int64)
        gaps = np.diff(ts_arr)
        spread = vals[:, names.index("spread_ticks_v1")]
        spread_ok = validity[:, names.index("spread_ticks_v1")].astype(bool)
        stats = label_stats.setdefault(iid, {
            "rows": 0, "crossed_rows": 0, "spread_valid_rows": 0,
            "gap_sum_ns": 0, "gap_count": 0, "median_sample_gap_ns": 0,
            "label_max_age_ns": 0, "series_samples": 0,
            "tradable_samples": 0,
            "by_horizon": {h: {"valid": 0, "zero": 0, "reason": {}}
                           for h in HORIZON_ORDER},
        })
        stats["rows"] += rows
        stats["spread_valid_rows"] += int(spread_ok.sum())
        stats["crossed_rows"] += int(((spread < 0) & spread_ok).sum())
        stats["gap_sum_ns"] += int(gaps.sum()) if gaps.size else 0
        stats["gap_count"] += int(gaps.size)
        stats["median_sample_gap_ns"] = buf.series.median_gap_ns()
        stats["label_max_age_ns"] = max_age
        stats["series_samples"] += len(buf.series)
        stats["tradable_samples"] += int(sum(buf.series.tradable))
        for h in HORIZON_ORDER:
            lab = labels[h]
            hv = stats["by_horizon"][h]
            valid_mask = np.asarray(lab.valid, dtype=bool)
            hv["valid"] += int(valid_mask.sum())
            lm = np.asarray(lab.mid, dtype=float)
            hv["zero"] += int(((lm == 0.0) & valid_mask).sum())
            for r in lab.reason:
                if r:
                    for nm in LabelReason.describe(r):
                        hv["reason"][nm] = hv["reason"].get(nm, 0) + 1

        cols: Dict[str, pa.Array] = {
            "instrument_id": pa.array([iid] * rows, type=pa.uint32()),
            "exchange_ts": pa.array(buf.ts, type=pa.int64()),
        }
        for j, name in enumerate(names):
            cols[name] = pa.array(vals[:, j], type=pa.float64())
        cols["validity_bits"] = pa.array(
            [bytes(buf.bits[i * nbytes:(i + 1) * nbytes]) for i in range(rows)],
            type=pa.binary(nbytes),
        )
        for h in HORIZON_ORDER:
            lab = labels[h]
            cols[f"label_mid_{h}"] = pa.array(lab.mid, type=pa.float64())
            cols[f"label_cost_{h}"] = pa.array(lab.cost, type=pa.float64())
            cols[f"label_valid_{h}"] = pa.array(lab.valid, type=pa.bool_())
            cols[f"label_reason_{h}"] = pa.array(lab.reason, type=pa.uint8())
        table = pa.table(cols, schema=schema)
        writer = writers.get(iid)
        if writer is None:
            writer = pq.ParquetWriter(
                out_dir / f"features_{iid}.parquet", schema, compression="zstd"
            )
            writers[iid] = writer
        writer.write_table(table)


def _build_schema(names: List[str]) -> pa.Schema:
    nbytes = (len(names) + 7) // 8
    fields = [
        pa.field("instrument_id", pa.uint32()),
        pa.field("exchange_ts", pa.int64()),
    ]
    fields += [pa.field(n, pa.float64()) for n in names]
    fields.append(pa.field("validity_bits", pa.binary(nbytes)))
    for h in HORIZON_ORDER:
        fields.append(pa.field(f"label_mid_{h}", pa.float64()))
        fields.append(pa.field(f"label_cost_{h}", pa.float64()))
        fields.append(pa.field(f"label_valid_{h}", pa.bool_()))
        fields.append(pa.field(f"label_reason_{h}", pa.uint8()))
    return pa.schema(fields)


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m iap.features",
                                 description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", default=str(_REPO / "data" / "normalized"))
    ap.add_argument("--out-dir", default=str(_REPO / "data" / "features"))
    ap.add_argument("--configs", default=str(_REPO / "configs"))
    ap.add_argument("--registry-out",
                    default=str(_REPO / "data" / "reference" / "feature_registry.json"))
    ap.add_argument("--cadence-ms", type=int, default=100,
                    help="emission cadence per instrument (0 = every event)")
    ap.add_argument("--files", nargs="*", default=None,
                    help="specific input file names inside --data-dir")
    args = ap.parse_args(argv)

    t_start = time.time()
    data_dir = Path(args.data_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.files:
        files = [data_dir / f for f in sorted(args.files)]
    else:
        files = sorted(data_dir.glob("*.normalized.iap1"))
        if not files:
            files = sorted(data_dir.glob("*.normalized.jsonl"))
    if not files:
        raise SystemExit(f"no normalized input files in {data_dir}")

    contexts = build_contexts(args.configs)
    write_registry(args.registry_out)
    registry = build_registry()
    names = [s.name for s in registry]
    schema = _build_schema(names)

    writers: Dict[int, pq.ParquetWriter] = {}
    profiles: Dict[int, object] = {}
    fam_valid: Dict[int, np.ndarray] = {}
    row_counts: Dict[int, int] = {}
    label_stats: Dict[int, dict] = {}
    total_events = 0
    total_vectors = 0

    for path in files:
        events = _load_events(path)
        engine = FeatureEngine(
            contexts, cadence_ns=args.cadence_ms * 1_000_000, profiles=profiles
        )
        buffers: Dict[int, _InstrumentBuffer] = {}
        for ev in events:
            vec = engine.apply(ev)
            iid = ev.instrument_id
            buf = buffers.get(iid)
            if buf is None:
                buf = buffers[iid] = _InstrumentBuffer()
            buf.last_event_ts = ev.exchange_ts
            st = engine.states[iid]
            # One mid sample per BOOK REFRESH (API_FEATURES §6): a refresh
            # that left the merged view one-sided / stale / halted is
            # recorded as a NON-TRADABLE sample so labels cannot span it.
            if st.refresh_seq != buf.last_refresh_seq:
                buf.last_refresh_seq = st.refresh_seq
                if st.label_tradable:
                    buf.series.append(
                        ev.exchange_ts, st.mid,
                        st.spread_ticks * st.tick / 2.0, True
                    )
                else:
                    buf.series.append(
                        ev.exchange_ts, float("nan"), float("nan"), False
                    )
            if vec is not None:
                buf.ts.append(vec.timestamp)
                buf.values.extend(vec.values)
                buf.bits.extend(vec.validity_bits())
        total_events += engine.events_processed
        total_vectors += engine.vectors_emitted
        _flush_file(writers, out_dir, schema, buffers, names, fam_valid,
                    row_counts, label_stats)
        print(f"{path.name}: {len(events)} events -> "
              f"{engine.vectors_emitted} vectors", file=sys.stderr)

    for writer in writers.values():
        writer.close()

    fam_slices: Dict[str, List[int]] = {f: [] for f in FAMILY_ORDER}
    for i, s in enumerate(registry):
        fam_slices[s.family].append(i)
    per_instrument = {}
    for iid in sorted(row_counts):
        rows = row_counts[iid]
        counts = fam_valid[iid]
        st = label_stats.get(iid, {})
        by_h = st.get("by_horizon", {})
        gap_n = st.get("gap_count", 0)
        per_instrument[str(iid)] = {
            "symbol": contexts[iid].symbol,
            "rows": rows,
            "valid_fraction_by_family": {
                fam: round(float(counts[idx].sum()) / (rows * len(idx)), 6)
                for fam, idx in fam_slices.items()
            },
            # data-quality facts (see the module docstring)
            "crossed_frac": (
                round(st["crossed_rows"] / st["spread_valid_rows"], 6)
                if st.get("spread_valid_rows") else None
            ),
            "mean_row_gap_ns": (
                int(st["gap_sum_ns"] / gap_n) if gap_n else None
            ),
            "median_sample_gap_ns": st.get("median_sample_gap_ns"),
            "label_max_age_ns": st.get("label_max_age_ns"),
            "tradable_sample_frac": (
                round(st["tradable_samples"] / st["series_samples"], 6)
                if st.get("series_samples") else None
            ),
            "label_valid_frac_by_horizon": {
                h: round(by_h[h]["valid"] / rows, 6) for h in by_h
            },
            "label_zero_frac_by_horizon": {
                h: (round(by_h[h]["zero"] / by_h[h]["valid"], 6)
                    if by_h[h]["valid"] else None)
                for h in by_h
            },
            "label_invalid_reasons_by_horizon": {
                h: dict(sorted(by_h[h]["reason"].items())) for h in by_h
            },
        }
    summary = {
        "registry_hash": registry_hash(),
        "registered_features": len(registry),
        "cadence_ms": args.cadence_ms,
        "label_horizons": list(HORIZON_ORDER),
        "files": [f.name for f in files],
        "events_processed": total_events,
        "vectors_emitted": total_vectors,
        "runtime_seconds": round(time.time() - t_start, 2),
        "instruments": per_instrument,
    }
    with open(out_dir / "features_summary.json", "w") as f:
        json.dump(summary, f, indent=2)
        f.write("\n")
    print(json.dumps({k: v for k, v in summary.items() if k != "instruments"},
                     indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
