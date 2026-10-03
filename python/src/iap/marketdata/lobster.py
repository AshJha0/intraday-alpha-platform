"""LOBSTER reader (message file + optional orderbook file) -> proto events.

LOBSTER distributes, per symbol and day, two CSV files without headers:

* the **message file** — ``time, type, order_id, size, price, direction``
  (``time`` in decimal seconds after midnight, ``price`` in 1/10000 dollars,
  ``direction`` 1 = buy limit order, -1 = sell limit order), event types
  1 submission, 2 partial cancellation, 3 deletion, 4 execution of a visible
  order, 5 execution of a hidden order, 6 cross trade, 7 trading halt;
* the **orderbook file** — row *i* is the book after message *i*:
  ``ask_price_1, ask_size_1, bid_price_1, bid_size_1, ask_price_2, ...``
  for N levels (empty levels carry the dummy prices ±9999999999, size 0).

The message file starts at the open with a book that already holds orders
it never shows the submission of.  With the orderbook file those are
**seeded**: the levels of row 0 become seed orders (ids from
:data:`SEED_ORDER_BASE`), messages that reference an unseen order are
applied to the seed order of that price, and depth that only becomes
visible later (the file shows N levels) is **back-filled** when it enters
the reference.  Every emitted event is applied to the real
:class:`iap.orderbook.book.OrderBook` and the book is compared with the
reference row level by level; the first divergence is reported.  Without
the orderbook file nothing can be seeded: messages on unseen orders are
counted (``unresolved_*``) and the book is incomplete until the pre-open
orders have drained (docs/REAL_DATA.md §6).

Timestamps are parsed from the decimal STRING (``"34200.004241176"`` ->
integer nanoseconds); no binary float is involved, so there is no drift.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from iap.core.events import EventType, MarketEvent, SessionStatus, Side
from iap.marketdata.feederrors import FeedFormatError, FeedTruncatedError
from iap.marketdata.itch50 import ProtoEvent, session_midnight_ns
from iap.orderbook.book import ApplyStatus, OrderBook

#: Seed / back-fill order ids start here (LOBSTER order ids are far smaller).
SEED_ORDER_BASE = 1 << 60

#: Dummy price magnitude of an empty level in the orderbook file.
EMPTY_LEVEL_PRICE = 9_999_999_999

_NS_PER_SEC = 1_000_000_000
_NS_PER_DAY = 86_400 * _NS_PER_SEC

_ADD = int(EventType.ADD)
_MODIFY = int(EventType.MODIFY)
_CANCEL = int(EventType.CANCEL)
_EXECUTE = int(EventType.EXECUTE)
_TRADE = int(EventType.TRADE)
_STATUS = int(EventType.STATUS)
_BID = int(Side.BID)
_ASK = int(Side.ASK)

#: LOBSTER halt codes (type 7, carried in the price column) -> session status.
HALT_STATUS = {
    -1: int(SessionStatus.HALT),
    0: int(SessionStatus.AUCTION),
    1: int(SessionStatus.TRADING),
}

LOBSTER_COUNTERS = (
    "seeded_levels",
    "backfilled_levels",
    "absorbed_first_message",
    "events_on_seed_orders",
    "unresolved_cancels",
    "unresolved_deletes",
    "unresolved_executions",
    "duplicate_order_ids",
    "overfilled_executions",
    "hidden_trades",
    "cross_trades",
    "halt_messages",
)


def parse_seconds_to_ns(text: str, *, line: int | None = None) -> int:
    """``"34200.004241176"`` -> 34200004241176 ns, exactly (decimal string
    arithmetic; at most 9 fractional digits)."""
    whole, dot, frac = text.strip().partition(".")
    if not whole.isdigit() or (dot and not frac.isdigit() and frac != "") or len(frac) > 9:
        raise FeedFormatError(f"not a decimal seconds timestamp: {text!r}", line=line)
    ns = int(whole) * _NS_PER_SEC + (int(frac.ljust(9, "0")) if frac else 0)
    if ns >= _NS_PER_DAY:
        raise FeedFormatError(f"timestamp {text!r} is not inside one day", line=line)
    return ns


def symbol_from_filename(path: str | Path) -> str | None:
    """``AAPL_2012-06-21_34200000_57600000_message_10.csv`` -> ``AAPL``."""
    name = Path(path).name
    if "_message_" not in name and "_orderbook_" not in name:
        return None
    head = name.split("_", 1)[0]
    return head or None


def sibling_orderbook(message_path: str | Path) -> Path | None:
    """The orderbook file LOBSTER ships beside a message file, if present."""
    path = Path(message_path)
    if "_message_" not in path.name:
        return None
    candidate = path.with_name(path.name.replace("_message_", "_orderbook_"))
    return candidate if candidate.is_file() else None


def _int(text: str, what: str, line: int) -> int:
    try:
        return int(text.strip())
    except ValueError:
        raise FeedFormatError(f"{what} is not an integer: {text!r}", line=line) from None


class LobsterConverter:
    """One symbol-day of LOBSTER -> proto events (+ book verification).

    Iterate :meth:`events` once; afterwards ``counters``, ``message_counts``
    (per event type 1..7), ``volume`` and ``verification`` are final.
    """

    def __init__(
        self,
        date: str,
        instrument_index: int,
        message_path: str | Path,
        orderbook_path: str | Path | None = None,
        limit_messages: int | None = None,
    ) -> None:
        self.date = date
        self.inst = instrument_index
        self.message_path = Path(message_path)
        self.orderbook_path = Path(orderbook_path) if orderbook_path is not None else None
        self.limit_messages = limit_messages
        self._midnight = session_midnight_ns(date)
        self.counters: dict[str, int] = {k: 0 for k in LOBSTER_COUNTERS}
        self.message_counts: dict[str, int] = {str(t): 0 for t in range(1, 8)}
        self.messages_read = 0
        self.volume = 0
        self.cross_volume = 0
        self.levels = 0
        self.verification: dict = {"status": "not_supplied"}
        #: order_id -> [side, price_e4, remaining]
        self._orders: dict[int, list[int]] = {}
        #: (side, price_e4) -> [[seed order id, remaining], ...]
        self._seeds: dict[tuple[int, int], list[list[int]]] = {}
        self._next_seed = 0
        self._trade_ord = 0
        self._book = OrderBook(instrument_index + 1, 1)
        self._seq = 0
        self._verifying = self.orderbook_path is not None
        self._horizon: dict[int, int | None] = {_BID: None, _ASK: None}
        self._rows_verified = 0

    # -- emission -------------------------------------------------------

    def _emit(
        self, out: list, ts: int, etype: int, side: int, price: int, qty: int, oid: int, tid: int
    ) -> None:
        out.append((self.inst, ts, etype, side, price, qty, oid, tid))
        if self.orderbook_path is None:
            return
        self._seq += 1
        status = self._book.apply(
            MarketEvent(
                0, self._book.instrument_id, 1, ts, ts, self._seq, etype, side, price, qty, oid, tid
            )
        )
        if status != ApplyStatus.APPLIED and self._verifying:
            self._diverge(
                {
                    "reason": "the platform book rejected a mapped event",
                    "event_type": int(etype),
                    "order_id": oid,
                }
            )

    def _trade(self, out: list, ts: int, resting_side: int, price: int, qty: int) -> None:
        self._trade_ord += 1
        aggressor = _ASK if resting_side == _BID else _BID
        self._emit(out, ts, _TRADE, aggressor, price, qty, 0, self._trade_ord)

    def _seed(self, out: list, ts: int, side: int, price: int, qty: int) -> None:
        self._next_seed += 1
        oid = SEED_ORDER_BASE + self._next_seed
        self._seeds.setdefault((side, price), []).append([oid, qty])
        self._emit(out, ts, _ADD, side, price, qty, oid, 0)

    def _seed_available(self, side: int, price: int) -> int:
        return sum(s[1] for s in self._seeds.get((side, price), ()))

    def _consume_seed(
        self, out: list, ts: int, side: int, price: int, qty: int, etype: int
    ) -> None:
        """Take ``qty`` out of the seed orders of a level (EXECUTE or reduce)."""
        seeds = self._seeds[(side, price)]
        self.counters["events_on_seed_orders"] += 1
        while qty > 0:
            oid, rem = seeds[0]
            take = min(rem, qty)
            qty -= take
            rem -= take
            if etype == _EXECUTE:
                self._emit(out, ts, _EXECUTE, side, price, take, oid, 0)
            elif rem > 0:
                self._emit(out, ts, _MODIFY, side, price, rem, oid, 0)
            else:
                self._emit(out, ts, _CANCEL, side, price, 0, oid, 0)
            if rem > 0:
                seeds[0][1] = rem
            else:
                seeds.pop(0)
        if not seeds:
            del self._seeds[(side, price)]

    # -- one message ----------------------------------------------------

    def _apply_message(self, out: list, line: int, ts: int, fields: list[str]) -> None:
        mtype = _int(fields[1], "event type", line)
        if not 1 <= mtype <= 7:
            raise FeedFormatError(f"unknown LOBSTER event type {mtype}", line=line)
        self.message_counts[str(mtype)] += 1
        oid = _int(fields[2], "order id", line)
        size = _int(fields[3], "size", line)
        price = _int(fields[4], "price", line)
        direction = _int(fields[5], "direction", line)
        if mtype == 7:
            self.counters["halt_messages"] += 1
            status = HALT_STATUS.get(price)
            if status is None:
                raise FeedFormatError(f"unknown halt code {price} (type 7 price column)", line=line)
            self._emit(out, ts, _STATUS, 0, 0, status, 0, 0)
            return
        if direction not in (1, -1):
            raise FeedFormatError(f"direction must be 1 or -1, got {direction}", line=line)
        side = _BID if direction == 1 else _ASK
        if size <= 0 or price <= 0:
            raise FeedFormatError(
                f"size and price must be > 0 on event type {mtype} (size {size}, price {price})",
                line=line,
            )
        if mtype == 6:
            self.counters["cross_trades"] += 1
            self.volume += size
            self.cross_volume += size
            return
        if mtype == 5:
            self.counters["hidden_trades"] += 1
            self.volume += size
            self._trade(out, ts, side, price, size)
            return
        if not 0 < oid < SEED_ORDER_BASE:
            raise FeedFormatError(f"order id {oid} outside (0, 2^60)", line=line)
        if mtype == 1:
            if oid in self._orders:
                self.counters["duplicate_order_ids"] += 1
                return
            self._orders[oid] = [side, price, size]
            self._emit(out, ts, _ADD, side, price, size, oid, 0)
            return
        order = self._orders.get(oid)
        if order is None:
            self._unseen_order(out, ts, mtype, side, price, size)
            return
        side, price = order[0], order[1]
        if mtype == 4:
            if size > order[2]:
                self.counters["overfilled_executions"] += 1
                size = order[2]
            order[2] -= size
            if order[2] == 0:
                del self._orders[oid]
            self.volume += size
            self._emit(out, ts, _EXECUTE, side, price, size, oid, 0)
            self._trade(out, ts, side, price, size)
        elif mtype == 2 and order[2] - size > 0:
            order[2] -= size
            self._emit(out, ts, _MODIFY, side, price, order[2], oid, 0)
        else:  # deletion, or a partial cancellation that empties the order
            del self._orders[oid]
            self._emit(out, ts, _CANCEL, side, price, 0, oid, 0)

    def _unseen_order(
        self, out: list, ts: int, mtype: int, side: int, price: int, size: int
    ) -> None:
        """A message on an order submitted before the file starts."""
        resolved = self._seed_available(side, price) >= size
        if mtype == 4:
            self.volume += size
            if resolved:
                self._consume_seed(out, ts, side, price, size, _EXECUTE)
            else:
                self.counters["unresolved_executions"] += 1
            self._trade(out, ts, side, price, size)
        elif resolved:
            self._consume_seed(out, ts, side, price, size, _MODIFY)
        elif mtype == 2:
            self.counters["unresolved_cancels"] += 1
        else:
            self.counters["unresolved_deletes"] += 1

    # -- verification ---------------------------------------------------

    def _parse_row(self, text: str, line: int) -> dict[int, list[tuple[int, int]]]:
        fields = text.strip().split(",")
        if len(fields) % 4 != 0 or not fields[0]:
            raise FeedFormatError(
                f"orderbook row has {len(fields)} columns (expected 4 per level)", line=line
            )
        levels = len(fields) // 4
        if self.levels == 0:
            self.levels = levels
        elif levels != self.levels:
            raise FeedFormatError(
                f"orderbook row has {levels} levels, earlier rows have {self.levels}", line=line
            )
        ref: dict[int, list[tuple[int, int]]] = {_ASK: [], _BID: []}
        for k in range(levels):
            for side, col in ((_ASK, 4 * k), (_BID, 4 * k + 2)):
                price = _int(fields[col], "orderbook price", line)
                size = _int(fields[col + 1], "orderbook size", line)
                if size > 0 and abs(price) != EMPTY_LEVEL_PRICE:
                    ref[side].append((price, size))
        return ref

    def _diverge(self, detail: dict) -> None:
        self._verifying = False
        self.verification = {
            "status": "diverged",
            "levels": self.levels,
            "rows_verified": self._rows_verified,
            "first_divergence": detail,
        }

    def _verify(
        self, out: list, index: int, ts: int, ref: dict[int, list[tuple[int, int]]]
    ) -> None:
        """Back-fill newly visible depth, then compare the book with row ``index``."""
        for side in (_ASK, _BID):
            levels = ref[side]
            horizon = self._horizon[side]
            if horizon is not None:
                for price, size in levels:
                    beyond = price > horizon if side == _ASK else price < horizon
                    if beyond:
                        have = self._book.level_qty(side, price)
                        if have < size:
                            self.counters["backfilled_levels"] += 1
                            self._seed(out, ts, side, price, size - have)
                            if not self._verifying:
                                return
            ours = self._book.depth(side, self.levels)
            if ours != levels:
                level = next(
                    (
                        k
                        for k in range(max(len(ours), len(levels)))
                        if k >= len(ours) or k >= len(levels) or ours[k] != levels[k]
                    ),
                    0,
                )
                self._diverge(
                    {
                        "reason": "level mismatch",
                        "message_index": index,
                        "line": index + 1,
                        "side": "ask" if side == _ASK else "bid",
                        "level": level + 1,
                        "expected": list(levels[level]) if level < len(levels) else None,
                        "actual": list(ours[level]) if level < len(ours) else None,
                    }
                )
                return
            if len(levels) < self.levels or horizon is None:
                self._horizon[side] = None  # the whole side is visible: complete
            else:
                worst = levels[-1][0]
                self._horizon[side] = max(horizon, worst) if side == _ASK else min(horizon, worst)
        self._rows_verified = index + 1

    def _first_row(self, out: list, ts: int, fields: list[str], ref: dict) -> None:
        """Seed the book from row 0 (the state AFTER message 0)."""
        mtype = _int(fields[1], "event type", 1)
        first_add: tuple[int, int, int] | None = None
        if mtype == 1:
            direction = _int(fields[5], "direction", 1)
            first_add = (
                _BID if direction == 1 else _ASK,
                _int(fields[4], "price", 1),
                _int(fields[3], "size", 1),
            )
        for side in (_ASK, _BID):
            for price, size in ref[side]:
                if first_add is not None and first_add[:2] == (side, price):
                    size -= first_add[2]
                if size > 0:
                    self.counters["seeded_levels"] += 1
                    self._seed(out, ts, side, price, size)
            # every level of a side that shows fewer than N levels is known
            self._horizon[side] = ref[side][-1][0] if len(ref[side]) == self.levels else None
        if mtype in (1, 7):
            self._apply_message(out, 1, ts, fields)
            return
        # the book effect of message 0 is already inside row 0: keep its print only
        self.counters["absorbed_first_message"] += 1
        self.message_counts[str(mtype)] += 1
        size = _int(fields[3], "size", 1)
        price = _int(fields[4], "price", 1)
        if mtype in (4, 5, 6) and size > 0 and price > 0:
            self.volume += size
            if mtype == 6:
                self.counters["cross_trades"] += 1
                self.cross_volume += size
            else:
                if mtype == 5:
                    self.counters["hidden_trades"] += 1
                self._trade(
                    out, ts, _BID if _int(fields[5], "direction", 1) == 1 else _ASK, price, size
                )

    # -- driver ---------------------------------------------------------

    def events(self) -> Iterator[ProtoEvent]:
        """Proto events of the symbol-day, in file order."""
        book_file = None
        if self.orderbook_path is not None:
            book_file = open(self.orderbook_path, encoding="ascii", errors="replace", newline="")
        try:
            with open(self.message_path, encoding="ascii", errors="replace", newline="") as msgs:
                last_ts = -1
                index = -1
                for index, raw in enumerate(msgs):
                    if self.limit_messages is not None and index >= self.limit_messages:
                        index -= 1
                        break
                    line = index + 1
                    fields = raw.strip().split(",")
                    if len(fields) < 6:
                        if not raw.strip():
                            raise FeedFormatError("blank line in the message file", line=line)
                        raise FeedFormatError(
                            f"message row has {len(fields)} columns, expected 6", line=line
                        )
                    ns = parse_seconds_to_ns(fields[0], line=line)
                    if ns < last_ts:
                        raise FeedFormatError("message timestamps go backwards", line=line)
                    last_ts = ns
                    ts = self._midnight + ns
                    out: list[ProtoEvent] = []
                    ref = None
                    if book_file is not None:
                        row = book_file.readline()
                        if not row.strip():
                            raise FeedTruncatedError(
                                f"{self.orderbook_path.name}: orderbook file ends before the "
                                "message file",
                                line=line,
                            )
                        ref = self._parse_row(row, line)
                    if index == 0:
                        self._emit(out, ts, _STATUS, 0, 0, int(SessionStatus.TRADING), 0, 0)
                    if index == 0 and ref is not None:
                        self._first_row(out, ts, fields, ref)
                    else:
                        self._apply_message(out, line, ts, fields)
                    if ref is not None and self._verifying:
                        self._verify(out, index, ts, ref)
                    yield from out
                self.messages_read = index + 1
            if book_file is not None:
                if self.limit_messages is None and book_file.readline().strip():
                    raise FeedFormatError(
                        f"{self.orderbook_path.name}: orderbook file has more rows than the "
                        "message file",
                        line=self.messages_read + 1,
                    )
                if self.verification["status"] != "diverged":
                    self.verification = {
                        "status": "match",
                        "levels": self.levels,
                        "rows_verified": self._rows_verified,
                    }
        finally:
            if book_file is not None:
                book_file.close()
