"""Databento DBN MBO (``XNAS.ITCH``) -> the ITCH 5.0 message model (v1.15, R7 tier 2).

Databento redistributes Nasdaq TotalView-ITCH as DBN records of the MBO
schema.  This module reads such a file (plain ``.dbn`` or ``.dbn.zst``) with
the official ``databento-dbn`` codec (optional extra ``iap[dbn]``; no
network code, the codec is a pure decoder) and **translates every record
into the message tuples of** :mod:`iap.marketdata.itch50`.  The ingest then
runs the unchanged :class:`~iap.marketdata.itch50.Itch50Mapper`, so a DBN
session is normalised by exactly the same rules as an ITCH session
(docs/REAL_DATA.md §4; the cross-check test pins it).

Translation (docs/REAL_DATA.md §4b):

=========  ==============================================================
DBN        ITCH message fed to the mapper
=========  ==============================================================
``A`` add  ``AddOrder`` (side ``B`` -> ``B``, ``A`` -> ``S``, ``N`` invalid)
``C``      ``OrderCancel`` of ``size`` shares (a cancel of the whole
           remaining size removes the order, exactly like ``D``)
``M``      ``OrderReplace`` keeping the order id (cancel + add, new priority)
``T``+``F``  a trade immediately followed by the fill of a resting order at
           the same time / price / size is ONE displayed execution:
           ``OrderExecuted`` (or ``OrderExecutedWithPrice`` printable when
           the price differs from the order's)
``F``      a fill with no matching trade: the same execution
``T``      an unpaired trade: side ``B`` / ``A`` -> a non-displayed print
           (``Trade``, buy/sell indicator = resting side); side ``N`` -> a
           cross print (``CrossTrade``; cross type by time: ``O`` up to one
           minute after the open, ``C`` from the close on, else ``H``)
``R``      clear: ``OrderDelete`` of every live order of the instrument
``N``      counted, no effect
=========  ==============================================================

DBN carries no ITCH system events, no stock directory and no match
numbers.  The session phases come from the calendar (``S`` 04:00, ``Q``
09:30, ``M`` 16:00 or 13:00 on an early close, ``E`` four hours after the
close, New York time); a directory row per universe symbol is synthesised
from the file's symbology (round lot 100, ETP flag from
:data:`KNOWN_ETPS`); every print gets a synthetic trade id (match number 0).
Prices are 1e-9 fixed point; a price not on the 1e-4 grid is rounded half
to even and counted.  Records outside the session's New York calendar day
are counted and dropped.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from iap.marketdata.feederrors import IngestError
from iap.marketdata.itch50 import (
    AddOrder,
    CrossTrade,
    OrderCancel,
    OrderDelete,
    OrderExecuted,
    OrderExecutedWithPrice,
    OrderReplace,
    StockDirectory,
    SystemEvent,
    Trade,
    session_midnight_ns,
)

#: The Databento dataset this reader accepts (Nasdaq TotalView-ITCH).
DATASET = "XNAS.ITCH"
#: DBN fixed-point price scale (1e-9) over the platform's 1e-4.
PRICE_DIVISOR = 100_000
UNDEF_PRICE = (1 << 63) - 1
_NS_PER_DAY = 86_400 * 1_000_000_000
_H = 3_600 * 1_000_000_000
_M = 60 * 1_000_000_000
#: Early closes (13:00 New York) — the NYSE half days of the event calendar.
EARLY_CLOSES: frozenset[str] = frozenset(
    {"2025-07-03", "2025-11-28", "2025-12-24", "2026-11-27", "2026-12-24"}
)
#: Symbols whose synthesised directory row is an ETP (DBN MBO has no ETP flag).
KNOWN_ETPS: frozenset[str] = frozenset({"QQQ", "SPY", "IWM", "DIA", "TQQQ", "SQQQ"})
CHUNK = 1 << 22
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
MBO_ACTIONS = ("A", "C", "M", "T", "F", "R", "N")


def _codec() -> Any:
    try:
        import databento_dbn
    except ImportError as exc:  # pragma: no cover - exercised without the extra
        raise IngestError(
            "--format dbn-mbo needs the databento-dbn codec: pip install 'iap[dbn]'"
        ) from exc
    return databento_dbn


def _char(value: Any) -> str:
    s = str(getattr(value, "value", value))
    return s if len(s) == 1 else s[:1]


def price_e4(price: int) -> tuple[int, bool]:
    """1e-9 fixed point -> 1e-4; ``(price, rounded)`` (half to even)."""
    if price == UNDEF_PRICE or price <= 0:
        return 0, False
    q, r = divmod(price, PRICE_DIVISOR)
    if r == 0:
        return q, False
    twice = 2 * r
    if twice > PRICE_DIVISOR or (twice == PRICE_DIVISOR and q % 2 == 1):
        q += 1
    return q, True


def session_boundaries(date: str) -> list[tuple[int, str]]:
    """``(ns since New York midnight, ITCH system event code)`` of the session."""
    close = 13 * _H if date in EARLY_CLOSES else 16 * _H
    return [(4 * _H, "S"), (9 * _H + 30 * _M, "Q"), (close, "M"), (close + 4 * _H, "E")]


def instrument_ids(metadata: Any, symbols: Iterable[str], date: str) -> dict[int, str]:
    """``instrument_id -> symbol`` of the universe on ``date`` from DBN metadata
    mappings (``{raw_symbol: [{start_date, end_date, symbol}]}``)."""
    day = _dt.date.fromisoformat(date)
    mappings = getattr(metadata, "mappings", None) or {}
    out: dict[int, str] = {}
    for sym in symbols:
        for iv in mappings.get(sym, []):
            get = iv.get if isinstance(iv, dict) else lambda k, _iv=iv: getattr(_iv, k)
            start, end = get("start_date"), get("end_date")
            if start <= day < end or start == end == day:
                try:
                    out[int(get("symbol"))] = sym
                except (TypeError, ValueError):
                    continue
    return out


class DbnMboReader:
    """A DBN MBO file -> ITCH message tuples for a fixed universe.

    Iterate it (once) and feed the messages to an ``Itch50Mapper``; pass the
    mapper as ``mapper`` so a clear (``R``) can delete the live orders and a
    fill can be matched against the resting order's price.
    """

    def __init__(
        self,
        path: str | Path,
        date: str,
        symbols: Iterable[str],
        *,
        limit_messages: int | None = None,
        mapper: Any = None,
        records: Iterable[Any] | None = None,
        metadata: Any = None,
    ) -> None:
        self.path = Path(path)
        self.date = date
        self.symbols = tuple(symbols)
        self.limit_messages = limit_messages
        self.mapper = mapper
        self._records = records
        self.metadata = metadata
        self.midnight = session_midnight_ns(date)
        self.messages_read = 0
        self.bytes_read = 0
        self.limit_reached = False
        self.filtered = 0
        self.counts: dict[str, int] = {a: 0 for a in MBO_ACTIONS}
        self.skipped_unknown: dict[str, int] = {}
        self.counters = {
            "outside_session_date": 0,
            "prices_rounded_to_e4": 0,
            "trades_paired_with_fill": 0,
            "fills_unpaired": 0,
            "hidden_trades": 0,
            "cross_trades": 0,
            "clears": 0,
            "clear_deletes": 0,
        }
        self.dataset: str | None = None
        self.locates: dict[int, str] = {}

    # -- decoding ----------------------------------------------------------

    def _decoded(self) -> Iterator[Any]:
        if self._records is not None:
            if self.metadata is not None:
                yield self.metadata
            yield from self._records
            return
        dbn = _codec()
        with open(self.path, "rb") as f:
            compressed = f.read(4) == ZSTD_MAGIC  # by content, not by file name
        dec = dbn.DBNDecoder(
            compression=dbn.Compression.ZSTD if compressed else dbn.Compression.NONE
        )
        with open(self.path, "rb") as f:
            while chunk := f.read(CHUNK):
                self.bytes_read += len(chunk)
                yield from dec.write_and_decode(chunk)

    # -- translation -------------------------------------------------------

    def _header(self, md: Any) -> Iterator[tuple]:
        self.metadata = md
        self.dataset = str(getattr(md, "dataset", "") or "")
        if self.dataset and self.dataset != DATASET:
            raise IngestError(f"{self.path.name}: dataset {self.dataset!r}, expected {DATASET!r}")
        schema = str(getattr(getattr(md, "schema", None), "value", getattr(md, "schema", "")))
        if schema and "mbo" not in schema.lower():
            raise IngestError(f"{self.path.name}: schema {schema!r}, expected MBO")
        self.locates = instrument_ids(md, self.symbols, self.date)
        for iid, sym in sorted(self.locates.items()):
            yield StockDirectory(
                iid, 0, sym, " ", " ", 100, "N", " ", " ", "P", " ", " ", " ",
                "Y" if sym in KNOWN_ETPS else "N", 0, "N",
            )  # fmt: skip

    def _fill(self, inst: int, ts: int, oid: int, size: int, price: int) -> tuple:
        order = self.mapper._orders.get(oid) if self.mapper is not None else None
        if order is not None and order[2] != price and price > 0:
            return OrderExecutedWithPrice(inst, ts, oid, size, 0, "Y", price)
        return OrderExecuted(inst, ts, oid, size, 0)

    def _unpaired_trade(self, inst: int, ts: int, side: str, size: int, price: int) -> tuple:
        sym = self.locates[inst]
        if side == "N":
            self.counters["cross_trades"] += 1
            open_ns = 9 * _H + 30 * _M
            close_ns = session_boundaries(self.date)[2][0]
            kind = "O" if ts <= open_ns + _M else "C" if ts >= close_ns else "H"
            return CrossTrade(inst, ts, size, sym, price, 0, kind)
        self.counters["hidden_trades"] += 1
        # ITCH P carries the resting side; DBN T carries the aggressor side
        return Trade(inst, ts, 0, "S" if side == "B" else "B", size, sym, price, 0)

    def __iter__(self) -> Iterator[tuple]:
        bounds = session_boundaries(self.date)
        b = 0
        pending: tuple | None = None  # (inst, ts, side, size, price) of a T
        header_done = False
        for rec in self._decoded():
            if not header_done and not hasattr(rec, "action"):
                if hasattr(rec, "mappings") or hasattr(rec, "dataset"):
                    yield from self._header(rec)
                    header_done = True
                    continue
            if not hasattr(rec, "action") or not hasattr(rec, "order_id"):
                name = type(rec).__name__
                self.skipped_unknown[name] = self.skipped_unknown.get(name, 0) + 1
                continue
            if not header_done:
                raise IngestError(f"{self.path.name}: DBN stream without metadata")
            if self.limit_messages is not None and self.messages_read >= self.limit_messages:
                self.limit_reached = True
                break
            self.messages_read += 1
            action = _char(rec.action)
            self.counts[action] = self.counts.get(action, 0) + 1
            inst = int(rec.instrument_id)
            if inst not in self.locates:
                self.filtered += 1
                continue
            ts = int(rec.ts_event) - self.midnight
            if not 0 <= ts < _NS_PER_DAY:
                self.counters["outside_session_date"] += 1
                continue
            price, rounded = price_e4(int(rec.price))
            if rounded:
                self.counters["prices_rounded_to_e4"] += 1
            size, oid, side = int(rec.size), int(rec.order_id), _char(rec.side)

            if pending is not None:
                p_inst, p_ts, p_side, p_size, p_price = pending
                pending = None
                if action == "F" and (inst, ts, size, price) == (p_inst, p_ts, p_size, p_price):
                    self.counters["trades_paired_with_fill"] += 1
                    yield self._fill(inst, ts, oid, size, price)
                    continue
                yield self._unpaired_trade(p_inst, p_ts, p_side, p_size, p_price)
            while b < len(bounds) and ts >= bounds[b][0]:
                yield SystemEvent(bounds[b][0], bounds[b][1])
                b += 1
            if action == "A":
                yield AddOrder(inst, ts, oid, {"B": "B", "A": "S"}.get(side, side), size,
                               self.locates[inst], price, "")  # fmt: skip
            elif action == "C":
                yield OrderCancel(inst, ts, oid, size)
            elif action == "M":
                yield OrderReplace(inst, ts, oid, oid, size, price)
            elif action == "T":
                pending = (inst, ts, side, size, price)
            elif action == "F":
                self.counters["fills_unpaired"] += 1
                yield self._fill(inst, ts, oid, size, price)
            elif action == "R":
                self.counters["clears"] += 1
                if self.mapper is not None:
                    want = self.mapper._index[self.locates[inst]]
                    live = [r for r, o in self.mapper._orders.items() if o[0] == want]
                    self.counters["clear_deletes"] += len(live)
                    for ref in live:
                        yield OrderDelete(inst, ts, ref)
        if pending is not None:
            yield self._unpaired_trade(*pending)
        if not header_done and self.messages_read == 0 and self._records is None:
            raise IngestError(f"{self.path.name}: not a DBN file (no metadata decoded)")
