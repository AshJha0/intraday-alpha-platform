"""Feature pipeline: replay normalized data -> feature + label parquet files.

Usage (from python/ with PYTHONPATH=src):

    python3 -m iap.features [--data-dir ../data/normalized]
                            [--out-dir ../data/features]
                            [--configs ../configs]
                            [--registry-out ../data/reference/feature_registry.json]
                            [--cadence-ms 100]
                            [--files eq_20260824.normalized.iap1 ...]
                            [--workers N] [--worker-mem-gb 5]

For every normalized input file (sorted by name = trading-day order; a fresh
engine per day, session profiles carried across days per instrument), the
pipeline replays events through the FeatureEngine and writes, per instrument:

    data/features/features_<instrument_id>.parquet

with columns: instrument_id, exchange_ts, one float64 column per registered
feature (NaN where invalid), a packed validity bitset, and per-horizon label
columns label_mid_<h> / label_cost_<h> / label_valid_<h> / label_reason_<h>
/ label_reopen_<h> (event-time forward returns, mid-to-mid and
cost-adjusted; two-pointer sweep, no lookahead; the reason bitmask explains
every invalid label — iap.labels.LabelReason; label_reopen_<h>, written
since v1.5.0, is the realised reopen return of a row whose label is invalid
for BLACKOUT alone and NaN everywhere else).

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

Parallel build (``--workers N``, v1.9.0): the expensive part -- replaying
each day's events through a fresh FeatureEngine -- runs one process per
input file.  The only cross-day state is the expanding per-instrument
SessionProfile, which feeds exactly the four ``norm_*_m5_v1`` features and is
itself fed by profile-independent metrics.  Each worker therefore replays its
day against an empty profile and records, per emitted row, the 5-minute
bucket and the four raw metric values; the parent then walks the days in
order, folding those observations into the carried profiles and rewriting the
four norm values + validity bits exactly as the serial engine would have.
Labels, statistics and parquet writes stay serial and in file order, so the
output is byte-identical to ``--workers 1`` (tested).  Memory: every worker
holds one day's events and rows (several GB on a full real ITCH day), so on a
16 GB machine use 2-3 workers for real data; the CLI clamps N to the number
of files, the CPU count and ``physical RAM / --worker-mem-gb`` (default 5).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from array import array
from concurrent.futures import ProcessPoolExecutor
from math import isfinite
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from iap.core.codec import read_iap1, read_jsonl
from iap.features.context import build_contexts
from iap.features.engine import FeatureEngine
from iap.features.registry import build_registry, registry_hash, write_registry
from iap.features.rolling import SessionProfile
from iap.features.spec import FAMILY_ORDER
from iap.features.timeofday import PROFILE_METRICS, _metric_value
from iap.labels.labels import (
    HORIZON_ORDER,
    LabelReason,
    MidSeries,
    compute_labels,
    max_sample_age,
)

_REPO = Path(__file__).resolve().parents[4]
_NAN = float("nan")


class _InstrumentBuffer:
    """Accumulates emitted rows for one instrument within one input file."""

    __slots__ = ("ts", "values", "bits", "series", "last_event_ts", "last_refresh_seq")

    def __init__(self) -> None:
        self.ts: list[int] = []
        self.values = array("d")
        self.bits = bytearray()
        self.series = MidSeries()
        self.last_event_ts = 0
        self.last_refresh_seq = 0


class _ProfileLog:
    """Per-row profile observations recorded by a parallel worker."""

    __slots__ = ("buckets", "curs")

    def __init__(self) -> None:
        self.buckets: list[int] = []
        #: one tuple per row: the PROFILE_METRICS values (None = not computable)
        self.curs: list[tuple] = []


def _replay_file(path: Path, contexts, cadence_ns: int, profiles, record: bool):
    """Replay one input file through a fresh engine.

    Returns ``(n_events, (events_processed, vectors_emitted), buffers, logs)``;
    ``logs`` is ``{iid: _ProfileLog}`` when ``record`` (parallel mode).
    """
    events = _load_events(path)
    engine = FeatureEngine(contexts, cadence_ns=cadence_ns, profiles=profiles)
    buffers: dict[int, _InstrumentBuffer] = {}
    logs: dict[int, _ProfileLog] = {}
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
                buf.series.append(ev.exchange_ts, st.mid, st.spread_ticks * st.tick / 2.0, True)
            else:
                buf.series.append(ev.exchange_ts, float("nan"), float("nan"), False)
        if vec is not None:
            buf.ts.append(vec.timestamp)
            buf.values.extend(vec.values)
            buf.bits.extend(vec.validity_bits())
            if record:
                # _emit is the last thing apply() does, so the state is still
                # the emission state: these are exactly the values the engine
                # folded into the (here empty) profile
                t = vec.timestamp
                log = logs.get(iid)
                if log is None:
                    log = logs[iid] = _ProfileLog()
                log.buckets.append(SessionProfile.bucket_of(t, st.ctx.clock.offset_seconds(t)))
                log.curs.append(tuple(_metric_value(st, m) for m in PROFILE_METRICS))
    counts = (engine.events_processed, engine.vectors_emitted)
    return len(events), counts, buffers, logs


def _worker(job):
    """Process-pool entry point: replay one day against an empty profile."""
    path, configs, cadence_ns = job
    return _replay_file(Path(path), build_contexts(configs), cadence_ns, {}, record=True)


def _patch_norms(
    buffers: dict[int, _InstrumentBuffer],
    logs: dict[int, _ProfileLog],
    profiles: dict,
    norm_idx: list[int],
    nfeat: int,
) -> None:
    """Rewrite one worker day's norm_*_m5_v1 values/bits against the carried
    profiles, folding the day's observations in serial order (mirrors
    timeofday.compute + FeatureEngine._emit + _famutil.put exactly)."""
    nbytes = (nfeat + 7) // 8
    for iid in sorted(logs):
        log = logs[iid]
        buf = buffers[iid]
        prof = profiles.get(iid)
        if prof is None:
            prof = profiles[iid] = SessionProfile(PROFILE_METRICS)
        for r, (bucket, curs) in enumerate(zip(log.buckets, log.curs, strict=True)):
            base = r * nfeat
            for m, cur, j in zip(PROFILE_METRICS, curs, norm_idx, strict=True):
                v = None
                if cur is not None:
                    n, mean = prof.prior(m, bucket)
                    if n >= SessionProfile.MIN_OBS and mean > 0.0:
                        v = cur / mean
                ok = False
                if v is not None:
                    fv = float(v)
                    if isfinite(fv):
                        buf.values[base + j] = fv
                        ok = True
                if not ok:
                    buf.values[base + j] = _NAN
                k = r * nbytes + (j >> 3)
                bit = 1 << (j & 7)
                if ok:
                    buf.bits[k] |= bit
                else:
                    buf.bits[k] &= ~bit & 0xFF
            for m, cur in zip(PROFILE_METRICS, curs, strict=True):
                if cur is not None:
                    prof.update(m, bucket, float(cur))


def _total_ram_bytes() -> int | None:
    """Physical RAM in bytes, or None when it cannot be determined."""
    try:
        if sys.platform == "win32":
            import ctypes

            class _MemStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            ms = _MemStatus()
            ms.dwLength = ctypes.sizeof(_MemStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
                return int(ms.ullTotalPhys)
            return None
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, OSError, ValueError):
        return None


def effective_workers(requested: int, n_files: int, worker_mem_gb: float) -> int:
    """Clamp a requested worker count to the number of files, the CPU count
    and ``physical RAM / worker_mem_gb`` (``requested <= 0`` = auto: the
    largest count the clamps allow; ``worker_mem_gb <= 0`` = no RAM cap)."""
    if requested == 1:
        return 1
    cap = min(max(1, n_files), os.cpu_count() or 1)
    ram = _total_ram_bytes()
    if ram is not None and worker_mem_gb > 0:
        cap = min(cap, max(1, int(ram // int(worker_mem_gb * 2**30))))
    return cap if requested <= 0 else max(1, min(requested, cap))


def _load_events(path: Path):
    if path.suffix == ".iap1":
        return read_iap1(path)
    return read_jsonl(path)


def _flush_file(
    writers: dict[int, pq.ParquetWriter],
    out_dir: Path,
    schema: pa.Schema,
    buffers: dict[int, _InstrumentBuffer],
    names: list[str],
    fam_valid: dict[int, np.ndarray],
    row_counts: dict[int, int],
    label_stats: dict[int, dict],
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
        labels = compute_labels(buf.ts, buf.series, buf.last_event_ts, max_age_ns=max_age)
        # Data-quality facts a researcher must see before trusting a row
        # (RESEARCH round-3): how many emissions came from a CROSSED merged
        # book (a stale LP quote makes spread_ticks < 0), how far apart the
        # rows are, and how many labels are exact zeros / why they are
        # invalid.
        ts_arr = np.asarray(buf.ts, dtype=np.int64)
        gaps = np.diff(ts_arr)
        spread = vals[:, names.index("spread_ticks_v1")]
        spread_ok = validity[:, names.index("spread_ticks_v1")].astype(bool)
        stats = label_stats.setdefault(
            iid,
            {
                "rows": 0,
                "crossed_rows": 0,
                "spread_valid_rows": 0,
                "gap_sum_ns": 0,
                "gap_count": 0,
                "median_sample_gap_ns": 0,
                "label_max_age_ns": 0,
                "series_samples": 0,
                "tradable_samples": 0,
                "by_horizon": {h: {"valid": 0, "zero": 0, "reason": {}} for h in HORIZON_ORDER},
            },
        )
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

        cols: dict[str, pa.Array] = {
            "instrument_id": pa.array([iid] * rows, type=pa.uint32()),
            "exchange_ts": pa.array(buf.ts, type=pa.int64()),
        }
        for j, name in enumerate(names):
            cols[name] = pa.array(vals[:, j], type=pa.float64())
        cols["validity_bits"] = pa.array(
            [bytes(buf.bits[i * nbytes : (i + 1) * nbytes]) for i in range(rows)],
            type=pa.binary(nbytes),
        )
        for h in HORIZON_ORDER:
            lab = labels[h]
            cols[f"label_mid_{h}"] = pa.array(lab.mid, type=pa.float64())
            cols[f"label_cost_{h}"] = pa.array(lab.cost, type=pa.float64())
            cols[f"label_valid_{h}"] = pa.array(lab.valid, type=pa.bool_())
            cols[f"label_reason_{h}"] = pa.array(lab.reason, type=pa.uint8())
            cols[f"label_reopen_{h}"] = pa.array(lab.reopen_mid, type=pa.float64())
        table = pa.table(cols, schema=schema)
        writer = writers.get(iid)
        if writer is None:
            writer = pq.ParquetWriter(
                out_dir / f"features_{iid}.parquet", schema, compression="zstd"
            )
            writers[iid] = writer
        writer.write_table(table)


def _build_schema(names: list[str]) -> pa.Schema:
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
        fields.append(pa.field(f"label_reopen_{h}", pa.float64()))
    return pa.schema(fields)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m iap.features", description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", default=str(_REPO / "data" / "normalized"))
    ap.add_argument("--out-dir", default=str(_REPO / "data" / "features"))
    ap.add_argument("--configs", default=str(_REPO / "configs"))
    ap.add_argument(
        "--registry-out", default=str(_REPO / "data" / "reference" / "feature_registry.json")
    )
    ap.add_argument(
        "--cadence-ms",
        type=int,
        default=100,
        help="emission cadence per instrument (0 = every event)",
    )
    ap.add_argument(
        "--files", nargs="*", default=None, help="specific input file names inside --data-dir"
    )
    ap.add_argument(
        "--workers",
        type=int,
        default=1,
        help="processes replaying days in parallel (1 = serial, 0 = auto); "
        "output is byte-identical to the serial build",
    )
    ap.add_argument(
        "--worker-mem-gb",
        type=float,
        default=5.0,
        help="per-worker memory budget that caps --workers by physical RAM (0 = no cap)",
    )
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

    writers: dict[int, pq.ParquetWriter] = {}
    profiles: dict[int, SessionProfile] = {}
    fam_valid: dict[int, np.ndarray] = {}
    row_counts: dict[int, int] = {}
    label_stats: dict[int, dict] = {}
    total_events = 0
    total_vectors = 0

    cadence_ns = args.cadence_ms * 1_000_000
    workers = effective_workers(args.workers, len(files), args.worker_mem_gb)
    if workers != args.workers:
        print(f"--workers {args.workers} -> {workers} (files / CPUs / RAM cap)", file=sys.stderr)

    def _consume(path: Path, n_events: int, counts, buffers) -> None:
        nonlocal total_events, total_vectors
        total_events += counts[0]
        total_vectors += counts[1]
        _flush_file(writers, out_dir, schema, buffers, names, fam_valid, row_counts, label_stats)
        print(f"{path.name}: {n_events} events -> {counts[1]} vectors", file=sys.stderr)

    if workers <= 1:
        for path in files:
            n_events, counts, buffers, _ = _replay_file(
                path, contexts, cadence_ns, profiles, record=False
            )
            _consume(path, n_events, counts, buffers)
    else:
        norm_idx = [names.index(f"norm_{m}_m5_v1") for m in PROFILE_METRICS]
        jobs = [(str(p), str(args.configs), cadence_ns) for p in files]
        with ProcessPoolExecutor(max_workers=workers) as pool:
            # map() yields in submission (= trading-day) order, so the
            # profile carry, labels and parquet row groups stay serial
            for path, (n_events, counts, buffers, logs) in zip(
                files, pool.map(_worker, jobs), strict=True
            ):
                _patch_norms(buffers, logs, profiles, norm_idx, len(names))
                _consume(path, n_events, counts, buffers)

    for writer in writers.values():
        writer.close()

    fam_slices: dict[str, list[int]] = {f: [] for f in FAMILY_ORDER}
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
                if st.get("spread_valid_rows")
                else None
            ),
            "mean_row_gap_ns": (int(st["gap_sum_ns"] / gap_n) if gap_n else None),
            "median_sample_gap_ns": st.get("median_sample_gap_ns"),
            "label_max_age_ns": st.get("label_max_age_ns"),
            "tradable_sample_frac": (
                round(st["tradable_samples"] / st["series_samples"], 6)
                if st.get("series_samples")
                else None
            ),
            "label_valid_frac_by_horizon": {h: round(by_h[h]["valid"] / rows, 6) for h in by_h},
            "label_zero_frac_by_horizon": {
                h: (round(by_h[h]["zero"] / by_h[h]["valid"], 6) if by_h[h]["valid"] else None)
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
    print(
        json.dumps({k: v for k, v in summary.items() if k != "instruments"}, indent=2),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
