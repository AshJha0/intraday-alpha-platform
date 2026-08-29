"""Canonical codecs: JSONL and IAP1 binary (normative layout: schemas/FORMAT.md).

Both encoders are byte-exact: the same event vector must produce byte-identical
files in every language (verified via SHA-256 golden tests).
"""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path
from typing import Iterable, Iterator, List, Sequence, Union

from iap.core.events import FIELDS, MarketEvent

# --------------------------------------------------------------------- JSONL

_KEYS = FIELDS  # canonical key order


def encode_jsonl_line(ev: MarketEvent) -> str:
    """Encode one event as a canonical JSONL line (no trailing newline).

    Canonical form: keys in pinned order, compact separators, integers only.
    """
    return json.dumps(ev.to_dict(), separators=(",", ":"))


def decode_jsonl_line(line: str) -> MarketEvent:
    """Decode one canonical JSONL line. Strict: exact keys, integer values."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed JSONL line: {exc}") from None
    if not isinstance(obj, dict):
        raise ValueError(f"JSONL line is not an object: {line[:80]!r}")
    if tuple(obj.keys()) != _KEYS:
        missing = [k for k in _KEYS if k not in obj]
        extra = [k for k in obj if k not in _KEYS]
        raise ValueError(
            f"JSONL keys mismatch (missing={missing}, extra={extra}, "
            f"order must be {list(_KEYS)})"
        )
    vals = []
    for k in _KEYS:
        v = obj[k]
        if type(v) is not int:  # bool is not acceptable either
            raise ValueError(f"JSONL field {k!r} must be an integer, got {v!r}")
        vals.append(v)
    return MarketEvent(*vals)


def encode_jsonl(events: Iterable[MarketEvent]) -> bytes:
    """Encode events to canonical JSONL bytes (LF after every line)."""
    return "".join(encode_jsonl_line(ev) + "\n" for ev in events).encode("utf-8")


def write_jsonl(path: Union[str, Path], events: Iterable[MarketEvent]) -> int:
    """Write canonical JSONL file; return number of events written."""
    n = 0
    with open(path, "wb") as f:
        for ev in events:
            f.write((encode_jsonl_line(ev) + "\n").encode("utf-8"))
            n += 1
    return n


def iter_jsonl(path: Union[str, Path]) -> Iterator[MarketEvent]:
    """Iterate events from a canonical JSONL file."""
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield decode_jsonl_line(line)


def read_jsonl(path: Union[str, Path]) -> List[MarketEvent]:
    """Read all events from a canonical JSONL file."""
    return list(iter_jsonl(path))


# --------------------------------------------------------------------- IAP1

IAP1_MAGIC = 0x49415031
IAP1_VERSION = 1
_HEADER = struct.Struct("<IIQ")  # magic u32 | version u32 | count u64  (16 bytes)
_RECORD = struct.Struct("<QIHBBqqQqqQQ")  # 72 bytes, no padding
IAP1_HEADER_SIZE = _HEADER.size
IAP1_RECORD_SIZE = _RECORD.size
assert IAP1_HEADER_SIZE == 16 and IAP1_RECORD_SIZE == 72


def encode_iap1(events: Sequence[MarketEvent]) -> bytes:
    """Encode events to IAP1 bytes (header + fixed 72-byte LE records)."""
    parts = [_HEADER.pack(IAP1_MAGIC, IAP1_VERSION, len(events))]
    pack = _RECORD.pack
    for ev in events:
        parts.append(
            pack(
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
        )
    return b"".join(parts)


def decode_iap1(data: bytes) -> List[MarketEvent]:
    """Decode IAP1 bytes. Rejects bad magic/version, truncation, count mismatch."""
    if len(data) < IAP1_HEADER_SIZE:
        raise ValueError(f"IAP1 file truncated: {len(data)} bytes < 16-byte header")
    magic, version, count = _HEADER.unpack_from(data, 0)
    if magic != IAP1_MAGIC:
        raise ValueError(f"bad IAP1 magic: 0x{magic:08X} (expected 0x{IAP1_MAGIC:08X})")
    if version != IAP1_VERSION:
        raise ValueError(f"unsupported IAP1 version: {version}")
    expected = IAP1_HEADER_SIZE + IAP1_RECORD_SIZE * count
    if len(data) != expected:
        raise ValueError(
            f"IAP1 size mismatch: {len(data)} bytes, header count={count} "
            f"implies {expected}"
        )
    events: List[MarketEvent] = []
    unpack = _RECORD.unpack_from
    off = IAP1_HEADER_SIZE
    for _ in range(count):
        (
            event_id,
            instrument_id,
            venue_id,
            event_type,
            side,
            exchange_ts,
            receive_ts,
            sequence,
            price_ticks,
            qty,
            order_id,
            trade_id,
        ) = unpack(data, off)
        events.append(
            MarketEvent(
                event_id,
                instrument_id,
                venue_id,
                exchange_ts,
                receive_ts,
                sequence,
                event_type,
                side,
                price_ticks,
                qty,
                order_id,
                trade_id,
            )
        )
        off += IAP1_RECORD_SIZE
    return events


def write_iap1(path: Union[str, Path], events: Sequence[MarketEvent]) -> int:
    """Write an IAP1 file; return number of events written."""
    with open(path, "wb") as f:
        f.write(encode_iap1(events))
    return len(events)


def read_iap1(path: Union[str, Path]) -> List[MarketEvent]:
    """Read an IAP1 file."""
    with open(path, "rb") as f:
        return decode_iap1(f.read())


# -------------------------------------------------------------------- SHA-256


def sha256_bytes(data: bytes) -> str:
    """Hex SHA-256 of a byte string."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Union[str, Path]) -> str:
    """Hex SHA-256 of a file's contents."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_events_iap1(events: Sequence[MarketEvent]) -> str:
    """Hex SHA-256 of the IAP1 encoding of an event vector (golden helper)."""
    return sha256_bytes(encode_iap1(events))
