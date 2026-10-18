"""IEX HIST (pcap.gz over IEX-TP) -> the ITCH 5.0 message model (v1.17, A1).

IEX publishes every trading day's market-data feeds as pcap captures
(``https://iextrading.com/trading/market-data/``, T+1, no account): DEEP+
(``IEXTP1_DEEP+``, displayed orders order by order, message protocol
``0x8005``), DEEP (``IEXTP1_DEEP``, aggregated price levels, ``0x8004``) and
TOPS (``IEXTP1_TOPS``, the best bid / offer, ``0x8003``).  This module reads
such a capture - the pcap framing, the UDP payloads, the IEX-TP v1 segments
and the message blocks inside them - and **translates every message into
the message tuples of** :mod:`iap.marketdata.itch50`, exactly as the
Databento reader does (:mod:`iap.marketdata.dbn_mbo`).  The ingest then runs
the unchanged :class:`~iap.marketdata.itch50.Itch50Mapper`, so an IEX session
is normalised by the same rules as a Nasdaq session (docs/REAL_DATA.md §4.5;
the cross-check tests pin it) and carries venue ``IEXG`` (id 102).

Translation of DEEP+ (``--format iex-deep-plus``; the order-level feed):

=========  ==============================================================
DEEP+      ITCH message fed to the mapper
=========  ==============================================================
``S``      ``SystemEvent`` (``O S R M E C`` -> ``O S Q M E C``: IEX's
           *Start of Regular Market Hours* is ITCH's ``Q``)
``D``      ``StockDirectory`` (round lot, ETP flag ``0x20``; a universe
           symbol with no directory row before its first order gets one
           synthesised, round lot 100, and is counted)
``H``      ``TradingAction`` (``H`` halted, ``P`` paused, ``O`` order
           acceptance period -> ``Q`` quoting, ``T`` trading)
``O``      ``OperationalHalt`` on the mapped market (``O`` halted,
           ``N`` released)
``a``      ``AddOrder`` (side ``8`` buy / ``5`` sell)
``M``      ``OrderCancel`` of the size reduction when the priority is
           maintained, the price unchanged and the size smaller (ITCH keeps
           queue position on a partial cancel); otherwise ``OrderReplace``
           keeping the order id (cancel + add, new priority)
``R``      ``OrderDelete``
``L``      ``OrderExecuted`` (``OrderExecutedWithPrice`` printable when the
           trade price differs from the resting order's); the IEX trade id
           is the match number
``T``      a non-displayed execution: ``Trade`` (signed by the mapper's
           quote rule) - or ``CrossTrade`` when the single-price-cross flag
           (``0x08``) is set (type by time: ``O`` up to one minute after the
           open, ``C`` from the close on, else ``H``)
``B``      ``BrokenTrade``
``C``      clear book: ``OrderDelete`` of every live order of the symbol
``E I P X A``  counted, no effect (security event, retail indicator, short
           sale test, official price, auction information)
=========  ==============================================================

Translation of DEEP (``--format iex-deep``; aggregated price levels): a
level feed has no orders, so every price level is represented by
**synthetic orders** (ids allocated by this module, counted): a level
increase adds one synthetic order of the increment at the back of the
level, a level decrease is consumed from the FRONT of the level when
trade reports of the same atomic event (same timestamp, price-level
updates with the event flag off, closed by one with the flag on) carry
that price - those are ``OrderExecuted`` with the trade id - and from the
BACK as ``OrderCancel`` / ``OrderDelete`` otherwise.  A trade report no
level update accounts for is a non-displayed print (``Trade``).  Queue
positions inside a level are therefore MODELLED, not observed; the
manifest says so (``iex.level_feed``).

**TOPS oracle.**  With ``--tops FILE`` the reader also reads the day's TOPS
capture and, whenever the mapped book's best bid or offer changes, checks
it against the TOPS quote of the same timestamp (price and aggregate
size, both sides).  The result is the dataset's per-symbol
``book_verification`` (``match`` / ``diverged`` with the first
divergence), so a divergent decode refuses the ingest unless
``--allow-book-divergence`` is passed - the same contract LOBSTER's
orderbook file gives the ITCH mapper.

Prices are IEX's 1e-4 fixed point, the same unit as ITCH; timestamps are
nanoseconds since the epoch and are converted to nanoseconds since the
session's New York midnight; messages outside the session date are
counted and dropped.  Nothing here downloads: the HIST file is obtained
by the owner (``tools/r7_download.py`` style, data/real) and passed in.
"""

from __future__ import annotations

import gzip
import json
import struct
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, BinaryIO, NamedTuple

from iap.marketdata.feederrors import IngestError
from iap.marketdata.itch50 import (
    AddOrder,
    BrokenTrade,
    CrossTrade,
    OperationalHalt,
    OrderCancel,
    OrderDelete,
    OrderExecuted,
    OrderExecutedWithPrice,
    OrderReplace,
    StockDirectory,
    SystemEvent,
    Trade,
    TradingAction,
    session_midnight_ns,
)

__all__ = [
    "PROTOCOL_DEEP",
    "TOPS_EXTRACT_SUFFIX",
    "PROTOCOL_DEEP_PLUS",
    "PROTOCOL_TOPS",
    "VENUE",
    "VENUE_ID",
    "IexReader",
    "TopsOracle",
    "TopsQuotes",
    "decode_message",
    "pcap_packets",
    "tp_segments",
]

#: IEX-TP message protocol ids (the feed specifications).
PROTOCOL_TOPS = 0x8003
PROTOCOL_DEEP = 0x8004
PROTOCOL_DEEP_PLUS = 0x8005
PROTOCOL_NAMES = {PROTOCOL_TOPS: "TOPS", PROTOCOL_DEEP: "DEEP", PROTOCOL_DEEP_PLUS: "DEEP+"}
#: The venue of an IEX dataset (real venue ids start at 101; Nasdaq is 101).
VENUE = "IEXG"
VENUE_ID = 102
#: A TOPS extract (:meth:`TopsQuotes.save`): the universe's quotes only.
TOPS_EXTRACT_SUFFIX = ".tops.json.gz"
TOPS_EXTRACT_SCHEMA = "iap.iex.tops_extract/v1"

_NS_PER_DAY = 86_400 * 1_000_000_000
_H = 3600 * 1_000_000_000
_M = 60 * 1_000_000_000
#: The regular session in New York time (the ITCH mapper's calendar).
_OPEN_NS = 9 * _H + 30 * _M
_CLOSE_NS = 16 * _H

# ----------------------------------------------------------------- framing

#: pcap magic -> (byte order, timestamp fraction unit in ns)
_PCAP_MAGIC = {
    b"\xd4\xc3\xb2\xa1": ("<", 1_000),
    b"\xa1\xb2\xc3\xd4": (">", 1_000),
    b"\x4d\x3c\xb2\xa1": ("<", 1),
    b"\xa1\xb2\x3c\x4d": (">", 1),
}
_PCAPNG_MAGIC = b"\x0a\x0d\x0d\x0a"
_LINKTYPE_ETHERNET = 1
_LINKTYPE_RAW = 101
_ETHERTYPE_IPV4 = 0x0800
_ETHERTYPE_VLAN = (0x8100, 0x88A8)
_IP_PROTO_UDP = 17
_CHUNK = 1 << 20


def open_capture(path: str | Path) -> BinaryIO:
    """The capture, gunzipped when its first bytes say so (by content)."""
    p = Path(path)
    with open(p, "rb") as f:
        magic = f.read(2)
    if magic == b"\x1f\x8b":
        return gzip.open(p, "rb")  # type: ignore[return-value]
    return open(p, "rb")


class PcapStats:
    """Counters of :func:`pcap_packets`."""

    __slots__ = ("packets", "bytes", "non_ipv4", "non_udp", "truncated", "vlan")

    def __init__(self) -> None:
        self.packets = 0
        self.bytes = 0
        self.non_ipv4 = 0
        self.non_udp = 0
        self.truncated = 0
        self.vlan = 0

    def as_dict(self) -> dict[str, int]:
        return {k: getattr(self, k) for k in self.__slots__}


def _udp_payload(data: bytes, linktype: int, stats: PcapStats) -> bytes | None:
    """The UDP payload of one link-layer frame (``None`` when it is not a
    UDP/IPv4 packet, counted)."""
    off = 0
    if linktype == _LINKTYPE_ETHERNET:
        if len(data) < 14:
            stats.truncated += 1
            return None
        ethertype = struct.unpack(">H", data[12:14])[0]
        off = 14
        while ethertype in _ETHERTYPE_VLAN:
            if len(data) < off + 4:
                ethertype = 0
                break
            stats.vlan += 1
            ethertype = struct.unpack(">H", data[off + 2 : off + 4])[0]
            off += 4
        if ethertype != _ETHERTYPE_IPV4:
            stats.non_ipv4 += 1
            return None
    if len(data) < off + 20 or data[off] >> 4 != 4:
        stats.non_ipv4 += 1
        return None
    ihl = (data[off] & 0x0F) * 4
    total = struct.unpack(">H", data[off + 2 : off + 4])[0]
    if data[off + 9] != _IP_PROTO_UDP:
        stats.non_udp += 1
        return None
    udp = off + ihl
    if len(data) < udp + 8:
        stats.truncated += 1
        return None
    udp_len = struct.unpack(">H", data[udp + 4 : udp + 6])[0]
    end = min(len(data), off + total, udp + udp_len)
    return data[udp + 8 : end]


def _read(f: BinaryIO, n: int, stats: PcapStats) -> bytes | None:
    """``n`` bytes, or ``None`` at a clean end; a short read or a truncated
    gzip member (a partial download) is counted and ends the stream."""
    try:
        data = f.read(n)
    except (EOFError, OSError):
        stats.truncated += 1
        return None
    if not data and n:
        return None
    if len(data) < n:
        stats.truncated += 1
        return None
    return data


_PCAPNG_SHB, _PCAPNG_IDB, _PCAPNG_SPB, _PCAPNG_EPB = 0x0A0D0D0A, 1, 3, 6
_PCAPNG_IF_TSRESOL = 9


def _pcapng_packets(f: BinaryIO, stats: PcapStats) -> Iterator[tuple[int, bytes]]:
    """pcapng (RFC draft-ietf-opsawg-pcapng): section header blocks set the
    byte order, interface description blocks the link type and timestamp
    resolution (``if_tsresol``), enhanced / simple packet blocks carry the
    frames; every other block is skipped."""
    order = "<"
    interfaces: list[tuple[int, int]] = []  # (linktype, ns per tick)
    while True:
        head = _read(f, 8, stats)
        if head is None:
            return
        if head[:4] == _PCAPNG_MAGIC:
            # a new section: the byte order comes from its magic
            rest = _read(f, 8, stats)
            if rest is None:
                return
            bom = rest[4:8]
            order = "<" if bom == b"M<+" else ">" if bom == b"+<M" else order
            total = struct.unpack(order + "I", head[4:8])[0]
            if _read(f, total - 16, stats) is None:
                return
            interfaces = []
            continue
        btype, total = struct.unpack(order + "II", head)
        if total < 12:
            stats.truncated += 1
            return
        body = _read(f, total - 8, stats)
        if body is None:
            return
        body = body[:-4]  # the trailing block total length
        if btype == _PCAPNG_IDB:
            linktype = struct.unpack(order + "H", body[0:2])[0]
            tick_ns = 1_000  # if_tsresol default: microseconds
            pos = 8
            while pos + 4 <= len(body):
                code, olen = struct.unpack(order + "HH", body[pos : pos + 4])
                val = body[pos + 4 : pos + 4 + olen]
                if code == 0:
                    break
                if code == _PCAPNG_IF_TSRESOL and olen >= 1:
                    v = val[0]
                    tick_ns = (
                        10 ** (9 - (v & 0x7F))
                        if not v & 0x80
                        else 1_000_000_000 // (1 << (v & 0x7F))
                    )
                pos += 4 + ((olen + 3) & ~3)
            interfaces.append((linktype, max(1, tick_ns)))
        elif btype == _PCAPNG_EPB:
            if_id, ts_hi, ts_lo, incl, _orig = struct.unpack(order + "IIIII", body[0:20])
            data = body[20 : 20 + incl]
            if if_id >= len(interfaces):
                stats.truncated += 1
                continue
            linktype, tick_ns = interfaces[if_id]
            stats.packets += 1
            stats.bytes += total
            payload = _udp_payload(data, linktype, stats)
            if payload is not None:
                yield ((ts_hi << 32) | ts_lo) * tick_ns, payload
        elif btype == _PCAPNG_SPB:
            if not interfaces:
                stats.truncated += 1
                continue
            (orig,) = struct.unpack(order + "I", body[0:4])
            data = body[4 : 4 + orig]
            linktype, _ = interfaces[0]
            stats.packets += 1
            stats.bytes += total
            payload = _udp_payload(data, linktype, stats)
            if payload is not None:
                yield 0, payload


def pcap_packets(f: BinaryIO, stats: PcapStats | None = None) -> Iterator[tuple[int, bytes]]:
    """``(capture_ts_ns, udp_payload)`` of every UDP/IPv4 packet of a classic
    pcap or a pcapng stream (Ethernet with optional 802.1Q tags, or the raw
    IP link type; IEX HIST files are pcapng).  Other packets are counted; a
    truncated stream (a partial download) ends it and is counted."""
    stats = stats if stats is not None else PcapStats()
    head = f.read(24)
    if len(head) < 24:
        raise IngestError("not a pcap file (short global header)")
    if head[:4] == _PCAPNG_MAGIC:
        yield from _pcapng_packets(_Prefixed(head, f), stats)
        return
    order_unit = _PCAP_MAGIC.get(head[:4])
    if order_unit is None:
        raise IngestError(f"not a pcap file (magic {head[:4].hex()})")
    order, unit = order_unit
    linktype = struct.unpack(order + "I", head[20:24])[0] & 0x0FFF_FFFF
    if linktype not in (_LINKTYPE_ETHERNET, _LINKTYPE_RAW):
        raise IngestError(f"pcap link type {linktype} is not Ethernet (1) or raw IP (101)")
    rec = struct.Struct(order + "IIII")
    while True:
        h = _read(f, 16, stats)
        if h is None:
            return
        sec, frac, incl, orig = rec.unpack(h)
        data = _read(f, incl, stats)
        if data is None:
            return
        stats.packets += 1
        stats.bytes += 16 + len(data)
        ts = sec * 1_000_000_000 + frac * unit
        off = 0
        if linktype == _LINKTYPE_ETHERNET:
            if len(data) < 14:
                stats.truncated += 1
                continue
            ethertype = struct.unpack(">H", data[12:14])[0]
            off = 14
            while ethertype in _ETHERTYPE_VLAN:
                if len(data) < off + 4:
                    ethertype = 0
                    break
                stats.vlan += 1
                ethertype = struct.unpack(">H", data[off + 2 : off + 4])[0]
                off += 4
            if ethertype != _ETHERTYPE_IPV4:
                stats.non_ipv4 += 1
                continue
        if len(data) < off + 20 or data[off] >> 4 != 4:
            stats.non_ipv4 += 1
            continue
        ihl = (data[off] & 0x0F) * 4
        total = struct.unpack(">H", data[off + 2 : off + 4])[0]
        if data[off + 9] != _IP_PROTO_UDP:
            stats.non_udp += 1
            continue
        udp = off + ihl
        if len(data) < udp + 8:
            stats.truncated += 1
            continue
        udp_len = struct.unpack(">H", data[udp + 4 : udp + 6])[0]
        end = min(len(data), off + total, udp + udp_len)
        yield ts, data[udp + 8 : end]


class _Prefixed:
    """A stream whose first bytes were already read (the magic probe)."""

    def __init__(self, head: bytes, f: BinaryIO) -> None:
        self._head = head
        self._f = f

    def read(self, n: int = -1) -> bytes:
        if self._head:
            take, self._head = self._head[:n], self._head[n:] if n >= 0 else b""
            if n < 0 or len(take) == n:
                return take if n >= 0 else take + self._f.read()
            return take + self._f.read(n - len(take))
        return self._f.read(n)


class Segment(NamedTuple):
    """One IEX-TP v1 outbound segment."""

    protocol: int
    channel: int
    session: int
    stream_offset: int
    first_sequence: int
    send_time: int
    messages: tuple[bytes, ...]


_TP_HEADER = struct.Struct("<BBHIIHHqqq")  # 40 bytes, little endian
TP_HEADER_SIZE = _TP_HEADER.size
TP_VERSION = 1


def tp_segment(payload: bytes) -> Segment | None:
    """Decode one UDP payload as an IEX-TP segment (``None`` when it is not
    one: too short or another version)."""
    if len(payload) < TP_HEADER_SIZE:
        return None
    version, _reserved, proto, channel, session, plen, count, offset, seq, send = (
        _TP_HEADER.unpack_from(payload)
    )
    if version != TP_VERSION:
        return None
    body = payload[TP_HEADER_SIZE : TP_HEADER_SIZE + plen]
    msgs: list[bytes] = []
    pos = 0
    for _ in range(count):
        if pos + 2 > len(body):
            break
        n = body[pos] | (body[pos + 1] << 8)
        pos += 2
        msgs.append(body[pos : pos + n])
        pos += n
    return Segment(proto, channel, session, offset, seq, send, tuple(msgs))


def tp_segments(payloads: Iterable[bytes]) -> Iterator[Segment]:
    for p in payloads:
        s = tp_segment(p)
        if s is not None:
            yield s


# ---------------------------------------------------------------- messages


class IexSystemEvent(NamedTuple):
    code: str
    ts: int


class IexDirectory(NamedTuple):
    flags: int
    ts: int
    symbol: str
    round_lot: int
    poc_price: int
    luld_tier: int


class IexTradingStatus(NamedTuple):
    status: str
    ts: int
    symbol: str
    reason: str


class IexOperationalHalt(NamedTuple):
    status: str
    ts: int
    symbol: str


class IexSymbolMessage(NamedTuple):
    """``E`` security event, ``I`` retail indicator, ``P`` short-sale test
    (counted, no effect)."""

    kind: str
    code: str
    ts: int
    symbol: str


class IexPriceLevel(NamedTuple):
    side: int  # 0 bid, 1 ask
    flags: int
    ts: int
    symbol: str
    size: int
    price: int


class IexQuote(NamedTuple):
    flags: int
    ts: int
    symbol: str
    bid_size: int
    bid: int
    ask: int
    ask_size: int


class IexTrade(NamedTuple):
    flags: int
    ts: int
    symbol: str
    size: int
    price: int
    trade_id: int


class IexTradeBreak(NamedTuple):
    flags: int
    ts: int
    symbol: str
    size: int
    price: int
    trade_id: int


class IexOfficialPrice(NamedTuple):
    kind: str
    ts: int
    symbol: str
    price: int


class IexAuction(NamedTuple):
    kind: str
    ts: int
    symbol: str
    paired: int
    reference: int
    indicative: int
    imbalance: int
    imbalance_side: str


class IexAddOrder(NamedTuple):
    side: int
    ts: int
    symbol: str
    order_id: int
    size: int
    price: int


class IexModifyOrder(NamedTuple):
    flags: int  # bit 0: priority maintained
    ts: int
    symbol: str
    order_id: int
    size: int
    price: int


class IexDeleteOrder(NamedTuple):
    ts: int
    symbol: str
    order_id: int


class IexExecuted(NamedTuple):
    flags: int
    ts: int
    symbol: str
    order_id: int
    size: int
    price: int
    trade_id: int


class IexClearBook(NamedTuple):
    ts: int
    symbol: str


#: Sale condition flags (DEEP / DEEP+ appendix A).
FLAG_ISO = 0x80
FLAG_EXTENDED_HOURS = 0x40
FLAG_ODD_LOT = 0x20
FLAG_TRADE_THROUGH_EXEMPT = 0x10
FLAG_SINGLE_PRICE_CROSS = 0x08
#: Security directory flags.
FLAG_TEST_SECURITY = 0x80
FLAG_WHEN_ISSUED = 0x40
FLAG_ETP = 0x20
#: Price level update event flag: the atomic update is complete.
FLAG_EVENT_COMPLETE = 0x01

_SIDE_BUY, _SIDE_SELL = 0x38, 0x35


def _sym(data: bytes, off: int) -> str:
    return data[off : off + 8].decode("ascii", "replace").rstrip(" ")


def _u32(data: bytes, off: int) -> int:
    return struct.unpack_from("<I", data, off)[0]


def _i64(data: bytes, off: int) -> int:
    return struct.unpack_from("<q", data, off)[0]


class UnknownMessage(NamedTuple):
    type_code: str
    length: int


def decode_message(protocol: int, data: bytes) -> tuple:
    """One message block's data -> a typed tuple (:class:`UnknownMessage`
    for a type the protocol does not define or a block too short for it).
    ``protocol`` decides the order-level types (``a M R L C`` exist in
    DEEP+ only; ``8 5`` in DEEP only; ``Q`` in TOPS only)."""
    if not data:
        return UnknownMessage("", 0)
    t = data[0]
    n = len(data)
    try:
        if t == 0x53 and n >= 10:  # S
            return IexSystemEvent(chr(data[1]), _i64(data, 2))
        if t == 0x44 and n >= 31:  # D
            return IexDirectory(
                data[1], _i64(data, 2), _sym(data, 10), _u32(data, 18), _i64(data, 22), data[30]
            )
        if t == 0x48 and n >= 22:  # H
            return IexTradingStatus(
                chr(data[1]),
                _i64(data, 2),
                _sym(data, 10),
                data[18:22].decode("ascii", "replace").rstrip(" "),
            )
        if t == 0x4F and n >= 18:  # O
            return IexOperationalHalt(chr(data[1]), _i64(data, 2), _sym(data, 10))
        if t in (0x45, 0x49, 0x50) and n >= 18:  # E I P
            return IexSymbolMessage(chr(t), chr(data[1]), _i64(data, 2), _sym(data, 10))
        if t in (0x38, 0x35) and protocol == PROTOCOL_DEEP and n >= 30:  # 8 5
            return IexPriceLevel(
                0 if t == 0x38 else 1,
                data[1],
                _i64(data, 2),
                _sym(data, 10),
                _u32(data, 18),
                _i64(data, 22),
            )
        if t == 0x51 and protocol == PROTOCOL_TOPS and n >= 42:  # Q
            return IexQuote(
                data[1],
                _i64(data, 2),
                _sym(data, 10),
                _u32(data, 18),
                _i64(data, 22),
                _i64(data, 30),
                _u32(data, 38),
            )
        if t == 0x54 and n >= 38:  # T
            return IexTrade(
                data[1],
                _i64(data, 2),
                _sym(data, 10),
                _u32(data, 18),
                _i64(data, 22),
                _i64(data, 30),
            )
        if t == 0x42 and n >= 38:  # B
            return IexTradeBreak(
                data[1],
                _i64(data, 2),
                _sym(data, 10),
                _u32(data, 18),
                _i64(data, 22),
                _i64(data, 30),
            )
        if t == 0x58 and n >= 26:  # X
            return IexOfficialPrice(chr(data[1]), _i64(data, 2), _sym(data, 10), _i64(data, 18))
        if t == 0x41 and n >= 43:  # A
            return IexAuction(
                chr(data[1]),
                _i64(data, 2),
                _sym(data, 10),
                _u32(data, 18),
                _i64(data, 22),
                _i64(data, 30),
                _u32(data, 38),
                chr(data[42]),
            )
        if protocol == PROTOCOL_DEEP_PLUS:
            if t == 0x61 and n >= 38:  # a
                side = data[1]
                if side not in (_SIDE_BUY, _SIDE_SELL):
                    return UnknownMessage("a", n)
                return IexAddOrder(
                    0 if side == _SIDE_BUY else 1,
                    _i64(data, 2),
                    _sym(data, 10),
                    _i64(data, 18),
                    _u32(data, 26),
                    _i64(data, 30),
                )
            if t == 0x4D and n >= 38:  # M
                return IexModifyOrder(
                    data[1],
                    _i64(data, 2),
                    _sym(data, 10),
                    _i64(data, 18),
                    _u32(data, 26),
                    _i64(data, 30),
                )
            if t == 0x52 and n >= 26:  # R
                return IexDeleteOrder(_i64(data, 2), _sym(data, 10), _i64(data, 18))
            if t == 0x4C and n >= 46:  # L
                return IexExecuted(
                    data[1],
                    _i64(data, 2),
                    _sym(data, 10),
                    _i64(data, 18),
                    _u32(data, 26),
                    _i64(data, 30),
                    _i64(data, 38),
                )
            if t == 0x43 and n >= 18:  # C
                return IexClearBook(_i64(data, 2), _sym(data, 10))
    except struct.error:
        pass
    return UnknownMessage(chr(t) if 32 <= t < 127 else f"0x{t:02x}", n)


# ------------------------------------------------------------ TOPS oracle


class TopsQuotes:
    """The TOPS quotes of a universe, by symbol and timestamp (the last
    quote of a timestamp wins: TOPS emits one quote per BBO change)."""

    def __init__(self) -> None:
        self.by_symbol: dict[str, dict[int, tuple[int, int, int, int]]] = {}
        self.quotes = 0
        self.protocol: int | None = None
        #: the capture an extract was taken from (:meth:`load_extract`)
        self.source: dict = {}

    @classmethod
    def load(cls, path: str | Path, symbols: Iterable[str], midnight: int) -> TopsQuotes:
        """The universe's quotes from the day's TOPS capture, or from an
        extract written by :meth:`save` (``*.tops.json.gz``: the process-and-
        delete chain reads the capture once, keeps the universe's quotes and
        deletes the capture before the book feed is downloaded)."""
        if Path(path).name.endswith(TOPS_EXTRACT_SUFFIX):
            return cls.load_extract(path, symbols)
        out = cls()
        wanted = set(symbols)
        stats = PcapStats()
        with open_capture(path) as f:
            for _, payload in pcap_packets(f, stats):
                seg = tp_segment(payload)
                if seg is None:
                    continue
                if seg.protocol != PROTOCOL_TOPS:
                    raise IngestError(
                        f"{Path(path).name}: message protocol 0x{seg.protocol:04x} is not TOPS"
                    )
                out.protocol = seg.protocol
                for m in seg.messages:
                    q = decode_message(PROTOCOL_TOPS, m)
                    if type(q) is IexQuote and q.symbol in wanted:
                        ts = q.ts - midnight
                        if 0 <= ts < _NS_PER_DAY:
                            out.quotes += 1
                            out.by_symbol.setdefault(q.symbol, {})[ts] = (
                                q.bid,
                                q.bid_size,
                                q.ask,
                                q.ask_size,
                            )
        if out.protocol is None:
            raise IngestError(f"{Path(path).name}: no IEX-TP TOPS segment found")
        return out

    def get(self, symbol: str, ts: int) -> tuple[int, int, int, int] | None:
        return self.by_symbol.get(symbol, {}).get(ts)

    def save(self, path: str | Path, *, source: dict | None = None) -> Path:
        """Write the loaded quotes as a gzip JSON extract (v1.17 A4); ``source``
        names the capture they came from (file, bytes, sha256)."""
        p = Path(path)
        if not p.name.endswith(TOPS_EXTRACT_SUFFIX):
            raise IngestError(f"a TOPS extract must end with {TOPS_EXTRACT_SUFFIX}: {p.name}")
        doc = {
            "schema": TOPS_EXTRACT_SCHEMA,
            "source": source or {},
            "quotes": self.quotes,
            "by_symbol": {
                s: [[ts, *q] for ts, q in sorted(rows.items())]
                for s, rows in self.by_symbol.items()
            },
        }
        # mtime=0: the same quotes give the same bytes (REL-13: a resumed day
        # reproduces the extract's sha256 recorded in dataset.json)
        data = json.dumps(doc, separators=(",", ":")).encode("utf-8")
        with open(p, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as f:
            f.write(data)
        return p

    @classmethod
    def load_extract(cls, path: str | Path, symbols: Iterable[str]) -> TopsQuotes:
        """Read an extract of :meth:`save`; refuses one that lacks a universe symbol."""
        with gzip.open(path, "rt", encoding="utf-8") as f:
            doc = json.load(f)
        if doc.get("schema") != TOPS_EXTRACT_SCHEMA:
            raise IngestError(f"{Path(path).name}: not a TOPS extract ({doc.get('schema')!r})")
        missing = [s for s in symbols if s not in doc["by_symbol"]]
        if missing:
            raise IngestError(f"{Path(path).name}: the TOPS extract has no quotes for {missing}")
        out = cls()
        out.protocol = PROTOCOL_TOPS
        out.quotes = int(doc["quotes"])
        out.by_symbol = {
            s: {int(r[0]): (int(r[1]), int(r[2]), int(r[3]), int(r[4])) for r in rows}
            for s, rows in doc["by_symbol"].items()
        }
        out.source = dict(doc.get("source") or {})
        return out


class TopsOracle:
    """Compare the mapped book's BBO with TOPS whenever it changes (module
    docs).  ``report()`` is the ingest's ``book_verification`` block."""

    def __init__(self, quotes: TopsQuotes, symbols: Iterable[str]) -> None:
        self.quotes = quotes
        self.symbols = tuple(symbols)
        self.compared = dict.fromkeys(self.symbols, 0)
        self.mismatched = dict.fromkeys(self.symbols, 0)
        self.no_tops_quote = dict.fromkeys(self.symbols, 0)
        self.first_divergence: dict[str, dict | None] = dict.fromkeys(self.symbols)
        self._last: dict[str, tuple[int, int, int, int] | None] = dict.fromkeys(self.symbols)

    def check(self, symbol: str, ts: int, bbo: tuple[int, int, int, int]) -> None:
        """``bbo`` = (bid, bid size, ask, ask size) of the mapped book at the
        end of timestamp ``ts`` (0 for an empty side)."""
        if bbo == self._last[symbol]:
            return
        self._last[symbol] = bbo
        tops = self.quotes.get(symbol, ts)
        if tops is None:
            self.no_tops_quote[symbol] += 1
            return
        self.compared[symbol] += 1
        if tops != bbo:
            self.mismatched[symbol] += 1
            if self.first_divergence[symbol] is None:
                self.first_divergence[symbol] = {
                    "ts_ns_since_midnight": ts,
                    "book": {"bid": bbo[0], "bid_size": bbo[1], "ask": bbo[2], "ask_size": bbo[3]},
                    "tops": {
                        "bid": tops[0],
                        "bid_size": tops[1],
                        "ask": tops[2],
                        "ask_size": tops[3],
                    },
                }

    def report(self) -> dict[str, dict]:
        out = {}
        for s in self.symbols:
            status = "diverged" if self.mismatched[s] else "match"
            if self.compared[s] == 0 and not self.mismatched[s]:
                status = "no_comparison"
            out[s] = {
                "status": status,
                "oracle": "tops_quote_equals_deep_cumulative_round_lot",
                "compared": self.compared[s],
                "mismatched": self.mismatched[s],
                "bbo_changes_without_tops_quote": self.no_tops_quote[s],
                "tops_quotes": len(self.quotes.by_symbol.get(s, {})),
                "first_divergence": self.first_divergence[s],
            }
        return out


# ---------------------------------------------------------------- reader

_SYSTEM_CODES = {"O": "O", "S": "S", "R": "Q", "M": "M", "E": "E", "C": "C"}
_TRADING_STATES = {"H": "H", "P": "P", "O": "Q", "T": "T"}
#: Synthetic order ids of the DEEP level feed start here (well below the
#: canonical synthetic range, far above any IEX order id seen).
LEVEL_ORDER_ID_BASE = 1 << 48


#: What the capture timestamp is and is NOT (v1.18 REL-04, review X-D).  The
#: file stores one timestamp per packet, taken by the capture host that
#: recorded the multicast feed; it is the nearest thing this repository has
#: to a ``receive_ts`` for IEX, and it must never be read as more than that.
CAPTURE_TS_PROVENANCE: dict[str, str] = {
    "what": "the capture host's packet timestamp (pcapng enhanced packet block / pcap record); "
    "kept per IEX-TP segment as IexReader.receive_ts, session-relative nanoseconds",
    "resolution": "pcapng: the interface's if_tsresol (microsecond or nanosecond, read from the "
    "file); classic pcap: by magic number; a pcapng SIMPLE packet block carries no timestamp "
    "(then receive_ts is None and counted in capture_ts_missing)",
    "clock_source": "unknown and not recorded in the file: whether the stamp is hardware (NIC) or "
    "kernel software, and whether the host was NTP- or PTP-disciplined, is not stated by IEX",
    "offset_limits": "the host clock's offset to IEX's send clock is unknown, so receive - send "
    "contains an unidentified constant offset (and the 37 s TAI/UTC leap-second question for the "
    "capture host, already removed from the ingested send times); only DIFFERENCES within one "
    "capture (jitter, spikes, ordering) are interpretable; the ABSOLUTE feed latency, and the "
    "skew against a Nasdaq stamp from another host, are not identified",
    "use": "report feed latency and cross-venue skew with these qualifications; never as a "
    "trading advantage or a measured order-entry latency",
}


class FeedLatency:
    """Streaming summary of ``receive_ts - send_ts`` (ns) of the messages read.

    Count, min, max, sum, the number negative (impossible for a single clock:
    evidence of the unknown offset or of reordering) and a base-2 histogram of
    the non-negative values; exact quantiles are not kept (memory).  The
    values carry the offset limits of :data:`CAPTURE_TS_PROVENANCE`.
    """

    def __init__(self) -> None:
        self.n = 0
        self.negative = 0
        self.total = 0
        self.min: int | None = None
        self.max: int | None = None
        self.hist: dict[int, int] = {}  #: floor(log2(ns + 1)) -> count

    def add(self, delta_ns: int) -> None:
        self.n += 1
        self.total += delta_ns
        self.min = delta_ns if self.min is None else min(self.min, delta_ns)
        self.max = delta_ns if self.max is None else max(self.max, delta_ns)
        if delta_ns < 0:
            self.negative += 1
        else:
            b = (delta_ns + 1).bit_length() - 1
            self.hist[b] = self.hist.get(b, 0) + 1

    def summary(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "negative": self.negative,
            "mean_ns": None if not self.n else self.total / self.n,
            "min_ns": self.min,
            "max_ns": self.max,
            "log2_histogram": {str(k): v for k, v in sorted(self.hist.items())},
            "qualification": CAPTURE_TS_PROVENANCE["offset_limits"],
        }


class IexReader:
    """An IEX HIST capture -> ITCH message tuples for a fixed universe.

    Iterate it (once) and feed the messages to an ``Itch50Mapper``; pass the
    mapper as ``mapper`` so a clear can delete the live orders, a modify can
    read the resting order and the TOPS oracle can read the book.
    ``level_feed`` selects the DEEP (price level) translation; ``None`` picks
    it from the first segment's message protocol.  ``packets`` injects
    ``(capture_ts, payload)`` pairs instead of reading ``path`` (tests).
    """

    def __init__(
        self,
        path: str | Path,
        date: str,
        symbols: Iterable[str],
        *,
        limit_messages: int | None = None,
        mapper: Any = None,
        level_feed: bool | None = None,
        tops: TopsQuotes | None = None,
        packets: Iterable[tuple[int, bytes]] | None = None,
        round_lots: dict[str, int] | None = None,
    ) -> None:
        self.path = Path(path)
        #: symbol -> round lot (the paired Nasdaq dataset's ``lot_size``, v1.17 A4);
        #: 100 when unknown.  It sizes a synthesised directory row and is the
        #: oracle's rule: TOPS quotes the best level holding at least a round lot.
        self.round_lots = {k: max(1, int(v)) for k, v in (round_lots or {}).items()}
        self.date = date
        self.symbols = tuple(symbols)
        self._index = {s: i for i, s in enumerate(self.symbols)}
        self.limit_messages = limit_messages
        self.mapper = mapper
        self.level_feed = level_feed
        self._packets = packets
        self.midnight = session_midnight_ns(date)
        self.pcap = PcapStats()
        self.messages_read = 0
        self.limit_reached = False
        self.filtered = 0
        self.counts: dict[str, int] = {}
        self.skipped_unknown: dict[str, int] = {}
        self.protocol: int | None = None
        self.sessions: set[int] = set()
        self.counters = {
            "segments": 0,
            "heartbeats": 0,
            "other_protocol_segments": 0,
            "sequence_gaps": 0,
            "outside_session_date": 0,
            "directory_synthesised": 0,
            "test_securities_skipped": 0,
            "extended_hours_trades": 0,
            "odd_lot_trades": 0,
            "cross_trades": 0,
            "hidden_trades": 0,
            "modify_in_place": 0,
            "modify_replace": 0,
            "modify_unknown_order": 0,
            "clears": 0,
            "clear_deletes": 0,
            "executed_at_other_price": 0,
            "level_adds": 0,
            "level_increments": 0,
            "level_executed_qty": 0,
            "level_cancelled_qty": 0,
            "level_deletes": 0,
            "trades_unmatched_to_level": 0,
            "trades_matched_to_level": 0,
            "level_negative_size": 0,
            "odd_lot_top": 0,
            "capture_ts_missing": 0,
        }
        self.directory_seen: set[str] = set()
        self.oracle = TopsOracle(tops, self.symbols) if tops is not None else None
        # level feed state: per instrument, per side: price -> [[ref, size], ...] (FIFO)
        self._levels: list[tuple[dict[int, list[list[int]]], dict[int, list[list[int]]]]] = [
            ({}, {}) for _ in self.symbols
        ]
        self._pending: list[list[list[int]]] = [[] for _ in self.symbols]  # [price, left, id]
        self._pending_ts: list[int | None] = [None] * len(self.symbols)
        self._next_ref = LEVEL_ORDER_ID_BASE
        self._last_ts: list[int | None] = [None] * len(self.symbols)
        self._expected_seq: int | None = None
        #: v1.18 REL-04 (X-D): session-relative capture time (ns since midnight of
        #: ``date``) of the packet whose message was last yielded; ``None`` when the
        #: packet carries no timestamp.  A consumer building events reads it right
        #: after each yielded message (:data:`CAPTURE_TS_PROVENANCE` for its limits).
        self.receive_ts: int | None = None
        self.feed_latency = FeedLatency()

    @property
    def bytes_read(self) -> int:
        return self.pcap.bytes

    # -- framing --------------------------------------------------------

    def _payloads(self) -> Iterator[tuple[int, bytes]]:
        if self._packets is not None:
            yield from self._packets
            return
        with open_capture(self.path) as f:
            yield from pcap_packets(f, self.pcap)

    def _messages(self) -> Iterator[tuple]:
        for capture_ts, payload in self._payloads():
            # kept (v1.18, X-D) as ``receive_ts``: 0 = a simple packet block, no stamp
            self.receive_ts = capture_ts - self.midnight if capture_ts > 0 else None
            if capture_ts <= 0:
                self.counters["capture_ts_missing"] += 1
            seg = tp_segment(payload)
            if seg is None:
                continue
            if self.protocol is None:
                if seg.protocol not in (PROTOCOL_DEEP, PROTOCOL_DEEP_PLUS):
                    raise IngestError(
                        f"{self.path.name}: message protocol 0x{seg.protocol:04x} is "
                        f"{PROTOCOL_NAMES.get(seg.protocol, 'unknown')}, not DEEP or DEEP+"
                    )
                self.protocol = seg.protocol
                want_level = seg.protocol == PROTOCOL_DEEP
                if self.level_feed is None:
                    self.level_feed = want_level
                elif self.level_feed != want_level:
                    raise IngestError(
                        f"{self.path.name}: the capture is {PROTOCOL_NAMES[seg.protocol]}, "
                        f"not the format asked for"
                    )
            if seg.protocol != self.protocol:
                self.counters["other_protocol_segments"] += 1
                continue
            self.counters["segments"] += 1
            self.sessions.add(seg.session)
            if not seg.messages:
                self.counters["heartbeats"] += 1
                continue
            if self._expected_seq is not None and seg.first_sequence != self._expected_seq:
                self.counters["sequence_gaps"] += 1
            self._expected_seq = seg.first_sequence + len(seg.messages)
            for data in seg.messages:
                if self.limit_messages is not None and self.messages_read >= self.limit_messages:
                    self.limit_reached = True
                    return
                self.messages_read += 1
                m = decode_message(self.protocol, data)
                if type(m) is UnknownMessage:
                    self.skipped_unknown[m.type_code] = self.skipped_unknown.get(m.type_code, 0) + 1
                    continue
                code = chr(data[0])
                self.counts[code] = self.counts.get(code, 0) + 1
                sent = getattr(m, "ts", None)
                if self.receive_ts is not None and isinstance(sent, int):
                    self.feed_latency.add(self.receive_ts - (sent - self.midnight))
                yield m

    # -- helpers --------------------------------------------------------

    def _ts(self, epoch_ns: int) -> int | None:
        ts = epoch_ns - self.midnight
        if not 0 <= ts < _NS_PER_DAY:
            self.counters["outside_session_date"] += 1
            return None
        return ts

    def _directory(self, inst: int, ts: int, sym: str, lot: int, etp: bool, luld: int) -> tuple:
        self.directory_seen.add(sym)
        return StockDirectory(
            inst, ts, sym, " ", " ", max(1, lot), "N", " ", " ", "P", " ", " ",
            str(luld) if luld in (1, 2) else " ", "Y" if etp else "N", 0, "N",
        )  # fmt: skip

    def _ensure_directory(self, inst: int, ts: int, sym: str) -> Iterator[tuple]:
        if sym not in self.directory_seen:
            self.counters["directory_synthesised"] += 1
            yield self._directory(inst, ts, sym, self.round_lots.get(sym, 100), False, 0)

    def _cross_type(self, ts: int) -> str:
        return "O" if ts <= _OPEN_NS + _M else "C" if ts >= _CLOSE_NS else "H"

    def _trade_flags(self, flags: int) -> None:
        if flags & FLAG_EXTENDED_HOURS:
            self.counters["extended_hours_trades"] += 1
        if flags & FLAG_ODD_LOT:
            self.counters["odd_lot_trades"] += 1

    def _bbo(self, inst: int) -> tuple[int, int, int, int]:
        """The mapper's book as TOPS quotes it (v1.17 A4, measured on
        2025-11-28): per side, walk the levels from the best price and
        accumulate the displayed quantity; the quote is the FIRST price at
        which the cumulative quantity reaches the symbol's round lot, with
        that cumulative quantity as its size (odd lots resting at better
        prices are folded into it); (0, 0) when the whole side holds less
        than a round lot.  The rule matched every one of 113,807 TOPS quotes
        of AAPL / MSFT / QQQ in the first 15 M DEEP+ messages of that day,
        where the raw top matched 87.6 % and the best round-lot level 89.2 %.
        A top level that is itself below a round lot counts ``odd_lot_top``."""
        book = self.mapper._levels[inst]
        lot = self.round_lots.get(self.symbols[inst], 100)
        out = []
        for side in (0, 1):
            totals = book.totals[side]
            best = book.best(side)
            if best is None:
                out += [0, 0]
                continue
            if totals[best] < lot:
                self.counters["odd_lot_top"] += 1
            price, cum = 0, 0
            for level in sorted(totals, reverse=side == 0):
                cum += totals[level]
                if cum >= lot:
                    price = level
                    break
            out += [price, cum] if price else [0, 0]
        return (out[0], out[1], out[2], out[3])

    def _close_ts(self, inst: int, ts: int) -> Iterator[tuple]:
        """Called before the first message of ``inst`` at a new timestamp:
        flush a dangling level-feed event and check the oracle at the old."""
        prev = self._last_ts[inst]
        if prev is not None and prev != ts:
            if self.level_feed:
                yield from self._flush_pending(inst)
            if self.oracle is not None and self.mapper is not None:
                self.oracle.check(self.symbols[inst], prev, self._bbo(inst))
        self._last_ts[inst] = ts

    # -- DEEP+ (orders) ---------------------------------------------------

    def _order_message(self, m: tuple, inst: int, ts: int, sym: str) -> Iterator[tuple]:
        kind = type(m)
        if kind is IexAddOrder:
            yield AddOrder(inst, ts, m.order_id, "B" if m.side == 0 else "S", m.size, sym,
                           m.price, "")  # fmt: skip
        elif kind is IexModifyOrder:
            order = self.mapper._orders.get(m.order_id) if self.mapper is not None else None
            if order is None:
                self.counters["modify_unknown_order"] += 1
                yield OrderReplace(inst, ts, m.order_id, m.order_id, m.size, m.price)
            elif (m.flags & 0x01) and order[2] == m.price and 0 < m.size < order[3]:
                self.counters["modify_in_place"] += 1
                yield OrderCancel(inst, ts, m.order_id, order[3] - m.size)
            else:
                self.counters["modify_replace"] += 1
                yield OrderReplace(inst, ts, m.order_id, m.order_id, m.size, m.price)
        elif kind is IexDeleteOrder:
            yield OrderDelete(inst, ts, m.order_id)
        elif kind is IexExecuted:
            self._trade_flags(m.flags)
            order = self.mapper._orders.get(m.order_id) if self.mapper is not None else None
            if order is not None and order[2] != m.price and m.price > 0:
                self.counters["executed_at_other_price"] += 1
                yield OrderExecutedWithPrice(inst, ts, m.order_id, m.size, m.trade_id, "Y", m.price)
            else:
                yield OrderExecuted(inst, ts, m.order_id, m.size, m.trade_id)
        elif kind is IexTrade:
            self._trade_flags(m.flags)
            if m.flags & FLAG_SINGLE_PRICE_CROSS:
                self.counters["cross_trades"] += 1
                yield CrossTrade(inst, ts, m.size, sym, m.price, m.trade_id, self._cross_type(ts))
            else:
                self.counters["hidden_trades"] += 1
                yield Trade(inst, ts, 0, "B", m.size, sym, m.price, m.trade_id)
        elif kind is IexClearBook:
            self.counters["clears"] += 1
            if self.mapper is not None:
                live = [r for r, o in self.mapper._orders.items() if o[0] == inst]
                self.counters["clear_deletes"] += len(live)
                for ref in live:
                    yield OrderDelete(inst, ts, ref)

    # -- DEEP (price levels) ------------------------------------------------

    def _flush_pending(self, inst: int) -> Iterator[tuple]:
        """Trade reports no level update accounted for: non-displayed prints."""
        sym = self.symbols[inst]
        ts = self._pending_ts[inst]
        for price, left, trade_id in self._pending[inst]:
            if left > 0 and ts is not None:
                self.counters["trades_unmatched_to_level"] += 1
                self.counters["hidden_trades"] += 1
                yield Trade(inst, ts, 0, "B", left, sym, price, trade_id)
        self._pending[inst] = []
        self._pending_ts[inst] = None

    def _level_message(self, m: tuple, inst: int, ts: int, sym: str) -> Iterator[tuple]:
        kind = type(m)
        if kind is IexTrade:
            self._trade_flags(m.flags)
            if m.flags & FLAG_SINGLE_PRICE_CROSS:
                self.counters["cross_trades"] += 1
                yield CrossTrade(inst, ts, m.size, sym, m.price, m.trade_id, self._cross_type(ts))
                return
            if self._pending_ts[inst] not in (None, ts):
                yield from self._flush_pending(inst)
            self._pending[inst].append([m.price, m.size, m.trade_id])
            self._pending_ts[inst] = ts
            return
        if kind is not IexPriceLevel:
            return
        if self._pending_ts[inst] not in (None, ts):
            yield from self._flush_pending(inst)
        side_levels = self._levels[inst][m.side]
        orders = side_levels.setdefault(m.price, [])
        current = sum(o[1] for o in orders)
        side_code = "B" if m.side == 0 else "S"
        if m.size > current:
            delta = m.size - current
            self._next_ref += 1
            orders.append([self._next_ref, delta])
            self.counters["level_increments" if current else "level_adds"] += 1
            yield AddOrder(inst, ts, self._next_ref, side_code, delta, sym, m.price, "")
        elif m.size < current:
            need = current - m.size
            # executions first, from the FRONT, against pending trades at this price
            for trade in self._pending[inst]:
                if need <= 0:
                    break
                if trade[0] != m.price or trade[1] <= 0:
                    continue
                while need > 0 and trade[1] > 0 and orders:
                    front = orders[0]
                    q = min(need, trade[1], front[1])
                    yield OrderExecuted(inst, ts, front[0], q, trade[2])
                    self.counters["level_executed_qty"] += q
                    front[1] -= q
                    trade[1] -= q
                    need -= q
                    if front[1] == 0:
                        orders.pop(0)
                if trade[1] == 0:
                    self.counters["trades_matched_to_level"] += 1
            # the rest are cancels, from the BACK
            while need > 0 and orders:
                back = orders[-1]
                q = min(need, back[1])
                if q == back[1]:
                    yield OrderDelete(inst, ts, back[0])
                    orders.pop()
                else:
                    yield OrderCancel(inst, ts, back[0], q)
                    back[1] -= q
                self.counters["level_cancelled_qty"] += q
                need -= q
            if need > 0:
                self.counters["level_negative_size"] += 1
        if m.size == 0:
            self.counters["level_deletes"] += 1
            side_levels.pop(m.price, None)
        if m.flags & FLAG_EVENT_COMPLETE:
            yield from self._flush_pending(inst)

    # -- iteration ------------------------------------------------------

    def __iter__(self) -> Iterator[tuple]:
        for m in self._messages():
            kind = type(m)
            if kind is IexSystemEvent:
                ts = self._ts(m.ts)
                code = _SYSTEM_CODES.get(m.code)
                if ts is not None and code is not None:
                    yield SystemEvent(ts, code)
                continue
            if kind in (IexOfficialPrice, IexAuction, IexSymbolMessage):
                continue
            sym = m.symbol
            inst = self._index.get(sym)
            if inst is None:
                self.filtered += 1
                continue
            ts = self._ts(m.ts)
            if ts is None:
                continue
            if kind is IexDirectory:
                if m.flags & FLAG_TEST_SECURITY:
                    self.counters["test_securities_skipped"] += 1
                    continue
                yield self._directory(
                    inst, ts, sym, m.round_lot, bool(m.flags & FLAG_ETP), m.luld_tier
                )
                continue
            yield from self._ensure_directory(inst, ts, sym)
            yield from self._close_ts(inst, ts)
            if kind is IexTradingStatus:
                state = _TRADING_STATES.get(m.status)
                if state is not None:
                    yield TradingAction(inst, ts, sym, state, m.reason)
                continue
            if kind is IexOperationalHalt:
                if m.status in ("O", "N"):
                    yield OperationalHalt(inst, ts, sym, "Q", "H" if m.status == "O" else "T")
                continue
            if kind is IexTradeBreak:
                yield BrokenTrade(inst, ts, m.trade_id)
                continue
            if self.level_feed:
                yield from self._level_message(m, inst, ts, sym)
            else:
                yield from self._order_message(m, inst, ts, sym)
        for inst in range(len(self.symbols)):
            if self.level_feed:
                yield from self._flush_pending(inst)
            prev = self._last_ts[inst]
            if prev is not None and self.oracle is not None and self.mapper is not None:
                self.oracle.check(self.symbols[inst], prev, self._bbo(inst))
        if self.protocol is None and self.messages_read == 0:
            raise IngestError(f"{self.path.name}: no IEX-TP DEEP / DEEP+ segment found")

    def summary(self) -> dict[str, Any]:
        """The manifest's ``iex`` block."""
        return {
            "protocol": None if self.protocol is None else PROTOCOL_NAMES[self.protocol],
            "message_protocol_id": self.protocol,
            "level_feed": bool(self.level_feed),
            "queue_positions": (
                "modelled: synthetic orders per price level (module docs)"
                if self.level_feed
                else "observed: displayed orders order by order"
            ),
            "tp_sessions": sorted(self.sessions),
            "pcap": self.pcap.as_dict(),
            "tops_oracle": self.oracle is not None,
            **self.counters,
        }
