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
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Sequence

from iap.core.events import EventType, MarketEvent
from iap.execution.algos import AlgoType, ParentOrder, slice_quantities, slice_times
from iap.execution.config import ExecConfig, SorOptions
from iap.execution.simulator import ExecutionSimulator
from iap.execution.sor import NO_ROUTE, SmartOrderRouter
from iap.execution.types import ChildOrder, Fill, OrderState, OrderType


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

    fills: List[Fill] = field(default_factory=list)
    parents: Dict[int, ParentReport] = field(default_factory=dict)
    events_processed: int = 0
    sor_no_route: int = 0  #: children not submitted: no eligible venue


@dataclass(slots=True)
class _ParentState:
    order: ParentOrder
    slice_qty: List[int] = field(default_factory=list)  #: TWAP/VWAP/IS
    slice_due: List[int] = field(default_factory=list)  #: TWAP/VWAP/IS
    next_slice: int = 0
    filled_qty: int = 0  #: fills booked so far
    pov_volume: int = 0  #: window TRADE volume (POV)
    child_ids: List[int] = field(default_factory=list)


class ExecutionReplay:
    """Works parent orders through a replayed event stream (one-shot)."""

    def __init__(
        self,
        config: ExecConfig,
        parents: Sequence[ParentOrder],
        sor_options: SorOptions = SorOptions(),
    ) -> None:
        self._config = config
        self._sim = ExecutionSimulator(config)
        self._sor = SmartOrderRouter(config.venues, sor_options)
        self._sor_candidates: List[int] = sorted(config.venues)
        self._parents: List[_ParentState] = []
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
            self._parents.append(ps)

    @property
    def simulator(self) -> ExecutionSimulator:
        return self._sim

    def _issue_child(
        self, ps: _ParentState, child_qty: int, decision_ts: int, passive: bool
    ) -> bool:
        """Issue one child of at most ``max_child_qty``; False when unroutable."""
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
            best = None
            if vb is not None:
                best = vb.best_bid() if p.side == 0 else vb.best_ask()
            if best is not None:
                order_type = OrderType.LIMIT
                limit_ticks = best[0]
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

    def _schedule(self, ps: _ParentState, ev: MarketEvent) -> None:
        p = ps.order
        t = ev.exchange_ts
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
            passive = p.algo in (AlgoType.TWAP, AlgoType.VWAP)
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
                    raise RuntimeError(
                        "fill outside the parent window (time-in-force broken)"
                    )
                r.filled_qty += f.qty
                r.notional += float(f.qty) * lot * float(f.price_ticks) * tick
                if f.fee >= 0.0:
                    r.fees += f.fee
                else:
                    r.rebates += -f.fee
                r.impact += f.impact_cost
            r.unfilled_qty = p.qty - r.filled_qty
            r.avg_price = (
                r.notional / (float(r.filled_qty) * lot) if r.filled_qty > 0 else 0.0
            )
            r.total_cost = r.fees - r.rebates + r.impact
            res.parents[r.parent_id] = r
        return res
