"""Reference L1/L2/MBO order book (PLATFORM_CONVENTIONS.md section 4 — pinned).

Semantics (all languages must mirror exactly; see API_CORE.md):

- ADD: new order at its price level, FIFO tail. A limit ADD that crosses the
  opposite side executes against the book (marketable) from the best opposite
  level's FIFO head; any leftover posts at its price (pinned).
- MODIFY: qty change only. Decrease keeps queue position; increase moves the
  order to the tail of its level (pinned). Price field of the event is ignored.
- CANCEL: remove by order_id.
- EXECUTE: fill the referenced order (FIFO head under valid flow); partial
  supported; order removed when qty reaches 0.
- TRADE: updates cumulative signed trade_flow only (+qty when side==BID i.e.
  buy aggressor, -qty when side==ASK). Never touches book levels.
- QUOTE (FX): replaces the venue's whole side at L1 with one synthetic order.
- SNAPSHOT: recovery burst (schemas/FORMAT.md section 4). First record of a
  burst clears both sides; each record adds one resting order; the record with
  trade_id == 0 completes the burst and clears `stale` — unless a sequence gap
  occurred INSIDE the burst, which marks the burst broken: a broken burst
  still ends at its trade_id == 0 record but leaves `stale` set; only a later
  complete burst with no interior gap clears it (pinned).
- STATUS: qty carries SessionStatus (TRADING/HALT/AUCTION/CLOSE).
- Side domain: for side-indexed event types (ADD, QUOTE, SNAPSHOT, TRADE)
  ``side`` must be BID (0) or ASK (1). An event with side > 1 is malformed:
  it is dropped and counted (``invalid_side_dropped``) through the same
  drop-don't-raise path as duplicates / unknown-order events — never raised
  mid-stream — after its sequence number is consumed (pinned).
- Sequence: duplicate (sequence <= last_sequence) => dropped + counted.
  Gap (sequence > last_sequence + 1) => book marked stale=True + counted;
  while stale only SNAPSHOT/STATUS/TRADE/HEARTBEAT are applied, other events
  are dropped + counted; recovery on completed SNAPSHOT burst (see the
  broken-burst rule above).
- Derived state after EVERY event: best bid/ask ticks + sizes, depth top 10,
  order_count per level, cumulative signed trade_flow, last_sequence,
  exchange/receive timestamps.
- Checkpoints: `checkpoint()` serializes the full book to a JSON-able dict;
  `OrderBook.restore()` rebuilds an identical book (replay-equivalent).
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

from iap.core.events import EventType, MarketEvent, SessionStatus, Side

DEPTH_LEVELS = 10

#: Event types whose application indexes book state by ``side`` (insertion
#: into (side, price) levels, or the trade-flow sign). ``side`` outside
#: {BID, ASK} on these is malformed => dropped + counted (pinned).
_SIDE_INDEXED = frozenset({
    int(EventType.ADD),
    int(EventType.QUOTE),
    int(EventType.SNAPSHOT),
    int(EventType.TRADE),
})


class _Level:
    """One price level: FIFO queue of (order_id -> qty) plus cached total."""

    __slots__ = ("price", "orders", "total_qty")

    def __init__(self, price: int) -> None:
        self.price = price
        self.orders: "OrderedDict[int, int]" = OrderedDict()  # FIFO by insertion
        self.total_qty = 0

    @property
    def order_count(self) -> int:
        return len(self.orders)


class OrderBook:
    """MBO order book for one instrument on one venue (venue_id=0: synthetic)."""

    __slots__ = (
        "instrument_id",
        "venue_id",
        "_levels",  # (side, price) -> _Level
        "_orders",  # order_id -> (side, price)
        "last_sequence",
        "exchange_ts",
        "receive_ts",
        "trade_flow",
        "status",
        "stale",
        "_snapshot_active",
        "_snapshot_broken",
        "duplicates_dropped",
        "gaps_detected",
        "dropped_while_stale",
        "unknown_order_events",
        "invalid_side_dropped",
        "events_applied",
    )

    def __init__(self, instrument_id: int, venue_id: int) -> None:
        self.instrument_id = instrument_id
        self.venue_id = venue_id
        self._levels: Dict[Tuple[int, int], _Level] = {}
        self._orders: Dict[int, Tuple[int, int]] = {}
        self.last_sequence = 0
        self.exchange_ts = 0
        self.receive_ts = 0
        self.trade_flow = 0
        self.status = int(SessionStatus.TRADING)
        self.stale = False
        self._snapshot_active = False
        self._snapshot_broken = False
        self.duplicates_dropped = 0
        self.gaps_detected = 0
        self.dropped_while_stale = 0
        self.unknown_order_events = 0
        self.invalid_side_dropped = 0
        self.events_applied = 0

    # ------------------------------------------------------------ application

    def apply(self, ev: MarketEvent) -> None:
        """Apply one event (sequence-checked). Raises ValueError on routing errors."""
        if ev.instrument_id != self.instrument_id or (
            self.venue_id != 0 and ev.venue_id != self.venue_id
        ):
            raise ValueError(
                f"event routed to wrong book: event {ev.instrument_id}@{ev.venue_id}, "
                f"book {self.instrument_id}@{self.venue_id}"
            )
        # Sequence handling (duplicates dropped, gaps => stale).
        if ev.sequence <= self.last_sequence:
            self.duplicates_dropped += 1
            return
        if ev.sequence > self.last_sequence + 1 and self.last_sequence != 0:
            self.gaps_detected += 1
            self.stale = True
            if self._snapshot_active:
                # Gap inside an active SNAPSHOT burst: the burst is broken —
                # its completion record must NOT clear `stale` (records are
                # missing). Only a later complete gap-free burst recovers.
                self._snapshot_broken = True
        self.last_sequence = ev.sequence
        self.exchange_ts = ev.exchange_ts
        self.receive_ts = ev.receive_ts

        et = ev.event_type
        # Side-domain validation for side-indexed event types: malformed
        # side => dropped + counted (never raised mid-stream), same path as
        # other malformed events; the sequence number above is consumed.
        if ev.side > 1 and et in _SIDE_INDEXED:
            self.invalid_side_dropped += 1
            return
        if self.stale and et not in (
            EventType.SNAPSHOT,
            EventType.STATUS,
            EventType.TRADE,
            EventType.HEARTBEAT,
        ):
            self.dropped_while_stale += 1
            return

        if et == EventType.ADD:
            self._apply_add(ev)
        elif et == EventType.MODIFY:
            self._apply_modify(ev)
        elif et == EventType.CANCEL:
            self._apply_cancel(ev)
        elif et == EventType.EXECUTE:
            self._apply_execute(ev)
        elif et == EventType.TRADE:
            self.trade_flow += ev.qty if ev.side == Side.BID else -ev.qty
        elif et == EventType.QUOTE:
            self._apply_quote(ev)
        elif et == EventType.SNAPSHOT:
            self._apply_snapshot(ev)
        elif et == EventType.STATUS:
            self.status = ev.qty
        elif et == EventType.HEARTBEAT:
            pass
        else:
            raise ValueError(f"unknown event_type {et} (event_id={ev.event_id})")
        self.events_applied += 1

    # ------------------------------------------------------------- primitives

    def _insert_order(self, side: int, price: int, order_id: int, qty: int) -> None:
        key = (side, price)
        level = self._levels.get(key)
        if level is None:
            level = _Level(price)
            self._levels[key] = level
        level.orders[order_id] = qty
        level.total_qty += qty
        self._orders[order_id] = key

    def _remove_order(self, order_id: int) -> None:
        key = self._orders.pop(order_id)
        level = self._levels[key]
        level.total_qty -= level.orders.pop(order_id)
        if not level.orders:
            del self._levels[key]

    def _apply_add(self, ev: MarketEvent) -> None:
        if ev.order_id in self._orders:
            self.unknown_order_events += 1  # duplicate order id: drop, count
            return
        remaining = self._match_marketable(ev.side, ev.price_ticks, ev.qty)
        if remaining > 0:
            self._insert_order(ev.side, ev.price_ticks, ev.order_id, remaining)

    def _match_marketable(self, side: int, price: int, qty: int) -> int:
        """Execute a crossing limit against the opposite side; return leftover."""
        opp = Side.ASK if side == Side.BID else Side.BID
        while qty > 0:
            best = self._best_level(opp)
            if best is None:
                break
            crosses = price >= best.price if side == Side.BID else price <= best.price
            if not crosses:
                break
            # Fill from FIFO head of the best opposite level.
            head_id, head_qty = next(iter(best.orders.items()))
            fill = min(qty, head_qty)
            qty -= fill
            if fill == head_qty:
                self._remove_order(head_id)
            else:
                best.orders[head_id] = head_qty - fill
                best.total_qty -= fill
        return qty

    def _apply_modify(self, ev: MarketEvent) -> None:
        key = self._orders.get(ev.order_id)
        if key is None:
            self.unknown_order_events += 1
            return
        level = self._levels[key]
        old_qty = level.orders[ev.order_id]
        new_qty = ev.qty
        if new_qty <= 0:
            self._remove_order(ev.order_id)
            return
        if new_qty <= old_qty:
            # Decrease: keep queue position.
            level.orders[ev.order_id] = new_qty
        else:
            # Increase: move to tail of the level.
            del level.orders[ev.order_id]
            level.orders[ev.order_id] = new_qty
        level.total_qty += new_qty - old_qty

    def _apply_cancel(self, ev: MarketEvent) -> None:
        if ev.order_id not in self._orders:
            self.unknown_order_events += 1
            return
        self._remove_order(ev.order_id)

    def _apply_execute(self, ev: MarketEvent) -> None:
        key = self._orders.get(ev.order_id)
        if key is None:
            self.unknown_order_events += 1
            return
        level = self._levels[key]
        old_qty = level.orders[ev.order_id]
        fill = min(ev.qty, old_qty)
        if fill >= old_qty:
            self._remove_order(ev.order_id)
        else:
            level.orders[ev.order_id] = old_qty - fill
            level.total_qty -= fill

    def _apply_quote(self, ev: MarketEvent) -> None:
        """FX QUOTE: replace this venue's whole side at L1."""
        side = ev.side
        for key in [k for k in self._levels if k[0] == side]:
            for oid in list(self._levels[key].orders):
                self._remove_order(oid)
        self._insert_order(side, ev.price_ticks, ev.order_id, ev.qty)

    def _apply_snapshot(self, ev: MarketEvent) -> None:
        if not self._snapshot_active:
            # Burst start: clear the whole book state (levels + orders).
            self._levels.clear()
            self._orders.clear()
            self._snapshot_active = True
            self._snapshot_broken = False
        if ev.order_id in self._orders:
            self._remove_order(ev.order_id)
        self._insert_order(ev.side, ev.price_ticks, ev.order_id, ev.qty)
        if ev.trade_id == 0:  # last record of the burst
            self._snapshot_active = False
            if not self._snapshot_broken:
                self.stale = False
            self._snapshot_broken = False

    # ---------------------------------------------------------- derived state

    def resting_orders(self, side: Optional[int] = None) -> List[Tuple[int, int, int, int]]:
        """[(order_id, side, price_ticks, qty)] in deterministic insertion order."""
        out = []
        for oid, (s, price) in self._orders.items():
            if side is None or s == side:
                out.append((oid, s, price, self._levels[(s, price)].orders[oid]))
        return out

    def order_count_total(self) -> int:
        """Total number of resting orders in the book."""
        return len(self._orders)

    def _best_level(self, side: int) -> Optional[_Level]:
        best_key = None
        for key in self._levels:
            if key[0] != side:
                continue
            if best_key is None:
                best_key = key
            elif side == Side.BID and key[1] > best_key[1]:
                best_key = key
            elif side == Side.ASK and key[1] < best_key[1]:
                best_key = key
        return self._levels[best_key] if best_key is not None else None

    def best_bid(self) -> Optional[Tuple[int, int]]:
        """(price_ticks, total_size) of the best bid, or None."""
        level = self._best_level(Side.BID)
        return (level.price, level.total_qty) if level else None

    def best_ask(self) -> Optional[Tuple[int, int]]:
        """(price_ticks, total_size) of the best ask, or None."""
        level = self._best_level(Side.ASK)
        return (level.price, level.total_qty) if level else None

    def _sorted_levels(self, side: int) -> List[_Level]:
        levels = [lvl for (s, _), lvl in self._levels.items() if s == side]
        levels.sort(key=lambda l: -l.price if side == Side.BID else l.price)
        return levels

    def depth(self, side: int, levels: int = DEPTH_LEVELS) -> List[Tuple[int, int]]:
        """Top-N [(price_ticks, total_size)] best-first."""
        return [(l.price, l.total_qty) for l in self._sorted_levels(side)[:levels]]

    def order_count(self, side: int, levels: int = DEPTH_LEVELS) -> List[Tuple[int, int]]:
        """Top-N [(price_ticks, order_count)] best-first."""
        return [(l.price, l.order_count) for l in self._sorted_levels(side)[:levels]]

    def state_summary(self) -> dict:
        """Golden-comparable exact-integer state (see tests/golden/)."""
        bb, ba = self.best_bid(), self.best_ask()
        return {
            "best_bid_ticks": bb[0] if bb else 0,
            "best_bid_size": bb[1] if bb else 0,
            "best_ask_ticks": ba[0] if ba else 0,
            "best_ask_size": ba[1] if ba else 0,
            "depth_bid_top5": [list(t) for t in self.depth(Side.BID, 5)],
            "depth_ask_top5": [list(t) for t in self.depth(Side.ASK, 5)],
            "order_count_bid_top3": [list(t) for t in self.order_count(Side.BID, 3)],
            "order_count_ask_top3": [list(t) for t in self.order_count(Side.ASK, 3)],
            "trade_flow": self.trade_flow,
            "sequence": self.last_sequence,
        }

    # ------------------------------------------------------------ checkpoints

    def checkpoint(self) -> dict:
        """Full deterministic serialization (JSON-able; keys sorted explicitly)."""
        levels_out = []
        for (side, price) in sorted(self._levels):
            level = self._levels[(side, price)]
            levels_out.append(
                {
                    "side": side,
                    "price_ticks": price,
                    "orders": [[oid, q] for oid, q in level.orders.items()],  # FIFO
                }
            )
        return {
            "instrument_id": self.instrument_id,
            "venue_id": self.venue_id,
            "levels": levels_out,
            # Global arrival order of resting orders (dict insertion order of
            # _orders) — restore() rebuilds it so resting_orders() round-trips
            # checkpoints exactly (levels alone only pin per-level FIFO).
            "arrival_order": list(self._orders),
            "last_sequence": self.last_sequence,
            "exchange_ts": self.exchange_ts,
            "receive_ts": self.receive_ts,
            "trade_flow": self.trade_flow,
            "status": self.status,
            "stale": self.stale,
            "snapshot_active": self._snapshot_active,
            "snapshot_broken": self._snapshot_broken,
            "counters": {
                "duplicates_dropped": self.duplicates_dropped,
                "gaps_detected": self.gaps_detected,
                "dropped_while_stale": self.dropped_while_stale,
                "unknown_order_events": self.unknown_order_events,
                "invalid_side_dropped": self.invalid_side_dropped,
                "events_applied": self.events_applied,
            },
        }

    @classmethod
    def restore(cls, cp: dict) -> "OrderBook":
        """Rebuild an identical book from ``checkpoint()`` output."""
        book = cls(cp["instrument_id"], cp["venue_id"])
        for lvl in cp["levels"]:
            for oid, qty in lvl["orders"]:
                book._insert_order(lvl["side"], lvl["price_ticks"], oid, qty)
        # Rebuild the global arrival order (levels above fixed per-level FIFO
        # order; _orders must iterate in original insertion order so e.g.
        # resting_orders() is checkpoint-round-trip exact).
        arrival = cp["arrival_order"]
        if len(arrival) != len(book._orders) or any(
            oid not in book._orders for oid in arrival
        ):
            raise ValueError("checkpoint arrival_order inconsistent with levels")
        book._orders = {oid: book._orders[oid] for oid in arrival}
        book.last_sequence = cp["last_sequence"]
        book.exchange_ts = cp["exchange_ts"]
        book.receive_ts = cp["receive_ts"]
        book.trade_flow = cp["trade_flow"]
        book.status = cp["status"]
        book.stale = cp["stale"]
        book._snapshot_active = cp["snapshot_active"]
        book._snapshot_broken = cp["snapshot_broken"]
        c = cp["counters"]
        book.duplicates_dropped = c["duplicates_dropped"]
        book.gaps_detected = c["gaps_detected"]
        book.dropped_while_stale = c["dropped_while_stale"]
        book.unknown_order_events = c["unknown_order_events"]
        book.invalid_side_dropped = c["invalid_side_dropped"]
        book.events_applied = c["events_applied"]
        return book


class ConsolidatedBook:
    """Consolidated view over per-venue books of one instrument.

    Routes events to per-venue books by venue_id and merges derived state:
    same price across venues => sizes and order counts summed; best = best
    across venues. Sequence/staleness remain per venue.
    """

    __slots__ = ("instrument_id", "books")

    def __init__(self, instrument_id: int) -> None:
        self.instrument_id = instrument_id
        self.books: Dict[int, OrderBook] = {}

    def venue_book(self, venue_id: int) -> OrderBook:
        """Get (or lazily create) the per-venue book."""
        book = self.books.get(venue_id)
        if book is None:
            book = OrderBook(self.instrument_id, venue_id)
            self.books[venue_id] = book
        return book

    def apply(self, ev: MarketEvent) -> None:
        """Route one event to its venue book."""
        self.venue_book(ev.venue_id).apply(ev)

    def _merged(self, side: int) -> List[Tuple[int, int, int]]:
        agg: Dict[int, List[int]] = {}
        for vid in sorted(self.books):
            for level in self.books[vid]._sorted_levels(side):
                slot = agg.setdefault(level.price, [0, 0])
                slot[0] += level.total_qty
                slot[1] += level.order_count
        out = [(p, sq, oc) for p, (sq, oc) in agg.items()]
        out.sort(key=lambda t: -t[0] if side == Side.BID else t[0])
        return out

    def best_bid(self) -> Optional[Tuple[int, int]]:
        m = self._merged(Side.BID)
        return (m[0][0], m[0][1]) if m else None

    def best_ask(self) -> Optional[Tuple[int, int]]:
        m = self._merged(Side.ASK)
        return (m[0][0], m[0][1]) if m else None

    def depth(self, side: int, levels: int = DEPTH_LEVELS) -> List[Tuple[int, int]]:
        return [(p, sq) for p, sq, _ in self._merged(side)[:levels]]

    def order_count(self, side: int, levels: int = DEPTH_LEVELS) -> List[Tuple[int, int]]:
        return [(p, oc) for p, _, oc in self._merged(side)[:levels]]

    def trade_flow(self) -> int:
        """Sum of per-venue cumulative signed trade flow."""
        return sum(b.trade_flow for _, b in sorted(self.books.items()))

    def checkpoint(self) -> dict:
        return {
            "instrument_id": self.instrument_id,
            "venues": {str(vid): b.checkpoint() for vid, b in sorted(self.books.items())},
        }

    @classmethod
    def restore(cls, cp: dict) -> "ConsolidatedBook":
        cons = cls(cp["instrument_id"])
        for vid, bcp in cp["venues"].items():
            cons.books[int(vid)] = OrderBook.restore(bcp)
        return cons
