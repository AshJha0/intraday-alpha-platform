"""iap.contracts: ids, canonical JSON, typed contracts, protocols, validation."""

from __future__ import annotations

import dataclasses
import math

import pytest

from conftest import CONFIGS_DIR
from iap.adaptive.lifecycle import LifecycleConfig, LifecycleTracker
from iap.alpha.base import AlphaModel
from iap.contracts import ids
from iap.contracts.examples import (
    EXAMPLE_TYPES,
    VENUE_NAMES,
    all_examples,
    example_trace,
)
from iap.contracts.protocols import (
    Alpha,
    AlphaLifecycle,
    BookViewLike,
    FeatureEngineLike,
    FeatureVectorLike,
    MarketEventLike,
    OrderBookLike,
)
from iap.contracts.types import (
    AlphaSignal,
    Attribution,
    ChildOrder,
    ContractError,
    Decision,
    DecisionTrace,
    ExecStatus,
    ExecutionReport,
    LifecycleState,
    LifecycleTransition,
    PortfolioTarget,
    RiskDecision,
    TCAResult,
    TraceStages,
    VenueDecision,
    explain,
)
from iap.contracts.validate import ContractValidationError, validate, validate_typed
from iap.contracts.versions import (
    SCHEMA_VERSIONS,
    canonical_json,
    content_hash,
    schema_dir,
    schema_id,
)
from iap.core.events import MarketEvent
from iap.features.context import build_contexts
from iap.features.engine import FeatureEngine, FeatureVector
from iap.orderbook.book import ConsolidatedBook, OrderBook

EXAMPLES = all_examples()
TYPE_NAMES = list(EXAMPLES)


def _wrong_value(value):
    """A JSON value of a different type than ``value`` (never coercible)."""
    if isinstance(value, bool):
        return "true"
    if isinstance(value, (int, float)):
        return "x"
    if isinstance(value, str):
        return 123
    if isinstance(value, list):
        return {}
    if isinstance(value, dict):
        return []
    return 123  # None


def _field_cases():
    for name, inst in EXAMPLES.items():
        for f in dataclasses.fields(inst):
            yield pytest.param(name, f.name, id=f"{name}.{f.name}")


# --------------------------------------------------------------------------
# ids
# --------------------------------------------------------------------------


def test_int_ids_validate_domain():
    assert ids.instrument_id(0) == 0
    assert ids.instrument_id(2**32 - 1) == 2**32 - 1
    assert ids.venue_id(65535) == 65535
    assert ids.order_id(2**64 - 1) == 2**64 - 1
    assert ids.timestamp(-(2**63)) == -(2**63)
    for fn, bad in ((ids.instrument_id, 2**32), (ids.venue_id, -1),
                    (ids.order_id, 2**64), (ids.timestamp, 2**63),
                    (ids.event_id, True), (ids.trade_id, 1.0)):
        with pytest.raises(ValueError):
            fn(bad)


def test_string_ids_and_versions():
    assert ids.alpha_id("EQ03") == "EQ03"
    assert ids.is_flagship_alpha_id("FX12") and not ids.is_flagship_alpha_id("eq03")
    assert ids.alpha_id("research.momentum-v2") == "research.momentum-v2"
    for bad in ("", "a|b", " x", "-lead", "x" * 129, 5):
        with pytest.raises(ValueError):
            ids.strategy_id(bad)
    digest = content_hash({"a": 1})
    assert ids.is_sha256_hex(digest)
    assert ids.feature_version(digest) == digest
    for bad in (digest.upper(), digest[:-1], "", None):
        assert not ids.is_sha256_hex(bad)
        with pytest.raises(ValueError):
            ids.model_version(bad)
    assert ids.NO_ROUTE == 0


def test_make_trace_id_is_deterministic_and_keyed():
    a = ids.make_trace_id("s1", 1, 10, 5)
    assert ids.is_trace_id(a) and len(a) == 32 and a == a.lower()
    assert ids.make_trace_id("s1", 1, 10, 5) == a
    assert ids.make_trace_id("s1", 1, 10, 6) != a
    assert ids.make_trace_id("s2", 1, 10, 5) != a
    assert ids.trace_id(a) == a
    with pytest.raises(ValueError):
        ids.trace_id(a.upper())
    with pytest.raises(ValueError):
        ids.make_trace_id("bad|session", 1, 10, 5)
    with pytest.raises(ValueError):
        ids.make_trace_id("s1", 1, 10, -1)


# --------------------------------------------------------------------------
# versions
# --------------------------------------------------------------------------


def test_canonical_json_is_sorted_compact_ascii():
    text = canonical_json({"b": [1, 2.5, None, True], "a": {"z": "é", "y": -0.0}})
    assert text == '{"a":{"y":-0.0,"z":"\\u00e9"},"b":[1,2.5,null,true]}'
    assert content_hash({"b": 1, "a": 2}) == content_hash({"a": 2, "b": 1})
    assert len(content_hash([])) == 64


@pytest.mark.parametrize("bad", [
    float("nan"), float("inf"), -float("inf"),
    {"x": [1, {"y": float("nan")}]}, [math.inf],
])
def test_canonical_json_rejects_non_finite(bad):
    with pytest.raises(ValueError):
        canonical_json(bad)


def test_canonical_json_rejects_non_string_keys():
    with pytest.raises(ValueError):
        canonical_json({1: "a"})


def test_schema_inventory_matches_disk():
    on_disk = sorted(p.relative_to(schema_dir()).as_posix()
                     for p in schema_dir().rglob("*.schema.json"))
    assert on_disk == sorted(SCHEMA_VERSIONS)
    assert schema_id("alpha/alpha_signal.schema.json") == \
        "https://iap.example/schemas/alpha/alpha_signal.schema.json"


def test_schema_dir_resolution_order(monkeypatch, tmp_path):
    """$IAP_SCHEMA_DIR wins and must exist (never falls through); without it
    the checkout is used, then the wheel's packaged copy (iap/_schemas,
    python/setup.py); nothing found is an error naming every candidate."""
    from iap.contracts import versions

    monkeypatch.delenv("IAP_SCHEMA_DIR", raising=False)
    assert schema_dir() == versions._default_schema_dir()
    assert schema_dir().is_dir()

    override = tmp_path / "override"
    override.mkdir()
    monkeypatch.setenv("IAP_SCHEMA_DIR", str(override))
    assert schema_dir() == override.resolve()
    monkeypatch.setenv("IAP_SCHEMA_DIR", str(tmp_path / "nowhere"))
    with pytest.raises(RuntimeError, match=r"\$IAP_SCHEMA_DIR"):
        schema_dir()

    monkeypatch.delenv("IAP_SCHEMA_DIR", raising=False)
    packaged = tmp_path / "_schemas"
    monkeypatch.setattr(versions, "_default_schema_dir", lambda: tmp_path / "no-checkout")
    monkeypatch.setattr(versions, "_packaged_schema_dir", lambda: packaged)
    with pytest.raises(RuntimeError, match="no-checkout.*_schemas"):
        schema_dir()
    packaged.mkdir()
    assert schema_dir() == packaged


# --------------------------------------------------------------------------
# typed contracts
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", TYPE_NAMES)
def test_round_trip_and_key_order(name):
    inst = EXAMPLES[name]
    data = inst.to_dict()
    assert list(data) == [f.name for f in dataclasses.fields(inst)]
    again = type(inst).from_dict(data)
    assert again == inst
    assert again.to_dict() == data
    assert type(inst).x_version == SCHEMA_VERSIONS[inst.SCHEMA.split("#")[0]]


@pytest.mark.parametrize("name", TYPE_NAMES)
def test_from_dict_rejects_unknown_and_missing_keys(name):
    inst = EXAMPLES[name]
    data = inst.to_dict()
    with pytest.raises(ContractError, match="unknown keys"):
        type(inst).from_dict({**data, "extra_field": 1})
    first = next(iter(data))
    missing = {k: v for k, v in data.items() if k != first}
    with pytest.raises(ContractError, match="missing keys"):
        type(inst).from_dict(missing)
    with pytest.raises(ContractError, match="expected object"):
        type(inst).from_dict([data])


@pytest.mark.parametrize("name,field_name", list(_field_cases()))
def test_wrong_field_type_rejected_by_type_and_schema(name, field_name):
    inst = EXAMPLES[name]
    data = inst.to_dict()
    bad = {**data, field_name: _wrong_value(data[field_name])}
    with pytest.raises(ContractError):
        type(inst).from_dict(bad)
    with pytest.raises(ContractValidationError):
        validate(bad, inst.SCHEMA)


def test_bool_is_never_an_int_or_number():
    sig = EXAMPLES["AlphaSignal"].to_dict()
    with pytest.raises(ContractError):
        AlphaSignal.from_dict({**sig, "instrument_id": True})
    with pytest.raises(ContractError):
        AlphaSignal.from_dict({**sig, "confidence": True})
    with pytest.raises(ContractValidationError):
        validate({**sig, "instrument_id": True}, AlphaSignal.SCHEMA)


def test_int_ranges_and_enums_enforced():
    sig = EXAMPLES["AlphaSignal"].to_dict()
    with pytest.raises(ContractError, match="outside"):
        AlphaSignal.from_dict({**sig, "instrument_id": 2**32})
    with pytest.raises(ContractError, match="not in Direction"):
        AlphaSignal.from_dict({**sig, "direction": 2})
    with pytest.raises(ContractError, match="outside"):
        AlphaSignal.from_dict({**sig, "confidence": 1.5})
    with pytest.raises(ContractError, match="non-finite"):
        AlphaSignal.from_dict({**sig, "expected_return": float("nan")})
    child = EXAMPLES["ChildOrder"].to_dict()
    with pytest.raises(ContractError):
        ChildOrder.from_dict({**child, "order_type": 7})
    lt = EXAMPLES["LifecycleTransition"].to_dict()
    with pytest.raises(ContractError, match="LifecycleState name"):
        LifecycleTransition.from_dict({**lt, "to_state": 4})
    assert LifecycleTransition.from_dict({**lt, "to_state": "WATCH"}).to_state \
        is LifecycleState.WATCH


def test_cross_field_invariants():
    sig = EXAMPLES["AlphaSignal"].to_dict()
    with pytest.raises(ContractError, match="confidence is 0"):
        AlphaSignal.from_dict({**sig, "confidence": 0.0})
    tca = EXAMPLES["TCAResult"].to_dict()
    with pytest.raises(ContractError, match="Perold"):
        TCAResult.from_dict({**tca, "delay_cost_bps": 5.0})
    with pytest.raises(ContractError, match="fill_rate"):
        TCAResult.from_dict({**tca, "fill_rate": 0.5})
    attr = EXAMPLES["Attribution"].to_dict()
    with pytest.raises(ContractError, match="total_bps"):
        Attribution.from_dict({**attr, "total_bps": 0.0})
    rd = EXAMPLES["RiskDecision"].to_dict()
    with pytest.raises(ContractError, match="rule_index"):
        RiskDecision.from_dict({**rd, "rule_index": 3})
    with pytest.raises(ContractError, match="rule_index"):
        RiskDecision.from_dict({**rd, "decision": 2})
    er = EXAMPLES["ExecutionReport"].to_dict()
    with pytest.raises(ContractError, match="receive_ts"):
        ExecutionReport.from_dict({**er, "receive_ts": er["exchange_ts"] - 1})
    with pytest.raises(ContractError, match="filled_qty"):
        ExecutionReport.from_dict({**er, "status": ExecStatus.CANCELED.value})
    lt = EXAMPLES["LifecycleTransition"].to_dict()
    with pytest.raises(ContractError, match="from_state == to_state"):
        LifecycleTransition.from_dict({**lt, "to_state": lt["from_state"]})
    vd = EXAMPLES["VenueDecision"].to_dict()
    with pytest.raises(ContractError, match="eligible candidate"):
        VenueDecision.from_dict({**vd, "venue_id": 9})
    pt = EXAMPLES["PortfolioTarget"].to_dict()
    dup = {**pt, "targets": pt["targets"] * 2}
    with pytest.raises(ContractError, match="sorted by unique"):
        PortfolioTarget.from_dict(dup)


def test_contracts_are_frozen_and_hashable_scalars():
    sig = EXAMPLES["AlphaSignal"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        sig.confidence = 0.5  # type: ignore[misc]
    assert hash(sig) == hash(AlphaSignal.from_dict(sig.to_dict()))
    assert not hasattr(sig, "__dict__")


def test_lifecycle_state_is_ordered():
    order = [s.name for s in sorted(LifecycleState)]
    assert order == ["RESEARCH", "CANDIDATE", "VALIDATING", "PAPER",
                     "ACTIVE", "WATCH", "RETIRED"]
    assert LifecycleState.PAPER < LifecycleState.ACTIVE < LifecycleState.RETIRED


def test_risk_decision_from_risk_event():
    event = {"timestamp": 5, "scope": "INSTRUMENT", "scope_id": "1",
             "rule_id": "MAX_ORDER_QTY", "severity": 3, "decision": 2,
             "reason": "qty 50000 > 20000"}
    rd = RiskDecision.from_risk_event(event, order_id=7, strategy_id="S1",
                                      instrument_id=1, rule_index=4)
    assert rd.decision is Decision.REJECT and rd.rule_index == 4
    assert rd.timestamp_ns == 5 and rd.rule_id == "MAX_ORDER_QTY"
    validate_typed(rd)
    with pytest.raises(ContractError):
        RiskDecision.from_risk_event(event, order_id=7, strategy_id="S1",
                                     instrument_id=1)
    with pytest.raises(ContractError, match="missing"):
        RiskDecision.from_risk_event({}, order_id=7, strategy_id="S1",
                                     instrument_id=1)


def test_explain_renders_none_stages():
    trace = example_trace()
    empty = DecisionTrace(**{**{f.name: getattr(trace, f.name)
                                for f in dataclasses.fields(trace)},
                             "stages": TraceStages(
                                 signal=(), portfolio=None, risk=(), parent_orders=(),
                                 child_orders=(), routing=(), fills=(), tca=(),
                                 attribution=None)})
    text = explain(empty)
    lines = text.splitlines()
    assert lines[0] == f"Trace {trace.trace_id}"
    assert lines[1:] == [
        "Alpha:      (none)", "Portfolio:  (none)", "Risk:       (none)",
        "Execution:  (none)", "SOR:        (none)", "Fills:      (none)",
        "TCA:        (none)", "Attribution: (none)"]
    # unnamed venues render as their decimal id
    assert "SOR:        1 = 45%  2 = 35%  3 = 20%" in explain(trace)
    assert "XV1 = 45%" in explain(trace, VENUE_NAMES)


def test_explain_labels_the_acting_signal_and_its_components():
    """signal[0] is the acting signal (the order's alpha_id); every further
    signal is a component labelled by its own model_version (an ensemble
    trace: ensemble first, members after)."""
    trace = example_trace()
    acting = trace.stages.signal[0]
    member = dataclasses.replace(acting, model_version="EQ01", expected_return=0.0001,
                                 confidence=0.5)
    multi = dataclasses.replace(trace, stages=dataclasses.replace(
        trace.stages, signal=(acting, member)))
    lines = explain(multi, VENUE_NAMES).splitlines()
    pinned = explain(trace, VENUE_NAMES).splitlines()
    assert lines[1] == pinned[1]                       # unchanged for the acting signal
    assert lines[2] == "Alpha:      EQ01  expected return = +1.0 bps  confidence = 0.50"
    assert lines[3:] == pinned[2:]
    # without a parent order every signal is labelled by its model_version
    orderless = dataclasses.replace(multi, stages=dataclasses.replace(
        multi.stages, parent_orders=()))
    lines = explain(orderless, VENUE_NAMES).splitlines()
    assert lines[1].startswith(f"Alpha:      {acting.model_version}  ")
    assert lines[2].startswith("Alpha:      EQ01  ")


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------


def test_validate_reports_sorted_paths_and_unknown_schema():
    sig = EXAMPLES["AlphaSignal"].to_dict()
    bad = {**sig, "confidence": 2, "instrument_id": -1, "junk": 0}
    with pytest.raises(ContractValidationError) as info:
        validate(bad, AlphaSignal.SCHEMA)
    paths = [e.split(":")[0] for e in info.value.errors]
    assert paths == sorted(paths) and len(paths) == 3
    with pytest.raises(KeyError):
        validate(sig, "alpha/nope.schema.json")


def test_validate_nested_definition_via_fragment():
    leg = EXAMPLES["PortfolioLeg"].to_dict()
    validate(leg, "portfolio/portfolio_target.schema.json#/$defs/PortfolioLeg")
    with pytest.raises(ContractValidationError):
        validate({**leg, "target_qty": 1.5},
                 "portfolio/portfolio_target.schema.json#/$defs/PortfolioLeg")


def test_validate_typed_returns_dict():
    inst = EXAMPLES["ParentOrder"]
    assert validate_typed(inst) == inst.to_dict()


# --------------------------------------------------------------------------
# protocols vs existing concrete classes
# --------------------------------------------------------------------------


def test_protocols_satisfied_by_existing_classes():
    ev = MarketEvent(event_id=1, instrument_id=1, venue_id=1, exchange_ts=0,
                     receive_ts=0, sequence=1, event_type=9, side=0,
                     price_ticks=0, qty=0, order_id=0, trade_id=0)
    assert isinstance(ev, MarketEventLike)
    assert isinstance(OrderBook(1, 1), OrderBookLike)
    assert isinstance(OrderBook(1, 1), BookViewLike)
    assert isinstance(ConsolidatedBook(1), BookViewLike)
    # the consolidated merge has no state_summary / is_fresh of its own
    assert not isinstance(ConsolidatedBook(1), OrderBookLike)
    fv = FeatureVector(instrument_id=1, timestamp=0, feature_version="x",
                       values=[0.0], validity=[True])
    assert isinstance(fv, FeatureVectorLike)
    engine = FeatureEngine(build_contexts(CONFIGS_DIR))
    assert isinstance(engine, FeatureEngineLike)
    # research batch scorer / ACTIVE-WATCH-RETIRED tracker are different
    # interfaces (documented in protocols.py), not protocol members
    assert not isinstance(AlphaModel, Alpha)
    tracker = LifecycleTracker(
        alpha_id="EQ03", policy="p",
        config=LifecycleConfig(watch_ic_gate=0.0, reactivate_ic_gate=0.0,
                               retire_breach_evals=2, reactivate_evals=2))
    assert not isinstance(tracker, AlphaLifecycle)


def test_example_types_cover_every_contract():
    from iap.contracts import types as T
    from iap.contracts.types import Contract
    declared = {name for name in T.__all__
                if isinstance(getattr(T, name), type)
                and issubclass(getattr(T, name), Contract)
                and getattr(T, name) is not Contract}
    assert declared == {cls.__name__ for cls, _ in EXAMPLE_TYPES}
