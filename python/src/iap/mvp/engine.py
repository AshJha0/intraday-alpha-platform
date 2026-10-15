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

import math
from dataclasses import replace
from functools import partial
from typing import Any

from iap.contracts.ids import NO_ROUTE
from iap.contracts.protocols import TraceSink
from iap.contracts.types import (
    AlphaSignal,
    ChildOrder,
    Decision,
    ExecStatus,
    OrderType,
    ParentOrder,
    PortfolioTarget,
    RiskDecision,
    Side,
    VenueDecision,
)
from iap.core.events import EventType, MarketEvent
from iap.execution.config import ExecConfig, SorOptions, load_sor_options
from iap.execution.passive import ExecPolicy, PassiveStats, max_behind_qty, post_price
from iap.execution.realism import RealismOptIns
from iap.execution.regulatory import RegulatoryFeeSchedule, load_default_schedule
from iap.execution.simulator import ExecutionSimulator
from iap.execution.sor import SmartOrderRouter
from iap.execution.types import (
    CancelReason,
    InstrumentSpec,
    LatencyConfig,
    Liquidity,
    OrderState,
    VenueSpec,
)
from iap.features.engine import FeatureEngine, FeatureVector
from iap.labels.labels import HORIZONS_NS, LabelResult, MidSeries
from iap.mvp.accounting import Account, Counters
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
from iap.mvp.config import REPO_ROOT, MvpConfig
from iap.mvp.engine_io import feature_context, horizon_name, load_controls
from iap.mvp.engine_metrics import EngineMetricsMixin
from iap.mvp.feed import FeedResult, build_reference_data
from iap.mvp.metrics import IcResult, OrderOutcome, pearson
from iap.mvp.orders import (
    _CANCEL_CROSS_BEHIND,
    _CANCEL_CROSS_TIMEOUT,
    _CANCEL_REPRICE,
    _DONE,
    _REST,
    BLOCK_CONTROLS,
    _Decision,
    _LiveParent,
    _Working,
)
from iap.mvp.portfolio import PortfolioConstraints, PortfolioState, SingleStockPortfolio
from iap.mvp.risk_wiring import RiskWiringMixin
from iap.mvp.sizing import SizingMixin
from iap.orderbook.book import DEPTH_LEVELS, ConsolidatedBook, OrderBook
from iap.reference.refdata import ReferenceData
from iap.risk.engine import RiskEngine
from iap.risk.refdata import instrument_refs_from_reference_data
from iap.tca.fills import MarketTimeline
from iap.trace.attribution import attribute
from iap.trace.builder import TraceBuilder

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


class MvpEngine(RiskWiringMixin, SizingMixin, EngineMetricsMixin):
    """The loop (module docstring).  Feed events through :meth:`on_event`
    in stream order, then :meth:`finish`; traces reach ``sink`` in decision
    order as they become ready."""

    def __init__(
        self,
        cfg: MvpConfig,
        feed: FeedResult,
        sink: TraceSink,
        *,
        ref: ReferenceData | None = None,
        realism: RealismOptIns | None = None,
    ) -> None:
        # ``realism`` (v1.15 X-P1-2, opt-in; Python only, never set on the
        # golden / parity path): the pre-registration execution opt-ins the
        # MVP loop exercises (regulatory fees, hidden-print fills, the feed
        # latency leg). ``None`` is the pinned v1 loop, byte for byte.
        self.realism = realism
        self.cfg = cfg
        self.feed = feed
        self.sink = sink
        self.ref = ref if ref is not None else build_reference_data(cfg)
        inst = self.ref.instrument(cfg.instrument)
        if inst.instrument_id != feed.instrument_id:
            raise ValueError(
                f"feed instrument {feed.instrument_id} != config instrument "
                f"{inst.instrument_id} ({cfg.instrument})"
            )
        self.iid = inst.instrument_id
        self.tick = float(inst.tick_size)
        self.strategy_id = cfg.strategy_id
        self.session_id = cfg.session_id
        self.controls = load_controls(cfg)
        # The end of the session is known EX ANTE from the configured calendar
        # (open/close in the session's zone -> UTC ns); the loop never reads
        # the end of the captured stream, which a live loop cannot know.
        self.session_open_ts, self.session_close_ts = self.ref.session_bounds_ns(
            inst.asset_class, cfg.session.trading_day
        )
        self.counters = Counters()
        self.account = Account()

        # ---- execution: simulator, SOR, algos ------------------------------
        venues: dict[int, VenueSpec] = {}
        for name in cfg.venues:
            v = self.ref.venue(name)
            venues[v.venue_id] = VenueSpec(
                venue_id=v.venue_id,
                name=v.venue,
                is_fx=False,
                taker_fee_per_share=float(v.fees.get("taker_fee_per_share", 0.0)),
                maker_rebate_per_share=float(v.fees.get("maker_rebate_per_share", 0.0)),
                commission_per_million=float(v.fees.get("commission_per_million", 0.0)),
                latency_mean_ns=v.latency_mean_ns,
                latency_jitter_ns=v.latency_jitter_ns,
            )
        self.venues = dict(sorted(venues.items()))
        self.venue_names = {vid: spec.name for vid, spec in self.venues.items()}
        feed_latency_ns = 0 if realism is None else realism.feed_latency_ns
        latency = LatencyConfig(
            cfg.execution.latency_decision_ns + feed_latency_ns,
            cfg.execution.latency_risk_ns,
            cfg.execution.latency_wire_ns,
        )
        exec_doc = cfg.reference_documents()[cfg.reference["execution"]]
        self.exec_config = ExecConfig(
            latency=latency,
            seed=cfg.seed,
            impact_coeff_bps_per_pct_adv=float(
                exec_doc["cost_model"]["impact_coeff_bps_per_pct_adv"]
            ),
            instruments={
                self.iid: InstrumentSpec(
                    self.iid, self.tick, 1.0, float(inst.adv), inst.currency or "USD"
                )
            },
            venues=self.venues,
        )
        sim_kwargs: dict[str, Any] = {}
        if realism is not None:
            if realism.regulatory_fees:
                self.exec_config = replace(
                    self.exec_config,
                    # v1.15: the schedule staged with the config (a real
                    # run copies it into its reference root, hashed into
                    # config_version) when named, else the shipped one.
                    regulatory_fees=(
                        RegulatoryFeeSchedule.load(cfg.reference_path("regulatory_fees"))
                        if "regulatory_fees" in cfg.reference
                        else load_default_schedule(REPO_ROOT / "configs")
                    ),
                )
            # v1.16 P0-3 / v1.17 X-2: one entry point. The impact rule needs a
            # calibration the MVP does not run (recorded as not applied in
            # ``self.realism_applied``).
            from iap.execution.realism import apply as apply_realism

            applied = apply_realism(realism, self.exec_config, impact=False)
            self.exec_config = applied.exec_config
            sim_kwargs = applied.simulator_kwargs
            self.realism_applied = applied.applied
        self.sim = SimulatorAdapter(ExecutionSimulator(self.exec_config, **sim_kwargs))
        self.book: ConsolidatedBook = self.sim.simulator.instrument_book(self.iid)
        sor_file = load_sor_options(cfg.reference_path("execution"))
        sor_options = SorOptions(cfg.sor.prefer_rebate, cfg.sor.max_venue_latency_ns)
        if sor_options != sor_file:
            raise ValueError(
                f"mvp.json sor block must equal execution.json sor ({sor_options} != {sor_file})"
            )
        self.sor = SorAdapter(
            SmartOrderRouter(self.venues, sor_options), self.venues, list(self.venues)
        )
        self._next_parent_id = 1
        self._next_child_id = 1
        self.scheduler = AlgoScheduler(
            self.controls.max_child_qty,
            cfg.execution.twap_slices,
            cfg.execution.is_slices,
            cfg.execution.is_risk_aversion,
            cfg.execution.pov_participation,
            self._new_child_id,
            self._committed,
            policy=cfg.execution.child_policy,
            passive=cfg.execution.passive,
        )
        #: Child execution policy and its transition counters. Not part of
        #: the report: the pinned NATIVE run never touches them.
        self.child_policy = cfg.execution.child_policy
        self.passive_params = cfg.execution.passive
        self.passive_stats = PassiveStats()
        self.min_venue_latency_ns = min(v.latency_mean_ns for v in self.venues.values())

        # ---- risk ---------------------------------------------------------
        risk_doc = cfg.reference_documents()[cfg.reference["risk"]]
        self.risk_engine = RiskEngine.from_config(
            risk_doc, instrument_refs_from_reference_data(self.ref)
        )
        self.risk = RiskEngineAdapter(self.risk_engine)
        self._venue_stale: dict[int, bool] = {}
        self._last_data_ts: dict[int, int] = {}
        self._mark_ts: int | None = None

        # ---- features + alphas + portfolio --------------------------------
        self.features = FeatureEngine(
            {self.iid: feature_context(cfg, self.ref)}, cadence_ns=cfg.decision_cadence_ns
        )
        self.feature_version = self.features.feature_version
        self._mid_index = self.features.feature_names.index(_MID_FEATURE)
        self.horizon = horizon_name(cfg.horizon_ns)
        self.alphas: tuple[LinearZAlpha, ...] = load_alphas(
            cfg.reference_path("alpha_params"),
            cfg.alphas,
            self.features.feature_names,
            self.feature_version,
            cfg.horizon_ns,
        )
        self.ensemble = AlphaEnsemble(self.alphas, cfg.horizon_ns)
        for alpha in self.alphas:
            if alpha.model.horizon not in HORIZONS_NS:
                raise ValueError(
                    f"{alpha.alpha_id}: fitted horizon {alpha.model.horizon!r} "
                    "is not a pinned label horizon"
                )
        self.model_version = self.ensemble.version
        self.constraints = PortfolioConstraints(cfg.portfolio)
        self.portfolio = SingleStockPortfolio(
            self.strategy_id, self.ensemble.alpha_id, self.feature_version, self.model_version
        )
        self.tca = TcaAdapter()
        self.data_version = feed.data_version
        self.config_version = cfg.config_version()

        # ---- rolling state -------------------------------------------------
        self.timeline = MarketTimeline()
        self._current_bar: int | None = None
        self._bar_mid: float | None = None
        self._last_bar_mid: float | None = None
        self.bar_returns: list[float] = []
        self._init_conformal()  # v1.17 D3 opt-in; a no-op without portfolio.conformal
        self._decisions: list[_Decision] = []
        self._live: _LiveParent | None = None
        #: v1.16 end-of-day flatten counters (reported only when enabled)
        self.eod_flatten: dict[str, int] = {"decisions": 0, "parents": 0, "too_late": 0}
        self._closing: list[_LiveParent] = []
        self.outcomes: list[OrderOutcome] = []
        #: (decision index, expected_return) of every confidence > 0 signal
        self.ic_samples: dict[str, list[tuple[int, float]]] = {a.alpha_id: [] for a in self.alphas}
        self.ic_samples[self.ensemble.alpha_id] = []
        #: decision timestamps (label anchors), in decision order
        self.decision_ts: list[int] = []
        #: the research label series: one sample per feature-engine book refresh
        self.mid_series = MidSeries()
        self._last_refresh_seq = 0
        self._labels: dict[str, tuple[tuple[int, int, int], LabelResult]] = {}
        self.first_ts: int | None = None
        self.last_ts: int | None = None
        self._finished = False
        self.trace_ids: list[str] = []
        self._trace_id_set: set[str] = set()
        #: v1.15 kill enforcement (not part of the pinned report): children
        #: cancelled because a kill latched, fills that raced such a cancel,
        #: and the alert messages of those fills.
        self._kill_cancelled: set[int] = set()
        self.kill_cancels = 0
        self.post_kill_fills = 0
        self.kill_alerts: list[str] = []

    @property
    def parent_count(self) -> int:
        """Number of parent orders created so far (public; replaces reads
        of the private id counter)."""
        return self._next_parent_id - 1

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
            raise ValueError(
                f"event {ev.event_id} is for instrument {ev.instrument_id}, "
                f"engine trades {self.iid}"
            )
        t = ev.exchange_ts
        if self.first_ts is None:
            self.first_ts = t
        self.last_ts = t
        self.counters.events += 1
        # 1. simulator + reports (fills -> account + risk, terminals -> risk)
        self._process_reports(self.sim.on_market_event(ev))
        self._enforce_kills(t)
        # 2. session volume — the TAPE, counted once.
        # The equity generator emits an EXECUTE *and* a matching TRADE print
        # for the same trade, so summing both counted every equity trade
        # twice and the participation cap enforced against this total was
        # ~2x looser than configured (on the golden vector: 93,800 counted
        # against a real tape of 48,900, so a 5% cap admitted 9.6%). The POV
        # algo already measures participation against TRADE only, so the two
        # disagreed; TRADE is the tape and is what both now use.
        if ev.event_type == EventType.TRADE:
            self.account.session_volume += ev.qty
        # v1.15 X-P1-3: the same tape feeds the risk engine's session market
        # volume (the 15c3-5 participation cap, check 27); it ignores every
        # non-TRADE type itself.
        self.risk_engine.on_market_event(self.iid, int(ev.event_type), ev.qty, t)
        # 3. risk wiring: stale transitions + reference price + account mark
        self._on_market(ev)
        self._enforce_kills(t)
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

    def _decide(self, ev: MarketEvent, vec: FeatureVector) -> None:
        t = ev.exchange_ts
        self.counters.decisions += 1
        builder = TraceBuilder(
            self.session_id,
            self.iid,
            t,
            ev.sequence,
            self.data_version,
            self.feature_version,
            self.model_version,
            self.config_version,
        )
        decision = _Decision(builder)
        self._decisions.append(decision)
        index = len(self.decision_ts)
        self.decision_ts.append(t)
        self._roll_bar(vec)
        member_signals: dict[str, AlphaSignal] = {}
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

        lead = self.cfg.execution.eod_flatten_lead_ns
        if lead is not None and t >= self.session_close_ts - lead:
            self._decide_eod_flatten(t, signal, decision)
            return
        state = PortfolioState(
            self.iid, t, self.account.position, self.account.mark, tuple(self.bar_returns)
        )
        if not self.portfolio.ready(state, self.constraints) or not self.account.mark_valid:
            self.counters.decisions_without_covariance += 1
            decision.ready = True
            return
        target: PortfolioTarget = self.portfolio.construct(
            list(member_signals.values()) + [signal], state, self.constraints
        )
        builder.set_portfolio(target)
        # v1.17 D3 (opt-in): the conformal multiplier scales the solved
        # target; identity when portfolio.conformal is absent.  The trace
        # keeps the solver's target; the parent carries the scaled one.
        target_qty = self._conformal_scale(target.targets[0].target_qty, signal.expected_return)
        delta = target_qty - (self.account.position + self._inflight_signed())
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
            parent_order_id=self._next_parent_id,
            strategy_id=self.strategy_id,
            alpha_id=self.ensemble.alpha_id,
            instrument_id=self.iid,
            side=Side.BID if delta > 0 else Side.ASK,
            qty=abs(delta),
            algo=algo,
            decision_ts=t,
            arrival_ts=t + self.exec_config.latency.internal_ns + self.min_venue_latency_ns,
            end_ts=end_ts,
            urgency=urgency,
            limit_price_ticks=0,
            params=self.scheduler.params_for(algo),
        )
        self._next_parent_id += 1
        self.counters.parent_orders += 1
        builder.add_parent_order(parent)
        self.scheduler.open(parent)
        self._live = _LiveParent(parent=parent, signal=signal, decision=decision)

    def _decide_eod_flatten(self, t: int, signal: AlphaSignal, decision: _Decision) -> None:
        """v1.16 end-of-day flatten (``execution.eod_flatten_lead_ns``): target
        flat, no portfolio solve; the residual (position + in-flight) is worked
        as ONE most-urgent parent ending at the session close, its costs
        charged like any other parent."""
        self.eod_flatten["decisions"] += 1
        delta = -(self.account.position + self._inflight_signed())
        if delta == 0:
            self.counters.decisions_flat += 1
            decision.ready = True
            return
        if self._live is not None and self._live.parent.end_ts > t:
            self.counters.decisions_parent_live += 1
            decision.ready = True
            return
        if t >= self.session_close_ts:
            self.eod_flatten["too_late"] += 1
            decision.ready = True
            return
        algo = self.cfg.execution.algo_for(1.0)
        parent = ParentOrder(
            parent_order_id=self._next_parent_id,
            strategy_id=self.strategy_id,
            alpha_id=self.ensemble.alpha_id,
            instrument_id=self.iid,
            side=Side.BID if delta > 0 else Side.ASK,
            qty=abs(delta),
            algo=algo,
            decision_ts=t,
            arrival_ts=t + self.exec_config.latency.internal_ns + self.min_venue_latency_ns,
            end_ts=self.session_close_ts,
            urgency=1.0,
            limit_price_ticks=0,
            params=self.scheduler.params_for(algo),
        )
        self._next_parent_id += 1
        self.counters.parent_orders += 1
        self.eod_flatten["parents"] += 1
        decision.builder.add_parent_order(parent)
        self.scheduler.open(parent)
        self._live = _LiveParent(parent=parent, signal=signal, decision=decision)

    # -------------------------------------------------------------- children

    def _contra_depth(self, book: OrderBook | None, side: Side) -> int:
        if book is None:
            return 0
        levels = book.depth(1 if side is Side.BID else 0, DEPTH_LEVELS)
        return sum(q for _, q in levels)

    def _issue_children(self, ev: MarketEvent) -> None:
        live = self._live
        if live is None:
            return
        work = None
        if self.child_policy is ExecPolicy.PASSIVE:
            work = partial(self._work_passive, live, ev)
        for child in self.scheduler.generate_child_orders(
            live.parent, MarketView(ev, self.book), work
        ):
            self._submit_child(live, child, ev, replacement=False, reprices=0)

    def _work_passive(self, live: _LiveParent, ev: MarketEvent) -> None:
        """PASSIVE policy: advance POST -> REST -> REPRICE / CROSS for every
        posted child of the live parent (``iap.execution.passive``; the
        machine of ``ExecutionReplay._work_passive``). A replacement child
        goes through routing, the latency-budget and participation controls
        and the risk check like any other child; it is exempt from the
        slice-interval control, because it re-sends quantity of a slice that
        control already admitted, and it does not move the slice clock."""
        parent = live.parent
        t = ev.exchange_ts
        params = self.passive_params
        ps = self.scheduler.schedule_state(parent.parent_order_id)
        filled = sum(r.filled_qty for r in live.reports)
        behind = ps.scheduled_qty - filled - ps.current_step_qty > max_behind_qty(
            params, parent.qty
        )
        stats = self.passive_stats
        for k in range(len(live.working)):  # children posted below: next event
            w = live.working[k]
            if w.state == _DONE:
                continue
            o = self.sim.sim_order(w.child_order_id)
            if w.state == _REST:
                if o.is_terminal:
                    w.state = _DONE
                elif t >= parent.end_ts:
                    continue  # expires with the window (rule 7)
                elif behind:
                    self.sim.cancel(w.child_order_id, t)
                    w.state = _CANCEL_CROSS_BEHIND
                elif t >= w.deadline:
                    if w.reprices >= params.max_reprices:
                        self.sim.cancel(w.child_order_id, t)
                        w.state = _CANCEL_CROSS_TIMEOUT
                        continue
                    vb = self.book.books.get(o.venue_id)
                    price = None
                    if vb is not None:
                        price = post_price(
                            o.side, vb.best_bid(), vb.best_ask(), params.improve_min_spread_ticks
                        )
                    if price == o.limit_ticks:
                        w.reprices += 1  # still at the target price: keep the queue position
                        w.deadline = min(t + ps.patience_ns, parent.end_ts - params.end_margin_ns)
                        stats.rest_extensions += 1
                    else:
                        self.sim.cancel(w.child_order_id, t)
                        w.state = _CANCEL_REPRICE
                continue
            if not o.is_terminal:
                continue  # the cancel is still in flight
            state = w.state
            w.state = _DONE
            if (
                o.state != OrderState.CANCELLED
                or o.cancel_reason != CancelReason.USER
                or o.remaining <= 0
                or t >= parent.end_ts
            ):
                continue  # filled, expired or the window closed: nothing to re-send
            rest = state == _CANCEL_REPRICE
            if rest:
                stats.reprices += 1
            elif state == _CANCEL_CROSS_BEHIND:
                stats.crosses_behind += 1
            else:
                stats.crosses_timeout += 1
            child = self.scheduler.replacement(parent, o.remaining, t, rest)
            self._submit_child(live, child, ev, replacement=True, reprices=w.reprices + 1)

    def _submit_child(
        self,
        live: _LiveParent,
        child: ChildOrder,
        ev: MarketEvent,
        *,
        replacement: bool,
        reprices: int,
    ) -> None:
        """Route, control, risk-check and submit one generated child."""
        parent = live.parent
        t = ev.exchange_ts
        builder = live.decision.builder
        posting = self.child_policy is ExecPolicy.PASSIVE
        self.counters.child_orders_generated += 1
        passive = child.order_type is OrderType.LIMIT
        if posting and not passive and not replacement:
            self.passive_stats.crosses_immediate += 1
        vd: VenueDecision = self.sor.route(child, SorMarket(self.book, passive))
        if vd.venue_id == NO_ROUTE:
            # A routing verdict: traced (routing + child) with venue 0.
            self.counters.sor_no_route += 1
            builder.add_routing(vd)
            builder.add_child_order(child)
            live.rejected += 1
            return
        venue_book = self.book.books.get(vd.venue_id)
        order_type = child.order_type
        price = 0
        if passive:
            limit = None
            if venue_book is not None and posting:
                # PASSIVE: near touch, one tick inside when the spread
                # allows, never at or through the opposite touch.
                limit = post_price(
                    int(child.side),
                    venue_book.best_bid(),
                    venue_book.best_ask(),
                    self.passive_params.improve_min_spread_ticks,
                )
            elif venue_book is not None:
                best = venue_book.best_bid() if child.side is Side.BID else venue_book.best_ask()
                limit = None if best is None else best[0]
            if limit is not None:
                price = limit
            else:
                order_type = OrderType.MARKET
        routed = replace(child, venue_id=vd.venue_id, price_ticks=price, order_type=order_type)
        # Declared controls (Java BacktestEngine.decide, pinned order).
        ctl = self.controls
        acct = self.account
        # A control-blocked child never leaves the strategy (BLOCK_CONTROLS):
        # counted, not traced.
        if (
            not replacement
            and ctl.min_slice_interval_ns > 0
            and acct.last_child_decision_ts is not None
            and t - acct.last_child_decision_ts < ctl.min_slice_interval_ns
        ):
            self.counters.slice_interval_blocked += 1
            live.blocked["slice_interval"] += 1
            live.rejected += 1
            return
        venue_latency = (
            self.exec_config.latency.internal_ns + self.venues[vd.venue_id].latency_mean_ns
        )
        if venue_latency > ctl.latency_budget_ns:
            self.counters.latency_budget_blocked += 1
            live.blocked["latency_budget"] += 1
            live.rejected += 1
            return
        if ctl.max_participation < 1.0:
            depth = self._contra_depth(venue_book, child.side)
            cap_depth = int(math.floor(ctl.max_participation * depth))
            cap_volume = (
                int(math.floor(ctl.max_participation * acct.session_volume)) - acct.filled_qty
            )
            cap = max(min(cap_depth, cap_volume), 0)
            if cap == 0:
                self.counters.participation_blocked += 1
                live.blocked["participation"] += 1
                live.rejected += 1
                return
            if routed.qty > cap:
                self.counters.participation_capped += 1
                routed = replace(routed, qty=cap)
        builder.add_routing(vd)
        builder.add_child_order(routed)
        rd: RiskDecision = self.risk.evaluate(
            routed, RiskContext(self.strategy_id, parent.urgency, t)
        )
        builder.add_risk(rd)
        if rd.decision is not Decision.ALLOW:
            self.counters.risk_rejected += 1
            live.rejected += 1
            return
        self.counters.risk_allowed += 1
        reports = self.sim.submit(routed)
        live.children[routed.child_order_id] = routed
        acct.orders_submitted += 1
        if not replacement:
            acct.last_child_decision_ts = t
        self.counters.child_orders_submitted += 1
        for rep in reports:
            live.reports.append(rep)
            builder.add_fill(rep)
        if posting and routed.order_type is OrderType.LIMIT:
            ps = self.scheduler.schedule_state(parent.parent_order_id)
            self.passive_stats.posts += 1
            live.working.append(
                _Working(
                    routed.child_order_id,
                    min(t + ps.patience_ns, parent.end_ts - self.passive_params.end_margin_ns),
                    reprices,
                )
            )

    # ------------------------------------------------------------- finalise

    def _finalise_due(self, t: int) -> None:
        live = self._live
        if live is not None and live.parent.end_ts <= t:
            self._closing.append(live)
            self._live = None
        for live in list(self._closing):
            if (
                live.all_terminal(self.sim)
                and self.timeline.last_ts is not None
                and self.timeline.last_ts >= live.parent.end_ts
            ):
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
        can_tca = (
            live.all_terminal(self.sim) and tl.last_ts is not None and tl.last_ts >= parent.end_ts
        )
        if not can_tca:
            if not force:
                raise RuntimeError(f"parent {parent.parent_order_id} finalised too early")
            self.counters.parents_without_tca += 1
            self.outcomes.append(
                OrderOutcome(
                    parent=parent,
                    signal=live.signal,
                    tca=None,
                    attribution=None,
                    realized_bps=0.0,
                    filled_notional=0.0,
                    n_children_submitted=len(live.children),
                    n_children_rejected=live.rejected,
                    venue_qty={},
                    latencies_ns=(),
                )
            )
            live.decision.ready = True
            return
        liquidity: dict[int, Liquidity] = {}
        venue_qty: dict[int, int] = {}
        for rep in live.reports:
            if rep.status in (ExecStatus.PARTIAL, ExecStatus.FILLED):
                fill = self.sim.fills_by_execution[rep.execution_id]
                liquidity[rep.execution_id] = fill.liquidity
                venue_qty[rep.venue_id] = venue_qty.get(rep.venue_id, 0) + rep.filled_qty
        latencies = {
            cid: self.sim.arrival_ts(cid) - child.submit_ts for cid, child in live.children.items()
        }
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
        self.outcomes.append(
            OrderOutcome(
                parent=parent,
                signal=live.signal,
                tca=tca,
                attribution=attribution,
                realized_bps=realized_bps,
                filled_notional=filled_notional,
                n_children_submitted=len(live.children),
                n_children_rejected=live.rejected,
                venue_qty=dict(sorted(venue_qty.items())),
                latencies_ns=tuple(latencies[c] for c in sorted(latencies)),
            )
        )
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
