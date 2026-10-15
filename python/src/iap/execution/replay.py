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

Complexity (v1.17 B1, review X-6): the scheduler no longer visits every
parent on every event. A parent is visited only when the event can change
it — one of its TWAP/VWAP/IS slices is due (a ``(due_ts, parent index)``
min-heap over the parents' next slices), the event is a TRADE of a POV
parent's instrument (parents indexed by instrument), or it is a PASSIVE
parent with a posted child still working (its state machine is time
driven). The visits of one event run in parent-list order, each parent
draining every slice due, exactly the order of the per-event scan, so the
output is byte-identical (``test_execution_golden``,
``test_passive_policy``); one pass over the events services every parent.
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace

from iap.core.events import EventType, MarketEvent
from iap.execution.algos import (
    URGENCY_POLICIES,
    AlgoType,
    ISModel,
    ParentOrder,
    slice_quantities,
    slice_times,
    slice_weights,
)
from iap.execution.config import ExecConfig, SorOptions
from iap.execution.optimal import ac_drift_from_signal, ac_trajectory
from iap.execution.pacing import PacingParams, apportion, pacing_scale, tilted_slice_qty
from iap.execution.passive import (
    ExecPolicy,
    PassiveStats,
    max_behind_qty,
    patience_ns,
    post_price,
)
from iap.execution.signal import SignalReading
from iap.execution.simulator import ExecutionSimulator
from iap.execution.sor import NO_ROUTE, SmartOrderRouter
from iap.execution.types import CancelReason, ChildOrder, Fill, OrderState, OrderType
from iap.execution.urgency import AlphaUrgencyPolicy, regime_from_z

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
    # ---- v1.17 B1 diagnostics (outside ``to_dict``: the golden rows are pinned) ----
    #: X-5: aggressive children of this parent cancelled for want of depth.
    depth_exhausted: int = 0
    #: X-5: qty those children left unfilled (base units).
    depth_exhausted_qty: int = 0
    #: X-7: filled qty by ``Fill.provenance`` (insertion order = first fill).
    provenance_qty: dict[str, int] = field(default_factory=dict)
    #: v1.17 B2 / B3: children and qty by ``ChildOrder.urgency_regime``
    #: (``iap.execution.signal.URGENCY_REGIMES``; ``"plain"`` for a pacing-only
    #: parent); empty for a parent without signal opt-ins.
    urgency_children: dict[str, int] = field(default_factory=dict)
    urgency_qty: dict[str, int] = field(default_factory=dict)

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

    def diagnostics(self) -> dict:
        """v1.17 B1 under-fill and provenance columns (not in any golden)."""
        return {
            "depth_exhausted": self.depth_exhausted,
            "depth_exhausted_qty": self.depth_exhausted_qty,
            "provenance_qty": dict(self.provenance_qty),
            "urgency_children": dict(self.urgency_children),
            "urgency_qty": dict(self.urgency_qty),
        }


def _pov_target(p: ParentOrder, market_volume: int) -> int:
    """POV target qty: pinned ``floor(p * V)``; ``pov_exact`` counts our own
    fills in the total, ``floor(p / (1 - p) * V)`` (v1.16 P0-5)."""
    if p.pov_exact:
        if not 0.0 < p.participation < 1.0:
            raise ValueError(f"pov_exact needs 0 < participation < 1, got {p.participation!r}")
        return int(math.floor(p.participation / (1.0 - p.participation) * float(market_volume)))
    return int(math.floor(p.participation * float(market_volume)))


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
    #: rest time of this child (the parent's ``patience_ns`` unless a B2
    #: *agree* decision posted it with the policy's ``passive_urgency``).
    patience: int = 0


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
    armed: bool = False  #: X-6: has an entry in the due-slice heap
    # ---- v1.17 B2 / B3 signal-aware state (unused for a plain parent) ----
    signal: bool = False
    urgency: AlphaUrgencyPolicy | None = None
    pacing: PacingParams | None = None
    slice_w: list[float] = field(default_factory=list)  #: TWAP/VWAP/IS weights
    patience_agree_ns: int = 0  #: rest time of an *agree* post
    pov_target_f: float = 0.0  #: B3 POV: sum of ``p_t * dV`` so far
    last_scale: float = 1.0  #: pacing scale of the latest decision
    last_z: float = 0.0  #: signal of the latest pacing decision
    urgency_children: dict[str, int] = field(default_factory=dict)
    urgency_qty: dict[str, int] = field(default_factory=dict)


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
        # X-6 indexes (see the module docstring): parents by parent_id for
        # fill booking, POV parents by instrument, the due-slice heap and
        # the PASSIVE parents with a working child.
        self._by_parent_id: dict[int, list[_ParentState]] = {}
        self._pov_by_instrument: dict[int, list[int]] = {}
        self._due: list[tuple[int, int]] = []
        self._live_passive: set[int] = set()
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
            if p.signal_aware:
                self._init_signal_state(ps)
            idx = len(self._parents)
            self._parents.append(ps)
            self._by_parent_id.setdefault(p.parent_id, []).append(ps)
            if p.algo == AlgoType.POV:
                self._pov_by_instrument.setdefault(p.instrument_id, []).append(idx)
            elif ps.slice_due:
                ps.armed = True
                heapq.heappush(self._due, (ps.slice_due[0], idx))

    @property
    def simulator(self) -> ExecutionSimulator:
        return self._sim

    # ------------------------------------------------ v1.17 B2 / B3 setup

    @staticmethod
    def _init_signal_state(ps: _ParentState) -> None:
        """Validate a signal-aware parent and prepare its state."""
        p = ps.order
        if p.signal_source is None:
            raise ValueError(f"parent {p.parent_id}: signal opt-ins need a signal_source")
        if p.urgency_policy not in URGENCY_POLICIES:
            raise ValueError(f"urgency_policy must be one of {URGENCY_POLICIES}")
        if p.pov_signal_pacing and p.algo != AlgoType.POV:
            raise ValueError("pov_signal_pacing needs algo=POV")
        if p.slice_signal_tilt and p.algo == AlgoType.POV:
            raise ValueError("slice_signal_tilt needs a TWAP / VWAP / IS parent")
        if p.ac_signal_drift and (
            p.algo != AlgoType.IS or p.is_model != ISModel.ALMGREN_CHRISS or p.ac_params is None
        ):
            raise ValueError("ac_signal_drift needs algo=IS, is_model=ALMGREN_CHRISS, ac_params")
        if p.ac_signal_drift and p.slice_signal_tilt:
            raise ValueError("ac_signal_drift and slice_signal_tilt are alternatives")
        ps.signal = True
        ps.urgency = p.alpha_urgency or AlphaUrgencyPolicy()
        ps.pacing = p.pacing or PacingParams()
        if p.algo != AlgoType.POV:
            ps.slice_w = slice_weights(p)
        is_algo = p.algo == AlgoType.IS
        ps.patience_agree_ns = patience_ns(
            p.passive, ps.urgency.passive_urgency, is_algo, p.risk_aversion
        )
        if p.policy != ExecPolicy.PASSIVE:
            ps.behind_qty = max_behind_qty(p.passive, p.qty)

    def _reading(
        self, ps: _ParentState, t: int, max_age_ns: int, latency_ns: int
    ) -> tuple[SignalReading | None, str]:
        """Causal signal reading as of ``t - latency_ns``: ``(reading, status)``
        with status ``"ok"`` / ``"no_signal"`` / ``"stale"``."""
        p = ps.order
        assert p.signal_source is not None
        at = t - latency_ns
        r = p.signal_source.at(p.instrument_id, at)
        if r is None:
            return None, "no_signal"
        if max_age_ns > 0 and at - r.ts > max_age_ns:
            return None, "stale"
        return r, "ok"

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
            for ps in self._by_parent_id.get(f.parent_id, ()):
                ps.filled_qty += f.qty

    # ------------------------------------------------------ PASSIVE policy

    def _post(
        self, ps: _ParentState, qty: int, t: int, reprices: int, patience: int | None = None
    ) -> None:
        """POST one child of ``qty`` (or CROSS it when it cannot rest).

        ``patience`` defaults to the parent's ``patience_ns`` (the pinned
        PASSIVE rule); a B2 *agree* decision passes its own rest time.
        """
        p = ps.order
        rest = ps.patience_ns if patience is None else patience
        cap = p.end_ts - p.passive.end_margin_ns
        if rest <= 0 or t >= cap:
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
        ps.working.append(_Working(oid, min(t + rest, cap), reprices, patience=rest))

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
                        w.deadline = min(t + w.patience, p.end_ts - p.passive.end_margin_ns)
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
            first = len(ps.child_ids)
            if state == _CANCEL_REPRICE:
                ps.stats.reprices += 1
                self._post(ps, o.remaining, t, w.reprices + 1, patience=w.patience)
            else:
                if state == _CANCEL_CROSS_BEHIND:
                    ps.stats.crosses_behind += 1
                else:
                    ps.stats.crosses_timeout += 1
                self._issue_child(ps, o.remaining, t, False)
            if ps.signal:
                # A re-send inherits the provenance of the decision it came from;
                # its qty was attributed at that decision (not counted twice).
                self._tag_children(
                    ps, first, o.urgency_regime, o.signal_z, o.pacing_scale, count_qty=False
                )

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
                ps.scheduled_qty = min(_pov_target(p, ps.pov_volume), p.qty)
            self._work_passive(ps, t)
            if in_window:
                deficit = ps.scheduled_qty - self._committed_qty(ps)
                while deficit > 0:
                    q = min(deficit, p.max_child_qty)
                    self._post(ps, q, t, 0)
                    deficit = deficit - q if p.pov_exact else 0
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

    # ------------------------------------------- v1.17 B2 / B3 signal path

    def _tag_children(
        self,
        ps: _ParentState,
        first: int,
        regime: str,
        z: float,
        scale: float | None = None,
        count_qty: bool = True,
    ) -> None:
        """Provenance of the children issued since ``first`` (B2 / B3)."""
        for oid in ps.child_ids[first:]:
            o = self._sim.orders[oid]
            o.urgency_regime = regime
            o.signal_z = z
            o.pacing_scale = ps.last_scale if scale is None else scale
            ps.urgency_children[regime] = ps.urgency_children.get(regime, 0) + 1
            if count_qty:
                ps.urgency_qty[regime] = ps.urgency_qty.get(regime, 0) + o.qty

    def _plain_child(self, ps: _ParentState, qty: int, t: int) -> None:
        """One child in the parent's base ``policy`` style (the neutral arm)."""
        p = ps.order
        if p.policy == ExecPolicy.PASSIVE:
            self._post(ps, qty, t, 0)
        elif p.policy == ExecPolicy.AGGRESSIVE:
            self._issue_child(ps, qty, t, False)
        else:
            self._issue_child(ps, qty, t, p.algo in (AlgoType.TWAP, AlgoType.VWAP))

    def _signal_child(self, ps: _ParentState, qty: int, t: int) -> None:
        """One child of at most ``max_child_qty``, its style decided NOW (B2)."""
        p = ps.order
        first = len(ps.child_ids)
        if p.urgency_policy == "alpha":
            urg = ps.urgency
            assert urg is not None
            r, status = self._reading(ps, t, urg.max_age_ns, urg.latency_ns)
            z = 0.0 if r is None else r.z
            regime = status if r is None else regime_from_z(p.side, z, urg.threshold_z)
            if regime == "adverse":
                self._issue_child(ps, qty, t, False)
            elif regime == "agree":
                self._post(ps, qty, t, 0, patience=ps.patience_agree_ns)
            else:
                self._plain_child(ps, qty, t)
        else:
            regime, z = "plain", ps.last_z
            self._plain_child(ps, qty, t)
        self._tag_children(ps, first, regime, z)

    def _signal_step(self, ps: _ParentState, step_qty: int, t: int) -> None:
        cap = ps.order.max_child_qty
        left = step_qty
        while left > 0:
            q = min(left, cap)
            self._signal_child(ps, q, t)
            left -= q

    def _mid_price(self, instrument_id: int) -> float | None:
        """Consolidated mid in currency, or None when not two-sided."""
        book = self._sim.instrument_book(instrument_id)
        bb, ba = book.best_bid(), book.best_ask()
        if bb is None or ba is None:
            return None
        ins = self._config.instruments.get(instrument_id)
        tick = 1.0 if ins is None else ins.tick_size
        return 0.5 * (bb[0] + ba[0]) * tick

    def _paced_slice_qty(self, ps: _ParentState, k: int, t: int) -> int:
        """Quantity of slice ``k`` under B3 (tilt or AC drift), decided at ``t``."""
        p = ps.order
        pac = ps.pacing
        assert pac is not None
        remaining = p.qty - ps.scheduled_qty
        n_left = len(ps.slice_qty) - k
        if remaining <= 0:
            return 0
        if n_left <= 1:
            return remaining
        r, _status = self._reading(ps, t, pac.max_age_ns, pac.latency_ns)
        ps.last_z = 0.0 if r is None else r.z
        if p.ac_signal_drift:
            acp = p.ac_params
            assert acp is not None
            t_rem = acp.horizon * n_left / len(ps.slice_qty)
            price = self._mid_price(p.instrument_id)
            drift = 0.0
            if r is not None and price is not None and price > 0:
                drift = ac_drift_from_signal(p.side, r.expected_return, price, t_rem)
            traj = ac_trajectory(float(remaining), replace(acp, horizon=t_rem), n_left, drift)
            q = apportion(remaining, [max(0.0, x) for x in traj])[0]
            plain = apportion(
                remaining,
                [
                    max(0.0, x)
                    for x in ac_trajectory(float(remaining), replace(acp, horizon=t_rem), n_left)
                ],
            )[0]
            ps.last_scale = q / plain if plain > 0 else (1.0 if q == 0 else math.inf)
            return q
        ps.last_scale = pacing_scale(p.side, None if r is None else r.z, pac)
        return tilted_slice_qty(remaining, ps.slice_w[k:], ps.last_scale)

    def _schedule_signal(self, ps: _ParentState, ev: MarketEvent) -> None:
        """The schedule of a signal-aware parent (B2 / B3): the PASSIVE
        order of evaluation (schedule state, working children, new steps)
        with the quantity and style of every step decided at the event."""
        p = ps.order
        pac = ps.pacing
        assert pac is not None
        t = ev.exchange_ts
        if p.algo == AlgoType.POV:
            in_window = (
                ev.instrument_id == p.instrument_id
                and ev.event_type == EventType.TRADE
                and p.start_ts <= t < p.end_ts
            )
            if in_window:
                ps.pov_volume += ev.qty
                if p.pov_signal_pacing:
                    r, _status = self._reading(ps, t, pac.max_age_ns, pac.latency_ns)
                    ps.last_z = 0.0 if r is None else r.z
                    ps.last_scale = pacing_scale(p.side, None if r is None else r.z, pac)
                    p_eff = min(p.participation * ps.last_scale, pac.max_participation)
                    rate = p_eff / (1.0 - p_eff) if p.pov_exact else p_eff
                    ps.pov_target_f += rate * float(ev.qty)
                    target = int(math.floor(ps.pov_target_f))
                else:
                    target = _pov_target(p, ps.pov_volume)
                ps.scheduled_qty = min(target, p.qty)
            self._work_passive(ps, t)
            if in_window:
                deficit = ps.scheduled_qty - self._committed_qty(ps)
                while deficit > 0:
                    q = min(deficit, p.max_child_qty)
                    self._signal_child(ps, q, t)
                    deficit = deficit - q if p.pov_exact else 0
            return
        steps: list[int] = []
        while ps.next_slice < len(ps.slice_due) and t >= ps.slice_due[ps.next_slice]:
            k = ps.next_slice
            q = ps.slice_qty[k]
            if (p.slice_signal_tilt or p.ac_signal_drift) and t < p.end_ts:
                q = self._paced_slice_qty(ps, k, t)
            ps.next_slice += 1
            ps.scheduled_qty += q
            ps.current_step_qty = q
            if t >= p.end_ts:
                continue
            steps.append(q)
        self._work_passive(ps, t)
        for q in steps:
            self._signal_step(ps, q, t)

    def _schedule(self, ps: _ParentState, ev: MarketEvent) -> None:
        p = ps.order
        t = ev.exchange_ts
        if ps.signal:
            self._schedule_signal(ps, ev)
            return
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
            # Deficit against FILLED + in-flight qty, never sent qty (pinned).
            deficit = min(_pov_target(p, ps.pov_volume), p.qty) - self._committed_qty(ps)
            while deficit > 0:
                q = min(deficit, p.max_child_qty)
                self._issue_child(ps, q, t, False)
                deficit = deficit - q if p.pov_exact else 0
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

    def _visits(self, ev: MarketEvent) -> list[int]:
        """Parent indices this event can change, in parent-list order (X-6)."""
        t = ev.exchange_ts
        due = self._due
        visit: set[int] = set()
        while due and due[0][0] <= t:
            idx = heapq.heappop(due)[1]
            self._parents[idx].armed = False
            visit.add(idx)
        if ev.event_type == EventType.TRADE:
            visit.update(self._pov_by_instrument.get(ev.instrument_id, ()))
        visit.update(self._live_passive)
        return sorted(visit)

    def _after_visit(self, idx: int) -> None:
        """Re-arm the due heap / the live set for a parent just visited."""
        ps = self._parents[idx]
        if not ps.armed and ps.order.algo != AlgoType.POV and ps.next_slice < len(ps.slice_due):
            ps.armed = True
            heapq.heappush(self._due, (ps.slice_due[ps.next_slice], idx))
        if ps.order.policy == ExecPolicy.PASSIVE or ps.signal:
            if any(w.state != _DONE for w in ps.working):
                self._live_passive.add(idx)
            else:
                self._live_passive.discard(idx)

    def run(self, events: Iterable[MarketEvent]) -> ExecReplayResult:
        """Replay the stream, working every parent. Callable once."""
        if self._ran:
            raise RuntimeError("ExecutionReplay.run is one-shot")
        self._ran = True
        res = ExecReplayResult()
        parents = self._parents
        for ev in events:
            self._sim.on_event(ev)
            self._book_new_fills()
            for idx in self._visits(ev):
                self._schedule(parents[idx], ev)
                self._after_visit(idx)
            res.events_processed += 1
        self._sim.cancel_all()
        self._book_new_fills()

        res.fills = list(self._sim.fills)
        res.sor_no_route = self._sor_no_route
        fills_of: dict[int, list[Fill]] = {}
        for f in res.fills:
            fills_of.setdefault(f.parent_id, []).append(f)
        orders = self._sim.orders
        # Reports keyed by parent_id in ascending order (C++ std::map order).
        for ps in sorted(self._parents, key=lambda s: s.order.parent_id):
            p = ps.order
            r = ParentReport(parent_id=p.parent_id, children=len(ps.child_ids))
            ins = self._config.instruments.get(p.instrument_id)
            lot = 1.0 if ins is None else ins.qty_unit
            tick = 1.0 if ins is None else ins.tick_size
            for f in fills_of.get(p.parent_id, ()):
                if f.ts < p.start_ts or f.ts > p.end_ts:
                    raise RuntimeError("fill outside the parent window (time-in-force broken)")
                r.filled_qty += f.qty
                r.notional += float(f.qty) * lot * float(f.price_ticks) * tick
                if f.fee >= 0.0:
                    r.fees += f.fee
                else:
                    r.rebates += -f.fee
                r.impact += f.impact_cost
                r.provenance_qty[f.provenance] = r.provenance_qty.get(f.provenance, 0) + f.qty
            for oid in ps.child_ids:
                o = orders[oid]
                if o.depth_exhausted:
                    r.depth_exhausted += 1
                    r.depth_exhausted_qty += o.remaining
            r.unfilled_qty = p.qty - r.filled_qty
            r.avg_price = r.notional / (float(r.filled_qty) * lot) if r.filled_qty > 0 else 0.0
            r.total_cost = r.fees - r.rebates + r.impact
            if ps.signal:
                r.urgency_children = dict(ps.urgency_children)
                r.urgency_qty = dict(ps.urgency_qty)
            res.parents[r.parent_id] = r
            if p.policy == ExecPolicy.PASSIVE or ps.signal:
                res.passive[p.parent_id] = ps.stats
            res.children[p.parent_id] = [self._sim.orders[oid] for oid in ps.child_ids]
        return res
