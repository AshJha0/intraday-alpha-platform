"""Canonical codecs: JSONL and IAP1 binary (normative layout: schemas/FORMAT.md).

Both encoders are byte-exact: the same event vector must produce byte-identical
files in every language (verified via SHA-256 golden tests).

Decoders fail closed (pinned, identical in every port):

- JSONL: exactly the 12 canonical keys in canonical order, integer tokens only
  (no floats, exponents, leading zeros, ``+``; ``-0`` is rejected on unsigned
  fields), no duplicate/extra keys, no trailing content, and every field in
  its integer domain (u64/u32/u16/u8/i64). Whitespace between tokens is
  tolerated (decoders are RFC 8259 readers; the encoder never emits it).
- IAP1: version 2 files carry a 16-byte integrity trailer (CRC-32 of header +
  records, count echo) that is verified on read; version 1 files (no
  trailer) are accepted as legacy input and reported via ``decode_iap1_ex``.
  Bad magic / unknown version / truncation / count mismatch / CRC mismatch
  raise ``ValueError`` with byte offsets. Encoders raise ``ValueError`` (never
  ``struct.error``) on out-of-domain fields, naming the event index.
"""

from __future__ import annotations

import hashlib
import re
import struct
import zlib
from pathlib import Path
from typing import Iterable, Iterator, List, NamedTuple, Sequence, Tuple, Union

from iap.core.events import (
    FIELDS,
    I64_MAX,
    I64_MIN,
    U16_MAX,
    U32_MAX,
    U64_MAX,
    MarketEvent,
)

# --------------------------------------------------------------------- JSONL

_KEYS = FIELDS  # canonical key order

#: Domain of every field: (min, max). Unsigned fields reject a leading '-'.
_DOMAIN: Tuple[Tuple[int, int], ...] = (
    (0, U64_MAX),      # event_id
    (0, U32_MAX),      # instrument_id
    (0, U16_MAX),      # venue_id
    (I64_MIN, I64_MAX),  # exchange_ts
    (I64_MIN, I64_MAX),  # receive_ts
    (0, U64_MAX),      # sequence
    (0, 0xFF),         # event_type
    (0, 0xFF),         # side
    (I64_MIN, I64_MAX),  # price_ticks
    (I64_MIN, I64_MAX),  # qty
    (0, U64_MAX),      # order_id
    (0, U64_MAX),      # trade_id
)
_SIGNED = tuple(lo < 0 for lo, _ in _DOMAIN)

# Strict JSON integer token: optional '-', then '0' or a non-zero-led digit run.
_INT = r"(-?(?:0|[1-9][0-9]*))"
_WS = r"[ \t\r\n]*"
_LINE_RE = re.compile(
    _WS + r"\{" + _WS
    + (_WS + "," + _WS).join(f'"{k}"{_WS}:{_WS}{_INT}' for k in _KEYS)
    + _WS + r"\}" + _WS + r"\Z"
)


def encode_jsonl_line(ev: MarketEvent) -> str:
    """Encode one event as a canonical JSONL line (no trailing newline).

    Canonical form: keys in pinned order, compact separators, integers only.
    """
    return (
        '{"event_id":%d,"instrument_id":%d,"venue_id":%d,"exchange_ts":%d,'
        '"receive_ts":%d,"sequence":%d,"event_type":%d,"side":%d,'
        '"price_ticks":%d,"qty":%d,"order_id":%d,"trade_id":%d}'
        % (
            ev.event_id, ev.instrument_id, ev.venue_id, ev.exchange_ts,
            ev.receive_ts, ev.sequence, ev.event_type, ev.side,
            ev.price_ticks, ev.qty, ev.order_id, ev.trade_id,
        )
    )


def _preview(line: str) -> str:
    return repr(line[:80] + ("..." if len(line) > 80 else ""))


def decode_jsonl_line(line: str) -> MarketEvent:
    """Decode one canonical JSONL line. Strict: exact keys, integer values."""
    m = _LINE_RE.match(line)
    if m is None:
        # Diagnose the most common shapes for a useful message.
        stripped = line.strip()
        if not stripped.startswith("{") or not stripped.endswith("}"):
            raise ValueError(f"malformed JSONL line: not an object: {_preview(line)}")
        found = re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"[ \t\r\n]*:', line)
        missing = [k for k in _KEYS if k not in found]
        extra = [k for k in found if k not in _KEYS]
        if missing or extra or list(found) != list(_KEYS):
            raise ValueError(
                f"JSONL keys mismatch (missing={missing}, extra={extra}, "
                f"order must be {list(_KEYS)}): {_preview(line)}"
            )
        raise ValueError(
            "JSONL field values must be integers (no floats, exponents, leading "
            f"zeros, '+', bools or strings; no trailing content): {_preview(line)}"
        )
    vals = []
    for i, tok in enumerate(m.groups()):
        if tok[0] == "-" and not _SIGNED[i]:
            raise ValueError(
                f"JSONL field {_KEYS[i]!r} must be a non-negative integer: {tok}"
            )
        v = int(tok)
        lo, hi = _DOMAIN[i]
        if not (lo <= v <= hi):
            raise ValueError(f"JSONL field {_KEYS[i]!r} out of range: {tok}")
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
    """Iterate events from a canonical JSONL file (blank lines skipped)."""
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if line:
                try:
                    yield decode_jsonl_line(line)
                except ValueError as exc:
                    raise ValueError(f"{path}:{lineno}: {exc}") from None


def read_jsonl(path: Union[str, Path]) -> List[MarketEvent]:
    """Read all events from a canonical JSONL file."""
    return list(iter_jsonl(path))


# --------------------------------------------------------------------- IAP1

IAP1_MAGIC = 0x49415031
#: Version written by ``encode_iap1`` (header + records + integrity trailer).
IAP1_VERSION = 2
#: Legacy version (no trailer) still accepted by the decoder.
IAP1_VERSION_LEGACY = 1
_HEADER = struct.Struct("<IIQ")  # magic u32 | version u32 | count u64  (16 bytes)
_RECORD = struct.Struct("<QIHBBqqQqqQQ")  # 72 bytes, no padding
_TRAILER = struct.Struct("<IIQ")  # crc32 u32 | reserved u32 = 0 | count u64
IAP1_HEADER_SIZE = _HEADER.size
IAP1_RECORD_SIZE = _RECORD.size
IAP1_TRAILER_SIZE = _TRAILER.size
assert IAP1_HEADER_SIZE == 16 and IAP1_RECORD_SIZE == 72 and IAP1_TRAILER_SIZE == 16


def crc32(data: bytes) -> int:
    """CRC-32 (IEEE 802.3, as zlib) of ``data`` — the IAP1 trailer checksum."""
    return zlib.crc32(data) & 0xFFFFFFFF


def _check_domain(ev: MarketEvent, index: int) -> None:
    for i, name in enumerate(_KEYS):
        v = getattr(ev, name)
        if type(v) is not int:
            raise ValueError(f"event {index}: field {name!r} must be an int, got {v!r}")
        lo, hi = _DOMAIN[i]
        if not (lo <= v <= hi):
            raise ValueError(f"event {index}: field {name!r} out of range: {v}")


def encode_iap1(events: Sequence[MarketEvent]) -> bytes:
    """Encode events to IAP1 v2 bytes (header + 72-byte LE records + trailer).

    Raises ValueError (never struct.error) if any field is out of domain.
    """
    parts = [_HEADER.pack(IAP1_MAGIC, IAP1_VERSION, len(events))]
    pack = _RECORD.pack
    for index, ev in enumerate(events):
        try:
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
        except struct.error:
            _check_domain(ev, index)  # raises the precise ValueError
            raise ValueError(f"event {index}: cannot encode {ev!r}") from None
    body = b"".join(parts)
    return body + _TRAILER.pack(crc32(body), 0, len(events))


class Iap1Decoded(NamedTuple):
    """Result of ``decode_iap1_ex``: events plus the file's format version."""

    events: List[MarketEvent]
    version: int
    #: True iff the file carried (and passed) the CRC-32 integrity trailer.
    integrity_checked: bool


def decode_iap1_ex(data: bytes) -> Iap1Decoded:
    """Decode IAP1 bytes (v2 with trailer, or legacy v1); see module docstring."""
    if len(data) < IAP1_HEADER_SIZE:
        raise ValueError(f"IAP1 file truncated: {len(data)} bytes < 16-byte header")
    magic, version, count = _HEADER.unpack_from(data, 0)
    if magic != IAP1_MAGIC:
        raise ValueError(f"bad IAP1 magic: 0x{magic:08X} (expected 0x{IAP1_MAGIC:08X})")
    if version not in (IAP1_VERSION, IAP1_VERSION_LEGACY):
        raise ValueError(f"unsupported IAP1 version: {version}")
    with_trailer = version == IAP1_VERSION
    body_size = IAP1_HEADER_SIZE + IAP1_RECORD_SIZE * count
    expected = body_size + (IAP1_TRAILER_SIZE if with_trailer else 0)
    if len(data) != expected:
        raise ValueError(
            f"IAP1 size mismatch: {len(data)} bytes, header count={count} "
            f"(version {version}) implies {expected}"
        )
    if with_trailer:
        crc, reserved, count_echo = _TRAILER.unpack_from(data, body_size)
        if reserved != 0:
            raise ValueError(f"IAP1 trailer reserved field must be 0: {reserved}")
        if count_echo != count:
            raise ValueError(
                f"IAP1 trailer count echo {count_echo} != header count {count}"
            )
        actual = crc32(data[:body_size])
        if actual != crc:
            raise ValueError(
                f"IAP1 CRC-32 mismatch: trailer 0x{crc:08X}, computed 0x{actual:08X}"
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
    return Iap1Decoded(events, version, with_trailer)


def decode_iap1(data: bytes) -> List[MarketEvent]:
    """Decode IAP1 bytes. Rejects bad magic/version, truncation, count/CRC mismatch."""
    return decode_iap1_ex(data).events


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
