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
its sort, as 72-byte IAP1 records rather than Python objects; per-stream QC state is O(DEDUP_WINDOW); the Parquet dataset is
written file by file through a ``ParquetWriter`` (never all files at once).

Outputs per raw file ``<stem>.jsonl``: ``<stem>.normalized.jsonl`` +
``<stem>.normalized.iap1`` (byte-exact canonical codecs), plus one combined
Parquet research dataset ``events.parquet`` (pyarrow) and
``qc_report.json`` with per-stream and total counts.
"""

from __future__ import annotations

import json
import os
import struct
import zlib
from array import array
from collections import deque
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path

from iap.core.codec import (
    IAP1_MAGIC,
    IAP1_RECORD_SIZE,
    IAP1_VERSION,
    encode_jsonl_line,
    iter_jsonl,
    write_jsonl,
)
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


def _new_counter() -> dict[str, int]:
    return {k: 0 for k in _COUNTER_KEYS}


class _StreamQC:
    """Bounded sequence bookkeeping for one (venue_id, instrument_id) stream."""

    __slots__ = ("recent", "recent_set", "max_seq", "max_exchange_ts", "epoch", "started")

    def __init__(self) -> None:
        self.recent: deque[int] = deque()
        self.recent_set: set[int] = set()
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


#: One IAP1 record (schemas/FORMAT.md §2) as numpy structured-type fields.
_IAP1_RECORD = [
    ("event_id", "<u8"),
    ("instrument_id", "<u4"),
    ("venue_id", "<u2"),
    ("event_type", "u1"),
    ("side", "u1"),
    ("exchange_ts", "<i8"),
    ("receive_ts", "<i8"),
    ("sequence", "<u8"),
    ("price_ticks", "<i8"),
    ("qty", "<i8"),
    ("order_id", "<u8"),
    ("trade_id", "<u8"),
]
#: ``struct`` twin of :data:`_IAP1_RECORD` (the codec's record layout).
RECORD_STRUCT = struct.Struct("<QIHBBqqQqqQQ")
#: IAP1 header (magic, version, count) and trailer (crc32, 0, count).
_FRAME = struct.Struct("<IIQ")
assert RECORD_STRUCT.size == IAP1_RECORD_SIZE
_CHUNK = 65536


def pack_record(ev: MarketEvent) -> bytes:
    """One event as its 72-byte IAP1 record (``struct.error`` if out of domain)."""
    return RECORD_STRUCT.pack(
        ev.event_id,
        ev.instrument_id,
        ev.venue_id,
        ev.event_type,
        ev.side,
        ev.exchange_ts,
        ev.receive_ts,
        ev.sequence,
        ev.price_ticks,
        ev.qty,
        ev.order_id,
        ev.trade_id,
    )


def _records(buf: bytes | bytearray):
    import numpy as np

    return np.frombuffer(buf, dtype=np.dtype(_IAP1_RECORD))


def iter_record_events(records) -> Iterator[MarketEvent]:
    """IAP1 records (bytes or a structured array) -> ``MarketEvent``s, a chunk at a time."""
    if isinstance(records, (bytes, bytearray)):
        records = _records(records)
    for lo in range(0, len(records), _CHUNK):
        for eid, inst, ven, et, side, ex, rc, seq, px, q, oid, tid in records[
            lo : lo + _CHUNK
        ].tolist():
            yield MarketEvent(eid, inst, ven, ex, rc, seq, et, side, px, q, oid, tid)


def _ts_regression_keep(rec, epoch, per_stream: dict[str, dict[str, int]]):
    """Step 4 as a mask: False for an in-stream exchange_ts regression.

    In (venue, instrument, epoch, sequence, event_id) order an event is
    dropped when its exchange_ts is below the running maximum of its stream
    epoch.  A dropped event never raises that maximum, so the maximum over
    every earlier event equals the one over the kept ones."""
    import numpy as np

    n = len(rec)
    keep = np.ones(n, dtype=bool)
    if n < 2:
        return keep
    order = np.lexsort(
        (rec["event_id"], rec["sequence"], epoch, rec["instrument_id"], rec["venue_id"])
    )
    ts = rec["exchange_ts"][order]
    ven = rec["venue_id"][order]
    ins = rec["instrument_id"][order]
    ep = epoch[order]
    cut = np.flatnonzero((ven[1:] != ven[:-1]) | (ins[1:] != ins[:-1]) | (ep[1:] != ep[:-1])) + 1
    drop = np.zeros(n, dtype=bool)
    bounds = zip(
        np.concatenate(([0], cut)).tolist(), np.concatenate((cut, [n])).tolist(), strict=True
    )
    for lo, hi in bounds:
        if hi - lo < 2:
            continue
        seg = ts[lo:hi]
        d = seg[1:] < np.maximum.accumulate(seg)[:-1]
        if d.any():
            drop[lo + 1 : hi] = d
            per_stream[f"{int(ven[lo])}:{int(ins[lo])}"]["ts_regression_dropped"] += int(d.sum())
    keep[order[drop]] = False
    return keep


def _write_iap1_records(path: Path, rec) -> None:
    """The bytes of ``write_iap1`` for these records (header, records, CRC trailer)."""
    header = _FRAME.pack(IAP1_MAGIC, IAP1_VERSION, len(rec))
    body = rec.tobytes()
    crc = zlib.crc32(body, zlib.crc32(header)) & 0xFFFFFFFF
    with open(path, "wb") as f:
        f.write(header)
        f.write(body)
        f.write(_FRAME.pack(crc, 0, len(rec)))


def normalize_file(
    raw_path: Path,
    out_dir: Path,
    stream_qc: dict[str, _StreamQC],
    per_stream: dict[str, dict[str, int]],
    events: Iterable[MarketEvent] | None = None,
    *,
    partition_instruments: int | None = None,
    sink: _ParquetSink | None = None,
    write_normalized_jsonl: bool = True,
):
    """Normalize one raw JSONL file; returns the kept, event-time-ordered
    events as a numpy structured array of IAP1 records.

    ``write_normalized_jsonl=False`` (v1.17, A2b) skips the
    ``*.normalized.jsonl`` twin of the IAP1 file (same events, 3x the
    bytes; the feature build and the MVP read the IAP1 first): the slim
    layout of a breadth dataset.  The IAP1 file and the Parquet sink are
    unchanged.

    ``partition_instruments`` (v1.17, A2; opt-in) bounds memory for a wide
    universe: the packed records are routed to on-disk partitions of that
    many instruments each, every partition is sorted on its own, and the
    partitions are merged in event-time windows into the SAME output bytes
    (:func:`_normalize_partitioned`; the sort key puts venue and instrument
    right after ``exchange_ts``, so a per-instrument partition is a slice of
    the global order).  Memory is then one partition plus one window, not
    the day.  The outputs are written by this function and the row count is
    returned instead of the records; ``sink`` receives one row group per
    merge window.

    ``events``, when given, are the events of ``raw_path`` already in hand
    (the caller has just written that file): they are used instead of
    decoding it again.  They are modified in place, like decoded ones.

    ``stream_qc``/``per_stream`` are shared across files of a run so sequences
    that continue across sessions are checked continuously (and resets that
    happen at a session boundary are detected as resets, not duplicates).

    Steps 1-3 run event by event and pack each kept event into its 72-byte
    IAP1 record at once, so a session costs ~80 bytes an event instead of a
    Python object per event (v1.7.2 needed ~17 GB for a 46 m-event day);
    steps 4-5 sort the records.
    """
    import numpy as np

    if partition_instruments is not None:
        if partition_instruments < 1:
            raise ValueError("partition_instruments must be >= 1")
        return _normalize_partitioned(
            raw_path,
            out_dir,
            stream_qc,
            per_stream,
            events,
            partition_instruments,
            sink,
            write_normalized_jsonl,
        )
    buf = bytearray()
    epochs = array("q")
    for ev in iter_jsonl(raw_path) if events is None else events:
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
        buf += pack_record(ev)  # in domain: validation_error passed
        epochs.append(qc.epoch)

    rec = _records(buf)
    epoch = np.frombuffer(epochs, dtype=np.int64)

    # 4. In-stream timestamp regression: drop + count, never re-sort.
    keep = _ts_regression_keep(rec, epoch, per_stream)
    if not keep.all():
        rec, epoch = rec[keep], epoch[keep]
    del keep
    if len(rec):
        streams = rec["venue_id"].astype(np.int64) << 32 | rec["instrument_id"].astype(np.int64)
        values, counts = np.unique(streams, return_counts=True)
        for v, c in zip(values.tolist(), counts.tolist(), strict=True):
            per_stream[f"{v >> 32}:{v & 0xFFFFFFFF}"]["events_out"] += c
        del streams

    # 5. Event-time ordering + event_id reassignment. Within one exchange_ts
    # tick, order by stream then epoch/sequence so late arrivals (e.g. a
    # delayed EXECUTE whose same-timestamp TRADE print arrived first) are
    # restored to correct intra-stream order; event_id (arrival) is the
    # final tiebreak. Step 4 guarantees the sort never inverts sequence
    # order inside a stream. (np.lexsort is stable; its LAST key is primary.)
    order = np.lexsort(
        (
            rec["event_id"],
            rec["sequence"],
            epoch,
            rec["instrument_id"],
            rec["venue_id"],
            rec["exchange_ts"],
        )
    )
    rec = rec[order]
    del order, epoch, epochs, buf
    rec["event_id"] = np.arange(1, len(rec) + 1, dtype=np.uint64)

    stem = raw_path.stem
    if write_normalized_jsonl:
        write_jsonl(out_dir / f"{stem}.normalized.jsonl", iter_record_events(rec))
    _write_iap1_records(out_dir / f"{stem}.normalized.iap1", rec)
    return rec


def _qc_steps(ev: MarketEvent, stream_qc, per_stream) -> int | None:
    """Steps 1-3 of :func:`normalize_file` for one event; the stream epoch
    of a kept event, ``None`` for a dropped one (counted).  One copy of the
    rules for both the in-memory and the partitioned path."""
    key = f"{ev.venue_id}:{ev.instrument_id}"
    counters = per_stream.get(key)
    if counters is None:
        counters = per_stream[key] = _new_counter()
    counters["events_in"] += 1
    if ev.receive_ts < ev.exchange_ts:
        ev.receive_ts = ev.exchange_ts
        counters["ts_clamped"] += 1
    if validation_error(ev) is not None:
        counters["invalid"] += 1
        return None
    qc = stream_qc.get(key)
    if qc is None:
        qc = stream_qc[key] = _StreamQC()
    if not qc.started:
        qc.started = True
        qc.max_seq = ev.sequence
        qc.max_exchange_ts = ev.exchange_ts
    elif ev.sequence < qc.max_seq and ev.exchange_ts > qc.max_exchange_ts:
        counters["sequence_resets"] += 1
        qc.reset()
        qc.max_seq = ev.sequence
    elif ev.sequence in qc.recent_set:
        counters["duplicates"] += 1
        return None
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
    return qc.epoch


#: Rows a merge window holds at most per partition (the memory bound of the
#: partitioned path together with one partition's sort).
_MERGE_WINDOW_ROWS = 1 << 20


def _normalize_partitioned(
    raw_path: Path,
    out_dir: Path,
    stream_qc: dict[str, _StreamQC],
    per_stream: dict[str, dict[str, int]],
    events: Iterable[MarketEvent] | None,
    partition_instruments: int,
    sink: _ParquetSink | None,
    write_normalized_jsonl: bool = True,
) -> int:
    """The partitioned path of :func:`normalize_file` (module docs, A2)."""
    import shutil
    import tempfile

    import numpy as np

    stem = raw_path.stem
    tmp = Path(tempfile.mkdtemp(prefix=f".norm_{stem}_", dir=out_dir))
    part_of: dict[int, int] = {}
    files: list = []
    epoch_struct = struct.Struct("<q")
    try:
        # steps 1-3, routed to per-partition record files (72 B record + 8 B epoch)
        for ev in iter_jsonl(raw_path) if events is None else events:
            epoch = _qc_steps(ev, stream_qc, per_stream)
            if epoch is None:
                continue
            part = part_of.get(ev.instrument_id)
            if part is None:
                part = part_of[ev.instrument_id] = len(part_of) // partition_instruments
                while len(files) <= part:
                    files.append(open(tmp / f"part_{len(files):04d}.bin", "wb"))
            files[part].write(pack_record(ev))
            files[part].write(epoch_struct.pack(epoch))
        for f in files:
            f.close()
        dtype_part = np.dtype(_IAP1_RECORD + [("epoch", "<i8")])
        dtype_rec = np.dtype(_IAP1_RECORD)
        sorted_paths: list[Path] = []
        total = 0
        # steps 4-5 per partition: drop ts regressions, sort, write a sorted run
        for k in range(len(files)):
            raw = np.fromfile(tmp / f"part_{k:04d}.bin", dtype=dtype_part)
            (tmp / f"part_{k:04d}.bin").unlink()
            epoch = raw["epoch"].copy()
            keep = _ts_regression_keep(raw, epoch, per_stream)
            if not keep.all():
                raw, epoch = raw[keep], epoch[keep]
            del keep
            if len(raw):
                streams = raw["venue_id"].astype(np.int64) << 32 | raw["instrument_id"].astype(
                    np.int64
                )
                values, counts = np.unique(streams, return_counts=True)
                for v, c in zip(values.tolist(), counts.tolist(), strict=True):
                    per_stream[f"{v >> 32}:{v & 0xFFFFFFFF}"]["events_out"] += c
                del streams
            order = np.lexsort(
                (
                    raw["event_id"],
                    raw["sequence"],
                    epoch,
                    raw["instrument_id"],
                    raw["venue_id"],
                    raw["exchange_ts"],
                )
            )
            rec = np.empty(len(raw), dtype=dtype_rec)
            for name in dtype_rec.names:
                rec[name] = raw[name][order]
            del raw, epoch, order
            p = tmp / f"sorted_{k:04d}.bin"
            rec.tofile(p)
            sorted_paths.append(p)
            total += len(rec)
            del rec
        # merge the sorted runs in event-time windows into the final outputs
        runs = [np.memmap(p, dtype=dtype_rec, mode="r") for p in sorted_paths]
        pos = [0] * len(runs)
        next_id = 1
        header = _FRAME.pack(IAP1_MAGIC, IAP1_VERSION, total)
        crc = zlib.crc32(header)
        jsonl_path = out_dir / f"{stem}.normalized.jsonl"
        with (
            open(out_dir / f"{stem}.normalized.iap1", "wb") as iap1,
            open(jsonl_path if write_normalized_jsonl else os.devnull, "wb") as jsonl,
        ):
            iap1.write(header)
            while any(pos[k] < len(runs[k]) for k in range(len(runs))):
                # the window ends at the earliest "chunk-ahead" timestamp of any
                # run, so each run contributes at most a chunk of rows
                bound = None
                for k, run in enumerate(runs):
                    if pos[k] < len(run):
                        t = int(run["exchange_ts"][min(pos[k] + _MERGE_WINDOW_ROWS, len(run) - 1)])
                        bound = t if bound is None else min(bound, t)
                assert bound is not None
                parts = []
                advanced = False
                for k, run in enumerate(runs):
                    if pos[k] >= len(run):
                        continue
                    hi = int(np.searchsorted(run["exchange_ts"][pos[k] :], bound, side="left"))
                    if hi:
                        parts.append(np.asarray(run[pos[k] : pos[k] + hi]))
                        pos[k] += hi
                        advanced = True
                if not advanced:
                    # every remaining row of the front shares ``bound``: take
                    # that timestamp from every run at once
                    for k, run in enumerate(runs):
                        if pos[k] >= len(run):
                            continue
                        hi = int(np.searchsorted(run["exchange_ts"][pos[k] :], bound, side="right"))
                        if hi:
                            parts.append(np.asarray(run[pos[k] : pos[k] + hi]))
                            pos[k] += hi
                chunk = np.concatenate(parts) if len(parts) > 1 else parts[0].copy()
                del parts
                # stable: rows of one run (same venue, instrument) keep their order
                order = np.lexsort(
                    (chunk["instrument_id"], chunk["venue_id"], chunk["exchange_ts"])
                )
                chunk = chunk[order]
                del order
                chunk["event_id"] = np.arange(next_id, next_id + len(chunk), dtype=np.uint64)
                next_id += len(chunk)
                if write_normalized_jsonl:
                    lines = [encode_jsonl_line(ev) for ev in iter_record_events(chunk)]
                    if lines:
                        jsonl.write(("\n".join(lines) + "\n").encode("utf-8"))
                    del lines
                body = chunk.tobytes()
                crc = zlib.crc32(body, crc)
                iap1.write(body)
                if sink is not None:
                    sink.write(stem, chunk)
                del chunk, body
            iap1.write(_FRAME.pack(crc & 0xFFFFFFFF, 0, total))
        # release the memory maps before the temp directory is removed
        # (Windows refuses to delete a mapped file)
        for run in runs:
            mm = getattr(run, "_mmap", None)
            if mm is not None:
                mm.close()
        runs.clear()
        return total
    finally:
        for f in files:
            if not f.closed:
                f.close()
        shutil.rmtree(tmp, ignore_errors=True)


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
        import numpy as np
        import pyarrow as pa
        import pyarrow.parquet as pq

        self._pa = pa
        self._np = np
        fields = [pa.field(n, getattr(pa, t)()) for n, t in _SCHEMA_TYPES.items()]
        fields.append(pa.field("source", pa.string()))
        self.schema = pa.schema(fields)
        self._writer = pq.ParquetWriter(path, self.schema)
        self.rows = 0

    def write(self, stem: str, records) -> None:
        """One row group from the IAP1 records of a normalized file."""
        if not len(records):
            return
        pa = self._pa
        np = self._np
        arrays = [
            pa.array(np.ascontiguousarray(records[n]), type=self.schema.field(n).type)
            for n in FIELDS
        ]
        arrays.append(pa.array([stem] * len(records), type=pa.string()))
        self._writer.write_table(pa.table(arrays, schema=self.schema))
        self.rows += len(records)

    def close(self) -> None:
        self._writer.close()


def normalize_run(
    raw_dir,
    normalized_dir,
    preloaded: Mapping[str, Iterable[MarketEvent]] | None = None,
    *,
    partition_instruments: int | None = None,
    write_normalized_jsonl: bool = True,
) -> dict:
    """Normalize every raw JSONL file in ``raw_dir``; write outputs + QC report.

    ``preloaded`` maps a raw file NAME to the events of that file already in
    memory; such a file is not decoded again (see :func:`normalize_file`).
    ``partition_instruments`` (v1.17, A2) selects the memory-bounded
    partitioned path; the outputs are byte-identical (tested), the Parquet
    file then holds one row group per merge window instead of per file.

    Returns the QC report dict (also written to
    ``normalized_dir/qc_report.json``).
    """
    raw_dir = Path(raw_dir)
    normalized_dir = Path(normalized_dir)
    normalized_dir.mkdir(parents=True, exist_ok=True)
    raw_files = sorted(raw_dir.glob("*.jsonl"))
    if not raw_files:
        raise ValueError(f"no raw *.jsonl files found in {raw_dir}")

    stream_qc: dict[str, _StreamQC] = {}
    per_stream: dict[str, dict[str, int]] = {}
    file_counts: dict[str, dict] = {}
    sink = _ParquetSink(normalized_dir / "events.parquet")
    try:
        for raw_path in raw_files:
            kept = normalize_file(
                raw_path,
                normalized_dir,
                stream_qc,
                per_stream,
                preloaded.get(raw_path.name) if preloaded else None,
                partition_instruments=partition_instruments,
                sink=sink,
                write_normalized_jsonl=write_normalized_jsonl,
            )
            if isinstance(kept, int):
                n_out = kept  # the partitioned path wrote the outputs and the sink
            else:
                sink.write(raw_path.stem, kept)
                n_out = len(kept)
            file_counts[raw_path.name] = {
                "events_out": n_out,
                "normalized_jsonl": (
                    f"{raw_path.stem}.normalized.jsonl" if write_normalized_jsonl else None
                ),
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
