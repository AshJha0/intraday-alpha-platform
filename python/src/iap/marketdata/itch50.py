"""Nasdaq TotalView-ITCH 5.0 reader and mapping to canonical events.

Two layers (docs/REAL_DATA.md is the user guide and holds the mapping table):

* :class:`Itch50Reader` — a streaming parser for the historical file format:
  a sequence of ``2-byte big-endian length | message`` records, optionally
  gzip-compressed (detected by magic, not by file name).  It decodes the
  message types the platform maps (``S R H Y L A F E C X D U P Q B``),
  length-checks and counts ``I`` (NOII), and skips every other type by its
  length prefix with a per-type counter.  With a symbol filter the stock
  locate codes of the wanted symbols are learnt from the day's ``R``
  messages and every other message is skipped after reading three bytes, so
  a multi-GB file is reduced to a universe in one pass with memory bounded
  by the read buffer.  Malformed or truncated input raises
  :class:`~iap.marketdata.feederrors.FeedFormatError` /
  :class:`~iap.marketdata.feederrors.FeedTruncatedError` with the byte
  offset (of the decompressed stream).

* :class:`Itch50Mapper` — turns the messages into *proto events*
  (:data:`ProtoEvent`): canonical event type, side, quantity and ids, with
  the price still in the feed's integer 1/10000-dollar units and no
  sequence number.  ``iap.marketdata.ingest`` converts prices to ticks,
  numbers the streams and writes the platform's raw files.  The mapper's
  only state is the set of live orders of the wanted symbols.

No float touches a price or a timestamp: prices are the feed's integers,
timestamps are ``midnight(America/New_York, date) + ns-since-midnight``.
"""

from __future__ import annotations

import datetime as _dt
import gzip
import struct
import zlib
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import BinaryIO, NamedTuple
from zoneinfo import ZoneInfo

from iap.core.events import SYNTHETIC_ID_BASE, EventType, SessionStatus, Side
from iap.marketdata.feederrors import FeedFormatError, FeedTruncatedError

#: Feed price unit: 1/10000 dollar (ITCH 5.0 ``Price (4)``).
PRICE_SCALE = 10_000

#: IANA zone ITCH "nanoseconds since midnight" is measured in.
ITCH_TIMEZONE = "America/New_York"

#: From this session date on the ``P`` message's buy/sell indicator is always
#: ``B`` (Nasdaq notice, effective 2014-07-14), so it carries no information.
P_SIDE_UNINFORMATIVE_FROM = _dt.date(2014, 7, 14)

#: Substitute trade ids (a zero match number cannot be a canonical trade_id).
SYNTHETIC_TRADE_ID_BASE = 1 << 62

_NS_PER_DAY = 86_400 * 1_000_000_000

# --------------------------------------------------------------------- layouts
# Every struct starts at the message-type byte ("x") and continues with the
# common header: stock locate u16, tracking number u16, timestamp u48 (read
# as u16 high + u32 low).
_S = struct.Struct(">xHHHIc")
_R = struct.Struct(">xHHHI8sccIcc2scccccIc")
_H = struct.Struct(">xHHHI8scc4s")
_Y = struct.Struct(">xHHHI8sc")
_L = struct.Struct(">xHHHI4s8sccc")
_A = struct.Struct(">xHHHIQcI8sI")
_F = struct.Struct(">xHHHIQcI8sI4s")
_E = struct.Struct(">xHHHIQIQ")
_C = struct.Struct(">xHHHIQIQcI")
_X = struct.Struct(">xHHHIQI")
_D = struct.Struct(">xHHHIQ")
_U = struct.Struct(">xHHHIQQII")
_P = struct.Struct(">xHHHIQcI8sIQ")
_Q = struct.Struct(">xHHHIQ8sIQc")
_B = struct.Struct(">xHHHIQ")

#: Pinned message lengths (type byte included), ITCH 5.0 specification.
MESSAGE_LENGTHS: dict[str, int] = {
    "S": 12,
    "R": 39,
    "H": 25,
    "Y": 20,
    "L": 26,
    "A": 36,
    "F": 40,
    "E": 31,
    "C": 36,
    "X": 23,
    "D": 19,
    "U": 35,
    "P": 44,
    "Q": 40,
    "B": 19,
    "I": 50,
}

_STRUCTS = {
    "S": _S,
    "R": _R,
    "H": _H,
    "Y": _Y,
    "L": _L,
    "A": _A,
    "F": _F,
    "E": _E,
    "C": _C,
    "X": _X,
    "D": _D,
    "U": _U,
    "P": _P,
    "Q": _Q,
    "B": _B,
}
for _code, _st in _STRUCTS.items():
    if _st.size != MESSAGE_LENGTHS[_code]:
        raise AssertionError(f"ITCH 5.0 layout {_code}: {_st.size} != {MESSAGE_LENGTHS[_code]}")

#: Message types the reader decodes or length-checks.
SUPPORTED_TYPES = tuple(MESSAGE_LENGTHS)


# -------------------------------------------------------------------- messages


class SystemEvent(NamedTuple):
    """``S`` — event_code O/S/Q/M/E/C (start of messages, system hours,
    market hours, end of market hours, system hours, messages)."""

    ts: int
    event_code: str


class StockDirectory(NamedTuple):
    """``R`` — the day's attributes of one security (the security-master row)."""

    locate: int
    ts: int
    stock: str
    market_category: str
    financial_status: str
    round_lot_size: int
    round_lots_only: str
    issue_classification: str
    issue_subtype: str
    authenticity: str
    short_sale_threshold: str
    ipo_flag: str
    luld_tier: str
    etp_flag: str
    etp_leverage_factor: int
    inverse_indicator: str


class TradingAction(NamedTuple):
    """``H`` — trading_state H (halted) / P (paused) / Q (quotation only) / T."""

    locate: int
    ts: int
    stock: str
    trading_state: str
    reason: str


class RegShoRestriction(NamedTuple):
    """``Y`` — Reg SHO short-sale price-test restriction indicator."""

    locate: int
    ts: int
    stock: str
    action: str


class MarketParticipantPosition(NamedTuple):
    """``L`` — market participant position."""

    locate: int
    ts: int
    mpid: str
    stock: str
    primary_market_maker: str
    market_maker_mode: str
    participant_state: str


class AddOrder(NamedTuple):
    """``A`` / ``F`` — a displayed order entered the book (``mpid`` is ``""``
    for the unattributed ``A``)."""

    locate: int
    ts: int
    order_ref: int
    side: str
    shares: int
    stock: str
    price: int
    mpid: str


class OrderExecuted(NamedTuple):
    """``E`` — a resting displayed order was executed at its own price."""

    locate: int
    ts: int
    order_ref: int
    shares: int
    match_number: int


class OrderExecutedWithPrice(NamedTuple):
    """``C`` — executed at ``price``; ``printable == "N"`` executions are
    already inside a cross print (``Q``) and must not be counted twice."""

    locate: int
    ts: int
    order_ref: int
    shares: int
    match_number: int
    printable: str
    price: int


class OrderCancel(NamedTuple):
    """``X`` — ``shares`` were removed from a resting order (partial cancel)."""

    locate: int
    ts: int
    order_ref: int
    shares: int


class OrderDelete(NamedTuple):
    """``D`` — the resting order was removed."""

    locate: int
    ts: int
    order_ref: int


class OrderReplace(NamedTuple):
    """``U`` — ``orig_ref`` was cancelled and replaced by ``new_ref``."""

    locate: int
    ts: int
    orig_ref: int
    new_ref: int
    shares: int
    price: int


class Trade(NamedTuple):
    """``P`` — execution of a NON-displayed order (never in the visible book)."""

    locate: int
    ts: int
    order_ref: int
    side: str
    shares: int
    stock: str
    price: int
    match_number: int


class CrossTrade(NamedTuple):
    """``Q`` — bulk print of an opening / closing / halt / IPO cross."""

    locate: int
    ts: int
    shares: int
    stock: str
    price: int
    match_number: int
    cross_type: str


class BrokenTrade(NamedTuple):
    """``B`` — an earlier execution (``match_number``) was broken."""

    locate: int
    ts: int
    match_number: int


def _txt(raw: bytes) -> str:
    return raw.decode("ascii", "replace").rstrip(" ")


def pad_symbol(symbol: str) -> bytes:
    """The 8-byte right-space-padded ASCII form of a symbol."""
    raw = symbol.encode("ascii")
    if not raw or len(raw) > 8:
        raise ValueError(f"ITCH symbols are 1..8 ASCII characters, got {symbol!r}")
    return raw.ljust(8, b" ")


# ---------------------------------------------------------------------- reader

_GZIP_MAGIC = b"\x1f\x8b"


def open_feed(path: str | Path) -> BinaryIO:
    """Open a feed file for binary reading; gzip is detected by its magic."""
    path = Path(path)
    with open(path, "rb") as probe:
        magic = probe.read(2)
    if magic == _GZIP_MAGIC:
        return gzip.open(path, "rb")  # type: ignore[return-value]
    return open(path, "rb")


class Itch50Reader:
    """Streaming ITCH 5.0 file parser.

    ``symbols`` (optional) restricts the output to those stocks; ``R``
    messages are always inspected so the locate codes are learnt, ``S``
    messages are always yielded.  ``limit_messages`` stops after that many
    messages of ANY type were read (a smoke run over the head of a big
    file).  Iterate once; the counters are final when iteration ends:

    * ``counts``        — messages read per supported type (before filtering)
    * ``skipped_unknown`` — messages skipped by length, per unknown type
    * ``filtered``      — supported messages of other symbols, not decoded
    * ``messages_read`` / ``bytes_read`` — totals over the decompressed stream
    * ``directory``     — ``{symbol: StockDirectory}`` of the yielded stocks
    """

    def __init__(
        self,
        path: str | Path,
        symbols: Iterable[str] | None = None,
        limit_messages: int | None = None,
        chunk_size: int = 1 << 20,
    ) -> None:
        if limit_messages is not None and limit_messages < 0:
            raise ValueError("limit_messages must be >= 0")
        if chunk_size < 64:
            raise ValueError("chunk_size must be >= 64 bytes")
        self.path = Path(path)
        self.symbols = None if symbols is None else tuple(symbols)
        self._wanted_raw = (
            None if self.symbols is None else frozenset(pad_symbol(s) for s in self.symbols)
        )
        self.limit_messages = limit_messages
        self.chunk_size = chunk_size
        self.counts: dict[str, int] = {t: 0 for t in SUPPORTED_TYPES}
        self.skipped_unknown: dict[str, int] = {}
        self.filtered = 0
        self.messages_read = 0
        self.bytes_read = 0
        self.directory: dict[str, StockDirectory] = {}
        self.limit_reached = False

    def _read(self, f: BinaryIO, offset: int) -> bytes:
        try:
            return f.read(self.chunk_size)
        except EOFError as exc:
            raise FeedTruncatedError(
                f"{self.path.name}: compressed stream ends before its end-of-stream marker",
                offset=offset,
            ) from exc
        except (gzip.BadGzipFile, zlib.error) as exc:
            raise FeedFormatError(
                f"{self.path.name}: corrupt gzip stream ({exc})", offset=offset
            ) from exc

    def __iter__(self) -> Iterator[tuple]:
        with open_feed(self.path) as f:
            yield from self._messages(f)

    def _messages(self, f: BinaryIO) -> Iterator[tuple]:
        name = self.path.name
        wanted_raw = self._wanted_raw
        filtering = wanted_raw is not None
        wanted: set[int] = set()
        counts = [0] * 256
        known = [0] * 256
        for code, length in MESSAGE_LENGTHS.items():
            known[ord(code)] = length
        limit = self.limit_messages
        total = 0
        filtered = 0
        buf = b""
        pos = 0
        base = 0  # absolute offset of buf[0] in the decompressed stream
        t_s, t_r, t_i = ord("S"), ord("R"), ord("I")
        try:
            while True:
                if limit is not None and total >= limit:
                    self.limit_reached = True
                    break
                n = len(buf)
                if n - pos < 2 or n - pos < 2 + ((buf[pos] << 8) | buf[pos + 1]):
                    chunk = self._read(f, base + n)
                    if not chunk:
                        if n == pos:
                            break
                        if n - pos < 2:
                            raise FeedTruncatedError(
                                f"{name}: file ends inside a length prefix", offset=base + pos
                            )
                        need = (buf[pos] << 8) | buf[pos + 1]
                        raise FeedTruncatedError(
                            f"{name}: message of declared length {need} is cut off after "
                            f"{n - pos - 2} bytes",
                            offset=base + pos,
                        )
                    buf = buf[pos:] + chunk
                    base += pos
                    pos = 0
                    continue
                length = (buf[pos] << 8) | buf[pos + 1]
                if length == 0:
                    raise FeedFormatError(f"{name}: zero-length message", offset=base + pos)
                start = pos + 2
                mtype = buf[start]
                pos = start + length
                total += 1
                counts[mtype] += 1
                expect = known[mtype]
                if expect == 0:
                    continue  # unknown type: skipped by its length prefix, counted
                if length != expect:
                    raise FeedFormatError(
                        f"{name}: message type {chr(mtype)!r} has length {length}, "
                        f"ITCH 5.0 says {expect}",
                        offset=base + start - 2,
                    )
                if mtype == t_i:
                    continue  # NOII: length-checked and counted, not mapped
                if mtype == t_s:
                    _, _, hi, lo, code = _S.unpack_from(buf, start)
                    yield SystemEvent((hi << 32) | lo, code.decode("ascii", "replace"))
                    continue
                if mtype == t_r:
                    if filtering and buf[start + 11 : start + 19] not in wanted_raw:
                        filtered += 1
                        continue
                    msg = self._directory(buf, start)
                    wanted.add(msg.locate)
                    self.directory[msg.stock] = msg
                    yield msg
                    continue
                if filtering and ((buf[start + 1] << 8) | buf[start + 2]) not in wanted:
                    filtered += 1
                    continue
                yield _DECODERS[mtype](buf, start)
        finally:
            self.messages_read = total
            self.bytes_read = base + pos
            self.filtered = filtered
            for code in range(256):
                if not counts[code]:
                    continue
                label = chr(code) if 32 <= code < 127 else f"0x{code:02x}"
                if known[code]:
                    self.counts[label] = counts[code]
                else:
                    self.skipped_unknown[label] = counts[code]

    @staticmethod
    def _directory(buf: bytes, start: int) -> StockDirectory:
        (
            loc,
            _,
            hi,
            lo,
            stock,
            category,
            fin,
            lot,
            lots_only,
            issue_class,
            issue_sub,
            auth,
            ssr,
            ipo,
            luld,
            etp,
            leverage,
            inverse,
        ) = _R.unpack_from(buf, start)
        return StockDirectory(
            loc,
            (hi << 32) | lo,
            _txt(stock),
            _txt(category),
            _txt(fin),
            lot,
            _txt(lots_only),
            _txt(issue_class),
            _txt(issue_sub),
            _txt(auth),
            _txt(ssr),
            _txt(ipo),
            _txt(luld),
            _txt(etp),
            leverage,
            _txt(inverse),
        )


def _dec_h(buf: bytes, start: int) -> TradingAction:
    loc, _, hi, lo, stock, state, _reserved, reason = _H.unpack_from(buf, start)
    return TradingAction(loc, (hi << 32) | lo, _txt(stock), _txt(state), _txt(reason))


def _dec_y(buf: bytes, start: int) -> RegShoRestriction:
    loc, _, hi, lo, stock, action = _Y.unpack_from(buf, start)
    return RegShoRestriction(loc, (hi << 32) | lo, _txt(stock), _txt(action))


def _dec_l(buf: bytes, start: int) -> MarketParticipantPosition:
    loc, _, hi, lo, mpid, stock, primary, mode, state = _L.unpack_from(buf, start)
    return MarketParticipantPosition(
        loc, (hi << 32) | lo, _txt(mpid), _txt(stock), _txt(primary), _txt(mode), _txt(state)
    )


def _dec_a(buf: bytes, start: int) -> AddOrder:
    loc, _, hi, lo, ref, side, shares, stock, price = _A.unpack_from(buf, start)
    return AddOrder(loc, (hi << 32) | lo, ref, side.decode("ascii"), shares, _txt(stock), price, "")


def _dec_f(buf: bytes, start: int) -> AddOrder:
    loc, _, hi, lo, ref, side, shares, stock, price, mpid = _F.unpack_from(buf, start)
    return AddOrder(
        loc, (hi << 32) | lo, ref, side.decode("ascii"), shares, _txt(stock), price, _txt(mpid)
    )


def _dec_e(buf: bytes, start: int) -> OrderExecuted:
    loc, _, hi, lo, ref, shares, match = _E.unpack_from(buf, start)
    return OrderExecuted(loc, (hi << 32) | lo, ref, shares, match)


def _dec_c(buf: bytes, start: int) -> OrderExecutedWithPrice:
    loc, _, hi, lo, ref, shares, match, printable, price = _C.unpack_from(buf, start)
    return OrderExecutedWithPrice(
        loc, (hi << 32) | lo, ref, shares, match, printable.decode("ascii", "replace"), price
    )


def _dec_x(buf: bytes, start: int) -> OrderCancel:
    loc, _, hi, lo, ref, shares = _X.unpack_from(buf, start)
    return OrderCancel(loc, (hi << 32) | lo, ref, shares)


def _dec_d(buf: bytes, start: int) -> OrderDelete:
    loc, _, hi, lo, ref = _D.unpack_from(buf, start)
    return OrderDelete(loc, (hi << 32) | lo, ref)


def _dec_u(buf: bytes, start: int) -> OrderReplace:
    loc, _, hi, lo, orig, new, shares, price = _U.unpack_from(buf, start)
    return OrderReplace(loc, (hi << 32) | lo, orig, new, shares, price)


def _dec_p(buf: bytes, start: int) -> Trade:
    loc, _, hi, lo, ref, side, shares, stock, price, match = _P.unpack_from(buf, start)
    return Trade(
        loc,
        (hi << 32) | lo,
        ref,
        side.decode("ascii", "replace"),
        shares,
        _txt(stock),
        price,
        match,
    )


def _dec_q(buf: bytes, start: int) -> CrossTrade:
    loc, _, hi, lo, shares, stock, price, match, cross_type = _Q.unpack_from(buf, start)
    return CrossTrade(
        loc,
        (hi << 32) | lo,
        shares,
        _txt(stock),
        price,
        match,
        cross_type.decode("ascii", "replace"),
    )


def _dec_b(buf: bytes, start: int) -> BrokenTrade:
    loc, _, hi, lo, match = _B.unpack_from(buf, start)
    return BrokenTrade(loc, (hi << 32) | lo, match)


_DECODERS = {
    ord("H"): _dec_h,
    ord("Y"): _dec_y,
    ord("L"): _dec_l,
    ord("A"): _dec_a,
    ord("F"): _dec_f,
    ord("E"): _dec_e,
    ord("C"): _dec_c,
    ord("X"): _dec_x,
    ord("D"): _dec_d,
    ord("U"): _dec_u,
    ord("P"): _dec_p,
    ord("Q"): _dec_q,
    ord("B"): _dec_b,
}


# ---------------------------------------------------------------------- mapper

#: ``(instrument_index, exchange_ts_ns, event_type, side, price_e4, qty,
#: order_id, trade_id)`` — a canonical event before tick conversion and
#: sequencing.  ``price_e4`` is in 1/10000 dollars.
ProtoEvent = tuple[int, int, int, int, int, int, int, int]

_ADD = int(EventType.ADD)
_MODIFY = int(EventType.MODIFY)
_CANCEL = int(EventType.CANCEL)
_EXECUTE = int(EventType.EXECUTE)
_TRADE = int(EventType.TRADE)
_STATUS = int(EventType.STATUS)
_BID = int(Side.BID)
_ASK = int(Side.ASK)

# session phases, driven by the S (system event) messages
_PRE_SYSTEM, _PRE_MARKET, _REGULAR, _POST_MARKET, _CLOSED = range(5)
_PHASE_OF_EVENT = {"S": _PRE_MARKET, "Q": _REGULAR, "M": _POST_MARKET, "E": _CLOSED}

#: Counters of every mapping decision that is not a plain 1:1 translation.
MAPPING_COUNTERS = (
    "unknown_order_refs",
    "duplicate_order_refs",
    "invalid_orders",
    "overfilled_executions",
    "overcancelled_orders",
    "nonprintable_executions",
    "hidden_trades",
    "hidden_trades_signed_by_tick_rule",
    "cross_trades",
    "broken_trades",
    "synthetic_trade_ids",
    "outside_session_dropped",
    "reg_sho_messages",
    "market_participant_messages",
    "unknown_trading_states",
)


def session_midnight_ns(date: str, timezone: str = ITCH_TIMEZONE) -> int:
    """UTC epoch nanoseconds of local midnight of ``date`` (YYYY-MM-DD)."""
    day = _dt.date.fromisoformat(date)
    local = _dt.datetime(day.year, day.month, day.day, tzinfo=ZoneInfo(timezone))
    return int(local.timestamp()) * 1_000_000_000


class Itch50Mapper:
    """ITCH 5.0 messages -> proto events for a fixed symbol universe.

    ``symbols`` fixes the instrument index of each symbol (its position).
    ``extended_hours=False`` (default) treats the regular session as the
    research session: before the start of market hours the book builds
    under ``STATUS AUCTION``, order flow after the end of market hours is
    dropped and counted.  ``extended_hours=True`` maps system hours to
    ``TRADING`` instead.  Mapping decisions: docs/REAL_DATA.md §4.
    """

    def __init__(self, date: str, symbols: Iterable[str], extended_hours: bool = False) -> None:
        self.date = date
        self.symbols = tuple(symbols)
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("duplicate symbols in the universe")
        self._index = {s: i for i, s in enumerate(self.symbols)}
        self.extended_hours = bool(extended_hours)
        self._midnight = session_midnight_ns(date)
        self._p_side_informative = _dt.date.fromisoformat(date) < P_SIDE_UNINFORMATIVE_FROM
        self._locate: dict[int, int] = {}
        #: order_ref -> [instrument index, side, price_e4, remaining shares]
        self._orders: dict[int, list[int]] = {}
        self._phase = _PRE_SYSTEM
        n = len(self.symbols)
        self._state = ["T"] * n
        self._emitted_status: list[int | None] = [None] * n
        self._last_price = [0] * n
        self._last_sign = [-1] * n
        self._synthetic_trade = 0
        self.counters: dict[str, int] = {k: 0 for k in MAPPING_COUNTERS}
        self.directory: dict[str, StockDirectory] = {}
        self.crosses: dict[str, list[dict]] = {s: [] for s in self.symbols}
        self.broken: list[int] = []
        self.system_events: list[dict] = []

    # -- helpers --------------------------------------------------------

    @property
    def live_orders(self) -> int:
        """Orders currently tracked (the mapper's whole state)."""
        return len(self._orders)

    def _ts(self, ns_since_midnight: int) -> int:
        if ns_since_midnight >= _NS_PER_DAY:
            raise FeedFormatError(
                f"timestamp {ns_since_midnight} ns is not inside one day (session {self.date})"
            )
        return self._midnight + ns_since_midnight

    def _dropping(self) -> bool:
        return self._phase >= (_CLOSED if self.extended_hours else _POST_MARKET)

    def _effective_status(self, inst: int) -> int:
        if self._dropping():
            return int(SessionStatus.CLOSE)
        state = self._state[inst]
        if state in ("H", "P"):
            return int(SessionStatus.HALT)
        if state == "Q":
            return int(SessionStatus.AUCTION)
        first_trading = _PRE_MARKET if self.extended_hours else _REGULAR
        if self._phase < first_trading:
            return int(SessionStatus.AUCTION)
        return int(SessionStatus.TRADING)

    def _sync_status(self, inst: int, ts: int) -> ProtoEvent | None:
        status = self._effective_status(inst)
        if self._emitted_status[inst] == status:
            return None
        self._emitted_status[inst] = status
        return (inst, ts, _STATUS, 0, 0, status, 0, 0)

    def _trade_id(self, match_number: int) -> int:
        if match_number:
            return match_number
        self._synthetic_trade += 1
        self.counters["synthetic_trade_ids"] += 1
        return SYNTHETIC_TRADE_ID_BASE + self._synthetic_trade

    def _print(self, inst: int, price: int, sign: int) -> None:
        self._last_price[inst] = price
        self._last_sign[inst] = sign

    # -- the mapping ----------------------------------------------------

    def events(self, messages: Iterable[tuple]) -> Iterator[ProtoEvent]:
        """Proto events of ``messages`` (an :class:`Itch50Reader` or any
        iterable of the message tuples of this module), in file order."""
        orders = self._orders
        locate = self._locate
        counters = self.counters
        for m in messages:
            kind = type(m)
            if kind is SystemEvent:
                yield from self._system_event(m)
                continue
            if kind is StockDirectory:
                idx = self._index.get(m.stock)
                if idx is not None:
                    locate[m.locate] = idx
                    self.directory[m.stock] = m
                continue
            inst = locate.get(m.locate)
            if inst is None:
                continue  # a symbol outside the universe (unfiltered reader)
            if kind is CrossTrade:
                counters["cross_trades"] += 1
                self.crosses[self.symbols[inst]].append(
                    {
                        "cross_type": m.cross_type,
                        "price_e4": m.price,
                        "shares": m.shares,
                        "exchange_ts": self._ts(m.ts),
                        "match_number": m.match_number,
                    }
                )
                continue
            if kind is BrokenTrade:
                counters["broken_trades"] += 1
                self.broken.append(m.match_number)
                continue
            if kind is RegShoRestriction:
                counters["reg_sho_messages"] += 1
                continue
            if kind is MarketParticipantPosition:
                counters["market_participant_messages"] += 1
                continue
            ts = self._ts(m.ts)
            if kind is TradingAction:
                if m.trading_state not in ("H", "P", "Q", "T"):
                    counters["unknown_trading_states"] += 1
                    continue
                self._state[inst] = m.trading_state
                if not self._dropping():
                    ev = self._sync_status(inst, ts)
                    if ev is not None:
                        yield ev
                continue
            if self._dropping():
                counters["outside_session_dropped"] += 1
                continue
            if self._emitted_status[inst] is None:
                ev = self._sync_status(inst, ts)
                if ev is not None:
                    yield ev

            if kind is AddOrder:
                ref = m.order_ref
                if m.shares <= 0 or m.price <= 0 or not (0 < ref < SYNTHETIC_ID_BASE):
                    counters["invalid_orders"] += 1
                    continue
                if m.side == "B":
                    side = _BID
                elif m.side == "S":
                    side = _ASK
                else:
                    counters["invalid_orders"] += 1
                    continue
                if ref in orders:
                    counters["duplicate_order_refs"] += 1
                    continue
                orders[ref] = [inst, side, m.price, m.shares]
                yield (inst, ts, _ADD, side, m.price, m.shares, ref, 0)
            elif kind is OrderDelete:
                order = orders.pop(m.order_ref, None)
                if order is None:
                    counters["unknown_order_refs"] += 1
                    continue
                yield (inst, ts, _CANCEL, order[1], order[2], 0, m.order_ref, 0)
            elif kind is OrderExecuted or kind is OrderExecutedWithPrice:
                order = orders.get(m.order_ref)
                if order is None:
                    counters["unknown_order_refs"] += 1
                    continue
                qty = m.shares
                if qty <= 0:
                    counters["invalid_orders"] += 1
                    continue
                if qty > order[3]:
                    counters["overfilled_executions"] += 1
                    qty = order[3]
                order[3] -= qty
                if order[3] == 0:
                    del orders[m.order_ref]
                side, price = order[1], order[2]
                yield (inst, ts, _EXECUTE, side, price, qty, m.order_ref, m.match_number)
                aggressor = _ASK if side == _BID else _BID
                if kind is OrderExecuted:
                    self._print(inst, price, aggressor)
                    yield (
                        inst,
                        ts,
                        _TRADE,
                        aggressor,
                        price,
                        qty,
                        0,
                        self._trade_id(m.match_number),
                    )
                elif m.printable == "N" or m.price <= 0:
                    counters["nonprintable_executions"] += 1
                else:
                    self._print(inst, m.price, aggressor)
                    yield (
                        inst,
                        ts,
                        _TRADE,
                        aggressor,
                        m.price,
                        qty,
                        0,
                        self._trade_id(m.match_number),
                    )
            elif kind is OrderCancel:
                order = orders.get(m.order_ref)
                if order is None:
                    counters["unknown_order_refs"] += 1
                    continue
                if m.shares > order[3]:
                    counters["overcancelled_orders"] += 1
                remaining = order[3] - m.shares
                if remaining > 0:
                    order[3] = remaining
                    yield (inst, ts, _MODIFY, order[1], order[2], remaining, m.order_ref, 0)
                else:
                    del orders[m.order_ref]
                    yield (inst, ts, _CANCEL, order[1], order[2], 0, m.order_ref, 0)
            elif kind is OrderReplace:
                order = orders.pop(m.orig_ref, None)
                if order is None:
                    counters["unknown_order_refs"] += 1
                    continue
                side = order[1]
                yield (inst, ts, _CANCEL, side, order[2], 0, m.orig_ref, 0)
                new = m.new_ref
                if m.shares <= 0 or m.price <= 0 or not (0 < new < SYNTHETIC_ID_BASE):
                    counters["invalid_orders"] += 1
                    continue
                if new in orders:
                    counters["duplicate_order_refs"] += 1
                    continue
                orders[new] = [inst, side, m.price, m.shares]
                yield (inst, ts, _ADD, side, m.price, m.shares, new, 0)
            elif kind is Trade:
                if m.shares <= 0 or m.price <= 0:
                    counters["invalid_orders"] += 1
                    continue
                counters["hidden_trades"] += 1
                aggressor = self._hidden_aggressor(inst, m)
                self._print(inst, m.price, aggressor)
                yield (
                    inst,
                    ts,
                    _TRADE,
                    aggressor,
                    m.price,
                    m.shares,
                    0,
                    self._trade_id(m.match_number),
                )

    def _hidden_aggressor(self, inst: int, m: Trade) -> int:
        """Aggressor side of a ``P`` print (docs/REAL_DATA.md §4, row P)."""
        from_indicator = _BID if m.side == "S" else _ASK
        if self._p_side_informative:
            return from_indicator
        self.counters["hidden_trades_signed_by_tick_rule"] += 1
        last = self._last_price[inst]
        if last == 0:
            return from_indicator
        if m.price > last:
            return _BID
        if m.price < last:
            return _ASK
        return self._last_sign[inst]

    def _system_event(self, m: SystemEvent) -> Iterator[ProtoEvent]:
        ts = self._ts(m.ts)
        self.system_events.append({"event_code": m.event_code, "exchange_ts": ts})
        phase = _PHASE_OF_EVENT.get(m.event_code)
        if phase is None or phase <= self._phase:
            return
        was_dropping = self._dropping()
        self._phase = phase
        if was_dropping:
            return
        for inst in sorted(set(self._locate.values())):
            status = self._effective_status(inst)
            if self._emitted_status[inst] != status:
                self._emitted_status[inst] = status
                yield (inst, ts, _STATUS, 0, 0, status, 0, 0)
