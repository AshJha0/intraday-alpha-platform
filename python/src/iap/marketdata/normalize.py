"""Raw -> normalized market-data pipeline with QC reporting (spec section 8).

Per raw JSONL file (arrival order):

1. **Timestamp normalization**: receive_ts < exchange_ts is clamped to
   exchange_ts (counted per stream as ``ts_clamped``).
2. **Invalid detection**: events violating the canonical contract
   (``iap.core.events.validation_error``) are dropped + counted.
3. **Sequence QC** per stream (venue_id, instrument_id), in arrival order:
   - sequence already seen        -> duplicate, dropped + counted
   - sequence < max seen          -> out-of-order (late arrival), kept + counted
   - sequence > max seen + 1      -> gap, kept + counted (occurrences and
                                     missing-event count); books recover via
                                     the SNAPSHOT bursts present in the stream
4. **Ordering**: surviving events are sorted into event time
   (exchange_ts, then venue/instrument/sequence to restore intra-stream order
   at identical timestamps, then arrival event_id) and event_id is reassigned
   1..N so the normalized file keeps the global-monotone-per-file invariant.

Outputs per raw file ``<stem>.jsonl``: ``<stem>.normalized.jsonl`` +
``<stem>.normalized.iap1`` (byte-exact canonical codecs), plus one combined
Parquet research dataset ``events.parquet`` (pyarrow) and
``qc_report.json`` with per-stream and total counts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

from iap.core.codec import iter_jsonl, write_iap1, write_jsonl
from iap.core.events import FIELDS, MarketEvent, validation_error


def _new_counter() -> Dict[str, int]:
    return {
        "events_in": 0,
        "events_out": 0,
        "gaps": 0,
        "gap_missing_events": 0,
        "duplicates": 0,
        "out_of_order": 0,
        "invalid": 0,
        "ts_clamped": 0,
    }


class _StreamQC:
    """Sequence bookkeeping for one (venue_id, instrument_id) stream."""

    __slots__ = ("seen", "max_seq")

    def __init__(self) -> None:
        self.seen: set = set()
        self.max_seq = 0


def normalize_file(
    raw_path: Path,
    out_dir: Path,
    stream_qc: Dict[str, _StreamQC],
    per_stream: Dict[str, Dict[str, int]],
) -> List[MarketEvent]:
    """Normalize one raw JSONL file; returns the kept, event-time-ordered events.

    ``stream_qc``/``per_stream`` are shared across files of a run so sequences
    that continue across sessions are checked continuously.
    """
    kept: List[MarketEvent] = []
    for ev in iter_jsonl(raw_path):
        key = f"{ev.venue_id}:{ev.instrument_id}"
        counters = per_stream.get(key)
        if counters is None:
            counters = per_stream[key] = _new_counter()
        counters["events_in"] += 1

        # 1. Timestamp normalization.
        if ev.receive_ts < ev.exchange_ts:
            ev.receive_ts = ev.exchange_ts
            counters["ts_clamped"] += 1

        # 2. Invalid detection.
        if validation_error(ev) is not None:
            counters["invalid"] += 1
            continue

        # 3. Sequence QC.
        qc = stream_qc.get(key)
        if qc is None:
            qc = stream_qc[key] = _StreamQC()
        if ev.sequence in qc.seen:
            counters["duplicates"] += 1
            continue
        if ev.sequence < qc.max_seq:
            counters["out_of_order"] += 1
        elif qc.max_seq != 0 and ev.sequence > qc.max_seq + 1:
            counters["gaps"] += 1
            counters["gap_missing_events"] += ev.sequence - qc.max_seq - 1
        qc.seen.add(ev.sequence)
        if ev.sequence > qc.max_seq:
            qc.max_seq = ev.sequence
        counters["events_out"] += 1
        kept.append(ev)

    # 4. Event-time ordering + event_id reassignment. Within one exchange_ts
    # tick, order by stream then sequence so late arrivals (e.g. a delayed
    # EXECUTE whose same-timestamp TRADE print arrived first) are restored to
    # correct intra-stream order; event_id (arrival) is the final tiebreak.
    kept.sort(
        key=lambda e: (e.exchange_ts, e.venue_id, e.instrument_id,
                       e.sequence, e.event_id)
    )
    for i, ev in enumerate(kept):
        ev.event_id = i + 1

    stem = raw_path.stem
    write_jsonl(out_dir / f"{stem}.normalized.jsonl", kept)
    write_iap1(out_dir / f"{stem}.normalized.iap1", kept)
    return kept


def _write_parquet(events_by_file: Dict[str, List[MarketEvent]], path: Path) -> int:
    """Write the combined Parquet research dataset; returns row count."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    cols: Dict[str, list] = {name: [] for name in FIELDS}
    sources: List[str] = []
    for stem in sorted(events_by_file):
        for ev in events_by_file[stem]:
            d = ev.to_dict()
            for name in FIELDS:
                cols[name].append(d[name])
            sources.append(stem)
    schema_types = {
        "event_id": pa.uint64(),
        "instrument_id": pa.uint32(),
        "venue_id": pa.uint16(),
        "exchange_ts": pa.int64(),
        "receive_ts": pa.int64(),
        "sequence": pa.uint64(),
        "event_type": pa.uint8(),
        "side": pa.uint8(),
        "price_ticks": pa.int64(),
        "qty": pa.int64(),
        "order_id": pa.uint64(),
        "trade_id": pa.uint64(),
    }
    arrays = {name: pa.array(cols[name], type=schema_types[name]) for name in FIELDS}
    arrays["source"] = pa.array(sources, type=pa.string())
    table = pa.table(arrays)
    pq.write_table(table, path)
    return table.num_rows


def normalize_run(raw_dir, normalized_dir) -> dict:
    """Normalize every raw JSONL file in ``raw_dir``; write outputs + QC report.

    Returns the QC report dict (also written to
    ``normalized_dir/qc_report.json``).
    """
    raw_dir = Path(raw_dir)
    normalized_dir = Path(normalized_dir)
    normalized_dir.mkdir(parents=True, exist_ok=True)
    raw_files = sorted(raw_dir.glob("*.jsonl"))
    if not raw_files:
        raise ValueError(f"no raw *.jsonl files found in {raw_dir}")

    stream_qc: Dict[str, _StreamQC] = {}
    per_stream: Dict[str, Dict[str, int]] = {}
    events_by_file: Dict[str, List[MarketEvent]] = {}
    file_counts: Dict[str, dict] = {}
    for raw_path in raw_files:
        kept = normalize_file(raw_path, normalized_dir, stream_qc, per_stream)
        events_by_file[raw_path.stem] = kept
        file_counts[raw_path.name] = {
            "events_out": len(kept),
            "normalized_jsonl": f"{raw_path.stem}.normalized.jsonl",
            "normalized_iap1": f"{raw_path.stem}.normalized.iap1",
        }

    parquet_rows = _write_parquet(events_by_file, normalized_dir / "events.parquet")

    totals = _new_counter()
    for counters in per_stream.values():
        for k, v in counters.items():
            totals[k] += v
    report = {
        "x-version": 1,
        "raw_dir": str(raw_dir),
        "files": file_counts,
        "parquet": {"path": "events.parquet", "rows": parquet_rows},
        "per_stream": {k: per_stream[k] for k in sorted(per_stream)},
        "totals": totals,
    }
    with open(normalized_dir / "qc_report.json", "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")
    return report
