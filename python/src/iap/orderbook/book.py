"""Reference L1/L2/MBO order book (PLATFORM_CONVENTIONS.md section 4 — pinned).

Semantics (all languages must mirror exactly; see API_CORE.md section 4):

- ADD: new order at its price level, FIFO tail. While the venue status is
  TRADING a limit ADD that crosses the opposite side executes against the
  book (marketable) from the best opposite level's FIFO head; any leftover
  posts at its price. While the status is HALT / AUCTION / CLOSE nothing
  matches: a crossing ADD rests and the book may be crossed (call phase);
  the venue's EXECUTE messages perform the uncross (pinned).
- MODIFY: qty change only. Decrease keeps queue position; increase moves the
  order to the tail of its level (pinned). A non-zero ``price_ticks`` that
  differs from the resting price is an adapter bug: dropped + counted
  (``modify_price_mismatch``). qty <= 0 removes the order.
- CANCEL: remove by order_id.
- EXECUTE: fill the referenced order (FIFO head under valid flow); partial
  supported; order removed when qty reaches 0.
- TRADE: updates cumulative signed trade_flow only (+qty when side==BID i.e.
  buy aggressor, -qty when side==ASK). Never touches book levels.
- QUOTE (FX L1 replace): clears the venue's whole side (walking the side's
  levels) and inserts one order. ``order_id == 0`` (id-less feeds) uses the
  deterministic synthetic id ``synthetic_order_id(side, 0)``; an explicit id
  must not rest elsewhere in the book (drop + count ``unknown_order_events``).
- SNAPSHOT: recovery burst (schemas/FORMAT.md section 4). First record of a
  burst clears both sides; each record adds one resting order; the record
  with trade_id == 0 completes the burst and clears `stale` — unless a
  sequence gap occurred INSIDE the burst (burst broken: `stale` stays set;
  only a later complete burst clears it). ``trade_id`` must count down by
  exactly one per record: a record whose trade_id is >= the previous one
  starts a new burst (the previous one was interrupted;
  ``snapshot_restarts``), one that skips ahead marks the burst broken
  (records missing). ``order_id == 0`` records get synthetic ids
  ``synthetic_order_id(side, ordinal)`` (ordinal per side per burst); a
  repeated id inside a burst is malformed (drop + count).
- STATUS: qty carries SessionStatus (TRADING/HALT/AUCTION/CLOSE); other codes
  are malformed (drop + count). STATUS never touches sequencing.
- Malformed payloads are dropped + counted (``invalid_payload_dropped``) after
  the sequence number is consumed, never raised: side > 1 on side-indexed
  types (``invalid_side_dropped``), unknown event_type
  (``unknown_type_dropped``), qty <= 0 / price <= 0 where the type needs a
  positive value, order_id == 0 on ADD/MODIFY/CANCEL/EXECUTE, explicit ids
  in the reserved synthetic range on ADD/QUOTE/SNAPSHOT (MODIFY/CANCEL/
  EXECUTE may reference a synthetic resting order), and any i64 overflow of
  a level total or trade_flow (checked arithmetic, never wraps).
- Sequence (per book): the first event of an epoch is accepted whatever its
  sequence (0 included). Then sequence <= last_sequence => duplicate, dropped
  + counted; sequence == last + 1 => in order; sequence > last + 1 => gap:
  with ``reorder_window == 0`` the book is marked stale immediately; with a
  window the event is held back (up to ``reorder_window`` events) and applied
  in order once the missing sequences arrive (``late_recovered`` counts the
  gap fillers); when the buffer is full the gap is declared and the held
  events are applied in sequence order. While stale only SNAPSHOT / STATUS /
  TRADE / HEARTBEAT are applied; other events drop + count.
- Sequence reset: a SNAPSHOT record that starts a burst with
  sequence < last_sequence is a venue sequence reset (daily restart /
  fail-over): ``sequence_resets`` += 1, ``sequence_epoch`` += 1, the book is
  marked stale and the burst recovers it. ``reset_sequence()`` is the
  explicit API for adapters that learn about a restart out of band.
- Apply result: ``apply`` returns an :class:`ApplyStatus`
  (APPLIED / DROPPED / HELD) for the event it was handed — the per-event
  verdict downstream consumers filter on (API_FEATURES.md §2: a feature
  engine contributes only APPLIED events to rolling state).
- Accounting invariant: every event handed to ``apply`` ends in exactly one
  of ``events_applied`` or a drop counter (``duplicates_dropped``,
  ``dropped_while_stale``, ``unknown_order_events``, ``invalid_side_dropped``,
  ``invalid_payload_dropped``, ``unknown_type_dropped``,
  ``modify_price_mismatch``) or is held in the reorder buffer;
  ``gaps_detected`` / ``snapshot_restarts`` / ``sequence_resets`` /
  ``late_recovered`` are informational.
- Derived state after EVERY event: best bid/ask ticks + sizes, depth top 10,
  order_count per level, cumulative signed trade_flow, last_sequence,
  exchange/receive timestamps.
- Checkpoints (x-version 2): `checkpoint()` serializes the full book to a
  JSON-able dict; `OrderBook.restore()` rebuilds an identical book
  (replay-equivalent), including a pending reorder buffer.
"""

from __future__ import annotations

from collections import OrderedDict
from enum import IntEnum
from typing import Dict, List, Optional, Tuple

from iap.core.events import (
    FIELDS,
    I64_MAX,
    I64_MIN,
    SYNTHETIC_ID_BASE,
    EventType,
    MarketEvent,
    SessionStatus,
    Side,
)

DEPTH_LEVELS = 10


class ApplyStatus(IntEnum):
    """Per-event verdict returned by :meth:`OrderBook.apply` (pinned).

    ``applied + dropped + held == events fed`` for every book; downstream
    consumers (feature engine, API_FEATURES §2) MUST ignore every event
    that is not ``APPLIED``.
    """

    #: the event changed book state (or was a valid no-op: HEARTBEAT/STATUS)
    APPLIED = 0
    #: the event was rejected and counted in exactly one drop counter
    DROPPED = 1
    #: the event is buffered behind a sequence hole (reorder_window > 0);
    #: it is reported APPLIED/DROPPED when the buffer drains
    HELD = 2


#: Checkpoint schema version (bumped when the checkpoint shape changes).
CHECKPOINT_VERSION = 2

#: Largest accepted ``reorder_window`` (hold-back buffer, events).
MAX_REORDER_WINDOW = 4096

#: Synthetic ids live in the reserved range [SYNTHETIC_ID_BASE, 2^64):
#: SYNTHETIC_ID_BASE | side << 40 | ordinal (ordinal < 2^40).
_SYNTHETIC_ORDINAL_MASK = (1 << 40) - 1

#: Counter names in pinned checkpoint order.
COUNTER_NAMES = (
    "duplicates_dropped",
    "gaps_detected",
    "dropped_while_stale",
    "unknown_order_events",
    "invalid_side_dropped",
    "invalid_payload_dropped",
    "unknown_type_dropped",
    "modify_price_mismatch",
    "snapshot_restarts",
    "sequence_resets",
    "late_recovered",
    "events_applied",
)

#: Event types whose application indexes book state by ``side`` (insertion
#: into (side, price) levels, or the trade-flow sign). ``side`` outside
#: {BID, ASK} on these is malformed => dropped + counted (pinned).
_SIDE_INDEXED = frozenset({
    int(EventType.ADD),
    int(EventType.QUOTE),
    int(EventType.SNAPSHOT),
    int(EventType.TRADE),
})

#: Event types that are applied while the book is stale.
_APPLIED_WHILE_STALE = frozenset({
    int(EventType.SNAPSHOT),
    int(EventType.STATUS),
    int(EventType.TRADE),
    int(EventType.HEARTBEAT),
})

_KNOWN_TYPES = frozenset(int(t) for t in EventType)
_STATUS_CODES = frozenset(int(s) for s in SessionStatus)


def synthetic_order_id(side: int, ordinal: int) -> int:
    """Deterministic synthetic order id for id-less QUOTE/SNAPSHOT records."""
    return SYNTHETIC_ID_BASE | (int(side) << 40) | (ordinal & _SYNTHETIC_ORDINAL_MASK)


def _fits_i64(v: int) -> bool:
    return I64_MIN <= v <= I64_MAX


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
        "reorder_window",
        "_levels",  # (side, price) -> _Level
        "_orders",  # order_id -> (side, price)
        "last_sequence",
        "has_sequence",
        "sequence_epoch",
        "exchange_ts",
        "receive_ts",
        "trade_flow",
        "status",
        "stale",
        "_snapshot_active",
        "_snapshot_broken",
        "_snapshot_countdown",
        "_snapshot_synthetic_next",
        "_pending",  # sequence -> MarketEvent (hold-back buffer)
        "duplicates_dropped",
        "gaps_detected",
        "dropped_while_stale",
        "unknown_order_events",
        "invalid_side_dropped",
        "invalid_payload_dropped",
        "unknown_type_dropped",
        "modify_price_mismatch",
        "snapshot_restarts",
        "sequence_resets",
        "late_recovered",
        "events_applied",
    )

    def __init__(self, instrument_id: int, venue_id: int, reorder_window: int = 0) -> None:
        if not (0 <= reorder_window <= MAX_REORDER_WINDOW):
            raise ValueError(
                f"reorder_window must be in [0, {MAX_REORDER_WINDOW}]: {reorder_window}"
            )
        self.instrument_id = instrument_id
        self.venue_id = venue_id
        self.reorder_window = reorder_window
        self._levels: Dict[Tuple[int, int], _Level] = {}
        self._orders: Dict[int, Tuple[int, int]] = {}
        self.last_sequence = 0
        self.has_sequence = False
        self.sequence_epoch = 0
        self.exchange_ts = 0
        self.receive_ts = 0
        self.trade_flow = 0
        self.status = int(SessionStatus.TRADING)
        self.stale = False
        self._snapshot_active = False
        self._snapshot_broken = False
        self._snapshot_countdown = 0
        self._snapshot_synthetic_next = [0, 0]
        self._pending: Dict[int, MarketEvent] = {}
        for name in COUNTER_NAMES:
            setattr(self, name, 0)

    # ------------------------------------------------------------ application

    def apply(self, ev: MarketEvent) -> ApplyStatus:
        """Apply one event (sequence-checked). Raises ValueError on routing errors.

        Returns the pinned per-event verdict (:class:`ApplyStatus`): APPLIED
        when the event reached book state, DROPPED when it was rejected and
        counted, HELD while it waits behind a sequence hole.
        """
        if ev.instrument_id != self.instrument_id or (
            self.venue_id != 0 and ev.venue_id != self.venue_id
        ):
            raise ValueError(
                f"event routed to wrong book: event {ev.instrument_id}@{ev.venue_id}, "
                f"book {self.instrument_id}@{self.venue_id}"
            )
        if (
            self.reorder_window
            and self.has_sequence
            and ev.sequence > self.last_sequence + 1
        ):
            # Out-of-sequence event ahead of a hole: hold it back until the
            # missing sequences arrive (bounded by reorder_window).
            if ev.sequence in self._pending:
                self.duplicates_dropped += 1
                return ApplyStatus.DROPPED
            if len(self._pending) < self.reorder_window:
                self._pending[ev.sequence] = ev
                return ApplyStatus.HELD
            # Buffer full: give up on the hole, declare the gap and apply
            # everything held so far in sequence order.
            self._pending[ev.sequence] = ev
            return self._flush_pending(ev.sequence)
        status = self._apply_sequenced(ev)
        if self._pending:
            self._drain_pending()
        return status

    def _flush_pending(self, target_seq: Optional[int] = None) -> ApplyStatus:
        """Apply every held-back event in sequence order (gap declared).

        Returns the verdict of ``target_seq`` (the event that triggered the
        flush), or APPLIED when the caller does not track one.
        """
        pending = self._pending
        self._pending = {}
        status = ApplyStatus.APPLIED
        for seq in sorted(pending):
            st = self._apply_sequenced(pending[seq], from_buffer=True)
            if seq == target_seq:
                status = st
        return status

    def _drain_pending(self) -> None:
        """Apply held-back events that became contiguous with last_sequence."""
        while self._pending:
            nxt = self.last_sequence + 1
            ev = self._pending.pop(nxt, None)
            if ev is None:
                return
            self._apply_sequenced(ev, from_buffer=True)

    def _apply_sequenced(
        self, ev: MarketEvent, from_buffer: bool = False
    ) -> ApplyStatus:
        """Sequence check + dispatch for one event (no hold-back)."""
        et = ev.event_type
        if self.has_sequence:
            if ev.sequence <= self.last_sequence:
                if (
                    et == EventType.SNAPSHOT
                    and not self._snapshot_active
                    and ev.sequence < self.last_sequence
                ):
                    # Venue sequence reset (daily restart / fail-over): the
                    # SNAPSHOT burst starting the new epoch recovers the book.
                    if self._pending:
                        self._flush_pending()  # old epoch: declare its gap
                    self.sequence_epoch += 1
                    self.sequence_resets += 1
                    self.stale = True
                else:
                    self.duplicates_dropped += 1
                    return ApplyStatus.DROPPED
            elif ev.sequence - self.last_sequence > 1:
                self.gaps_detected += 1
                self.stale = True
                if self._snapshot_active:
                    # Gap inside an active SNAPSHOT burst: the burst is
                    # broken — its completion record must NOT clear `stale`.
                    self._snapshot_broken = True
            elif self._pending and not from_buffer:
                self.late_recovered += 1  # a gap filler arrived late
        self.has_sequence = True
        self.last_sequence = ev.sequence
        self.exchange_ts = ev.exchange_ts
        self.receive_ts = ev.receive_ts

        # Malformed-event classes: dropped + counted (never raised), after
        # the sequence number above is consumed.
        if et not in _KNOWN_TYPES:
            self.unknown_type_dropped += 1
            return ApplyStatus.DROPPED
        if ev.side > 1 and et in _SIDE_INDEXED:
            self.invalid_side_dropped += 1
            return ApplyStatus.DROPPED
        if not self._payload_ok(ev):
            self.invalid_payload_dropped += 1
            return ApplyStatus.DROPPED
        if self.stale and et not in _APPLIED_WHILE_STALE:
            self.dropped_while_stale += 1
            return ApplyStatus.DROPPED

        if et == EventType.ADD:
            if not self._apply_add(ev):
                return ApplyStatus.DROPPED
        elif et == EventType.MODIFY:
            if not self._apply_modify(ev):
                return ApplyStatus.DROPPED
        elif et == EventType.CANCEL:
            if not self._apply_cancel(ev):
                return ApplyStatus.DROPPED
        elif et == EventType.EXECUTE:
            if not self._apply_execute(ev):
                return ApplyStatus.DROPPED
        elif et == EventType.TRADE:
            flow = self.trade_flow + (ev.qty if ev.side == Side.BID else -ev.qty)
            if not _fits_i64(flow):
                self.invalid_payload_dropped += 1
                return ApplyStatus.DROPPED
            self.trade_flow = flow
        elif et == EventType.QUOTE:
            if not self._apply_quote(ev):
                return ApplyStatus.DROPPED
        elif et == EventType.SNAPSHOT:
            if not self._apply_snapshot(ev):
                return ApplyStatus.DROPPED
        elif et == EventType.STATUS:
            self.status = ev.qty
        # HEARTBEAT: timestamps/sequence only.
        self.events_applied += 1
        return ApplyStatus.APPLIED

    @staticmethod
    def _payload_ok(ev: MarketEvent) -> bool:
        """Payload-domain rule (pinned): False => invalid_payload_dropped."""
        et = ev.event_type
        if et in (EventType.ADD, EventType.MODIFY, EventType.CANCEL, EventType.EXECUTE):
            if ev.order_id == 0:
                return False
            if et == EventType.ADD:
                return ev.qty > 0 and ev.price_ticks > 0 and ev.order_id < SYNTHETIC_ID_BASE
            if et == EventType.EXECUTE:
                return ev.qty > 0
            return True  # MODIFY (qty <= 0 removes), CANCEL: may reference synthetic ids
        if et in (EventType.QUOTE, EventType.SNAPSHOT):
            return ev.qty > 0 and ev.price_ticks > 0 and ev.order_id < SYNTHETIC_ID_BASE
        if et == EventType.TRADE:
            return ev.qty > 0 and ev.price_ticks > 0
        if et == EventType.STATUS:
            return ev.qty in _STATUS_CODES
        return True  # HEARTBEAT

    def reset_sequence(self) -> None:
        """Explicit venue sequence reset (session roll / fail-over known out of band).

        Held-back events are applied first (declaring their gap), then the
        book enters a new epoch: the next event is accepted whatever its
        sequence, and the book is stale until a complete SNAPSHOT burst.
        """
        if self._pending:
            self._flush_pending()
        self.has_sequence = False
        self.sequence_epoch += 1
        self.sequence_resets += 1
        self.stale = True
        self._snapshot_active = False
        self._snapshot_broken = False
        self._snapshot_countdown = 0

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

    def _level_total(self, side: int, price: int) -> int:
        level = self._levels.get((side, price))
        return level.total_qty if level is not None else 0

    def _remove_order(self, order_id: int) -> None:
        key = self._orders.pop(order_id)
        level = self._levels[key]
        level.total_qty -= level.orders.pop(order_id)
        if not level.orders:
            del self._levels[key]

    def _apply_add(self, ev: MarketEvent) -> bool:
        if ev.order_id in self._orders:
            self.unknown_order_events += 1  # duplicate order id: drop, count
            return False
        if not _fits_i64(self._level_total(ev.side, ev.price_ticks) + ev.qty):
            self.invalid_payload_dropped += 1
            return False
        remaining = ev.qty
        if self.status == SessionStatus.TRADING:
            remaining = self._match_marketable(ev.side, ev.price_ticks, ev.qty)
        if remaining > 0:
            self._insert_order(ev.side, ev.price_ticks, ev.order_id, remaining)
        return True

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

    def _apply_modify(self, ev: MarketEvent) -> bool:
        key = self._orders.get(ev.order_id)
        if key is None:
            self.unknown_order_events += 1
            return False
        if ev.price_ticks != 0 and ev.price_ticks != key[1]:
            self.modify_price_mismatch += 1  # price change must be CANCEL+ADD
            return False
        level = self._levels[key]
        old_qty = level.orders[ev.order_id]
        new_qty = ev.qty
        if new_qty <= 0:
            self._remove_order(ev.order_id)
            return True
        if new_qty <= old_qty:
            # Decrease: keep queue position.
            level.orders[ev.order_id] = new_qty
        else:
            if not _fits_i64(level.total_qty + (new_qty - old_qty)):
                self.invalid_payload_dropped += 1
                return False
            # Increase: move to tail of the level.
            del level.orders[ev.order_id]
            level.orders[ev.order_id] = new_qty
        level.total_qty += new_qty - old_qty
        return True

    def _apply_cancel(self, ev: MarketEvent) -> bool:
        if ev.order_id not in self._orders:
            self.unknown_order_events += 1
            return False
        self._remove_order(ev.order_id)
        return True

    def _apply_execute(self, ev: MarketEvent) -> bool:
        key = self._orders.get(ev.order_id)
        if key is None:
            self.unknown_order_events += 1
            return False
        level = self._levels[key]
        old_qty = level.orders[ev.order_id]
        fill = min(ev.qty, old_qty)
        if fill >= old_qty:
            self._remove_order(ev.order_id)
        else:
            level.orders[ev.order_id] = old_qty - fill
            level.total_qty -= fill
        return True

    def _clear_side(self, side: int) -> None:
        for key in [k for k in self._levels if k[0] == side]:
            for oid in list(self._levels[key].orders):
                self._remove_order(oid)

    def _apply_quote(self, ev: MarketEvent) -> bool:
        """FX QUOTE: replace this venue's whole side at L1."""
        side = ev.side
        oid = ev.order_id if ev.order_id != 0 else synthetic_order_id(side, 0)
        resting = self._orders.get(oid)
        if resting is not None and resting[0] != side:
            self.unknown_order_events += 1  # id rests on the other side
            return False
        self._clear_side(side)
        self._insert_order(side, ev.price_ticks, oid, ev.qty)
        return True

    def _apply_snapshot(self, ev: MarketEvent) -> bool:
        if self._snapshot_active:
            if ev.trade_id >= self._snapshot_countdown:
                # Countdown went up (or repeated): the previous burst was
                # interrupted and this record starts a new burst.
                self._snapshot_active = False
                self.snapshot_restarts += 1
            elif ev.trade_id != self._snapshot_countdown - 1:
                # Countdown skipped ahead: records of this burst are missing
                # (with or without a visible sequence gap) — burst broken.
                self._snapshot_broken = True
        if not self._snapshot_active:
            # Burst start: clear the whole book state (levels + orders).
            self._levels.clear()
            self._orders.clear()
            self._snapshot_active = True
            self._snapshot_broken = False
            self._snapshot_synthetic_next = [0, 0]
        self._snapshot_countdown = ev.trade_id
        if ev.order_id != 0:
            oid = ev.order_id
        else:
            ordinal = self._snapshot_synthetic_next[ev.side]
            self._snapshot_synthetic_next[ev.side] = ordinal + 1
            oid = synthetic_order_id(ev.side, ordinal)
        ok = True
        if oid in self._orders:
            self.unknown_order_events += 1  # repeated id inside a burst
            ok = False
        elif not _fits_i64(self._level_total(ev.side, ev.price_ticks) + ev.qty):
            self.invalid_payload_dropped += 1
            ok = False
        else:
            self._insert_order(ev.side, ev.price_ticks, oid, ev.qty)
        if ev.trade_id == 0:  # last record of the burst
            self._snapshot_active = False
            if not self._snapshot_broken:
                self.stale = False
            self._snapshot_broken = False
        return ok

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

    def pending_count(self) -> int:
        """Number of events currently held back in the reorder buffer."""
        return len(self._pending)

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

    def is_crossed(self) -> bool:
        """True when best bid > best ask (call phase / malformed feed)."""
        bb, ba = self.best_bid(), self.best_ask()
        return bb is not None and ba is not None and bb[0] > ba[0]

    def is_locked(self) -> bool:
        """True when best bid == best ask."""
        bb, ba = self.best_bid(), self.best_ask()
        return bb is not None and ba is not None and bb[0] == ba[0]

    def is_fresh(self, now_ns: int, max_age_ns: int) -> bool:
        """True if not stale and the last event was received within max_age_ns."""
        return (
            not self.stale
            and self.has_sequence
            and now_ns - self.receive_ts <= max_age_ns
        )

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

    def counters(self) -> dict:
        """All QC counters in pinned order (exact integers)."""
        return {name: getattr(self, name) for name in COUNTER_NAMES}

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
            "x-version": CHECKPOINT_VERSION,
            "instrument_id": self.instrument_id,
            "venue_id": self.venue_id,
            "levels": levels_out,
            # Global arrival order of resting orders (dict insertion order of
            # _orders) — restore() rebuilds it so resting_orders() round-trips
            # checkpoints exactly (levels alone only pin per-level FIFO).
            "arrival_order": list(self._orders),
            "last_sequence": self.last_sequence,
            "has_sequence": self.has_sequence,
            "sequence_epoch": self.sequence_epoch,
            "exchange_ts": self.exchange_ts,
            "receive_ts": self.receive_ts,
            "trade_flow": self.trade_flow,
            "status": self.status,
            "stale": self.stale,
            "snapshot_active": self._snapshot_active,
            "snapshot_broken": self._snapshot_broken,
            "snapshot_countdown": self._snapshot_countdown,
            "snapshot_synthetic_next": list(self._snapshot_synthetic_next),
            "reorder_window": self.reorder_window,
            "reorder_pending": [
                [getattr(self._pending[seq], f) for f in FIELDS]
                for seq in sorted(self._pending)
            ],
            "counters": self.counters(),
        }

    @classmethod
    def restore(cls, cp: dict) -> "OrderBook":
        """Rebuild an identical book from ``checkpoint()`` output."""
        if cp.get("x-version") != CHECKPOINT_VERSION:
            raise ValueError(
                f"unsupported book checkpoint x-version: {cp.get('x-version')!r}"
            )
        book = cls(cp["instrument_id"], cp["venue_id"], cp["reorder_window"])
        for lvl in cp["levels"]:
            if lvl["side"] not in (0, 1):
                raise ValueError(f"invalid side {lvl['side']} in checkpoint level")
            for oid, qty in lvl["orders"]:
                if oid in book._orders:
                    raise ValueError(f"duplicate order_id {oid} in checkpoint")
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
        book.has_sequence = cp["has_sequence"]
        book.sequence_epoch = cp["sequence_epoch"]
        book.exchange_ts = cp["exchange_ts"]
        book.receive_ts = cp["receive_ts"]
        book.trade_flow = cp["trade_flow"]
        book.status = cp["status"]
        book.stale = cp["stale"]
        book._snapshot_active = cp["snapshot_active"]
        book._snapshot_broken = cp["snapshot_broken"]
        book._snapshot_countdown = cp["snapshot_countdown"]
        nxt = cp["snapshot_synthetic_next"]
        if len(nxt) != 2:
            raise ValueError("checkpoint snapshot_synthetic_next must have 2 entries")
        book._snapshot_synthetic_next = [nxt[0], nxt[1]]
        pending = cp["reorder_pending"]
        if len(pending) > book.reorder_window:
            raise ValueError("checkpoint reorder_pending exceeds reorder_window")
        for row in pending:
            if len(row) != len(FIELDS):
                raise ValueError("checkpoint reorder_pending row must have 12 fields")
            ev = MarketEvent(*row)
            if ev.sequence in book._pending:
                raise ValueError(f"duplicate pending sequence {ev.sequence} in checkpoint")
            book._pending[ev.sequence] = ev
        c = cp["counters"]
        for name in COUNTER_NAMES:
            setattr(book, name, c[name])
        return book


class ConsolidatedBook:
    """Consolidated view over per-venue books of one instrument.

    Routes events to per-venue books by venue_id and merges derived state
    over the NON-STALE venues only (pinned): same price across venues =>
    sizes and order counts summed; best = best across venues. A venue whose
    book is stale (sequence gap not yet recovered) contributes nothing until
    a complete SNAPSHOT burst recovers it. Sequence/staleness/status remain
    per venue (``books[venue_id]``).
    """

    __slots__ = ("instrument_id", "reorder_window", "books")

    def __init__(self, instrument_id: int, reorder_window: int = 0) -> None:
        self.instrument_id = instrument_id
        self.reorder_window = reorder_window
        self.books: Dict[int, OrderBook] = {}

    def venue_book(self, venue_id: int) -> OrderBook:
        """Get (or lazily create) the per-venue book."""
        book = self.books.get(venue_id)
        if book is None:
            book = OrderBook(self.instrument_id, venue_id, self.reorder_window)
            self.books[venue_id] = book
        return book

    def apply(self, ev: MarketEvent) -> ApplyStatus:
        """Route one event to its venue book; returns the pinned verdict."""
        return self.venue_book(ev.venue_id).apply(ev)

    def reset_sequences(self) -> None:
        """Explicit sequence reset on every venue book (session roll)."""
        for vid in sorted(self.books):
            self.books[vid].reset_sequence()

    def active_venues(self) -> List[int]:
        """Sorted venue ids whose books are not stale (merged into the view)."""
        return [vid for vid in sorted(self.books) if not self.books[vid].stale]

    def stale_venues(self) -> List[int]:
        """Sorted venue ids whose books are stale (excluded from the view)."""
        return [vid for vid in sorted(self.books) if self.books[vid].stale]

    def venue_status(self, venue_id: int) -> Optional[int]:
        """SessionStatus code of one venue's book (None if the venue is unknown)."""
        book = self.books.get(venue_id)
        return book.status if book is not None else None

    def _merged(self, side: int) -> List[Tuple[int, int, int]]:
        agg: Dict[int, List[int]] = {}
        for vid in sorted(self.books):
            book = self.books[vid]
            if book.stale:
                continue
            for level in book._sorted_levels(side):
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

    def is_crossed(self) -> bool:
        """True when the merged best bid > merged best ask."""
        bb, ba = self.best_bid(), self.best_ask()
        return bb is not None and ba is not None and bb[0] > ba[0]

    def is_locked(self) -> bool:
        """True when the merged best bid == merged best ask."""
        bb, ba = self.best_bid(), self.best_ask()
        return bb is not None and ba is not None and bb[0] == ba[0]

    def depth(self, side: int, levels: int = DEPTH_LEVELS) -> List[Tuple[int, int]]:
        return [(p, sq) for p, sq, _ in self._merged(side)[:levels]]

    def order_count(self, side: int, levels: int = DEPTH_LEVELS) -> List[Tuple[int, int]]:
        return [(p, oc) for p, _, oc in self._merged(side)[:levels]]

    def trade_flow(self) -> int:
        """Sum of per-venue cumulative signed trade flow (all venues), saturated to i64."""
        total = sum(b.trade_flow for _, b in sorted(self.books.items()))
        return max(I64_MIN, min(I64_MAX, total))

    def consolidated_summary(self) -> dict:
        """Golden-comparable merged view (non-stale venues only)."""
        bb, ba = self.best_bid(), self.best_ask()
        return {
            "best_bid": list(bb) if bb else None,
            "best_ask": list(ba) if ba else None,
            "depth_bid_top5": [list(t) for t in self.depth(Side.BID, 5)],
            "depth_ask_top5": [list(t) for t in self.depth(Side.ASK, 5)],
            "is_crossed": self.is_crossed(),
            "is_locked": self.is_locked(),
            "active_venues": self.active_venues(),
            "trade_flow": self.trade_flow(),
        }

    def checkpoint(self) -> dict:
        return {
            "instrument_id": self.instrument_id,
            "reorder_window": self.reorder_window,
            "venues": {str(vid): b.checkpoint() for vid, b in sorted(self.books.items())},
        }

    @classmethod
    def restore(cls, cp: dict) -> "ConsolidatedBook":
        cons = cls(cp["instrument_id"], cp["reorder_window"])
        for vid, bcp in cp["venues"].items():
            cons.books[int(vid)] = OrderBook.restore(bcp)
        return cons
