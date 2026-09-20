"""Event-driven execution simulator (spec sections 17-18).

Python reference port of ``cpp/src/execution/execution.cpp``; the nine
pinned rules in ``cpp/include/iap/execution/execution.hpp`` are the
contract (PLATFORM_CONVENTIONS.md section 11.2) and are restated here so
this module is self-contained:

1. **Latency.** A child decided at ``decision_ts`` arrives at
   ``decision_ts + decision_ns + risk_ns + wire_ns + venue.latency_mean_ns
   + jitter`` with ``jitter = SplitMix64(seed).below(venue.latency_jitter_ns
   + 1)`` — one draw per submitted order OR cancel, in submission order.
2. **Activation.** A pending order activates while processing the first
   market event with ``exchange_ts >= arrival_ts``, BEFORE that event is
   applied to the books; orders activate in ``(arrival_ts, order_id)``
   order. Aggressive fills are stamped with ``arrival_ts``.
3. **Aggressive execution** (MARKET and the marketable part of LIMIT/IOC/
   FOK) walks the DISPLAYED top-10 opposite depth of the target venue, best
   first, up to the limit (MARKET: unconstrained), one Fill per level.
   Simulated fills never mutate the replayed book. Unfilled MARKET/IOC
   remainders are cancelled (``UNFILLED_REMAINDER``); FOK fills fully or
   not at all (checked against displayed depth within the limit first).
3b. **Displayed-liquidity consumption.** A per-(instrument, venue, side,
   price) overlay records the displayed size our aggressive fills already
   consumed; walks see ``displayed - consumed`` and debit it. When an
   applied event changes a level's displayed size, its overlay entry
   becomes ``min(consumed, new displayed)`` (0 removes it).
4. **Passive queue position.** Governing principle: the simulator never
   fills more than the market actually traded, and our own resting orders
   queue behind each other. A resting LIMIT remainder at ``P`` starts with
   ``ahead_qty`` = displayed qty at ``(side, P)`` PLUS the sum of
   ``remaining`` over our own still-ACTIVE orders already resting at that
   exact ``(venue, side, P)``. On that venue, ONE observed trade of
   ``qty`` at ``(side, price)`` is ONE pool of liquidity, budgeted once
   across the resting orders it reaches, visited in queue order (the
   ``_resting`` order = pinned activation order: ascending arrival_ts,
   then ascending order_id): each order pays down its queue
   (``dec = min(ahead_qty, budget)``) and then, while budget remains,
   fills ``min(budget, remaining)`` at ITS OWN limit. A trade reaches an
   order when it prints AT its limit or STRICTLY WORSE (below our bid /
   above our ask — the market traded THROUGH us); a trade-through fills at
   OUR limit but is bounded by the observed volume, not a free fill of the
   whole residual. A CANCEL at ``(side, P)`` depletes ``ahead_qty`` by its
   full qty (floored at 0) and never fills us; a marketable ADD is
   expanded into the per-level volumes it consumes over the pre-event
   displayed depth, each level's volume being its own pool; after the
   event is applied, an opposite best crossing ``P`` fills us at ``P``,
   bounded by the DISPLAYED size of that crossing level (one pool per
   side, ``ahead_qty`` consumed first) — EXEMPT while the display still
   shows the liquidity our own aggressive leg consumed, until the display
   first shows an uncrossed opposite best; MODIFY never changes
   ``ahead_qty``. Passive fills are stamped with the triggering event's
   ``exchange_ts``.
5. **Fees.** Equity: ``taker_fee_per_share * qty`` (taker), ``-
   maker_rebate_per_share * qty`` (maker). FX: ``commission_per_million *
   notional / 1e6`` on every fill, ``notional = qty * qty_unit *
   price_ticks * tick_size``.
6. **Linear impact** (taker fills only, identical to ``iap.backtest.costs``):
   ``impact_bps = coeff * (child_qty * qty_unit / adv * 100)``; each taker
   fill is charged ``impact_bps * 1e-4 * its own notional``.
7. **Cancels** travel the same latency path (one jitter draw) and take
   effect at ``max(cancel arrival, order arrival)`` while processing the
   first event at/after that time, merged with activations in timestamp
   order (activation first on ties). ``expire_ts != 0`` expires pending or
   resting orders at the first event with ``exchange_ts >= expire_ts``,
   before any activation. ``cancel_all()`` is the end-of-stream sweep.
8. **Venue trading-state gate.** While the target venue's book is
   missing, stale or not TRADING no fill of any kind is produced: MARKET/
   IOC/FOK arrivals are cancelled ``VENUE_NOT_TRADING``, a LIMIT rests
   without executing, resting orders are not consumed and the crossing
   check is skipped. On the first event after which the venue is open
   again, every resting order crossed by the post-event opposite best
   fills at the TOUCH price — bounded by the same displayed-size pool and
   ``ahead_qty`` consumption as the rule-4 crossing check.
9. **Processing order.** Expiries, then activations and cancel arrivals
   merged by time, then passive queue tracking on the raw event, then the
   book update, then the overlay reset, then the post-apply crossing check.

Deterministic: same config + seed => identical fills, bit for bit
(SplitMix64 only, no wall clock, no unordered iteration).
"""

from __future__ import annotations

import bisect
from dataclasses import replace
from typing import Dict, List, Optional, Tuple

from iap.core.events import EventType, MarketEvent, SessionStatus
from iap.core.rng import SplitMix64
from iap.execution.config import ExecConfig
from iap.execution.types import (
    CancelReason,
    ChildOrder,
    ExecCounters,
    Fill,
    InstrumentSpec,
    Liquidity,
    OrderState,
    OrderType,
    VenueSpec,
)
from iap.orderbook.book import DEPTH_LEVELS, ConsolidatedBook, OrderBook

#: Overlay key: (instrument_id, venue_id, side, price_ticks).
OverlayKey = Tuple[int, int, int, int]


class ExecutionSimulator:
    """Simulates child-order lifecycles against the replayed market stream."""

    def __init__(self, config: ExecConfig) -> None:
        self._config = config
        self._rng = SplitMix64(config.seed)
        self._books: Dict[int, ConsolidatedBook] = {}
        self._orders: Dict[int, ChildOrder] = {}
        # (arrival_ts, order_id), sorted — the activation queue (rule 2).
        self._pending: List[Tuple[int, int]] = []
        # ACTIVE order ids in activation order (rule 4 tracking order).
        self._resting: List[int] = []
        # (cancel effective ts, order_id), sorted (rule 7).
        self._cancels: List[Tuple[int, int]] = []
        # Rule 3b overlay: consumed displayed size per level.
        self._consumed: Dict[OverlayKey, int] = {}
        self._counters = ExecCounters()
        self._fills: List[Fill] = []
        self._next_order_id = 1
        self._next_fill_id = 1

    # ------------------------------------------------------------ accessors

    @property
    def config(self) -> ExecConfig:
        return self._config

    @property
    def counters(self) -> ExecCounters:
        return self._counters

    @property
    def fills(self) -> List[Fill]:
        """Every fill emitted so far, in emission (fill_id) order."""
        return self._fills

    @property
    def orders(self) -> Dict[int, ChildOrder]:
        """All submitted orders keyed by order_id (ascending insertion order)."""
        return self._orders

    def venue_book(self, instrument_id: int, venue_id: int) -> Optional[OrderBook]:
        """Venue book for (instrument, venue); None before any event touched it."""
        cons = self._books.get(instrument_id)
        return None if cons is None else cons.books.get(venue_id)

    def instrument_book(self, instrument_id: int) -> ConsolidatedBook:
        """Consolidated book of an instrument (created on first use)."""
        cons = self._books.get(instrument_id)
        if cons is None:
            cons = ConsolidatedBook(instrument_id)
            self._books[instrument_id] = cons
        return cons

    @staticmethod
    def venue_open(book: Optional[OrderBook]) -> bool:
        """True when the venue book exists, is not stale and is TRADING (rule 8)."""
        return (
            book is not None
            and not book.stale
            and book.status == int(SessionStatus.TRADING)
        )

    def _venue(self, venue_id: int) -> VenueSpec:
        return self._config.venue(venue_id)

    def _instrument(self, instrument_id: int) -> InstrumentSpec:
        return self._config.instrument(instrument_id)

    # ---------------------------------------------------------- order entry

    def _latency_to(self, venue: VenueSpec, decision_ts: int) -> int:
        """Rule 1 / rule 7 arrival time: one jitter draw per call."""
        jitter = (
            self._rng.below(venue.latency_jitter_ns + 1)
            if venue.latency_jitter_ns > 0 else 0
        )
        return (
            decision_ts
            + self._config.latency.internal_ns
            + venue.latency_mean_ns
            + jitter
        )

    def submit(self, child: ChildOrder) -> int:
        """Submit a child order (decision-time semantics, rule 1); returns its order_id.

        The caller's record is not modified: the simulator keeps its own copy
        with the runtime state reset.
        """
        if child.qty <= 0:
            raise ValueError("child qty must be > 0")
        if child.side not in (0, 1):
            raise ValueError("child side must be 0/1")
        if child.type != OrderType.MARKET and child.limit_ticks <= 0:
            raise ValueError("non-MARKET child needs a limit price")
        if child.expire_ts < 0:
            raise ValueError("expire_ts must be >= 0")
        venue = self._venue(child.venue_id)
        order_id = self._next_order_id
        self._next_order_id += 1
        o = replace(
            child,
            order_id=order_id,
            arrival_ts=self._latency_to(venue, child.decision_ts),
            state=OrderState.PENDING,
            remaining=child.qty,
            ahead_qty=0,
            resting=False,
            cross_exempt=False,
            cancel_reason=CancelReason.NONE,
            cancel_arrival_ts=0,
        )
        self._orders[order_id] = o
        bisect.insort(self._pending, (o.arrival_ts, order_id))
        return order_id

    def cancel(self, order_id: int, cancel_ts: int) -> None:
        """Request a cancel at decision time ``cancel_ts`` (rule 7).

        No-op for terminal orders or when a cancel is already in flight;
        raises ValueError on an unknown id.
        """
        o = self._orders.get(order_id)
        if o is None:
            raise ValueError(f"unknown order_id {order_id}")
        if o.is_terminal or o.cancel_arrival_ts != 0:
            return
        # Same latency path (and jitter stream) as a submit; a cancel never
        # overtakes its own order.
        arrival = self._latency_to(self._venue(o.venue_id), cancel_ts)
        o.cancel_arrival_ts = max(arrival, o.arrival_ts)
        bisect.insort(self._cancels, (o.cancel_arrival_ts, order_id))

    def cancel_all(self) -> None:
        """Cancel every non-terminal order (end of stream, immediate)."""
        while self._pending:
            self._terminate(self._orders[self._pending[0][1]], CancelReason.END_OF_STREAM)
        while self._resting:
            self._terminate(self._orders[self._resting[0]], CancelReason.END_OF_STREAM)
        self._cancels.clear()

    # ----------------------------------------------------------- lifecycle

    def _terminate(self, o: ChildOrder, reason: CancelReason) -> None:
        o.state = OrderState.CANCELLED
        o.cancel_reason = reason
        o.resting = False
        self._pending = [e for e in self._pending if e[1] != o.order_id]
        self._resting = [i for i in self._resting if i != o.order_id]
        self._cancels = [e for e in self._cancels if e[1] != o.order_id]

    def _fill_fee(
        self, o: ChildOrder, price_ticks: int, qty: int, liq: Liquidity
    ) -> float:
        """Rule 5."""
        v = self._venue(o.venue_id)
        if v.is_fx:
            ins = self._instrument(o.instrument_id)
            notional = float(qty) * ins.qty_unit * float(price_ticks) * ins.tick_size
            return v.commission_per_million * notional / 1e6
        if liq == Liquidity.TAKER:
            return v.taker_fee_per_share * float(qty)
        return -v.maker_rebate_per_share * float(qty)

    def _emit_fill(
        self, o: ChildOrder, price_ticks: int, qty: int, ts: int, liq: Liquidity
    ) -> None:
        impact_cost = 0.0
        if liq == Liquidity.TAKER:
            # Rule 6: linear impact from the child's total size in base units.
            ins = self._instrument(o.instrument_id)
            impact_bps = self._config.impact_coeff_bps_per_pct_adv * (
                float(o.qty) * ins.qty_unit / ins.adv * 100.0
            )
            notional = float(qty) * ins.qty_unit * float(price_ticks) * ins.tick_size
            impact_cost = impact_bps * 1e-4 * notional
        self._fills.append(
            Fill(
                fill_id=self._next_fill_id,
                order_id=o.order_id,
                parent_id=o.parent_id,
                instrument_id=o.instrument_id,
                venue_id=o.venue_id,
                side=o.side,
                price_ticks=price_ticks,
                qty=qty,
                ts=ts,
                liquidity=liq,
                fee=self._fill_fee(o, price_ticks, qty, liq),
                impact_cost=impact_cost,
            )
        )
        self._next_fill_id += 1
        o.remaining -= qty
        if o.remaining == 0:
            o.state = OrderState.FILLED
            o.resting = False
            self._cancels = [e for e in self._cancels if e[1] != o.order_id]

    def _consumed_at(
        self, instrument_id: int, venue_id: int, side: int, price_ticks: int
    ) -> int:
        return self._consumed.get((instrument_id, venue_id, side, price_ticks), 0)

    def _aggressive_fill(self, o: ChildOrder, book: OrderBook) -> None:
        """Rule 3 / 3b: walk the displayed opposite depth net of the overlay."""
        opp_side = 1 if o.side == 0 else 0
        depth = book.depth(opp_side, DEPTH_LEVELS)

        def within_limit(price: int) -> bool:
            if o.type == OrderType.MARKET:
                return True
            return price <= o.limit_ticks if o.side == 0 else price >= o.limit_ticks

        def available(price: int, displayed: int) -> int:
            c = self._consumed_at(o.instrument_id, o.venue_id, opp_side, price)
            return max(displayed - c, 0)

        if o.type == OrderType.FOK:
            avail = 0
            for p, q in depth:
                if within_limit(p):
                    avail += available(p, q)
            if avail < o.remaining:
                return  # all-or-none: no fills at all
        for p, q in depth:
            if o.remaining == 0:
                break
            if not within_limit(p):
                break  # levels are sorted best-first
            avail = available(p, q)
            if avail < q:
                self._counters.overlay_thinned_fills += 1
            if avail <= 0:
                continue
            take = min(o.remaining, avail)
            key = (o.instrument_id, o.venue_id, opp_side, p)
            self._consumed[key] = self._consumed.get(key, 0) + take
            self._emit_fill(o, p, take, o.arrival_ts, Liquidity.TAKER)

    def _activate(self, o: ChildOrder) -> None:
        """Rule 2 (with rules 3 and 8) for one arrived order."""
        book = self.venue_book(o.instrument_id, o.venue_id)
        is_open = self.venue_open(book)
        if is_open:
            self._aggressive_fill(o, book)
        if o.remaining == 0:
            return  # fully filled aggressively
        if o.type == OrderType.LIMIT:
            # Rest passively: queue position = displayed qty at our level.
            o.state = OrderState.ACTIVE
            o.resting = True
            o.ahead_qty = book.level_qty(o.side, o.limit_ticks) if book is not None else 0
            # Rule 4: our own children already resting at this exact
            # (venue, side, price) are ahead of us in the FIFO queue.
            # Without this a later sibling would be handed the very
            # liquidity its earlier sibling is still queued for.
            for oid in self._resting:
                ahead = self._orders[oid]
                if (
                    ahead.state == OrderState.ACTIVE
                    and ahead.instrument_id == o.instrument_id
                    and ahead.venue_id == o.venue_id
                    and ahead.side == o.side
                    and ahead.limit_ticks == o.limit_ticks
                ):
                    o.ahead_qty += ahead.remaining
            # Crossing exemption: the display may still show the liquidity
            # our aggressive leg just consumed (rule 4). While the venue is
            # gated (rule 8) nothing was consumed: no exemption.
            o.cross_exempt = False
            if is_open:
                opp = book.best_ask() if o.side == 0 else book.best_bid()
                o.cross_exempt = opp is not None and (
                    opp[0] <= o.limit_ticks if o.side == 0 else opp[0] >= o.limit_ticks
                )
            self._resting.append(o.order_id)
            return
        # MARKET / IOC / FOK: the unfilled remainder is cancelled (rule 3 / 8).
        if is_open:
            self._terminate(o, CancelReason.UNFILLED_REMAINDER)
        else:
            self._counters.venue_not_trading_cancels += 1
            self._terminate(o, CancelReason.VENUE_NOT_TRADING)

    def _apply_cancel_arrival(self, o: ChildOrder) -> None:
        if o.is_terminal:
            return
        self._counters.user_cancels += 1
        self._terminate(o, CancelReason.USER)

    def _expire_due(self, t: int) -> None:
        """Rule 7: time-in-force, pending or resting, before any activation."""
        due: List[int] = []
        for _, oid in self._pending:
            o = self._orders[oid]
            if o.expire_ts != 0 and o.expire_ts <= t:
                due.append(oid)
        for oid in self._resting:
            o = self._orders[oid]
            if o.expire_ts != 0 and o.expire_ts <= t:
                due.append(oid)
        for oid in sorted(due):
            self._counters.expired_orders += 1
            self._terminate(self._orders[oid], CancelReason.EXPIRED)

    def _activate_and_cancel_due(self, t: int) -> None:
        """Rules 2 + 7: activations and cancel arrivals merged by time."""
        while True:
            have_act = bool(self._pending) and self._pending[0][0] <= t
            have_cxl = bool(self._cancels) and self._cancels[0][0] <= t
            if not have_act and not have_cxl:
                break
            do_act = have_act
            if have_act and have_cxl:
                do_act = self._pending[0][0] <= self._cancels[0][0]
            if do_act:
                _, oid = self._pending.pop(0)
                self._activate(self._orders[oid])
            else:
                _, oid = self._cancels.pop(0)
                self._apply_cancel_arrival(self._orders[oid])

    def _track_consumption(
        self,
        instrument_id: int,
        venue_id: int,
        side: int,
        price_ticks: int,
        qty: int,
        ts: int,
    ) -> None:
        """Rule 4: one observed trade of ``qty`` at one level, budgeted once.

        Handing each resting order the full ``qty`` would manufacture
        liquidity that never traded — n children at one level would each
        fill from the same print. Queue order is ``self._resting`` order,
        which is the pinned activation order (ascending arrival_ts, then
        ascending order_id): the earlier child is served first, as on a
        FIFO book.
        """
        budget = qty
        i = 0
        while i < len(self._resting):
            o = self._orders[self._resting[i]]
            if (
                budget > 0
                and o.instrument_id == instrument_id
                and o.venue_id == venue_id
                and o.side == side
                and o.state == OrderState.ACTIVE
            ):
                # A trade reaches us when it prints AT our limit, or STRICTLY
                # WORSE than it (below our bid / above our ask) — the market
                # traded through our level, so we must have been hit first.
                # Both fill at OUR limit (we never get price improvement) and
                # both are capped by the observed volume.
                through = (
                    price_ticks < o.limit_ticks if o.side == 0
                    else price_ticks > o.limit_ticks
                )
                if price_ticks == o.limit_ticks or through:
                    dec = min(o.ahead_qty, budget)
                    o.ahead_qty -= dec
                    budget -= dec
                    if budget > 0:
                        fill = min(budget, o.remaining)
                        self._emit_fill(o, o.limit_ticks, fill, ts, Liquidity.MAKER)
                        budget -= fill
            if o.state == OrderState.FILLED:
                del self._resting[i]
            else:
                i += 1

    def _crossing_check(self, ev: MarketEvent, book: OrderBook, reopened: bool) -> None:
        """Rule 4 last bullet / rule 8 reopen, against the post-event book.

        No trade was observed here, only a crossed display, so the pool is
        the DISPLAYED size of the crossing opposite best (the most an
        incoming aggressor could have brought): one pool per side, shared by
        every order of ours resting against it, in ``_track_consumption``
        queue order.
        """
        t = ev.exchange_ts
        best_ask = book.best_ask()
        best_bid = book.best_bid()
        # Indexed by OUR side: a buy crosses against the ask, a sell the bid.
        budget = [
            0 if best_ask is None else best_ask[1],
            0 if best_bid is None else best_bid[1],
        ]
        i = 0
        while i < len(self._resting):
            o = self._orders[self._resting[i]]
            if (
                o.instrument_id == ev.instrument_id
                and o.venue_id == ev.venue_id
                and o.state == OrderState.ACTIVE
            ):
                opp = best_ask if o.side == 0 else best_bid
                crossed = opp is not None and (
                    opp[0] <= o.limit_ticks if o.side == 0 else opp[0] >= o.limit_ticks
                )
                if not crossed:
                    o.cross_exempt = False  # display uncrossed: exemption ends
                elif reopened or not o.cross_exempt:
                    # The queue ahead of us would have been served by that
                    # same aggressor first.
                    dec = min(o.ahead_qty, budget[o.side])
                    o.ahead_qty -= dec
                    budget[o.side] -= dec
                    if budget[o.side] > 0:
                        fill = min(budget[o.side], o.remaining)
                        # Rule 8 uncrosses AT THE TOUCH; the rule-4 crossing
                        # fills at our own limit (no price improvement).
                        self._emit_fill(
                            o, opp[0] if reopened else o.limit_ticks, fill, t,
                            Liquidity.MAKER,
                        )
                        budget[o.side] -= fill
                        if reopened:
                            self._counters.reopen_touch_fills += 1
            if o.state == OrderState.FILLED:
                del self._resting[i]
            else:
                i += 1

    # -------------------------------------------------------------- events

    def on_event(self, ev: MarketEvent) -> None:
        """Process one market event in the pinned rule-9 order."""
        t = ev.exchange_ts

        # 1. Expiries, then activations + cancel arrivals (rules 7, 2),
        #    against the pre-event book state.
        self._expire_due(t)
        self._activate_and_cancel_due(t)

        # 2. Passive queue tracking on the raw event (rule 4), before the
        #    book is mutated — only while the venue is open (rule 8).
        et = ev.event_type
        pre = self.venue_book(ev.instrument_id, ev.venue_id)
        pre_open = self.venue_open(pre)
        if self._resting and pre_open:
            if et == EventType.EXECUTE:
                self._track_consumption(
                    ev.instrument_id, ev.venue_id, ev.side, ev.price_ticks, ev.qty, t
                )
            elif et == EventType.CANCEL:
                for oid in self._resting:
                    o = self._orders[oid]
                    if (
                        o.instrument_id == ev.instrument_id
                        and o.venue_id == ev.venue_id
                        and o.side == ev.side
                        and ev.price_ticks == o.limit_ticks
                        and o.state == OrderState.ACTIVE
                    ):
                        o.ahead_qty -= min(o.ahead_qty, ev.qty)
            elif et == EventType.ADD:
                # Marketable-ADD expansion (rule 4): the replayed book matches
                # a crossing ADD internally without EXECUTE events; walk the
                # pre-event displayed opposite depth and track the consumption.
                consumed_side = 1 if ev.side == 0 else 0
                incoming = ev.qty
                for p, q in pre.depth(consumed_side, DEPTH_LEVELS):
                    if incoming <= 0:
                        break
                    crosses = p <= ev.price_ticks if ev.side == 0 else p >= ev.price_ticks
                    if not crosses:
                        break
                    consumed = min(incoming, q)
                    self._track_consumption(
                        ev.instrument_id, ev.venue_id, consumed_side, p, consumed, t
                    )
                    incoming -= consumed

        # 3. Snapshot the displayed sizes behind this venue's overlay entries,
        #    apply the event, then cap the entries whose display changed at
        #    the new displayed size (rule 3b).
        watched: List[Tuple[OverlayKey, int]] = []
        for key in self._consumed:
            if key[0] == ev.instrument_id and key[1] == ev.venue_id:
                before = 0 if pre is None else pre.level_qty(key[2], key[3])
                watched.append((key, before))
        self.instrument_book(ev.instrument_id).apply(ev)
        book = self.venue_book(ev.instrument_id, ev.venue_id)
        for key, before in watched:
            after = 0 if book is None else book.level_qty(key[2], key[3])
            if after != before:
                consumed = self._consumed.get(key)
                if consumed is None:
                    continue
                consumed = min(consumed, after)
                if consumed <= 0:
                    del self._consumed[key]
                else:
                    self._consumed[key] = consumed

        # 4. Post-apply crossing check (rule 4 last bullet / rule 8 reopen).
        if self.venue_open(book):
            self._crossing_check(ev, book, not pre_open)
