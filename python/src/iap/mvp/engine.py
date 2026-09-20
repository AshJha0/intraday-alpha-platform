"""``MvpEngine`` — the event-driven, single-pass, deterministic MVP loop.

Per market event, in pinned order (mirrors the Java ``BacktestEngine.onEvent``
+ ``PaperTrading.RiskWiring`` chain, PLATFORM_CONVENTIONS.md §11.4):

1. the execution simulator processes the event (expiries, child activation,
   passive queue tracking, book update, crossing checks) and its new fills /
   terminal states become :class:`~iap.contracts.types.ExecutionReport`\\s:
   every fill is booked into the account and fed to the risk engine
   (``on_fill``) BEFORE the next decision, every terminal child calls
   ``on_order_done``;
2. session volume (EXECUTE + TRADE qty) is accumulated for the participation
   control;
3. the risk wiring runs: per-venue stale transitions -> ``on_sequence_gap`` /
   ``on_feed_recovered``; the reference price is the best bid / ask over the
   NON-STALE venue books stamped with the minimum ``lastDataTs`` (last
   non-HEARTBEAT event) of the venues forming the touch -> ``on_market``;
   the account is marked at the same mid so the §12.1 identity is exact;
4. the TCA market timeline records the consolidated state (crossed states
   skipped + counted, locked kept), trades and halts;
5. a parent whose window has closed, whose children are all terminal and
   whose ``end_ts`` the timeline covers is finalised: TCA, attribution, and
   its decision trace becomes ready;
6. the feature engine applies the event (and the research label mid series
   takes one sample per book refresh, exactly as the feature pipeline
   builds it); an emitted vector (decision cadence) runs the decision: bar
   roll for the EWMA variance, the three alphas + ensemble, the portfolio
   solve, delta vs position + in-flight, one parent order (algo by urgency
   band) per decision cycle.  The trace's ``signal`` stage carries the
   ENSEMBLE signal first (index 0: the signal the portfolio sized on, the
   one ``v_order_chain`` / ``explain`` attribute to the order) followed by
   the member signals in ensemble order, each labelled by its alpha id in
   ``model_version``;
7. the live parent's algo issues the children due at this event; EACH child
   is routed (SOR, ``NO_ROUTE`` rejects + counts), checked against the
   declared controls (``min_slice_interval_ns``, ``latency_budget_ns``,
   ``max_participation`` — blocked + counted), checked pre-trade by the hard
   risk engine (REJECT -> never submitted), then submitted;
8. ready traces are emitted in decision order to the sinks.

Nothing on this path reads a wall clock or an unordered container; every
number a port must reproduce is either an integer or a float produced by
the same reference component in the same order.

**Realized IC (pinned to the research definition).**  The realized IC of an
alpha is the Pearson correlation of its ``expected_return`` on the
confidence > 0 decisions against the event-time forward label of
:func:`iap.labels.compute_labels` at the decision timestamps — the SAME
function, on the SAME kind of mid series (one sample per feature-engine
book refresh, non-tradable refreshes as blackout samples), with the SAME
validity rules (observed through ``t + h``, tradable anchor, fresh tradable
forward sample, no blackout inside ``(t, t + h]``) the research pipeline
applies.  Both the mid-to-mid and the cost-adjusted (half-spread at both
ends) label are reported, plus the shift-by-one IC (signal lagged one
decision — conventions §7: a lookahead collapses under it, a genuine
signal does not), at the MVP holding horizon AND at the alpha's own fitted
horizon (the research IC in the registry is measured there, so only that
pair is a like-for-like ``ic_gap``).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Sequence, Set, Tuple

from iap.contracts.ids import NO_ROUTE
from iap.contracts.types import (
    AlphaSignal,
    Attribution,
    ChildOrder,
    Decision,
    ExecStatus,
    ExecutionReport,
    OrderType,
    ParentOrder,
    PortfolioTarget,
    RiskDecision,
    Side,
    TCAResult,
    VenueDecision,
)
from iap.core.events import EventType, MarketEvent, SessionStatus
from iap.execution.config import ExecConfig, SorOptions, load_sor_options
from iap.execution.sor import SmartOrderRouter
from iap.execution.simulator import ExecutionSimulator
from iap.execution.types import InstrumentSpec, LatencyConfig, Liquidity, VenueSpec
from iap.features.context import InstrumentContext, SessionClock
from iap.features.engine import FeatureEngine, FeatureVector
from iap.labels.labels import HORIZONS_NS, LabelResult, MidSeries, compute_labels, max_sample_age
from iap.mvp.adapters import (
    AlgoScheduler,
    MarketView,
    RiskContext,
    RiskEngineAdapter,
    SimulatorAdapter,
    SorAdapter,
    SorMarket,
    TcaAdapter,
    TcaMarket,
)
from iap.mvp.alpha import AlphaEnsemble, LinearZAlpha, load_alphas
from iap.mvp.config import ControlsSpec, MvpConfig
from iap.mvp.feed import FeedResult, build_reference_data
from iap.mvp.portfolio import PortfolioConstraints, PortfolioState, SingleStockPortfolio
from iap.orderbook.book import DEPTH_LEVELS, ConsolidatedBook, OrderBook
from iap.reference.refdata import ReferenceData
from iap.risk.engine import RiskEngine
from iap.risk.events import Decision as RiskDecisionCode
from iap.risk.orders import Fill as RiskFill
from iap.risk.refdata import instrument_refs_from_reference_data
from iap.tca.fills import MarketTimeline
from iap.trace.attribution import attribute
from iap.trace.builder import TraceBuilder
from iap.contracts.protocols import TraceSink

__all__ = [
    "BAR_NS",
    "Account",
    "Counters",
    "IcResult",
    "OrderOutcome",
    "MvpEngine",
    "feature_context",
    "horizon_name",
    "load_controls",
    "pearson",
]

BAR_NS = 60_000_000_000
_NS = 1_000_000_000
_MID_FEATURE = "mid_price_v1"
_PNL_IDENTITY_TOL = 1e-9


def horizon_name(horizon_ns: int) -> str:
    """The pinned label-horizon name of ``horizon_ns`` (conventions §7);
    raises when the duration is not one of the eleven pinned horizons."""
    for name, ns in HORIZONS_NS.items():
        if ns == horizon_ns:
            return name
    raise ValueError(f"horizon_ns {horizon_ns} is not a pinned label horizon "
                     f"({', '.join(HORIZONS_NS)})")


def load_controls(cfg: MvpConfig) -> ControlsSpec:
    """The declared controls from ``execution.json`` ``defaults`` (fail-fast)."""
    path = cfg.reference_path("execution")
    with open(path, encoding="utf-8") as fh:
        root = json.load(fh)
    defaults = root.get("defaults")
    if not isinstance(defaults, dict):
        raise ValueError(f"{path}: missing defaults block")

    def integer(key: str, lo: int) -> int:
        v = defaults.get(key)
        if isinstance(v, bool) or not isinstance(v, int) or v < lo:
            raise ValueError(f"{path}: defaults.{key} must be an integer >= {lo}")
        return v

    part = defaults.get("max_participation")
    if isinstance(part, bool) or not isinstance(part, (int, float)) or not 0.0 < part <= 1.0:
        raise ValueError(f"{path}: defaults.max_participation must be in (0, 1]")
    return ControlsSpec(
        max_child_qty=integer("max_child_qty", 1),
        max_participation=float(part),
        min_slice_interval_ns=integer("min_slice_interval_ns", 0),
        latency_budget_ns=integer("latency_budget_ns", 1),
    )


def feature_context(cfg: MvpConfig, ref: ReferenceData) -> InstrumentContext:
    """The feature engine's :class:`InstrumentContext` of the configured
    instrument (session bounds from ``mvp.json``, tick / class from the
    MVP reference data) — the one context both the engine and the research
    frame builder use."""
    inst = ref.instrument(cfg.instrument)
    h0, m0, _ = (int(x) for x in cfg.session.open.split(":"))
    h1, m1, s1 = (int(x) for x in cfg.session.close.split(":"))
    return InstrumentContext(
        instrument_id=inst.instrument_id, symbol=inst.symbol, asset_class=inst.asset_class,
        tick_size=float(inst.tick_size), session_open_min=h0 * 60 + m0,
        session_close_min=h1 * 60 + m1 + (1 if s1 >= 30 else 0),
        ref_instrument_id=inst.instrument_id, session_timezone=cfg.session.timezone,
        clock=SessionClock(cfg.session.timezone),
    )


def pearson(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    """Pearson correlation (``None`` with fewer than 3 pairs or a degenerate side)."""
    n = len(xs)
    if n < 3 or n != len(ys):
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0.0 or syy <= 0.0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


@dataclass
class Account:
    """Per-instrument deterministic P&L account (quote currency = USD), the
    Java ``BacktestEngine.Account`` semantics: ``equity = cash + position *
    mark``; ``gross_pnl`` accumulates ``position * dmark``; ``spread_cost``
    is the fill-vs-mark slippage; fees / rebates / impact are exact sums."""

    position: int = 0
    cash: float = 0.0
    fees: float = 0.0
    rebates: float = 0.0
    impact: float = 0.0
    spread_cost: float = 0.0
    gross_pnl: float = 0.0
    mark: float = 0.0
    mark_valid: bool = False
    fill_count: int = 0
    orders_submitted: int = 0
    session_volume: int = 0
    filled_qty: int = 0
    last_child_decision_ts: Optional[int] = None
    equity_peak: float = 0.0
    max_drawdown: float = 0.0

    def equity(self) -> float:
        return self.cash + float(self.position) * (self.mark if self.mark_valid else 0.0)

    def mark_to(self, mid: float) -> None:
        """Mark-to-market at ``mid`` (the risk engine's reference mid)."""
        if self.mark_valid:
            self.gross_pnl += float(self.position) * (mid - self.mark)
        self.mark = mid
        self.mark_valid = True
        eq = self.equity()
        self.equity_peak = max(self.equity_peak, eq)
        self.max_drawdown = max(self.max_drawdown, self.equity_peak - eq)

    def book_fill(self, side: int, qty: int, price: float, fee: float, impact: float) -> None:
        sgn = 1.0 if side == 0 else -1.0
        self.cash -= sgn * float(qty) * price
        self.cash -= fee
        self.cash -= impact
        if fee >= 0.0:
            self.fees += fee
        else:
            self.rebates += -fee
        self.impact += impact
        if not self.mark_valid:
            self.mark = price
            self.mark_valid = True
        self.spread_cost += sgn * float(qty) * (price - self.mark)
        self.position += qty if side == 0 else -qty
        self.filled_qty += qty
        self.fill_count += 1

    @property
    def fees_net(self) -> float:
        return self.fees - self.rebates


@dataclass
class Counters:
    """Every drop / block / skip on the path is counted (conventions §8)."""

    events: int = 0
    decisions: int = 0
    decisions_without_covariance: int = 0
    decisions_flat: int = 0
    decisions_parent_live: int = 0
    decisions_window_beyond_session: int = 0   #: parent window would end after the session close
    parent_orders: int = 0
    child_orders_generated: int = 0
    child_orders_submitted: int = 0
    risk_allowed: int = 0
    risk_rejected: int = 0
    sor_no_route: int = 0
    participation_capped: int = 0
    participation_blocked: int = 0
    slice_interval_blocked: int = 0
    latency_budget_blocked: int = 0
    sequence_gaps: int = 0
    feed_recoveries: int = 0
    fills: int = 0
    parents_finalised: int = 0
    parents_without_tca: int = 0
    timeline_crossed_skipped: int = 0

    def to_dict(self) -> Dict[str, int]:
        return {k: int(v) for k, v in sorted(self.__dict__.items())}


@dataclass(frozen=True)
class IcResult:
    """Realized IC of one alpha at one horizon (module docstring).

    ``ic`` / ``ic_cost`` are the Pearson correlations of the expected
    return against the mid-to-mid / cost-adjusted label (``None`` with
    fewer than three valid pairs or a degenerate side); ``ic_shifted`` is
    the mid IC with the signal lagged by one decision (the shift-by-one
    leakage test); ``n`` counts the valid pairs, ``n_signals`` the
    confidence > 0 decisions the labels were requested for.
    """

    horizon: str
    ic: Optional[float]
    ic_cost: Optional[float]
    ic_shifted: Optional[float]
    n: int
    n_signals: int


@dataclass
class OrderOutcome:
    """Everything the report needs about one finalised parent order."""

    parent: ParentOrder
    signal: AlphaSignal
    tca: Optional[TCAResult]
    attribution: Optional[Attribution]
    realized_bps: float
    filled_notional: float
    n_children_submitted: int
    n_children_rejected: int
    venue_qty: Dict[int, int]
    latencies_ns: Tuple[int, ...]


#: The declared controls a generated child can be blocked by, in check order
#: (Java BacktestEngine.decide).  A blocked child never leaves the strategy:
#: it gets no routing, no risk decision and no report, so it is NOT written
#: into the trace's child_orders / routing stages — only counted here and in
#: ``ParentOrder.params["children_blocked_<control>"]`` at finalisation.
BLOCK_CONTROLS = ("slice_interval", "latency_budget", "participation")


@dataclass
class _LiveParent:
    parent: ParentOrder
    signal: AlphaSignal
    decision: "_Decision"
    children: Dict[int, ChildOrder] = field(default_factory=dict)  #: submitted, by id
    reports: List[ExecutionReport] = field(default_factory=list)
    rejected: int = 0          #: generated but not submitted (any reason)
    #: control-blocked children by control (never routed to risk or a venue;
    #: written into ParentOrder.params as children_blocked_<control>)
    blocked: Dict[str, int] = field(default_factory=lambda: dict.fromkeys(BLOCK_CONTROLS, 0))
    fill_impact: float = 0.0
    fill_fees: float = 0.0

    def all_terminal(self, sim: SimulatorAdapter) -> bool:
        return all(sim.open_qty(cid) == 0 for cid in self.children)

    def committed(self, sim: SimulatorAdapter) -> int:
        """Filled + open qty over the parent's submitted children."""
        filled = sum(r.filled_qty for r in self.reports)
        return filled + sum(sim.open_qty(cid) for cid in self.children)


@dataclass
class _Decision:
    builder: TraceBuilder
    ready: bool = False


class MvpEngine:
    """The loop (module docstring).  Feed events through :meth:`on_event`
    in stream order, then :meth:`finish`; traces reach ``sink`` in decision
    order as they become ready."""

    def __init__(self, cfg: MvpConfig, feed: FeedResult, sink: TraceSink, *,
                 ref: Optional[ReferenceData] = None) -> None:
        self.cfg = cfg
        self.feed = feed
        self.sink = sink
        self.ref = ref if ref is not None else build_reference_data(cfg)
        inst = self.ref.instrument(cfg.instrument)
        if inst.instrument_id != feed.instrument_id:
            raise ValueError(f"feed instrument {feed.instrument_id} != config instrument "
                             f"{inst.instrument_id} ({cfg.instrument})")
        self.iid = inst.instrument_id
        self.tick = float(inst.tick_size)
        self.strategy_id = cfg.strategy_id
        self.session_id = cfg.session_id
        self.controls = load_controls(cfg)
        # The end of the session is known EX ANTE from the configured calendar
        # (open/close in the session's zone -> UTC ns); the loop never reads
        # the end of the captured stream, which a live loop cannot know.
        self.session_open_ts, self.session_close_ts = self.ref.session_bounds_ns(
            inst.asset_class, cfg.session.trading_day)
        self.counters = Counters()
        self.account = Account()

        # ---- execution: simulator, SOR, algos ------------------------------
        venues: Dict[int, VenueSpec] = {}
        for name in cfg.venues:
            v = self.ref.venue(name)
            venues[v.venue_id] = VenueSpec(
                venue_id=v.venue_id, name=v.venue, is_fx=False,
                taker_fee_per_share=float(v.fees.get("taker_fee_per_share", 0.0)),
                maker_rebate_per_share=float(v.fees.get("maker_rebate_per_share", 0.0)),
                commission_per_million=float(v.fees.get("commission_per_million", 0.0)),
                latency_mean_ns=v.latency_mean_ns, latency_jitter_ns=v.latency_jitter_ns,
            )
        self.venues = dict(sorted(venues.items()))
        self.venue_names = {vid: spec.name for vid, spec in self.venues.items()}
        latency = LatencyConfig(cfg.execution.latency_decision_ns, cfg.execution.latency_risk_ns,
                                cfg.execution.latency_wire_ns)
        exec_doc = cfg.reference_documents()[cfg.reference["execution"]]
        self.exec_config = ExecConfig(
            latency=latency, seed=cfg.seed,
            impact_coeff_bps_per_pct_adv=float(exec_doc["cost_model"]["impact_coeff_bps_per_pct_adv"]),
            instruments={self.iid: InstrumentSpec(self.iid, self.tick, 1.0, float(inst.adv),
                                                  inst.currency or "USD")},
            venues=self.venues,
        )
        self.sim = SimulatorAdapter(ExecutionSimulator(self.exec_config))
        self.book: ConsolidatedBook = self.sim.simulator.instrument_book(self.iid)
        sor_file = load_sor_options(cfg.reference_path("execution"))
        sor_options = SorOptions(cfg.sor.prefer_rebate, cfg.sor.max_venue_latency_ns)
        if sor_options != sor_file:
            raise ValueError("mvp.json sor block must equal execution.json sor "
                             f"({sor_options} != {sor_file})")
        self.sor = SorAdapter(SmartOrderRouter(self.venues, sor_options), self.venues,
                              list(self.venues))
        self._next_parent_id = 1
        self._next_child_id = 1
        self.scheduler = AlgoScheduler(
            self.controls.max_child_qty, cfg.execution.twap_slices, cfg.execution.is_slices,
            cfg.execution.is_risk_aversion, cfg.execution.pov_participation,
            self._new_child_id, self._committed,
        )
        self.min_venue_latency_ns = min(v.latency_mean_ns for v in self.venues.values())

        # ---- risk ---------------------------------------------------------
        risk_doc = cfg.reference_documents()[cfg.reference["risk"]]
        self.risk_engine = RiskEngine.from_config(
            risk_doc, instrument_refs_from_reference_data(self.ref))
        self.risk = RiskEngineAdapter(self.risk_engine)
        self._venue_stale: Dict[int, bool] = {}
        self._last_data_ts: Dict[int, int] = {}
        self._mark_ts: Optional[int] = None

        # ---- features + alphas + portfolio --------------------------------
        self.features = FeatureEngine({self.iid: feature_context(cfg, self.ref)},
                                      cadence_ns=cfg.decision_cadence_ns)
        self.feature_version = self.features.feature_version
        self._mid_index = self.features.feature_names.index(_MID_FEATURE)
        self.horizon = horizon_name(cfg.horizon_ns)
        self.alphas: Tuple[LinearZAlpha, ...] = load_alphas(
            cfg.reference_path("alpha_params"), cfg.alphas, self.features.feature_names,
            self.feature_version, cfg.horizon_ns)
        self.ensemble = AlphaEnsemble(self.alphas, cfg.horizon_ns)
        for alpha in self.alphas:
            if alpha.model.horizon not in HORIZONS_NS:
                raise ValueError(f"{alpha.alpha_id}: fitted horizon {alpha.model.horizon!r} "
                                 "is not a pinned label horizon")
        self.model_version = self.ensemble.version
        self.constraints = PortfolioConstraints(cfg.portfolio)
        self.portfolio = SingleStockPortfolio(self.strategy_id, self.ensemble.alpha_id,
                                              self.feature_version, self.model_version)
        self.tca = TcaAdapter()
        self.data_version = feed.data_version
        self.config_version = cfg.config_version()

        # ---- rolling state -------------------------------------------------
        self.timeline = MarketTimeline()
        self._current_bar: Optional[int] = None
        self._bar_mid: Optional[float] = None
        self._last_bar_mid: Optional[float] = None
        self.bar_returns: List[float] = []
        self._decisions: List[_Decision] = []
        self._live: Optional[_LiveParent] = None
        self._closing: List[_LiveParent] = []
        self.outcomes: List[OrderOutcome] = []
        #: (decision index, expected_return) of every confidence > 0 signal
        self.ic_samples: Dict[str, List[Tuple[int, float]]] = {a.alpha_id: [] for a in self.alphas}
        self.ic_samples[self.ensemble.alpha_id] = []
        #: decision timestamps (label anchors), in decision order
        self.decision_ts: List[int] = []
        #: the research label series: one sample per feature-engine book refresh
        self.mid_series = MidSeries()
        self._last_refresh_seq = 0
        self._labels: Dict[str, Tuple[int, LabelResult]] = {}
        self.first_ts: Optional[int] = None
        self.last_ts: Optional[int] = None
        self._finished = False
        self.trace_ids: List[str] = []
        self._trace_id_set: Set[str] = set()

    # ------------------------------------------------------------- id pools

    def _new_child_id(self) -> int:
        cid = self._next_child_id
        self._next_child_id += 1
        return cid

    def _committed(self, parent_order_id: int) -> int:
        live = self._live
        if live is None or live.parent.parent_order_id != parent_order_id:
            raise ValueError(f"parent {parent_order_id} is not live")
        return live.committed(self.sim)

    # ----------------------------------------------------------- event loop

    def on_event(self, ev: MarketEvent) -> None:
        """Process one market event in the pinned order (module docstring)."""
        if self._finished:
            raise RuntimeError("engine already finished")
        if ev.instrument_id != self.iid:
            raise ValueError(f"event {ev.event_id} is for instrument {ev.instrument_id}, "
                             f"engine trades {self.iid}")
        t = ev.exchange_ts
        if self.first_ts is None:
            self.first_ts = t
        self.last_ts = t
        self.counters.events += 1
        # 1. simulator + reports (fills -> account + risk, terminals -> risk)
        self._process_reports(self.sim.on_market_event(ev))
        # 2. session volume
        if ev.event_type in (EventType.EXECUTE, EventType.TRADE):
            self.account.session_volume += ev.qty
        # 3. risk wiring: stale transitions + reference price + account mark
        self._on_market(ev)
        # 4. TCA timeline
        self._record_timeline(ev)
        # 5. finalise closed parents
        self._finalise_due(t)
        # 6. features (+ label series sample) -> decision
        vec = self.features.apply(ev)
        self._sample_mid_series(t)
        if vec is not None:
            self._decide(ev, vec)
        # 7. algo children of the live parent
        if self._live is not None:
            self._issue_children(ev)
        # 8. emit ready traces in decision order
        self._flush()

    def finish(self) -> None:
        """End of stream: cancel residual children, finalise every parent,
        emit every trace, assert the §12.1 identity."""
        if self._finished:
            return
        self._finished = True
        end_ts = self.last_ts if self.last_ts is not None else 0
        self._process_reports(self.sim.finish(end_ts))
        if self._live is not None:
            self._closing.append(self._live)
            self._live = None
        for live in list(self._closing):
            self._finalise(live, force=True)
        self._closing.clear()
        self._flush()
        if self._decisions:
            raise RuntimeError("traces left unemitted at session end")
        self.assert_pnl_identity()

    # -------------------------------------------------------------- reports

    def _process_reports(self, reports: Sequence[ExecutionReport]) -> None:
        filled = False
        for rep in reports:
            live = self._owner_of(rep.order_id)
            if live is not None:
                live.reports.append(rep)
                live.decision.builder.add_fill(rep)
            if rep.status in (ExecStatus.PARTIAL, ExecStatus.FILLED):
                fill = self.sim.fills_by_execution[rep.execution_id]
                price = float(rep.fill_price_ticks) * self.tick
                self.account.book_fill(int(fill.side), rep.filled_qty, price, rep.fees,
                                       fill.impact_cost)
                self.counters.fills += 1
                filled = True
                if live is not None:
                    live.fill_fees += rep.fees
                    live.fill_impact += fill.impact_cost
                applied = self.risk_engine.on_fill(RiskFill(
                    ts=rep.exchange_ts, strategy_id=self.strategy_id, instrument_id=self.iid,
                    order_id=rep.order_id, side=int(fill.side), qty=rep.filled_qty,
                    price_ticks=rep.fill_price_ticks))
                if not applied:
                    # The account booked a fill the risk engine refused as
                    # malformed: the two positions have diverged and the
                    # §12.1 identity is gone — fail closed, never continue.
                    raise RuntimeError(f"risk engine rejected fill execution "
                                       f"{rep.execution_id} of child {rep.order_id} as malformed")
            if rep.status in (ExecStatus.FILLED, ExecStatus.CANCELED, ExecStatus.EXPIRED,
                              ExecStatus.REJECTED):
                self.risk_engine.on_order_done(rep.order_id)
        if filled:
            # §12.1 (PaperUnitsTest): after EVERY fill the risk position equals
            # the account position and the risk daily P&L equals gross - spread.
            self.assert_pnl_identity()

    def _owner_of(self, child_order_id: int) -> Optional[_LiveParent]:
        if self._live is not None and child_order_id in self._live.children:
            return self._live
        for live in self._closing:
            if child_order_id in live.children:
                return live
        return None

    # ---------------------------------------------------------- risk wiring

    def _on_market(self, ev: MarketEvent) -> None:
        """PaperTrading.RiskWiring.onMarket, verbatim semantics."""
        if ev.event_type != EventType.HEARTBEAT:
            self._last_data_ts[ev.venue_id] = ev.exchange_ts
        best_bid: Optional[int] = None
        best_ask: Optional[int] = None
        books = self.book.books
        for vid in sorted(books):
            vb = books[vid]
            stale = vb.stale
            was = self._venue_stale.get(vid, False)
            self._venue_stale[vid] = stale
            if stale and not was:
                self.counters.sequence_gaps += 1
                self.risk_engine.on_sequence_gap(self.iid, ev.exchange_ts)
            elif was and not stale:
                self.counters.feed_recoveries += 1
                self.risk_engine.on_feed_recovered(self.iid, ev.exchange_ts)
            if stale:
                continue
            bb = vb.best_bid()
            ba = vb.best_ask()
            if bb is not None:
                best_bid = bb[0] if best_bid is None else max(best_bid, bb[0])
            if ba is not None:
                best_ask = ba[0] if best_ask is None else min(best_ask, ba[0])
        if best_bid is None or best_ask is None:
            return  # no fresh two-sided venue: the previous mark ages
        mark_ts: Optional[int] = None
        for vid in sorted(books):
            vb = books[vid]
            if vb.stale:
                continue
            bb = vb.best_bid()
            ba = vb.best_ask()
            at_touch = (bb is not None and bb[0] == best_bid) or \
                (ba is not None and ba[0] == best_ask)
            data_ts = self._last_data_ts.get(vid)
            if at_touch and data_ts is not None:
                mark_ts = data_ts if mark_ts is None else min(mark_ts, data_ts)
        if mark_ts is None:
            return
        self.risk_engine.on_market(self.iid, best_bid, best_ask, mark_ts)
        # The account marks at exactly the reference the risk engine keeps:
        # an update older than the stored mark is dropped there (and
        # counted, ``risk_market_regressions_dropped_total``), so it is
        # dropped here too — the §12.1 identity then holds at every event.
        if self._mark_ts is not None and mark_ts < self._mark_ts:
            return
        self._mark_ts = mark_ts
        self.account.mark_to(float(best_bid + best_ask) * self.tick / 2.0)

    # ------------------------------------------------------------- timeline

    def _record_timeline(self, ev: MarketEvent) -> None:
        tl = self.timeline
        if ev.event_type == EventType.TRADE:
            tl.add_trade(ev.exchange_ts, ev.price_ticks * self.tick, ev.qty)
        if ev.event_type == EventType.STATUS and ev.qty == int(SessionStatus.HALT):
            tl.add_halt(ev.exchange_ts)
        bb, ba = self.book.best_bid(), self.book.best_ask()
        if bb is None or ba is None:
            return
        if not tl.append_state_pinned(ev.exchange_ts, bb[0] * self.tick, ba[0] * self.tick,
                                      bb[1], ba[1]):
            self.counters.timeline_crossed_skipped += 1

    # ------------------------------------------------------------- decisions

    def _sample_mid_series(self, t: int) -> None:
        """One label-series sample per feature-engine book refresh, exactly
        as ``iap.features.__main__`` / ``iap.alpha.goldenframes`` build it
        (API_FEATURES §6): the merged mid + half-spread of a tradable
        refresh, a blackout sample (NaN, ``tradable=False``) otherwise."""
        st = self.features.states[self.iid]
        if st.refresh_seq == self._last_refresh_seq:
            return
        self._last_refresh_seq = st.refresh_seq
        if st.label_tradable:
            self.mid_series.append(t, st.mid, st.spread_ticks * st.tick / 2.0, True)
        else:
            self.mid_series.append(t, math.nan, math.nan, False)

    def _roll_bar(self, vec: FeatureVector) -> None:
        """Java ``PaperTrading`` sizing bar roll: last mid per 1-minute bucket."""
        if not vec.validity[self._mid_index]:
            return
        mid = vec.values[self._mid_index]
        bar = (vec.timestamp // BAR_NS) * BAR_NS
        if bar != self._current_bar:
            if self._bar_mid is not None:
                if self._last_bar_mid is not None:
                    self.bar_returns.append(math.log(self._bar_mid / self._last_bar_mid))
                self._last_bar_mid = self._bar_mid
            self._current_bar = bar
        self._bar_mid = mid

    def _inflight_signed(self) -> int:
        live = self._live
        if live is None:
            return 0
        signed = 0
        for cid, child in live.children.items():
            q = self.sim.open_qty(cid)
            signed += q if child.side is Side.BID else -q
        return signed

    def _decide(self, ev: MarketEvent, vec: FeatureVector) -> None:
        t = ev.exchange_ts
        self.counters.decisions += 1
        builder = TraceBuilder(self.session_id, self.iid, t, ev.sequence, self.data_version,
                               self.feature_version, self.model_version, self.config_version)
        decision = _Decision(builder)
        self._decisions.append(decision)
        index = len(self.decision_ts)
        self.decision_ts.append(t)
        self._roll_bar(vec)
        member_signals: Dict[str, AlphaSignal] = {}
        for alpha in self.ensemble.members:
            sig = alpha.generate(vec)
            member_signals[alpha.alpha_id] = sig
            if sig.confidence > 0.0:
                self.ic_samples[alpha.alpha_id].append((index, sig.expected_return))
        signal = self.ensemble.combine(member_signals)
        if signal.confidence > 0.0:
            self.ic_samples[self.ensemble.alpha_id].append((index, signal.expected_return))
        # signal[0] = the acting (ensemble) signal, then its components.
        builder.add_signal(signal)
        for alpha in self.ensemble.members:
            builder.add_signal(member_signals[alpha.alpha_id])

        state = PortfolioState(self.iid, t, self.account.position, self.account.mark,
                               tuple(self.bar_returns))
        if not self.portfolio.ready(state, self.constraints) or not self.account.mark_valid:
            self.counters.decisions_without_covariance += 1
            decision.ready = True
            return
        target: PortfolioTarget = self.portfolio.construct(
            list(member_signals.values()) + [signal], state, self.constraints)
        builder.set_portfolio(target)
        delta = target.targets[0].target_qty - (self.account.position + self._inflight_signed())
        if delta == 0:
            self.counters.decisions_flat += 1
            decision.ready = True
            return
        if self._live is not None and self._live.parent.end_ts > t:
            self.counters.decisions_parent_live += 1
            decision.ready = True
            return
        end_ts = t + self.cfg.execution.parent_window_ns
        if end_ts > self.session_close_ts:
            self.counters.decisions_window_beyond_session += 1
            decision.ready = True
            return
        urgency = signal.confidence
        algo = self.cfg.execution.algo_for(urgency)
        parent = ParentOrder(
            parent_order_id=self._next_parent_id, strategy_id=self.strategy_id,
            alpha_id=self.ensemble.alpha_id, instrument_id=self.iid,
            side=Side.BID if delta > 0 else Side.ASK, qty=abs(delta), algo=algo,
            decision_ts=t,
            arrival_ts=t + self.exec_config.latency.internal_ns + self.min_venue_latency_ns,
            end_ts=end_ts, urgency=urgency, limit_price_ticks=0,
            params=self.scheduler.params_for(algo),
        )
        self._next_parent_id += 1
        self.counters.parent_orders += 1
        builder.add_parent_order(parent)
        self.scheduler.open(parent)
        self._live = _LiveParent(parent=parent, signal=signal, decision=decision)

    # -------------------------------------------------------------- children

    def _contra_depth(self, book: Optional[OrderBook], side: Side) -> int:
        if book is None:
            return 0
        levels = book.depth(1 if side is Side.BID else 0, DEPTH_LEVELS)
        return sum(q for _, q in levels)

    def _issue_children(self, ev: MarketEvent) -> None:
        live = self._live
        if live is None:
            return
        parent = live.parent
        t = ev.exchange_ts
        builder = live.decision.builder
        for child in self.scheduler.generate_child_orders(parent, MarketView(ev, self.book)):
            self.counters.child_orders_generated += 1
            passive = child.order_type is OrderType.LIMIT
            vd: VenueDecision = self.sor.route(child, SorMarket(self.book, passive))
            if vd.venue_id == NO_ROUTE:
                # A routing verdict: traced (routing + child) with venue 0.
                self.counters.sor_no_route += 1
                builder.add_routing(vd)
                builder.add_child_order(child)
                live.rejected += 1
                continue
            venue_book = self.book.books.get(vd.venue_id)
            order_type = child.order_type
            price = 0
            if passive:
                best = None
                if venue_book is not None:
                    best = venue_book.best_bid() if child.side is Side.BID \
                        else venue_book.best_ask()
                if best is not None:
                    price = best[0]
                else:
                    order_type = OrderType.MARKET
            routed = replace(child, venue_id=vd.venue_id, price_ticks=price, order_type=order_type)
            # Declared controls (Java BacktestEngine.decide, pinned order).
            ctl = self.controls
            acct = self.account
            # A control-blocked child never leaves the strategy (BLOCK_CONTROLS):
            # counted, not traced.
            if (ctl.min_slice_interval_ns > 0 and acct.last_child_decision_ts is not None
                    and t - acct.last_child_decision_ts < ctl.min_slice_interval_ns):
                self.counters.slice_interval_blocked += 1
                live.blocked["slice_interval"] += 1
                live.rejected += 1
                continue
            venue_latency = self.exec_config.latency.internal_ns + \
                self.venues[vd.venue_id].latency_mean_ns
            if venue_latency > ctl.latency_budget_ns:
                self.counters.latency_budget_blocked += 1
                live.blocked["latency_budget"] += 1
                live.rejected += 1
                continue
            if ctl.max_participation < 1.0:
                depth = self._contra_depth(venue_book, child.side)
                cap_depth = int(math.floor(ctl.max_participation * depth))
                cap_volume = int(math.floor(ctl.max_participation * acct.session_volume)) \
                    - acct.filled_qty
                cap = max(min(cap_depth, cap_volume), 0)
                if cap == 0:
                    self.counters.participation_blocked += 1
                    live.blocked["participation"] += 1
                    live.rejected += 1
                    continue
                if routed.qty > cap:
                    self.counters.participation_capped += 1
                    routed = replace(routed, qty=cap)
            builder.add_routing(vd)
            builder.add_child_order(routed)
            rd: RiskDecision = self.risk.evaluate(
                routed, RiskContext(self.strategy_id, parent.urgency, t))
            builder.add_risk(rd)
            if rd.decision is not Decision.ALLOW:
                self.counters.risk_rejected += 1
                live.rejected += 1
                continue
            self.counters.risk_allowed += 1
            reports = self.sim.submit(routed)
            live.children[routed.child_order_id] = routed
            acct.orders_submitted += 1
            acct.last_child_decision_ts = t
            self.counters.child_orders_submitted += 1
            for rep in reports:
                live.reports.append(rep)
                builder.add_fill(rep)

    # ------------------------------------------------------------- finalise

    def _finalise_due(self, t: int) -> None:
        live = self._live
        if live is not None and live.parent.end_ts <= t:
            self._closing.append(live)
            self._live = None
        for live in list(self._closing):
            if live.all_terminal(self.sim) and self.timeline.last_ts is not None \
                    and self.timeline.last_ts >= live.parent.end_ts:
                self._finalise(live, force=False)
                self._closing.remove(live)

    def _finalise(self, live: _LiveParent, *, force: bool) -> None:
        builder = live.decision.builder
        # Preserve the control blocks in the trace: the parent's params gain
        # one children_blocked_<control> counter per declared control.
        params = dict(live.parent.params)
        for control in BLOCK_CONTROLS:
            params[f"children_blocked_{control}"] = float(live.blocked[control])
        parent = replace(live.parent, params=params)
        builder.replace_parent_order(parent)
        live.parent = parent
        self.scheduler.close(parent.parent_order_id)
        tl = self.timeline
        can_tca = live.all_terminal(self.sim) and tl.last_ts is not None \
            and tl.last_ts >= parent.end_ts
        if not can_tca:
            if not force:
                raise RuntimeError(f"parent {parent.parent_order_id} finalised too early")
            self.counters.parents_without_tca += 1
            self.outcomes.append(OrderOutcome(
                parent=parent, signal=live.signal, tca=None, attribution=None,
                realized_bps=0.0, filled_notional=0.0,
                n_children_submitted=len(live.children),
                n_children_rejected=live.rejected, venue_qty={}, latencies_ns=()))
            live.decision.ready = True
            return
        liquidity: Dict[int, Liquidity] = {}
        venue_qty: Dict[int, int] = {}
        for rep in live.reports:
            if rep.status in (ExecStatus.PARTIAL, ExecStatus.FILLED):
                fill = self.sim.fills_by_execution[rep.execution_id]
                liquidity[rep.execution_id] = fill.liquidity
                venue_qty[rep.venue_id] = venue_qty.get(rep.venue_id, 0) + rep.filled_qty
        latencies = {cid: self.sim.arrival_ts(cid) - child.submit_ts
                     for cid, child in live.children.items()}
        market = TcaMarket(tl, self.tick, liquidity, latencies)
        tca = self.tca.analyse(parent, live.reports, market)
        builder.add_tca(tca)
        # Realized P&L of the order in bps of its filled notional (at the
        # decision mid): fills marked to the end-of-window mid, net of fees
        # and impact; 0 when nothing filled.  The attribution's residual
        # against it is reported, never absorbed.
        m_d = tl.mid_at(parent.decision_ts)
        m_e = tl.mid_at(parent.end_ts)
        s = 1.0 if parent.side is Side.BID else -1.0
        filled_notional = 0.0
        mtm = 0.0
        for rep in live.reports:
            if rep.status in (ExecStatus.PARTIAL, ExecStatus.FILLED):
                price = rep.fill_price_ticks * self.tick
                filled_notional += rep.filled_qty * m_d
                mtm += s * rep.filled_qty * (m_e - price)
        realized_bps = 0.0
        if filled_notional > 0.0:
            realized_bps = (mtm - live.fill_fees - live.fill_impact) / filled_notional * 1e4
        attribution = attribute(parent, live.signal, tca, realized_bps)
        builder.set_attribution(attribution)
        self.outcomes.append(OrderOutcome(
            parent=parent, signal=live.signal, tca=tca, attribution=attribution,
            realized_bps=realized_bps, filled_notional=filled_notional,
            n_children_submitted=len(live.children),
            n_children_rejected=live.rejected, venue_qty=dict(sorted(venue_qty.items())),
            latencies_ns=tuple(latencies[c] for c in sorted(latencies))))
        self.counters.parents_finalised += 1
        live.decision.ready = True

    def _flush(self) -> None:
        while self._decisions and self._decisions[0].ready:
            decision = self._decisions.pop(0)
            trace = decision.builder.build()
            if trace.trace_id in self._trace_id_set:
                raise RuntimeError(f"duplicate trace id {trace.trace_id}")
            self.sink.emit(trace)
            self.trace_ids.append(trace.trace_id)
            self._trace_id_set.add(trace.trace_id)

    # -------------------------------------------------------------- results

    def risk_daily_pnl(self) -> float:
        """The risk engine's daily P&L (realized + unrealized, reporting ccy)."""
        pnl = self.risk_engine.global_daily_pnl()
        if pnl is None:
            raise RuntimeError("risk daily P&L undeterminable (missing conversion rate)")
        return pnl

    def assert_pnl_identity(self) -> None:
        """§12.1: risk daily P&L == gross - spread_cost; the risk position ==
        the account position."""
        acct = self.account
        if self.risk_engine.position(self.iid) != acct.position:
            raise RuntimeError(f"risk position {self.risk_engine.position(self.iid)} != "
                               f"account position {acct.position}")
        lhs = self.risk_daily_pnl()
        rhs = acct.gross_pnl - acct.spread_cost
        if abs(lhs - rhs) > _PNL_IDENTITY_TOL * max(1.0, abs(lhs), abs(rhs)):
            raise RuntimeError(f"P&L identity broken: risk daily {lhs!r} != "
                               f"gross - spread {rhs!r}")

    def labels(self, horizon: str) -> LabelResult:
        """The research forward labels (:func:`iap.labels.compute_labels`) at
        every decision timestamp for one pinned ``horizon``, computed over
        the stream observed so far (cached per observed extent)."""
        if horizon not in HORIZONS_NS:
            raise ValueError(f"unknown horizon {horizon!r}")
        last_ts = self.last_ts if self.last_ts is not None else 0
        key = (len(self.decision_ts), len(self.mid_series), last_ts)
        cached = self._labels.get(horizon)
        if cached is not None and cached[0] == key:
            return cached[1]
        result = compute_labels(self.decision_ts, self.mid_series, last_ts, [horizon],
                                max_age_ns=max_sample_age(self.mid_series))[horizon]
        self._labels[horizon] = (key, result)
        return result

    def realized_pairs(self, alpha_id: str, horizon: str) -> List[Tuple[int, float, float, float]]:
        """``(decision index, expected_return, label_mid, label_cost)`` of the
        alpha's confidence > 0 decisions whose label at ``horizon`` is valid,
        in decision order."""
        lab = self.labels(horizon)
        out: List[Tuple[int, float, float, float]] = []
        for index, er in self.ic_samples[alpha_id]:
            if lab.valid[index]:
                out.append((index, er, lab.mid[index], lab.cost[index]))
        return out

    def realized_ic(self, alpha_id: str, horizon: Optional[str] = None) -> IcResult:
        """Realized IC of ``alpha_id`` at ``horizon`` (default: the MVP
        holding horizon) — see the module docstring for the definition."""
        h = self.horizon if horizon is None else horizon
        pairs = self.realized_pairs(alpha_id, h)
        xs = [er for _, er, _, _ in pairs]
        ys = [m for _, _, m, _ in pairs]
        cs = [c for _, _, _, c in pairs]
        # Shift-by-one: the signal of the PREVIOUS confidence > 0 decision
        # against this decision's label (conventions §7).
        signals = [er for _, er in self.ic_samples[alpha_id]]
        position = {index: k for k, (index, _) in enumerate(self.ic_samples[alpha_id])}
        xs_shift: List[float] = []
        ys_shift: List[float] = []
        for index, _, m, _ in pairs:
            k = position[index]
            if k > 0:
                xs_shift.append(signals[k - 1])
                ys_shift.append(m)
        return IcResult(horizon=h, ic=pearson(xs, ys), ic_cost=pearson(xs, cs),
                        ic_shifted=pearson(xs_shift, ys_shift), n=len(pairs),
                        n_signals=len(self.ic_samples[alpha_id]))

    def n_kill_events(self) -> int:
        return sum(1 for e in self.risk_engine.audit() if e.decision == RiskDecisionCode.KILL)

    def risk_decisions_by_rule(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for e in self.risk_engine.audit():
            out[e.rule_id] = out.get(e.rule_id, 0) + 1
        return dict(sorted(out.items()))
