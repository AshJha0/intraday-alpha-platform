"""Execution replay — the event-driven backtest driver (spec section 18).

Python reference port of ``cpp/include/iap/replay/exec_replay.hpp`` +
``exec_replay.cpp``. Consumes the normalized event stream in file order
(event time), drives the ``ExecutionSimulator``'s books and order
lifecycle, and works a set of parent orders through their TWAP/VWAP/POV/IS
schedules (``iap.execution.algos``). Same events + config + seed =>
identical fills, bit for bit; the pinned output is
``tests/golden/expected_replay_fills.json``.

Per event, in pinned order:

1. the simulator processes the event (child activation, passive queue
   tracking, book application, crossing checks — simulator rules);
2. the scheduler then evaluates every parent against the post-event state:
   due TWAP/VWAP/IS slices are issued (``decision_ts`` = the event's
   ``exchange_ts``, limit prices read from the just-updated book), and POV
   targets are re-evaluated after TRADE events of the parent's instrument
   inside its window.

Children expire at their parent's ``end_ts`` (simulator rule 7); after the
last event every still-unfinished child is cancelled (unfilled residual =
opportunity cost, reported per parent). Every fill attributed to a parent
lies inside ``[start_ts, end_ts]`` by construction — a fill outside it is
a RuntimeError.

Parent accounting identity (tested): ``total_cost = fees - rebates +
impact`` where fees/rebates/impact are exact sums over the parent's fills.

Execution policies (``ParentOrder.policy``, ``iap.execution.passive``): the
steps above describe ``NATIVE``. ``AGGRESSIVE`` sends every child as a
MARKET order. ``PASSIVE`` evaluates, per parent and per event, in pinned
order: (a) the schedule state at the event (slices come due / POV volume),
(b) the state machine of every posted child in posting order — cancel
remainders that took effect are re-posted or crossed, deadlines and the
BEHIND test are checked — and (c) the new schedule steps are posted. The
pinned output is ``tests/golden/expected_replay_fills_passive.json``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from iap.core.events import EventType, MarketEvent
from iap.execution.algos import AlgoType, ParentOrder, slice_quantities, slice_times
from iap.execution.config import ExecConfig, SorOptions
from iap.execution.passive import (
    ExecPolicy,
    PassiveStats,
    max_behind_qty,
    patience_ns,
    post_price,
)
from iap.execution.simulator import ExecutionSimulator
from iap.execution.sor import NO_ROUTE, SmartOrderRouter
from iap.execution.types import CancelReason, ChildOrder, Fill, OrderState, OrderType

#: States of a posted child in the PASSIVE state machine.
_REST = 0
_CANCEL_REPRICE = 1
_CANCEL_CROSS_TIMEOUT = 2
_CANCEL_CROSS_BEHIND = 3
_DONE = 4


@dataclass(slots=True)
class ParentReport:
    """Per-parent fill accounting."""

    parent_id: int = 0
    filled_qty: int = 0
    unfilled_qty: int = 0
    children: int = 0
    notional: float = 0.0  #: sum of fill qty * price * tick * lot
    avg_price: float = 0.0  #: notional / (filled qty * lot); 0 if unfilled
    fees: float = 0.0  #: taker fees (>= 0)
    rebates: float = 0.0  #: maker rebates (>= 0)
    impact: float = 0.0  #: linear impact charges (>= 0)
    total_cost: float = 0.0  #: fees - rebates + impact

    def to_dict(self) -> dict:
        """Row in the key order of ``expected_replay_fills.json`` ``parents``."""
        return {
            "filled_qty": self.filled_qty,
            "unfilled_qty": self.unfilled_qty,
            "children": self.children,
            "notional": self.notional,
            "avg_price": self.avg_price,
            "fees": self.fees,
            "rebates": self.rebates,
            "impact": self.impact,
            "total_cost": self.total_cost,
        }


@dataclass(slots=True)
class ExecReplayResult:
    """Outcome of one ``ExecutionReplay.run``."""

    fills: list[Fill] = field(default_factory=list)
    parents: dict[int, ParentReport] = field(default_factory=dict)
    events_processed: int = 0
    sor_no_route: int = 0  #: children not submitted: no eligible venue
    #: PASSIVE transition counters, one row per PASSIVE parent (parent_id order).
    passive: dict[int, PassiveStats] = field(default_factory=dict)
    #: every submitted child in its final state, by parent_id, submission order.
    children: dict[int, list[ChildOrder]] = field(default_factory=dict)


@dataclass(slots=True)
class _Working:
    """One posted child of a PASSIVE parent."""

    order_id: int
    deadline: int
    reprices: int
    state: int = _REST


@dataclass(slots=True)
class _ParentState:
    order: ParentOrder
    slice_qty: list[int] = field(default_factory=list)  #: TWAP/VWAP/IS
    slice_due: list[int] = field(default_factory=list)  #: TWAP/VWAP/IS
    next_slice: int = 0
    filled_qty: int = 0  #: fills booked so far
    pov_volume: int = 0  #: window TRADE volume (POV)
    child_ids: list[int] = field(default_factory=list)
    # ---- PASSIVE policy state ----
    scheduled_qty: int = 0  #: qty the schedule has called for so far
    current_step_qty: int = 0  #: q_cur: the most recent slice (0 for POV)
    patience_ns: int = 0
    behind_qty: int = 0  #: BEHIND tolerance in qty
    working: list[_Working] = field(default_factory=list)
    stats: PassiveStats = field(default_factory=PassiveStats)


class ExecutionReplay:
    """Works parent orders through a replayed event stream (one-shot)."""

    def __init__(
        self,
        config: ExecConfig,
        parents: Sequence[ParentOrder],
        sor_options: SorOptions = SorOptions(),  # noqa: B008 (frozen dataclass default, one shared immutable instance is intended)
    ) -> None:
        self._config = config
        self._sim = ExecutionSimulator(config)
        self._sor = SmartOrderRouter(config.venues, sor_options)
        self._sor_candidates: list[int] = sorted(config.venues)
        self._parents: list[_ParentState] = []
        self._fills_booked = 0
        self._sor_no_route = 0
        self._ran = False
        for p in parents:
            if p.qty <= 0:
                raise ValueError("parent qty must be > 0")
            if p.max_child_qty <= 0:
                raise ValueError("max_child_qty must be > 0")
            if p.end_ts <= p.start_ts:
                raise ValueError("parent window must have end_ts > start_ts")
            ps = _ParentState(order=p)
            if p.algo != AlgoType.POV:
                ps.slice_qty = slice_quantities(p)
                ps.slice_due = slice_times(p)
            if p.policy == ExecPolicy.PASSIVE:
                ps.patience_ns = patience_ns(
                    p.passive, p.urgency, p.algo == AlgoType.IS, p.risk_aversion
                )
                ps.behind_qty = max_behind_qty(p.passive, p.qty)
            self._parents.append(ps)

    @property
    def simulator(self) -> ExecutionSimulator:
        return self._sim

    def _issue_child(
        self,
        ps: _ParentState,
        child_qty: int,
        decision_ts: int,
        passive: bool,
        post: bool = False,
    ) -> bool:
        """Issue one child of at most ``max_child_qty``; False when unroutable.

        ``passive`` joins the same-side best; with ``post`` the limit is the
        PASSIVE policy's ``post_price`` instead (MARKET fallback either way).
        """
        if child_qty <= 0:
            return True
        p = ps.order
        book = self._sim.instrument_book(p.instrument_id)
        if p.venue_id != 0:
            venue_id = p.venue_id
        elif passive:
            venue_id = self._sor.route_passive(book, p.side, self._sor_candidates)
        else:
            venue_id = self._sor.route_aggressive(book, p.side, self._sor_candidates)
        if venue_id == NO_ROUTE:
            self._sor_no_route += 1  # no eligible venue: do not submit (pinned)
            return False
        order_type = OrderType.MARKET
        limit_ticks = 0
        if passive:
            # Join the same-side best on the routed venue; MARKET fallback.
            vb = self._sim.venue_book(p.instrument_id, venue_id)
            price = None
            if vb is not None and post:
                price = post_price(
                    p.side, vb.best_bid(), vb.best_ask(), p.passive.improve_min_spread_ticks
                )
            elif vb is not None:
                best = vb.best_bid() if p.side == 0 else vb.best_ask()
                price = None if best is None else best[0]
            if price is not None:
                order_type = OrderType.LIMIT
                limit_ticks = price
        child = ChildOrder(
            parent_id=p.parent_id,
            instrument_id=p.instrument_id,
            venue_id=venue_id,
            side=p.side,
            type=order_type,
            limit_ticks=limit_ticks,
            qty=child_qty,
            decision_ts=decision_ts,
            expire_ts=p.end_ts,  # pinned: no child outlives the window
        )
        ps.child_ids.append(self._sim.submit(child))
        return True

    def _issue_slice(
        self, ps: _ParentState, slice_qty: int, decision_ts: int, passive: bool
    ) -> None:
        """Split a slice into children of at most ``max_child_qty`` (pinned)."""
        cap = ps.order.max_child_qty
        left = slice_qty
        while left > 0:
            q = min(left, cap)
            self._issue_child(ps, q, decision_ts, passive)
            left -= q

    def _committed_qty(self, ps: _ParentState) -> int:
        """Filled + still open/in-flight qty of the parent's children."""
        open_qty = 0
        orders = self._sim.orders
        for oid in ps.child_ids:
            o = orders[oid]
            if o.state in (OrderState.PENDING, OrderState.ACTIVE):
                open_qty += o.remaining
        return ps.filled_qty + open_qty

    def _book_new_fills(self) -> None:
        fills = self._sim.fills
        while self._fills_booked < len(fills):
            f = fills[self._fills_booked]
            self._fills_booked += 1
            for ps in self._parents:
                if ps.order.parent_id == f.parent_id:
                    ps.filled_qty += f.qty

    # ------------------------------------------------------ PASSIVE policy

    def _post(self, ps: _ParentState, qty: int, t: int, reprices: int) -> None:
        """POST one child of ``qty`` (or CROSS it when it cannot rest)."""
        p = ps.order
        cap = p.end_ts - p.passive.end_margin_ns
        if ps.patience_ns <= 0 or t >= cap:
            ps.stats.crosses_immediate += 1
            self._issue_child(ps, qty, t, False)
            return
        if not self._issue_child(ps, qty, t, True, post=True):
            return
        oid = ps.child_ids[-1]
        if self._sim.orders[oid].type != OrderType.LIMIT:
            ps.stats.crosses_immediate += 1  # no same-side quote: MARKET fallback
            return
        ps.stats.posts += 1
        ps.working.append(_Working(oid, min(t + ps.patience_ns, cap), reprices))

    def _post_step(self, ps: _ParentState, step_qty: int, t: int) -> None:
        """POST a schedule step, split at ``max_child_qty`` (pinned)."""
        cap = ps.order.max_child_qty
        left = step_qty
        while left > 0:
            q = min(left, cap)
            self._post(ps, q, t, 0)
            left -= q

    def _work_passive(self, ps: _ParentState, t: int) -> None:
        """Advance the state machine of every posted child (posting order)."""
        p = ps.order
        orders = self._sim.orders
        behind = ps.scheduled_qty - ps.filled_qty - ps.current_step_qty > ps.behind_qty
        n = len(ps.working)  # children posted below are evaluated next event
        for k in range(n):
            w = ps.working[k]
            if w.state == _DONE:
                continue
            o = orders[w.order_id]
            if w.state == _REST:
                if o.is_terminal:
                    w.state = _DONE
                elif t >= p.end_ts:
                    continue  # expires with the window (rule 7)
                elif behind:
                    self._sim.cancel(o.order_id, t)
                    w.state = _CANCEL_CROSS_BEHIND
                elif t >= w.deadline:
                    if w.reprices >= p.passive.max_reprices:
                        self._sim.cancel(o.order_id, t)
                        w.state = _CANCEL_CROSS_TIMEOUT
                        continue
                    vb = self._sim.venue_book(p.instrument_id, o.venue_id)
                    price = None
                    if vb is not None:
                        price = post_price(
                            p.side,
                            vb.best_bid(),
                            vb.best_ask(),
                            p.passive.improve_min_spread_ticks,
                        )
                    if price == o.limit_ticks:
                        # Still at the target price: keep the queue position.
                        w.reprices += 1
                        w.deadline = min(t + ps.patience_ns, p.end_ts - p.passive.end_margin_ns)
                        ps.stats.rest_extensions += 1
                    else:
                        self._sim.cancel(o.order_id, t)
                        w.state = _CANCEL_REPRICE
                continue
            # A cancel is in flight: act once it has taken effect.
            if not o.is_terminal:
                continue
            state = w.state
            w.state = _DONE
            if (
                o.state != OrderState.CANCELLED
                or o.cancel_reason != CancelReason.USER
                or o.remaining <= 0
                or t >= p.end_ts
            ):
                continue  # filled, expired or the window closed: nothing to re-send
            if state == _CANCEL_REPRICE:
                ps.stats.reprices += 1
                self._post(ps, o.remaining, t, w.reprices + 1)
            else:
                if state == _CANCEL_CROSS_BEHIND:
                    ps.stats.crosses_behind += 1
                else:
                    ps.stats.crosses_timeout += 1
                self._issue_child(ps, o.remaining, t, False)

    def _schedule_passive(self, ps: _ParentState, ev: MarketEvent) -> None:
        p = ps.order
        t = ev.exchange_ts
        steps: list[int] = []
        if p.algo == AlgoType.POV:
            in_window = (
                ev.instrument_id == p.instrument_id
                and ev.event_type == EventType.TRADE
                and p.start_ts <= t < p.end_ts
            )
            if in_window:
                ps.pov_volume += ev.qty
                target = int(math.floor(p.participation * float(ps.pov_volume)))
                ps.scheduled_qty = min(target, p.qty)
            self._work_passive(ps, t)
            if in_window:
                deficit = ps.scheduled_qty - self._committed_qty(ps)
                if deficit > 0:
                    self._post(ps, min(deficit, p.max_child_qty), t, 0)
            return
        while ps.next_slice < len(ps.slice_due) and t >= ps.slice_due[ps.next_slice]:
            q = ps.slice_qty[ps.next_slice]
            ps.next_slice += 1
            ps.scheduled_qty += q
            ps.current_step_qty = q
            if t >= p.end_ts:
                continue
            steps.append(q)
        self._work_passive(ps, t)
        for q in steps:
            self._post_step(ps, q, t)

    def _schedule(self, ps: _ParentState, ev: MarketEvent) -> None:
        p = ps.order
        t = ev.exchange_ts
        if p.policy == ExecPolicy.PASSIVE:
            self._schedule_passive(ps, ev)
            return
        if p.algo == AlgoType.POV:
            if (
                ev.instrument_id != p.instrument_id
                or ev.event_type != EventType.TRADE
                or t < p.start_ts
                or t >= p.end_ts
            ):
                return
            ps.pov_volume += ev.qty
            target = int(math.floor(p.participation * float(ps.pov_volume)))
            # Deficit against FILLED + in-flight qty, never sent qty (pinned).
            deficit = min(target, p.qty) - self._committed_qty(ps)
            if deficit > 0:
                self._issue_child(ps, min(deficit, p.max_child_qty), t, False)
            return
        # TWAP / VWAP / IS: issue every slice that has come due (inside the
        # window only — a slice due at/after end_ts would expire on arrival).
        while ps.next_slice < len(ps.slice_due) and t >= ps.slice_due[ps.next_slice]:
            q = ps.slice_qty[ps.next_slice]
            ps.next_slice += 1
            if t >= p.end_ts:
                continue
            passive = p.policy != ExecPolicy.AGGRESSIVE and p.algo in (
                AlgoType.TWAP,
                AlgoType.VWAP,
            )
            self._issue_slice(ps, q, t, passive)

    def run(self, events: Iterable[MarketEvent]) -> ExecReplayResult:
        """Replay the stream, working every parent. Callable once."""
        if self._ran:
            raise RuntimeError("ExecutionReplay.run is one-shot")
        self._ran = True
        res = ExecReplayResult()
        for ev in events:
            self._sim.on_event(ev)
            self._book_new_fills()
            for ps in self._parents:
                self._schedule(ps, ev)
            res.events_processed += 1
        self._sim.cancel_all()
        self._book_new_fills()

        res.fills = list(self._sim.fills)
        res.sor_no_route = self._sor_no_route
        # Reports keyed by parent_id in ascending order (C++ std::map order).
        for ps in sorted(self._parents, key=lambda s: s.order.parent_id):
            p = ps.order
            r = ParentReport(parent_id=p.parent_id, children=len(ps.child_ids))
            ins = self._config.instruments.get(p.instrument_id)
            lot = 1.0 if ins is None else ins.qty_unit
            tick = 1.0 if ins is None else ins.tick_size
            for f in res.fills:
                if f.parent_id != p.parent_id:
                    continue
                if f.ts < p.start_ts or f.ts > p.end_ts:
                    raise RuntimeError("fill outside the parent window (time-in-force broken)")
                r.filled_qty += f.qty
                r.notional += float(f.qty) * lot * float(f.price_ticks) * tick
                if f.fee >= 0.0:
                    r.fees += f.fee
                else:
                    r.rebates += -f.fee
                r.impact += f.impact_cost
            r.unfilled_qty = p.qty - r.filled_qty
            r.avg_price = r.notional / (float(r.filled_qty) * lot) if r.filled_qty > 0 else 0.0
            r.total_cost = r.fees - r.rebates + r.impact
            res.parents[r.parent_id] = r
            if p.policy == ExecPolicy.PASSIVE:
                res.passive[p.parent_id] = ps.stats
            res.children[p.parent_id] = [self._sim.orders[oid] for oid in ps.child_ids]
        return res
