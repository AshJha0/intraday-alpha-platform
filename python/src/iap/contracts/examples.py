"""One canonical example instance per contract type.

The examples are pure functions of pinned constants (no clock, no RNG), so
the golden document ``tests/golden/expected_contracts_examples.json``
generated from them by ``python/tools/make_golden_contracts.py`` is
reproducible byte-for-byte, and other language ports can load it as the
reference instance of every contract.  The :func:`example_trace` is the
decision whose :func:`iap.contracts.types.explain` rendering is pinned in
the golden file.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Mapping, Tuple, Type

from iap.contracts.ids import make_trace_id
from iap.contracts.types import (
    Actor,
    Algo,
    AlphaSignal,
    Attribution,
    BookSnapshotRef,
    ChildOrder,
    Contract,
    Decision,
    DecisionTrace,
    Direction,
    ExecStatus,
    ExecutionReport,
    ExperimentResult,
    ExperimentSpec,
    FeatureVectorRef,
    GateResult,
    LatencyStats,
    LifecycleState,
    LifecycleTransition,
    MarketEventRef,
    OrderType,
    ParentOrder,
    Period,
    PortfolioLeg,
    PortfolioTarget,
    RiskDecision,
    Side,
    SolverStatus,
    TCAResult,
    TraceStages,
    VenueDecision,
    VenueScore,
    Verdict,
    explain,
)
from iap.contracts.versions import canonical_json, content_hash

__all__ = [
    "EXAMPLE_TYPES",
    "GOLDEN_X_VERSION",
    "VENUE_NAMES",
    "all_examples",
    "example_trace",
    "golden_document",
    "render_golden",
]

#: ``x-version`` of the golden document itself.
GOLDEN_X_VERSION = 1

#: Golden session t0 (the pinned synthetic session start) + 500 s.
T0 = 1_787_578_200_000_000_000 + 500 * 1_000_000_000
SESSION_ID = "golden-session-2026-09-19"
INSTRUMENT_ID = 1
SEQUENCE = 500
STRATEGY_ID = "EQ-INTRADAY-1"
ALPHA_ID = "EQ03"
PARENT_ORDER_ID = 12345

#: Version hashes: content hashes of pinned labels (deterministic stand-ins
#: for the dataset / registry / model / config hashes of a real run).
DATA_VERSION = content_hash({"golden": "data_version"})
FEATURE_VERSION = content_hash({"golden": "feature_version"})
MODEL_VERSION = content_hash({"golden": "model_version"})
CONFIG_VERSION = content_hash({"golden": "config_version"})
PORTFOLIO_VERSION = content_hash({"golden": "portfolio_version"})

#: Display names for the SOR line of the pinned ``explain`` rendering
#: (``configs/venues/venues.json`` equity venues).
VENUE_NAMES: Mapping[int, str] = {1: "XV1", 2: "XV2", 3: "XV3"}


def _market_event_ref() -> MarketEventRef:
    return MarketEventRef(event_id=500, instrument_id=INSTRUMENT_ID, venue_id=1,
                          exchange_ts=T0, sequence=SEQUENCE)


def _book_snapshot_ref() -> BookSnapshotRef:
    return BookSnapshotRef(
        instrument_id=INSTRUMENT_ID, venue_id=0, exchange_ts=T0, sequence=SEQUENCE,
        state_hash=content_hash({"best_bid": [10000, 1200], "best_ask": [10001, 900]}))


def _feature_vector_ref() -> FeatureVectorRef:
    return FeatureVectorRef(instrument_id=INSTRUMENT_ID, timestamp_ns=T0,
                            feature_version=FEATURE_VERSION, n_values=205, n_valid=198)


def _alpha_signal() -> AlphaSignal:
    return AlphaSignal(timestamp=T0, instrument_id=INSTRUMENT_ID,
                       expected_return=4.2e-4, confidence=0.81,
                       horizon_ns=5_000_000_000, direction=Direction.UP,
                       model_version="linear_z_v1")


def _portfolio_target() -> PortfolioTarget:
    return PortfolioTarget(
        strategy_id=STRATEGY_ID, timestamp_ns=T0,
        portfolio_version=PORTFOLIO_VERSION, feature_version=FEATURE_VERSION,
        model_version=MODEL_VERSION, solver_status=SolverStatus.OPTIMAL,
        objective_value=1.5e-5, turnover=0.02,
        targets=(PortfolioLeg(instrument_id=INSTRUMENT_ID, target_qty=20_000,
                              target_weight=0.02, expected_return_bps=4.2,
                              prev_qty=0),))


def _risk_decision() -> RiskDecision:
    return RiskDecision(order_id=PARENT_ORDER_ID, strategy_id=STRATEGY_ID,
                        instrument_id=INSTRUMENT_ID, timestamp_ns=T0,
                        decision=Decision.ALLOW, rule_id="", rule_index=-1,
                        reason="all checks passed")


def _parent_order() -> ParentOrder:
    return ParentOrder(
        parent_order_id=PARENT_ORDER_ID, strategy_id=STRATEGY_ID, alpha_id=ALPHA_ID,
        instrument_id=INSTRUMENT_ID, side=Side.BID, qty=20_000, algo=Algo.POV,
        decision_ts=T0, arrival_ts=T0 + 150_000, end_ts=T0 + 300_000_000_000,
        urgency=0.5, limit_price_ticks=0, params={"participation": 0.15})


_CHILD_SLICES: Tuple[Tuple[int, int], ...] = ((1, 9_000), (2, 7_000), (3, 4_000))


def _child_orders() -> Tuple[ChildOrder, ...]:
    return tuple(
        ChildOrder(child_order_id=PARENT_ORDER_ID * 100 + i + 1,
                   parent_order_id=PARENT_ORDER_ID, instrument_id=INSTRUMENT_ID,
                   venue_id=venue, side=Side.BID, qty=qty, price_ticks=10_001,
                   order_type=OrderType.LIMIT, submit_ts=T0 + 150_000 + i * 1_000_000,
                   expire_ts=T0 + 300_000_000_000, slice_index=i)
        for i, (venue, qty) in enumerate(_CHILD_SLICES))


def _candidates() -> Tuple[VenueScore, ...]:
    return (
        VenueScore(venue_id=1, eligible=True, displayed_price_ticks=10_001,
                   displayed_qty=1_200, taker_fee=0.003, maker_rebate=0.002,
                   commission_per_million=0.0, latency_mean_ns=150_000, rank=1),
        VenueScore(venue_id=2, eligible=True, displayed_price_ticks=10_001,
                   displayed_qty=800, taker_fee=0.0028, maker_rebate=0.0015,
                   commission_per_million=0.0, latency_mean_ns=220_000, rank=2),
        VenueScore(venue_id=3, eligible=True, displayed_price_ticks=10_002,
                   displayed_qty=500, taker_fee=0.0025, maker_rebate=0.001,
                   commission_per_million=0.0, latency_mean_ns=300_000, rank=3),
    )


def _routing() -> Tuple[VenueDecision, ...]:
    return tuple(
        VenueDecision(child_order_id=child.child_order_id, venue_id=child.venue_id,
                      reason="best displayed price, then lowest latency",
                      candidates=_candidates())
        for child in _child_orders())


def _fills() -> Tuple[ExecutionReport, ...]:
    filled = ((9_000, ExecStatus.FILLED), (7_000, ExecStatus.FILLED),
              (2_000, ExecStatus.PARTIAL))
    return tuple(
        ExecutionReport(order_id=child.child_order_id, execution_id=910_000 + i,
                        status=status, filled_qty=qty, fill_price_ticks=10_001,
                        venue_id=child.venue_id,
                        exchange_ts=child.submit_ts + 2_000_000,
                        receive_ts=child.submit_ts + 2_150_000,
                        fees=0.003 * qty)
        for i, (child, (qty, status)) in enumerate(zip(_child_orders(), filled)))


def _tca_result() -> TCAResult:
    return TCAResult(
        parent_order_id=PARENT_ORDER_ID, instrument_id=INSTRUMENT_ID, side=Side.BID,
        qty=20_000, filled_qty=18_000, fill_rate=0.9, arrival_price_ticks=10_000,
        avg_fill_price=10_001.0, interval_vwap=10_000.5, interval_twap=10_000.2,
        implementation_shortfall_bps=2.1, delay_cost_bps=0.3, trading_cost_bps=1.5,
        opportunity_cost_bps=0.3, spread_cost_bps=0.8, impact_bps=0.5,
        fees_bps=0.4, timing_cost_bps=0.2, slippage_bps=1.0,
        participation_rate=0.15, n_fills=3,
        venue_contribution_bps={"1": 0.7, "2": 0.5, "3": 0.3}, algo=Algo.POV,
        latency_ns=LatencyStats(min=150_000, mean=190_000.0, max=250_000,
                                p50=180_000, p99=250_000))


def _attribution() -> Attribution:
    return Attribution(alpha_bps=6.2, spread_bps=-0.8, impact_bps=-2.1,
                       fees_bps=-0.4, timing_bps=-0.2, total_bps=2.7)


def _experiment_spec() -> ExperimentSpec:
    body: Dict[str, Any] = {
        "alpha_id": ALPHA_ID, "dataset_version": DATA_VERSION,
        "feature_version": FEATURE_VERSION, "model_version": None,
        "configuration": {"n_folds": 4, "embargo_ns": 60_000_000_000,
                          "cost_multiplier": 1.0, "purge": True},
        "train_period": {"start_ts": T0 - 7_200_000_000_000, "end_ts": T0 - 3_600_000_000_000},
        "validation_period": {"start_ts": T0 - 3_600_000_000_000, "end_ts": T0},
        "test_period": {"start_ts": T0, "end_ts": T0 + 3_600_000_000_000},
        "seed": 20_260_919, "horizon": "5s",
    }
    return ExperimentSpec(experiment_id=content_hash(body)[:16], **body)


def _experiment_result() -> ExperimentResult:
    spec = _experiment_spec()
    return ExperimentResult(
        experiment_id=spec.experiment_id, alpha_id=ALPHA_ID,
        dataset_version=DATA_VERSION, feature_version=FEATURE_VERSION,
        model_version=None, ic=0.0123, rank_ic=0.0118, t_stat=3.4, nw_lags=5,
        hit_rate=0.53, turnover=42.0, gross_return_bps=11.5,
        transaction_cost_bps=4.5, net_return_bps=7.0, max_drawdown_bps=18.0,
        sharpe=1.9, fold_consistency=1.0, n_folds=4, leakage_passed=True,
        leakage_detail={"shift_by_one_ic": -0.0004, "label_columns_guarded": True},
        hypothesis_sign_confirmed=True, verdict=Verdict.PROMOTE,
        n_experiments_in_ledger=128, git_commit="unversioned-workspace",
        created_ts=spec.test_period.end_ts)


def _lifecycle_transition() -> LifecycleTransition:
    return LifecycleTransition(
        alpha_id=ALPHA_ID, from_state=LifecycleState.PAPER, to_state=LifecycleState.ACTIVE,
        event_ts=T0, reason="paper gates passed",
        gates={"oos_ic": GateResult(passed=True, value=0.0123, threshold=0.01),
               "nw_tstat": GateResult(passed=True, value=3.4, threshold=3.0),
               "leakage": GateResult(passed=True, value=None, threshold=None)},
        policy="default_v1", actor=Actor.SYSTEM)


def example_trace() -> DecisionTrace:
    """The pinned decision: EQ03 buys 20,000 of instrument 1 via POV 15%
    across XV1/XV2/XV3, 90% filled, IS 2.1 bps."""
    return DecisionTrace(
        trace_id=make_trace_id(SESSION_ID, INSTRUMENT_ID, T0, SEQUENCE),
        session_id=SESSION_ID, instrument_id=INSTRUMENT_ID, event_ts=T0,
        sequence=SEQUENCE, data_version=DATA_VERSION, feature_version=FEATURE_VERSION,
        model_version=MODEL_VERSION, config_version=CONFIG_VERSION,
        stages=TraceStages(
            signal=(_alpha_signal(),), portfolio=_portfolio_target(),
            risk=(_risk_decision(),), parent_orders=(_parent_order(),),
            child_orders=_child_orders(), routing=_routing(), fills=_fills(),
            tca=(_tca_result(),), attribution=_attribution()))


#: Every contract type with its example builder, in golden-document order.
EXAMPLE_TYPES: Tuple[Tuple[Type[Contract], Any], ...] = (
    (MarketEventRef, _market_event_ref),
    (BookSnapshotRef, _book_snapshot_ref),
    (FeatureVectorRef, _feature_vector_ref),
    (AlphaSignal, _alpha_signal),
    (PortfolioLeg, lambda: _portfolio_target().targets[0]),
    (PortfolioTarget, _portfolio_target),
    (RiskDecision, _risk_decision),
    (ParentOrder, _parent_order),
    (ChildOrder, lambda: _child_orders()[0]),
    (VenueScore, lambda: _candidates()[0]),
    (VenueDecision, lambda: _routing()[0]),
    (ExecutionReport, lambda: _fills()[0]),
    (LatencyStats, lambda: _tca_result().latency_ns),
    (TCAResult, _tca_result),
    (Period, lambda: _experiment_spec().train_period),
    (ExperimentSpec, _experiment_spec),
    (ExperimentResult, _experiment_result),
    (GateResult, lambda: _lifecycle_transition().gates["oos_ic"]),
    (LifecycleTransition, _lifecycle_transition),
    (Attribution, _attribution),
    (TraceStages, lambda: example_trace().stages),
    (DecisionTrace, example_trace),
)


def all_examples() -> Dict[str, Contract]:
    """``{type name: example instance}`` in golden-document order."""
    return {cls.__name__: build() for cls, build in EXAMPLE_TYPES}


def golden_document() -> Dict[str, Any]:
    """The golden document (JSON-ready, key order pinned)."""
    canonical_input = {"b": [1, 2.5, None, True], "a": {"z": "é", "y": -0.0}}
    canonical_text = canonical_json(canonical_input)
    return {
        "x-version": GOLDEN_X_VERSION,
        "description": (
            "One canonical instance per contract type (iap.contracts.examples), "
            "the pinned explain() rendering of the example DecisionTrace, the "
            "pinned trace id and a canonical_json known answer. Generated by "
            "python/tools/make_golden_contracts.py; every port loads these as "
            "the reference instance of each schema."),
        "examples": {
            name: {"schema": inst.SCHEMA, "x_version": inst.x_version,
                   "value": inst.to_dict()}
            for name, inst in all_examples().items()},
        "explain": {
            "venue_names": {str(k): v for k, v in VENUE_NAMES.items()},
            "text": explain(example_trace(), VENUE_NAMES)},
        "trace_id": {
            "session_id": SESSION_ID, "instrument_id": INSTRUMENT_ID,
            "event_ts": T0, "sequence": SEQUENCE,
            "expected": make_trace_id(SESSION_ID, INSTRUMENT_ID, T0, SEQUENCE)},
        "canonical_json": {
            "input": canonical_input, "text": canonical_text,
            "sha256": content_hash(canonical_input)},
    }


def render_golden(doc: Mapping[str, Any]) -> str:
    """The exact bytes of the golden file for ``doc`` (2-space indent,
    ASCII, insertion key order, trailing newline)."""
    return json.dumps(doc, indent=2, ensure_ascii=True, sort_keys=False) + "\n"
