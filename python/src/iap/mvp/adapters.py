"""Protocol adapters over the reference implementations (no new semantics).

Each adapter maps the typed contracts of :mod:`iap.contracts` onto an
existing reference component and back, so the MVP loop speaks contracts
end to end while every rule stays where it is pinned:

- :class:`RiskEngineAdapter`   — :class:`~iap.contracts.protocols.RiskEngineLike`
  over :class:`iap.risk.RiskEngine` (``check_order`` -> ``RiskDecision``);
- :class:`AlgoScheduler`        — :class:`~iap.contracts.protocols.ExecutionAlgorithm`
  over :mod:`iap.execution.algos` (TWAP / IS slice schedules, POV deficit
  rule, ``max_child_qty`` splitting — the ``ExecutionReplay`` scheduling
  rules, restated on contracts);
- :class:`SorAdapter`           — :class:`~iap.contracts.protocols.SmartOrderRouterLike`
  over :class:`iap.execution.sor.SmartOrderRouter` (``VenueDecision`` with
  every candidate scored; ``NO_ROUTE`` = venue 0);
- :class:`SimulatorAdapter`     — :class:`~iap.contracts.protocols.ExecutionSimulatorLike`
  over :class:`iap.execution.simulator.ExecutionSimulator` (fills and
  terminal states -> ``ExecutionReport``);
- :class:`TcaAdapter`           — :class:`~iap.contracts.protocols.TCAEngine`
  over :func:`iap.tca.tca.order_tca` (``TCAResult``; the Perold identities
  are the contract's invariants).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Sequence, Tuple

from iap.contracts.ids import NO_ROUTE
from iap.contracts.types import (
    Algo,
    ChildOrder,
    ExecStatus,
    ExecutionReport,
    LatencyStats,
    OrderType,
    ParentOrder,
    RiskDecision,
    TCAResult,
    VenueDecision,
    VenueScore,
)
from iap.core.events import EventType, MarketEvent
from iap.execution import algos as _algos
from iap.execution.simulator import ExecutionSimulator
from iap.execution.sor import SmartOrderRouter
from iap.execution.types import ChildOrder as SimChild
from iap.execution.types import Fill as SimFill
from iap.execution.types import Liquidity, OrderState
from iap.execution.types import OrderType as SimOrderType
from iap.execution.types import VenueSpec
from iap.orderbook.book import ConsolidatedBook
from iap.risk.engine import RiskEngine
from iap.risk.events import Rules
from iap.risk.orders import OrderRequest
from iap.tca import fills as tca_fills
from iap.tca.tca import interval_twap, interval_vwap, order_tca

__all__ = [
    "RiskContext",
    "RiskEngineAdapter",
    "MarketView",
    "AlgoScheduler",
    "SorMarket",
    "SorAdapter",
    "SimulatorAdapter",
    "TcaMarket",
    "TcaAdapter",
    "rule_index_of",
]

#: Pinned check index of every rule id (``NOT_BOOTSTRAPPED`` shares check 0).
_RULE_INDEX: Dict[str, int] = {rid: i for i, rid in enumerate(Rules.CHECK_ORDER)}
_RULE_INDEX[Rules.NOT_BOOTSTRAPPED] = 0

_SIM_ORDER_TYPE = {OrderType.MARKET: SimOrderType.MARKET, OrderType.LIMIT: SimOrderType.LIMIT,
                   OrderType.IOC: SimOrderType.IOC, OrderType.FOK: SimOrderType.FOK}


def rule_index_of(rule_id: str) -> int:
    """The pinned check index of a deciding rule (``-1`` for ALLOW)."""
    if rule_id == Rules.ALLOW:
        return -1
    if rule_id not in _RULE_INDEX:
        raise ValueError(f"unknown risk rule id {rule_id!r}")
    return _RULE_INDEX[rule_id]


# --------------------------------------------------------------------- risk


@dataclass(frozen=True)
class RiskContext:
    """What the pre-trade check needs besides the child order."""

    strategy_id: str
    urgency: float
    timestamp_ns: int


class RiskEngineAdapter:
    """``evaluate(child, context)`` -> :class:`RiskDecision` via ``check_order``."""

    def __init__(self, engine: RiskEngine) -> None:
        self.engine = engine

    def evaluate(self, order: ChildOrder, state: RiskContext) -> RiskDecision:
        request = OrderRequest(
            order_id=order.child_order_id, instrument_id=order.instrument_id,
            side=int(order.side), qty=order.qty, price_ticks=order.price_ticks,
            order_type=int(order.order_type), venue_id=order.venue_id,
            strategy_id=state.strategy_id, urgency=state.urgency,
            timestamp=state.timestamp_ns,
        )
        outcome = self.engine.check_order(request)
        # ``check_order`` appended exactly one RiskEvent carrying these four
        # fields at the order's timestamp; rebuilding the record from the
        # returned decision avoids copying the whole audit log per child.
        event = {"timestamp": request.timestamp, "decision": int(outcome.decision),
                 "rule_id": outcome.rule_id, "reason": outcome.reason}
        return RiskDecision.from_risk_event(
            event, order_id=order.child_order_id, strategy_id=state.strategy_id,
            instrument_id=order.instrument_id, rule_index=rule_index_of(outcome.rule_id),
        )


# -------------------------------------------------------------------- algos


@dataclass(frozen=True)
class MarketView:
    """The market at the parent's current event time."""

    event: MarketEvent
    book: ConsolidatedBook


@dataclass
class _ParentSchedule:
    parent: ParentOrder
    slice_qty: List[int] = field(default_factory=list)
    slice_due: List[int] = field(default_factory=list)
    next_slice: int = 0
    next_child: int = 0
    pov_volume: int = 0


class AlgoScheduler:
    """Contract-level child scheduling (TWAP / POV / IS) — one live schedule
    per parent; ``generate_child_orders`` returns the children due at the
    market view's event (unrouted: ``venue_id`` 0, unpriced: ``price_ticks``
    0 — the router and the engine finish them).

    The POV deficit is measured against filled + open qty, which the engine
    supplies through ``committed(parent_order_id)`` (pinned: never sent qty).
    Child ids are drawn from the engine's order-id sequence (``next_id``).
    """

    def __init__(self, max_child_qty: int, twap_slices: int, is_slices: int,
                 is_risk_aversion: float, pov_participation: float,
                 next_id: Callable[[], int], committed: Callable[[int], int]) -> None:
        if max_child_qty <= 0:
            raise ValueError("max_child_qty must be > 0")
        self._max_child_qty = max_child_qty
        self._twap_slices = twap_slices
        self._is_slices = is_slices
        self._is_risk_aversion = is_risk_aversion
        self._pov_participation = pov_participation
        self._next_id = next_id
        self._committed = committed
        self._schedules: Dict[int, _ParentSchedule] = {}

    def open(self, parent: ParentOrder) -> None:
        """Register a parent (computes its slice schedule)."""
        if parent.parent_order_id in self._schedules:
            raise ValueError(f"parent {parent.parent_order_id} already scheduled")
        ps = _ParentSchedule(parent=parent)
        if parent.algo is not Algo.POV:
            slices = self._twap_slices if parent.algo is Algo.TWAP else self._is_slices
            po = _algos.ParentOrder(
                parent_id=parent.parent_order_id, instrument_id=parent.instrument_id,
                side=int(parent.side), qty=parent.qty,
                algo=_algos.AlgoType.TWAP if parent.algo is Algo.TWAP else _algos.AlgoType.IS,
                start_ts=parent.decision_ts, end_ts=parent.end_ts, slices=slices,
                risk_aversion=self._is_risk_aversion, max_child_qty=self._max_child_qty,
            )
            ps.slice_qty = _algos.slice_quantities(po)
            ps.slice_due = _algos.slice_times(po)
        self._schedules[parent.parent_order_id] = ps

    def close(self, parent_order_id: int) -> None:
        del self._schedules[parent_order_id]

    def params_for(self, algo: Algo) -> Dict[str, float]:
        """The ``ParentOrder.params`` block of an algo."""
        if algo is Algo.TWAP:
            return {"slices": float(self._twap_slices)}
        if algo is Algo.IS:
            return {"slices": float(self._is_slices), "risk_aversion": self._is_risk_aversion}
        return {"participation": self._pov_participation}

    def _child(self, ps: _ParentSchedule, qty: int, decision_ts: int,
               order_type: OrderType) -> ChildOrder:
        p = ps.parent
        child = ChildOrder(
            child_order_id=self._next_id(), parent_order_id=p.parent_order_id,
            instrument_id=p.instrument_id, venue_id=0, side=p.side, qty=qty, price_ticks=0,
            order_type=order_type, submit_ts=decision_ts, expire_ts=p.end_ts,
            slice_index=ps.next_child,
        )
        ps.next_child += 1
        return child

    def _split(self, ps: _ParentSchedule, slice_qty: int, decision_ts: int,
               order_type: OrderType) -> List[ChildOrder]:
        out: List[ChildOrder] = []
        left = slice_qty
        while left > 0:
            q = min(left, self._max_child_qty)
            out.append(self._child(ps, q, decision_ts, order_type))
            left -= q
        return out

    def generate_child_orders(self, parent: ParentOrder,
                              market: MarketView) -> Sequence[ChildOrder]:
        """Children due at ``market.event`` for ``parent`` (possibly none)."""
        ps = self._schedules[parent.parent_order_id]
        ev = market.event
        t = ev.exchange_ts
        if parent.algo is Algo.POV:
            if (ev.instrument_id != parent.instrument_id or ev.event_type != EventType.TRADE
                    or t < parent.decision_ts or t >= parent.end_ts):
                return ()
            ps.pov_volume += ev.qty
            target = int(math.floor(self._pov_participation * float(ps.pov_volume)))
            deficit = min(target, parent.qty) - self._committed(parent.parent_order_id)
            if deficit <= 0:
                return ()
            return (self._child(ps, min(deficit, self._max_child_qty), t, OrderType.MARKET),)
        out: List[ChildOrder] = []
        passive = parent.algo is Algo.TWAP
        while ps.next_slice < len(ps.slice_due) and t >= ps.slice_due[ps.next_slice]:
            q = ps.slice_qty[ps.next_slice]
            ps.next_slice += 1
            if t >= parent.end_ts:
                continue
            out.extend(self._split(ps, q, t, OrderType.LIMIT if passive else OrderType.MARKET))
        return tuple(out)


# ---------------------------------------------------------------------- SOR


@dataclass(frozen=True)
class SorMarket:
    """The routing input: the instrument's consolidated book and the intent."""

    book: ConsolidatedBook
    passive: bool


class SorAdapter:
    """``route(child, market)`` -> :class:`VenueDecision` over the pinned router."""

    def __init__(self, router: SmartOrderRouter, venues: Mapping[int, VenueSpec],
                 candidates: Sequence[int]) -> None:
        self._router = router
        self._venues = dict(sorted(venues.items()))
        self._candidates = sorted(candidates)
        if not self._candidates:
            raise ValueError("SOR: no candidate venues")

    def _route(self, market: SorMarket, side: int, candidates: Sequence[int]) -> int:
        if market.passive:
            return self._router.route_passive(market.book, side, candidates)
        return self._router.route_aggressive(market.book, side, candidates)

    def route(self, order: ChildOrder, venues: SorMarket) -> VenueDecision:
        side = int(order.side)
        # Ranking by repeated routing over the remaining candidates reuses the
        # router's own tie-break ladder for every rank, not just the winner.
        ranks: Dict[int, int] = {}
        remaining = list(self._candidates)
        rank = 1
        while remaining:
            vid = self._route(venues, side, remaining)
            if vid == NO_ROUTE:
                break
            ranks[vid] = rank
            rank += 1
            remaining.remove(vid)
        scores = []
        for vid in self._candidates:
            spec = self._venues[vid]
            vb = venues.book.books.get(vid)
            quote = None
            if vb is not None:
                if venues.passive:
                    quote = vb.best_bid() if side == 0 else vb.best_ask()
                else:
                    quote = vb.best_ask() if side == 0 else vb.best_bid()
            scores.append(VenueScore(
                venue_id=vid, eligible=vid in ranks,
                displayed_price_ticks=quote[0] if quote else 0,
                displayed_qty=quote[1] if quote else 0,
                taker_fee=spec.taker_fee_per_share, maker_rebate=spec.maker_rebate_per_share,
                commission_per_million=spec.commission_per_million,
                latency_mean_ns=spec.latency_mean_ns, rank=ranks.get(vid, 0),
            ))
        winner = next((v for v, r in ranks.items() if r == 1), NO_ROUTE)
        intent = "passive" if venues.passive else "aggressive"
        if winner == NO_ROUTE:
            reason = f"NO_ROUTE: no eligible venue quotes the {intent} side"
        else:
            reason = f"{intent}: best {'rebate' if venues.passive else 'displayed price'} " \
                     f"among eligible venues (ties: fee, commission, venue id)"
        return VenueDecision(child_order_id=order.child_order_id, venue_id=winner,
                             reason=reason, candidates=tuple(scores))


# ---------------------------------------------------------------- simulator


class SimulatorAdapter:
    """``submit`` / ``on_market_event`` -> :class:`ExecutionReport` streams.

    Keeps the mapping between the strategy-side ``child_order_id`` and the
    simulator's own order id; ``execution_id`` is one sequence over every
    report (NEW / PARTIAL / FILLED / CANCELED / EXPIRED) so the store's
    executions table has a unique key per report.  ``receive_ts`` of a
    report is its ``exchange_ts`` plus the venue's mean latency (the
    report travels back over the same wire, no jitter draw — documented).
    """

    def __init__(self, simulator: ExecutionSimulator) -> None:
        self.simulator = simulator
        self._sim_id_of: Dict[int, int] = {}      #: child_order_id -> sim order id
        self._child_id_of: Dict[int, int] = {}    #: sim order id -> child_order_id
        self._live: List[int] = []                #: sim ids not yet reported terminal
        self._filled_qty: Dict[int, int] = {}     #: sim order id -> cumulative fill qty
        self._fills_reported = 0
        self._next_execution_id = 1
        self.fills_by_execution: Dict[int, SimFill] = {}

    def _report(self, child_id: int, status: ExecStatus, qty: int, price: int,
                venue_id: int, ts: int, fees: float) -> ExecutionReport:
        eid = self._next_execution_id
        self._next_execution_id += 1
        latency = self.simulator.config.venue(venue_id).latency_mean_ns
        return ExecutionReport(
            order_id=child_id, execution_id=eid, status=status, filled_qty=qty,
            fill_price_ticks=price, venue_id=venue_id, exchange_ts=ts,
            receive_ts=ts + latency, fees=fees,
        )

    def sim_order_id(self, child_order_id: int) -> int:
        return self._sim_id_of[child_order_id]

    def child_order_id(self, sim_order_id: int) -> int:
        return self._child_id_of[sim_order_id]

    def open_qty(self, child_order_id: int) -> int:
        """Remaining qty of a PENDING / ACTIVE child (0 once terminal)."""
        o = self.simulator.orders[self._sim_id_of[child_order_id]]
        return o.remaining if o.state in (OrderState.PENDING, OrderState.ACTIVE) else 0

    def arrival_ts(self, child_order_id: int) -> int:
        return self.simulator.orders[self._sim_id_of[child_order_id]].arrival_ts

    def submit(self, order: ChildOrder) -> Sequence[ExecutionReport]:
        if order.venue_id == NO_ROUTE:
            raise ValueError("submit: child must be routed to a venue")
        if order.child_order_id in self._sim_id_of:
            raise ValueError(f"submit: duplicate child_order_id {order.child_order_id}")
        child = SimChild(
            parent_id=order.parent_order_id, instrument_id=order.instrument_id,
            venue_id=order.venue_id, side=int(order.side), type=_SIM_ORDER_TYPE[order.order_type],
            limit_ticks=order.price_ticks, qty=order.qty, decision_ts=order.submit_ts,
            expire_ts=order.expire_ts,
        )
        sim_id = self.simulator.submit(child)
        self._sim_id_of[order.child_order_id] = sim_id
        self._child_id_of[sim_id] = order.child_order_id
        self._live.append(sim_id)
        return (self._report(order.child_order_id, ExecStatus.NEW, 0, 0, order.venue_id,
                             order.submit_ts, 0.0),)

    def _drain(self, terminal_ts: int) -> List[ExecutionReport]:
        reports: List[ExecutionReport] = []
        fills = self.simulator.fills
        orders = self.simulator.orders
        while self._fills_reported < len(fills):
            f = fills[self._fills_reported]
            self._fills_reported += 1
            o = orders[f.order_id]
            child_id = self._child_id_of[f.order_id]
            cum = self._filled_qty.get(f.order_id, 0) + f.qty
            self._filled_qty[f.order_id] = cum
            status = ExecStatus.FILLED if cum == o.qty else ExecStatus.PARTIAL
            rep = self._report(child_id, status, f.qty, f.price_ticks, f.venue_id, f.ts, f.fee)
            self.fills_by_execution[rep.execution_id] = f
            reports.append(rep)
        i = 0
        while i < len(self._live):
            sim_id = self._live[i]
            o = orders[sim_id]
            if o.state == OrderState.FILLED:
                del self._live[i]
                continue
            if o.state == OrderState.CANCELLED:
                del self._live[i]
                child_id = self._child_id_of[sim_id]
                status = ExecStatus.EXPIRED if o.cancel_reason.name == "EXPIRED" \
                    else ExecStatus.CANCELED
                reports.append(self._report(child_id, status, 0, 0, o.venue_id,
                                            terminal_ts, 0.0))
                continue
            i += 1
        return reports

    def on_market_event(self, event: MarketEvent) -> Sequence[ExecutionReport]:
        self.simulator.on_event(event)
        return tuple(self._drain(event.exchange_ts))

    def finish(self, end_ts: int) -> Sequence[ExecutionReport]:
        """End-of-stream sweep (``cancel_all``) and the resulting terminal reports."""
        self.simulator.cancel_all()
        return tuple(self._drain(end_ts))


# ---------------------------------------------------------------------- TCA


@dataclass(frozen=True)
class TcaMarket:
    """The market context of one parent: timeline, tick size and the
    liquidity flag + latency of every execution."""

    timeline: tca_fills.MarketTimeline
    tick_size: float
    liquidity: Mapping[int, Liquidity]       #: execution_id -> liquidity
    child_latency_ns: Mapping[int, int]      #: child_order_id -> decision->arrival ns


def _latency_stats(values: Sequence[int]) -> LatencyStats:
    if not values:
        return LatencyStats(min=0, mean=0.0, max=0, p50=0, p99=0)
    s = sorted(values)
    n = len(s)

    def rank(q: float) -> int:
        return s[min(n - 1, max(0, int(math.ceil(q * n)) - 1))]

    return LatencyStats(min=s[0], mean=sum(s) / n, max=s[-1], p50=rank(0.5), p99=rank(0.99))


class TcaAdapter:
    """``analyse(parent, executions, market)`` -> :class:`TCAResult`.

    Mapping from :func:`iap.tca.tca.order_tca` (API_PORTFOLIO_TCA.md §2):
    Perold ``total_is / delay / trading / opportunity`` bps as given;
    ``spread_cost / impact_cost / timing_cost`` (currency) and the fees
    converted to bps of ``qty * decision_mid``; ``avg_fill_price``,
    ``interval_vwap`` / ``interval_twap`` in ticks — ``0.0`` is the explicit
    sentinel for an undefined benchmark (no fill / no market trade in the
    window), never a fabricated price; ``arrival_price_ticks`` is the
    arrival mid rounded half-up to a tick; ``participation_rate`` = filled
    qty / market trade volume inside ``[arrival_ts, end_ts]``;
    ``venue_contribution_bps`` = each venue's share of the trading cost;
    ``latency_ns`` = decision -> arrival of the submitted children
    (nearest-rank quantiles).
    """

    @staticmethod
    def _ticks(price: float, tick: float) -> float:
        return price / tick

    def analyse(self, parent_order: ParentOrder, executions: Sequence[ExecutionReport],
                market: TcaMarket) -> TCAResult:
        tick = market.tick_size
        tl = market.timeline
        order = tca_fills.ParentOrder(
            order_id=parent_order.parent_order_id, instrument_id=parent_order.instrument_id,
            side=int(parent_order.side), qty_target=parent_order.qty,
            decision_ts=parent_order.decision_ts, arrival_ts=parent_order.arrival_ts,
            end_ts=parent_order.end_ts,
        )
        fees_total = 0.0
        venue_fills: Dict[int, List[Tuple[float, int]]] = {}
        for rep in executions:
            if rep.status not in (ExecStatus.PARTIAL, ExecStatus.FILLED):
                continue
            liq = tca_fills.MAKER if market.liquidity[rep.execution_id] == Liquidity.MAKER \
                else tca_fills.TAKER
            order.fills.append(tca_fills.stamp_fill(
                tl, rep.exchange_ts, rep.fill_price_ticks * tick, rep.filled_qty,
                int(parent_order.side), liq))
            fees_total += rep.fees
            venue_fills.setdefault(rep.venue_id, []).append(
                (rep.fill_price_ticks * tick, rep.filled_qty))
        rec = order_tca(order, tl)
        perold = rec["perold"]
        m_d = float(rec["decision_mid"])
        m_a = float(rec["arrival_mid"])
        denom = float(parent_order.qty) * m_d
        s = float(order.sign)
        to_bps = 1e4 / denom
        filled = order.qty_filled
        vwap_mkt = interval_vwap(tl, order.arrival_ts, order.end_ts)
        twap_mkt = interval_twap(tl, order.arrival_ts, order.end_ts)
        window_volume = sum(q for ts, _, q in tl.trades if order.arrival_ts <= ts <= order.end_ts)
        venue_contribution = {
            str(vid): sum(s * q * (p - m_a) for p, q in fl) * to_bps
            for vid, fl in sorted(venue_fills.items())
        }
        latencies = [market.child_latency_ns[c] for c in sorted(market.child_latency_ns)]
        return TCAResult(
            parent_order_id=parent_order.parent_order_id,
            instrument_id=parent_order.instrument_id, side=parent_order.side,
            qty=parent_order.qty, filled_qty=filled, fill_rate=filled / parent_order.qty,
            arrival_price_ticks=int(math.floor(m_a / tick + 0.5)),
            avg_fill_price=self._ticks(order.fill_vwap, tick) if filled else 0.0,
            interval_vwap=self._ticks(vwap_mkt, tick) if vwap_mkt else 0.0,
            interval_twap=self._ticks(twap_mkt, tick) if twap_mkt else 0.0,
            implementation_shortfall_bps=float(perold["total_is_bps"]),
            delay_cost_bps=float(perold["delay_bps"]),
            trading_cost_bps=float(perold["trading_bps"]),
            opportunity_cost_bps=float(perold["opportunity_bps"]),
            spread_cost_bps=float(rec["spread_cost"]) * to_bps,
            impact_bps=float(rec["impact_cost"]) * to_bps,
            fees_bps=fees_total * to_bps,
            timing_cost_bps=float(rec["timing_cost"]) * to_bps,
            slippage_bps=float(rec["arrival_slippage_bps"] or 0.0),
            participation_rate=min(1.0, filled / window_volume) if window_volume > 0 else 0.0,
            n_fills=int(rec["n_fills"]),
            venue_contribution_bps=venue_contribution,
            algo=parent_order.algo,
            latency_ns=_latency_stats(latencies),
        )
