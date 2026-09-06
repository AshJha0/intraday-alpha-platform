"""Raw -> normalized market-data pipeline with QC reporting (spec section 8).

Per raw JSONL file (arrival order):

1. **Timestamp normalization** (clock skew): ``receive_ts < exchange_ts`` is
   clamped to ``exchange_ts`` and counted per stream as ``ts_clamped``
   (pinned: the exchange clock is authoritative; the event is kept).
2. **Invalid detection**: events violating the canonical contract
   (``iap.core.events.validation_error``) are dropped + counted.
3. **Sequence QC** per stream (venue_id, instrument_id), in arrival order,
   with a bounded per-stream state (``max_seq``, ``max_exchange_ts``, the
   current ``epoch`` and a window of the last ``DEDUP_WINDOW`` sequences):
   - sequence already in the window   -> duplicate, dropped + counted
   - sequence < max seen and exchange_ts > max exchange_ts seen
                                      -> **venue sequence reset** (daily
                                         restart / fail-over): new epoch,
                                         kept + counted ``sequence_resets``
   - sequence < max seen (otherwise)  -> out-of-order (late arrival), kept +
                                         counted
   - sequence > max seen + 1          -> gap, kept + counted (occurrences and
                                         missing-event count); books recover
                                         via the SNAPSHOT bursts in the stream
4. **In-stream timestamp regression** (pinned): within one stream, in
   (epoch, sequence) order, an event whose ``exchange_ts`` is lower than that
   of an earlier-sequenced event is dropped + counted
   (``ts_regression_dropped``). Sequence order is never silently inverted by
   the event-time sort below; the drop is visible in the QC report.
5. **Ordering**: surviving events are sorted into event time
   (exchange_ts, then venue/instrument/epoch/sequence to restore intra-stream
   order at identical timestamps, then arrival event_id) and event_id is
   reassigned 1..N so the normalized file keeps the global-monotone-per-file
   invariant.

Memory bound (documented): one raw file's kept events are resident during
its sort; per-stream QC state is O(DEDUP_WINDOW); the Parquet dataset is
written file by file through a ``ParquetWriter`` (never all files at once).

Outputs per raw file ``<stem>.jsonl``: ``<stem>.normalized.jsonl`` +
``<stem>.normalized.iap1`` (byte-exact canonical codecs), plus one combined
Parquet research dataset ``events.parquet`` (pyarrow) and
``qc_report.json`` with per-stream and total counts.
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Deque, Dict, List, Set, Tuple

from iap.core.codec import iter_jsonl, write_iap1, write_jsonl
from iap.core.events import FIELDS, MarketEvent, validation_error

#: Per-stream duplicate-detection window (sequences remembered), pinned.
DEDUP_WINDOW = 65536

_COUNTER_KEYS = (
    "events_in",
    "events_out",
    "gaps",
    "gap_missing_events",
    "duplicates",
    "out_of_order",
    "sequence_resets",
    "ts_regression_dropped",
    "invalid",
    "ts_clamped",
)


def _new_counter() -> Dict[str, int]:
    return {k: 0 for k in _COUNTER_KEYS}


class _StreamQC:
    """Bounded sequence bookkeeping for one (venue_id, instrument_id) stream."""

    __slots__ = ("recent", "recent_set", "max_seq", "max_exchange_ts", "epoch", "started")

    def __init__(self) -> None:
        self.recent: Deque[int] = deque()
        self.recent_set: Set[int] = set()
        self.max_seq = 0
        self.max_exchange_ts = 0
        self.epoch = 0
        self.started = False

    def remember(self, seq: int) -> None:
        self.recent.append(seq)
        self.recent_set.add(seq)
        if len(self.recent) > DEDUP_WINDOW:
            self.recent_set.discard(self.recent.popleft())

    def reset(self) -> None:
        self.recent.clear()
        self.recent_set.clear()
        self.epoch += 1


def _drop_ts_regressions(
    kept: List[Tuple[MarketEvent, int]], per_stream: Dict[str, Dict[str, int]]
) -> List[Tuple[MarketEvent, int]]:
    """Drop in-stream exchange_ts regressions (checked in (epoch, sequence) order)."""
    order = sorted(
        range(len(kept)),
        key=lambda i: (kept[i][0].venue_id, kept[i][0].instrument_id, kept[i][1],
                       kept[i][0].sequence, kept[i][0].event_id),
    )
    drop: Set[int] = set()
    last_key = None
    running_max = 0
    for i in order:
        ev, epoch = kept[i]
        key = (ev.venue_id, ev.instrument_id, epoch)
        if key != last_key:
            last_key = key
            running_max = ev.exchange_ts
            continue
        if ev.exchange_ts < running_max:
            drop.add(i)
            per_stream[f"{ev.venue_id}:{ev.instrument_id}"]["ts_regression_dropped"] += 1
        else:
            running_max = ev.exchange_ts
    if not drop:
        return kept
    return [row for i, row in enumerate(kept) if i not in drop]


def normalize_file(
    raw_path: Path,
    out_dir: Path,
    stream_qc: Dict[str, _StreamQC],
    per_stream: Dict[str, Dict[str, int]],
) -> List[MarketEvent]:
    """Normalize one raw JSONL file; returns the kept, event-time-ordered events.

    ``stream_qc``/``per_stream`` are shared across files of a run so sequences
    that continue across sessions are checked continuously (and resets that
    happen at a session boundary are detected as resets, not duplicates).
    """
    kept: List[Tuple[MarketEvent, int]] = []  # (event, stream epoch)
    for ev in iter_jsonl(raw_path):
        key = f"{ev.venue_id}:{ev.instrument_id}"
        counters = per_stream.get(key)
        if counters is None:
            counters = per_stream[key] = _new_counter()
        counters["events_in"] += 1

        # 1. Timestamp normalization (clock skew: exchange clock wins).
        if ev.receive_ts < ev.exchange_ts:
            ev.receive_ts = ev.exchange_ts
            counters["ts_clamped"] += 1

        # 2. Invalid detection.
        if validation_error(ev) is not None:
            counters["invalid"] += 1
            continue

        # 3. Sequence QC (bounded state).
        qc = stream_qc.get(key)
        if qc is None:
            qc = stream_qc[key] = _StreamQC()
        if not qc.started:
            qc.started = True
            qc.max_seq = ev.sequence
            qc.max_exchange_ts = ev.exchange_ts
        elif ev.sequence < qc.max_seq and ev.exchange_ts > qc.max_exchange_ts:
            # Sequence numbers restarted while time moved on: venue reset
            # (checked before the duplicate window — a retransmission never
            # carries a later exchange_ts than the stream's maximum).
            counters["sequence_resets"] += 1
            qc.reset()
            qc.max_seq = ev.sequence
        elif ev.sequence in qc.recent_set:
            counters["duplicates"] += 1
            continue
        elif ev.sequence < qc.max_seq:
            counters["out_of_order"] += 1
        elif ev.sequence > qc.max_seq + 1:
            counters["gaps"] += 1
            counters["gap_missing_events"] += ev.sequence - qc.max_seq - 1
        qc.remember(ev.sequence)
        if ev.sequence > qc.max_seq:
            qc.max_seq = ev.sequence
        if ev.exchange_ts > qc.max_exchange_ts:
            qc.max_exchange_ts = ev.exchange_ts
        kept.append((ev, qc.epoch))

    # 4. In-stream timestamp regression: drop + count, never re-sort.
    kept = _drop_ts_regressions(kept, per_stream)
    for ev, _ in kept:
        per_stream[f"{ev.venue_id}:{ev.instrument_id}"]["events_out"] += 1

    # 5. Event-time ordering + event_id reassignment. Within one exchange_ts
    # tick, order by stream then epoch/sequence so late arrivals (e.g. a
    # delayed EXECUTE whose same-timestamp TRADE print arrived first) are
    # restored to correct intra-stream order; event_id (arrival) is the
    # final tiebreak. Step 4 guarantees the sort never inverts sequence
    # order inside a stream.
    kept.sort(
        key=lambda row: (row[0].exchange_ts, row[0].venue_id, row[0].instrument_id,
                         row[1], row[0].sequence, row[0].event_id)
    )
    out = [ev for ev, _ in kept]
    for i, ev in enumerate(out):
        ev.event_id = i + 1

    stem = raw_path.stem
    write_jsonl(out_dir / f"{stem}.normalized.jsonl", out)
    write_iap1(out_dir / f"{stem}.normalized.iap1", out)
    return out


_SCHEMA_TYPES = {
    "event_id": "uint64",
    "instrument_id": "uint32",
    "venue_id": "uint16",
    "exchange_ts": "int64",
    "receive_ts": "int64",
    "sequence": "uint64",
    "event_type": "uint8",
    "side": "uint8",
    "price_ticks": "int64",
    "qty": "int64",
    "order_id": "uint64",
    "trade_id": "uint64",
}


class _ParquetSink:
    """Streaming Parquet writer: one row group per normalized file."""

    def __init__(self, path: Path) -> None:
        import pyarrow as pa
        import pyarrow.parquet as pq

        self._pa = pa
        fields = [pa.field(n, getattr(pa, t)()) for n, t in _SCHEMA_TYPES.items()]
        fields.append(pa.field("source", pa.string()))
        self.schema = pa.schema(fields)
        self._writer = pq.ParquetWriter(path, self.schema)
        self.rows = 0

    def write(self, stem: str, events: List[MarketEvent]) -> None:
        if not events:
            return
        pa = self._pa
        cols = {name: [getattr(ev, name) for ev in events] for name in FIELDS}
        arrays = [pa.array(cols[n], type=self.schema.field(n).type) for n in FIELDS]
        arrays.append(pa.array([stem] * len(events), type=pa.string()))
        self._writer.write_table(pa.table(arrays, schema=self.schema))
        self.rows += len(events)

    def close(self) -> None:
        self._writer.close()


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
    file_counts: Dict[str, dict] = {}
    sink = _ParquetSink(normalized_dir / "events.parquet")
    try:
        for raw_path in raw_files:
            kept = normalize_file(raw_path, normalized_dir, stream_qc, per_stream)
            sink.write(raw_path.stem, kept)
            file_counts[raw_path.name] = {
                "events_out": len(kept),
                "normalized_jsonl": f"{raw_path.stem}.normalized.jsonl",
                "normalized_iap1": f"{raw_path.stem}.normalized.iap1",
            }
    finally:
        sink.close()

    totals = _new_counter()
    for counters in per_stream.values():
        for k, v in counters.items():
            totals[k] += v
    report = {
        "x-version": 2,
        "raw_dir": str(raw_dir),
        "files": file_counts,
        "parquet": {"path": "events.parquet", "rows": sink.rows},
        "per_stream": {k: per_stream[k] for k in sorted(per_stream)},
        "totals": totals,
    }
    with open(normalized_dir / "qc_report.json", "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")
    return report
