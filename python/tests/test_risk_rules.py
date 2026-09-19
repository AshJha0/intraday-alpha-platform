"""Per-rule unit tests and real-world scenario tests for the hard risk
engine — the Python port of ``rust/risk/tests/rules.rs`` (every scenario,
same names), the Rust in-crate unit tests of ``limits.rs`` / ``event.rs``,
and the Java-only cases of ``RiskRuleTest`` / ``RiskScenarioTest``
(unsigned u64 ids in reasons, u32-bound instrument scope ids, serde_json
escaping), plus the property tests of this port: a REJECT never changes
exposure state, and applied decisions are replayable (from a fresh engine
and from any restored snapshot).
"""

from __future__ import annotations

import copy
import json
import math
from typing import Dict, List

import pytest

from iap.core.rng import SplitMix64
from iap.reference.refdata import ReferenceData
from iap.risk import (
    Decision,
    Fill,
    InstrumentRef,
    OrderRequest,
    OrderType,
    RiskEngine,
    RiskEvent,
    RiskLimits,
    Rules,
    Scope,
    Severity,
    equity_refs,
    fmt_fixed,
    format_f64,
    instrument_refs_from_config,
    instrument_refs_from_golden,
    instrument_refs_from_reference_data,
    load_instrument_refs,
    order_validation_error,
    to_canonical_json,
)
from iap.risk.serialize import rust_display_f64

T0 = 1_700_000_000_000_000_000
NS = 1_000_000_000
SEC = NS


@pytest.fixture(scope="module")
def config_doc(golden_dir) -> dict:
    with open(golden_dir.parents[1] / "configs" / "risk" / "risk.json") as f:
        return json.load(f)


def config(config_doc: dict) -> dict:
    return copy.deepcopy(config_doc)


def ticks() -> Dict[int, float]:
    return {1: 0.01, 2: 0.01}


def engine(cfg: dict) -> RiskEngine:
    """Engine with fresh two-sided market data on instruments 1 and 2."""
    eng = RiskEngine.from_config_ticks(cfg, ticks())
    eng.on_market(1, 2450, 2452, T0)  # mid 24.51
    eng.on_market(2, 3119, 3121, T0)  # mid 31.20
    return eng


def fx_refs() -> Dict[int, InstrumentRef]:
    """Instruments 1/2 (USD equities) plus USD/JPY (103, qty_unit 1000,
    JPY) and EUR/GBP (108, GBP) with GBP/USD (102) as the GBP pair."""
    return {
        1: InstrumentRef.equity(0.01),
        2: InstrumentRef.equity(0.01),
        102: InstrumentRef(1e-5, 1000.0, "USD"),
        103: InstrumentRef(0.001, 1000.0, "JPY"),
        108: InstrumentRef(1e-5, 1000.0, "GBP"),
    }


def fx_engine(cfg: dict) -> RiskEngine:
    eng = RiskEngine.from_config(cfg, fx_refs())
    eng.on_market(1, 2450, 2452, T0)
    eng.on_market(103, 147_515, 147_525, T0)  # mid 147.520 JPY
    return eng


def typed(oid: int, iid: int, side: int, qty: int, price: int, ot: OrderType,
          ts: int, strategy: str = "S1", venue: int = 1) -> OrderRequest:
    return OrderRequest(oid, iid, side, qty, price, int(ot), venue, strategy, 0.5, ts)


def order(oid: int, side: int, qty: int, price: int, **kw) -> OrderRequest:
    fields = dict(
        order_id=oid, instrument_id=1, side=side, qty=qty, price_ticks=price,
        order_type=int(OrderType.LIMIT if price > 0 else OrderType.MARKET),
        venue_id=1, strategy_id="S1", urgency=0.5, timestamp=T0 + 100_000_000,
    )
    fields.update(kw)
    return OrderRequest(**fields)


def fill(strategy: str, iid: int, side: int, qty: int, price: int,
         ts: int = T0 + NS, order_id: int = 0) -> Fill:
    return Fill(ts, strategy, iid, order_id, side, qty, price)


def kills(eng: RiskEngine) -> List[str]:
    return [e.rule_id for e in eng.audit() if e.decision == int(Decision.KILL)]


def expect(eng: RiskEngine, o: OrderRequest, rule: str) -> None:
    d = eng.check_order(o)
    assert d.rule_id == rule, f"rule ({d.reason})"
    if rule == Rules.ALLOW:
        assert d.decision == Decision.ALLOW and d.severity == Severity.INFO
    else:
        assert d.decision == Decision.REJECT


# ------------------------------------------------------------- fail-closed


def test_missing_limit_fails_closed(config_doc):
    doc = config(config_doc)
    del doc["per_order"]["max_order_qty"]
    eng = RiskEngine.from_config_ticks(doc, ticks())
    eng.on_market(1, 2450, 2452, T0)
    d = eng.check_order(order(1, 0, 100, 2450))
    assert d.decision == Decision.REJECT
    assert d.rule_id == Rules.CONFIG_MISSING
    assert d.severity == Severity.BREACH
    assert "max_order_qty" in d.reason
    # the reason carries the Rust error rendering
    assert d.reason == "fail-closed: invalid argument: risk.json: missing/non-integer per_order.max_order_qty"
    # and it stays closed for every order
    assert eng.check_order(order(2, 1, 1, 2452)).rule_id == Rules.CONFIG_MISSING
    # overrides are refused on a fail-closed engine
    with pytest.raises(ValueError):
        eng.override_loss_limit(Scope.GLOBAL, "", 1.0, T0, "x")


def test_invalid_config_value_fails_closed(config_doc):
    doc = config(config_doc)
    doc["global"]["max_daily_loss"] = 0.0
    eng = RiskEngine.from_config_ticks(doc, ticks())
    d = eng.check_order(order(1, 0, 100, 2450))
    assert d.rule_id == Rules.CONFIG_MISSING
    assert d.reason.endswith("global.max_daily_loss must be > 0, got 0")
    doc = config(config_doc)
    doc["global"]["max_daily_loss"] = -5.0
    assert RiskEngine.from_config_ticks(doc, ticks()).check_order(
        order(1, 0, 100, 2450)).rule_id == Rules.CONFIG_MISSING
    doc = config(config_doc)
    doc["per_order"]["max_order_qty"] = "many"
    assert RiskEngine.from_config_ticks(doc, ticks()).check_order(
        order(1, 0, 100, 2450)).rule_id == Rules.CONFIG_MISSING
    # an entirely empty document, and a non-object
    assert RiskEngine.from_config_ticks({}, ticks()).check_order(
        order(1, 0, 100, 2450)).rule_id == Rules.CONFIG_MISSING
    assert RiskEngine.from_config_ticks([], ticks()).check_order(
        order(1, 0, 100, 2450)).rule_id == Rules.CONFIG_MISSING


def test_config_kill_switch_engaged_starts_killed(config_doc):
    doc = config(config_doc)
    doc["global"]["kill_switch_engaged"] = True
    eng = RiskEngine.from_config_ticks(doc, ticks())
    eng.on_market(1, 2450, 2452, T0)
    assert eng.check_order(order(1, 0, 100, 2450)).rule_id == Rules.KILL_GLOBAL
    assert eng.kill_switch_engaged()
    assert eng.metrics.gauge_value("risk_kill_switch_engaged") == 1.0


# ---------------------------------------------------------- limits parser


def test_repo_config_parses_completely(config_doc):
    limits = RiskLimits.from_json(config_doc)
    assert limits.max_order_qty == 50_000
    assert limits.max_order_notional > 0.0
    assert not limits.kill_switch_engaged
    assert limits.duplicate_order_window_ns == 0  # whole session
    assert limits.reporting_ccy == "USD"
    assert limits.max_order_rate_per_sec == 500.0
    assert isinstance(limits.order_rate_burst, float)
    assert limits.fx_conversion["JPY"].instrument_id == 103
    assert limits.fx_conversion["JPY"].invert
    assert not limits.fx_conversion["EUR"].invert
    assert list(limits.fx_conversion) == sorted(limits.fx_conversion)


def test_missing_currency_block_is_a_config_error(config_doc):
    doc = config(config_doc)
    del doc["currency"]
    with pytest.raises(ValueError, match="currency.reporting_ccy"):
        RiskLimits.from_json(doc)
    doc = config(config_doc)
    doc["currency"]["conversion"]["JPY"]["invert"] = "yes"
    with pytest.raises(ValueError, match="conversion.JPY.invert"):
        RiskLimits.from_json(doc)
    doc = config(config_doc)
    doc["currency"]["conversion"]["JPY"]["instrument_id"] = 0
    with pytest.raises(ValueError, match="out of u32 range"):
        RiskLimits.from_json(doc)
    doc = config(config_doc)
    doc["currency"]["reporting_ccy"] = ""
    with pytest.raises(ValueError, match="missing/empty currency.reporting_ccy"):
        RiskLimits.from_json(doc)


def test_missing_and_invalid_limits_are_config_errors(config_doc):
    doc = config(config_doc)
    del doc["per_order"]["max_order_qty"]
    with pytest.raises(ValueError, match="missing/non-integer per_order.max_order_qty"):
        RiskLimits.from_json(doc)
    doc = config(config_doc)
    doc["global"]["max_daily_loss"] = -5.0
    with pytest.raises(ValueError, match="global.max_daily_loss must be > 0, got -5"):
        RiskLimits.from_json(doc)
    doc = config(config_doc)
    doc["per_order"]["max_order_qty"] = "many"
    with pytest.raises(ValueError, match="missing/non-integer"):
        RiskLimits.from_json(doc)
    # serde strictness: a JSON float is not an integer, a bool is not a number
    doc = config(config_doc)
    doc["per_order"]["max_order_qty"] = 50000.0
    with pytest.raises(ValueError, match="missing/non-integer"):
        RiskLimits.from_json(doc)
    doc = config(config_doc)
    doc["global"]["max_gross_notional"] = True
    with pytest.raises(ValueError, match="missing/non-numeric"):
        RiskLimits.from_json(doc)
    doc = config(config_doc)
    doc["per_order"]["stale_book_reject"] = 1
    with pytest.raises(ValueError, match="missing/non-bool"):
        RiskLimits.from_json(doc)
    doc = config(config_doc)
    doc["per_order"]["duplicate_order_window_ns"] = -1
    with pytest.raises(ValueError, match="must be >= 0"):
        RiskLimits.from_json(doc)
    doc = config(config_doc)
    doc["market_data"]["max_sequence_gap_before_halt"] = -1
    with pytest.raises(ValueError, match="max_sequence_gap_before_halt"):
        RiskLimits.from_json(doc)
    # the duplicate window is checked first, exactly like the Rust parser
    doc = config(config_doc)
    del doc["per_order"]
    with pytest.raises(ValueError, match="missing per_order.duplicate_order_window_ns"):
        RiskLimits.from_json(doc)


def test_limits_load_from_path(golden_dir, tmp_path):
    root = golden_dir.parents[1]
    limits = RiskLimits.load(root / "configs" / "risk" / "risk.json")
    assert limits.stale_feed_timeout_ns == 5 * NS
    with pytest.raises(ValueError, match="io error"):
        RiskLimits.load(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(ValueError, match="codec error: risk.json"):
        RiskLimits.load(bad)


# ------------------------------------------------------------ kill switches


def test_kill_switch_scopes_and_precedence(config_doc):
    eng = engine(config_doc)
    assert eng.check_order(order(1, 0, 100, 2450)).allowed()

    eng.engage_kill(Scope.VENUE, "1", T0, "test")
    assert eng.check_order(order(2, 0, 100, 2450)).rule_id == Rules.KILL_VENUE

    eng.engage_kill(Scope.INSTRUMENT, "1", T0, "test")
    assert eng.check_order(order(3, 0, 100, 2450)).rule_id == Rules.KILL_INSTRUMENT

    eng.engage_kill(Scope.STRATEGY, "S1", T0, "test")
    assert eng.check_order(order(4, 0, 100, 2450)).rule_id == Rules.KILL_STRATEGY

    eng.engage_kill(Scope.GLOBAL, "", T0, "test")
    d = eng.check_order(order(5, 0, 100, 2450))
    assert d.rule_id == Rules.KILL_GLOBAL  # global beats everything
    assert d.severity == Severity.BREACH

    # clearing restores, innermost first
    eng.clear_kill(Scope.GLOBAL, "", T0, "clear")
    assert eng.check_order(order(6, 0, 100, 2450)).rule_id == Rules.KILL_STRATEGY
    eng.clear_kill(Scope.STRATEGY, "S1", T0, "clear")
    assert eng.check_order(order(8, 0, 100, 2450)).rule_id == Rules.KILL_INSTRUMENT
    eng.clear_kill(Scope.INSTRUMENT, "1", T0, "clear")
    assert eng.check_order(order(9, 0, 100, 2450)).rule_id == Rules.KILL_VENUE
    eng.clear_kill(Scope.VENUE, "1", T0, "clear")
    assert eng.check_order(order(7, 0, 100, 2450)).allowed()
    # venue 0 (SOR-routed) skips the venue kill switch entirely
    eng.engage_kill(Scope.VENUE, "1", T0, "test")
    assert eng.check_order(order(10, 0, 100, 2450, venue_id=0)).allowed()


def test_strategy_kill_only_hits_that_strategy(config_doc):
    eng = engine(config_doc)
    eng.engage_kill(Scope.STRATEGY, "S1", T0, "test")
    assert eng.check_order(order(1, 0, 100, 2450)).rule_id == Rules.KILL_STRATEGY
    assert eng.check_order(order(2, 0, 100, 2450, strategy_id="S2")).allowed()


def test_instrument_kill_scope_id_bound_to_u32(config_doc):
    """The Rust reference parses the INSTRUMENT scope id with
    ``parse::<u32>()``: out-of-range or unparseable ids are a no-op."""
    eng = engine(config_doc)
    eng.engage_kill(Scope.INSTRUMENT, "4294967295", T0, "ops")
    d = eng.check_order(order(1, 0, 10, 2450, instrument_id=4294967295, timestamp=T0 + SEC))
    assert d.rule_id == Rules.KILL_INSTRUMENT
    for bad_id in ("4294967296", "-1", "", " 1", "1 ", "1.0", "0x1", "1_0"):
        eng.engage_kill(Scope.INSTRUMENT, bad_id, T0, "ops")
    assert eng.snapshot()["kill_instruments"] == {"4294967295": True}
    # a leading '+' is accepted by Rust's integer parser
    eng.engage_kill(Scope.INSTRUMENT, "+7", T0, "ops")
    assert eng.snapshot()["kill_instruments"] == {"4294967295": True, "7": True}
    eng.clear_kill(Scope.INSTRUMENT, "4294967295", T0 + 4 * SEC, "clear")
    d = eng.check_order(order(4, 0, 10, 2450, instrument_id=4294967295, timestamp=T0 + 5 * SEC))
    assert d.rule_id == Rules.UNKNOWN_INSTRUMENT  # kill lifted
    # venue scope ids are bound to u16 the same way
    eng.engage_kill(Scope.VENUE, "65536", T0, "ops")
    eng.engage_kill(Scope.VENUE, "65535", T0, "ops")
    assert eng.snapshot()["kill_venues"] == {"65535": True}
    # the audit record carries whatever id text was given
    assert eng.audit()[-2].scope_id == "65536"


# ------------------------------------------------- malformed / reference


def test_malformed_orders_reject(config_doc):
    eng = engine(config_doc)
    d = eng.check_order(order(1, 0, 0, 2450))  # qty 0
    assert d.rule_id == Rules.MALFORMED_ORDER
    assert d.reason == "qty must be > 0: 0"
    assert d.severity == Severity.WARN
    assert eng.check_order(order(2, 3, 100, 2450)).reason == "side must be 0 or 1: 3"
    assert eng.check_order(order(3, 0, 100, 0, order_type=int(OrderType.LIMIT))).reason \
        == "LIMIT order needs price_ticks > 0: 0"
    assert eng.check_order(order(4, 0, 100, 2450, urgency=2.0)).reason \
        == "urgency must be in [0, 1]: 2"
    assert eng.check_order(order(5, 0, 10, 2450, order_type=9)).reason \
        == "unknown order_type: 9"
    assert eng.check_order(order(6, 0, 10, 2450, order_type=int(OrderType.MARKET))).reason \
        == "MARKET order must carry price_ticks 0: 2450"
    assert eng.check_order(order(7, 0, 10, -1, order_type=int(OrderType.IOC))).reason \
        == "price_ticks must be >= 0: -1"
    assert eng.check_order(order(8, 0, 10, 5, order_type=int(OrderType.PEG))).reason \
        == "PEG/MID orders carry price_ticks 0: 5"
    assert eng.check_order(order(9, 0, 10, 2450, urgency=float("nan"))).reason \
        == "urgency must be in [0, 1]: NaN"
    assert eng.check_order(order(10, 0, 10, 2450, urgency=1.5)).reason \
        == "urgency must be in [0, 1]: 1.5"
    # the schema check is side, type, qty, urgency, price — in that order
    assert order_validation_error(order(11, 5, 0, 0, order_type=0)) == "side must be 0 or 1: 5"
    assert order_validation_error(order(12, 0, 0, 0, order_type=0)) == "unknown order_type: 0"
    assert order_validation_error(order(13, 0, 100, 2450)) is None


def test_order_and_fill_wire_domains_are_enforced():
    with pytest.raises(ValueError):
        OrderRequest(-1, 1, 0, 1, 1, 2, 1, "S1", 0.5, 0)
    with pytest.raises(ValueError):
        OrderRequest(1 << 64, 1, 0, 1, 1, 2, 1, "S1", 0.5, 0)
    with pytest.raises(ValueError):
        OrderRequest(1, 1 << 32, 0, 1, 1, 2, 1, "S1", 0.5, 0)
    with pytest.raises(ValueError):
        OrderRequest(1, 1, 256, 1, 1, 2, 1, "S1", 0.5, 0)
    with pytest.raises(ValueError):
        OrderRequest(1, 1, 0, 1 << 63, 1, 2, 1, "S1", 0.5, 0)
    with pytest.raises(ValueError):
        OrderRequest(1, 1, 0, 1, 1, 2, 1 << 16, "S1", 0.5, 0)
    with pytest.raises(ValueError):
        OrderRequest(1, 1, 0, 1, 1, 2, 1, "S1", "high", 0)
    with pytest.raises(ValueError):
        OrderRequest(1, 1, 0, 1, 1, 2, 1, 7, 0.5, 0)
    with pytest.raises(ValueError):
        Fill(0, "S1", 1, 1 << 64, 0, 1, 1)
    with pytest.raises(ValueError):
        Fill(0, "S1", 1, 0, 0, -(1 << 63) - 1, 1)
    assert OrderRequest(1, 1, 0, 1, 1, 2, 1, "S1", 1, 0).urgency == 1.0


def test_unknown_instrument_rejects(config_doc):
    eng = engine(config_doc)
    d = eng.check_order(order(1, 0, 100, 2450, instrument_id=999))
    assert d.rule_id == Rules.UNKNOWN_INSTRUMENT
    assert d.reason == "no reference data for instrument 999"


def test_duplicate_order_id_rejects_even_after_reject(config_doc):
    eng = engine(config_doc)
    assert eng.check_order(order(7, 0, 100, 2450)).allowed()
    d = eng.check_order(order(7, 1, 50, 2455))
    assert d.rule_id == Rules.DUPLICATE_ORDER_ID
    assert d.reason == f"order_id 7 already used at ts {T0 + 100_000_000}"
    # an id consumed by a rejected order is consumed too (pinned)
    assert eng.check_order(order(8, 0, 0, 2450)).rule_id == Rules.MALFORMED_ORDER
    # ... but only ids that reached the duplicate check are recorded:
    # order 8 failed schema validation BEFORE registration
    assert eng.check_order(order(8, 0, 100, 2450)).allowed(), "id of a malformed order was not consumed"
    assert eng.check_order(order(8, 0, 100, 2450)).rule_id == Rules.DUPLICATE_ORDER_ID
    # a rejection AFTER registration (venue down) still consumes the id
    eng.on_venue_disconnect(1, T0)
    assert eng.check_order(order(9, 0, 100, 2450)).rule_id == Rules.VENUE_DISCONNECTED
    eng.on_venue_reconnect(1, T0)
    assert eng.check_order(order(9, 0, 100, 2450)).rule_id == Rules.DUPLICATE_ORDER_ID


def test_u64_order_ids_print_unsigned_and_sort_unsigned(config_doc):
    """Audit parity with the Rust reference: order ids are u64, printed
    as unsigned decimals and iterated in unsigned order (BTreeMap<u64>)."""
    eng = engine(config_doc)
    big = 1 << 63
    expect(eng, order(big, 1, 10, 2451, timestamp=T0 + SEC), Rules.ALLOW)
    d = eng.check_order(order(7, 0, 10, 2451, timestamp=T0 + 2 * SEC))
    assert d.rule_id == Rules.SELF_MATCH
    assert d.reason == "would cross own open order 9223372036854775808 at 2451"
    assert '"reason":"would cross own open order 9223372036854775808 at 2451"' in eng.audit_jsonl()
    # resting orders are iterated in UNSIGNED id order: 5 < 2^63
    eng2 = engine(config_doc)
    expect(eng2, order(big, 1, 10, 2451, timestamp=T0 + SEC), Rules.ALLOW)
    expect(eng2, order(5, 1, 10, 2451, timestamp=T0 + 2 * SEC), Rules.ALLOW)
    d = eng2.check_order(order(9, 0, 10, 2451, timestamp=T0 + 3 * SEC))
    assert d.reason == "would cross own open order 5 at 2451"
    max_u64 = (1 << 64) - 1
    eng3 = engine(config_doc)
    expect(eng3, order(max_u64, 0, 10, 2450, timestamp=T0 + SEC), Rules.ALLOW)
    d = eng3.check_order(order(max_u64, 0, 10, 2450, timestamp=T0 + 2 * SEC))
    assert d.reason == f"order_id 18446744073709551615 already used at ts {T0 + SEC}"
    assert eng3.snapshot()["open"] == {
        "18446744073709551615": {"instrument_id": 1, "side": 0, "price_ticks": 2450, "qty": 10}
    }


# -------------------------------------------------------- market-data gates


def test_sequence_gap_gates_until_recovery(config_doc):
    eng = engine(config_doc)
    eng.on_sequence_gap(1, T0)
    d = eng.check_order(order(1, 0, 100, 2450))
    assert d.rule_id == Rules.SEQUENCE_GAP
    assert d.reason == "instrument 1 feed has an unrecovered gap"
    # other instruments unaffected
    assert eng.check_order(order(2, 0, 100, 3120, instrument_id=2)).allowed()
    eng.on_feed_recovered(1, T0 + NS)
    assert eng.check_order(order(3, 0, 100, 2450)).allowed()
    assert eng.snapshot()["market"]["1"]["gaps"] == 0
    # a gap on a never-marked instrument creates a gated, unpriced entry
    eng.on_sequence_gap(2, T0)
    eng.on_sequence_gap(7, T0)
    assert eng.snapshot()["market"]["7"] == {
        "bid_ticks": 0, "ask_ticks": 0, "ts": T0, "gaps": 1, "gated": True}
    eng.on_feed_recovered(8, T0)  # unknown instrument: no-op
    assert "8" not in eng.snapshot()["market"]


def test_gap_threshold_counts_gaps(config_doc):
    doc = config(config_doc)
    doc["market_data"]["max_sequence_gap_before_halt"] = 3
    eng = RiskEngine.from_config_ticks(doc, ticks())
    eng.on_market(1, 2450, 2452, T0)
    eng.on_sequence_gap(1, T0)
    eng.on_sequence_gap(1, T0)
    assert eng.check_order(order(1, 0, 100, 2450)).allowed()
    eng.on_sequence_gap(1, T0)
    assert eng.check_order(order(2, 0, 100, 2450)).rule_id == Rules.SEQUENCE_GAP
    # a fail-closed engine has threshold 0: every gap gates (moot: CONFIG_MISSING decides)
    closed = RiskEngine.fail_closed("x")
    closed.on_sequence_gap(1, T0)
    assert closed.snapshot()["market"]["1"]["gated"]


def test_stale_price_rejects_and_recovers(config_doc):
    eng = engine(config_doc)
    # no market data at all for a fresh instrument id
    eng2 = RiskEngine.from_config_ticks(config_doc, {**ticks(), 3: 0.01})
    d = eng2.check_order(order(1, 0, 100, 2450, instrument_id=3))
    assert d.rule_id == Rules.STALE_PRICE
    assert d.reason == "no reference price for instrument 3"
    # a one-sided book is no reference price either
    eng2.on_market(3, 0, 2452, T0)
    assert eng2.check_order(order(2, 0, 100, 2450, instrument_id=3)).rule_id == Rules.STALE_PRICE
    # age beyond the 5s timeout
    d = eng.check_order(order(1, 0, 100, 2450, timestamp=T0 + 6 * NS))
    assert d.rule_id == Rules.STALE_PRICE
    assert d.reason == f"reference price age {6 * NS}ns exceeds {5 * NS}ns"
    # exactly at the timeout is fresh
    assert eng.check_order(order(3, 0, 100, 2450, timestamp=T0 + 5 * NS)).allowed()
    # refresh -> allowed
    eng.on_market(1, 2450, 2452, T0 + 6 * NS)
    assert eng.check_order(order(2, 0, 100, 2450, timestamp=T0 + 6 * NS + 1)).allowed()
    # stale_book_reject false disables the age gate (but not the no-mid gate)
    doc = config(config_doc)
    doc["per_order"]["stale_book_reject"] = False
    eng3 = engine(doc)
    assert eng3.check_order(order(1, 0, 100, 2450, timestamp=T0 + 60 * NS)).allowed()


def test_venue_disconnect_rejects_until_reconnect(config_doc):
    eng = engine(config_doc)
    eng.on_venue_disconnect(1, T0)
    d = eng.check_order(order(1, 0, 100, 2450))
    assert d.rule_id == Rules.VENUE_DISCONNECTED
    assert d.reason == "venue 1 is disconnected"
    # a different venue still works, and venue 0 (SOR) skips the check
    assert eng.check_order(order(2, 0, 100, 2450, venue_id=2)).allowed()
    eng.on_order_done(2)
    assert eng.check_order(order(4, 0, 100, 2450, venue_id=0)).allowed()
    eng.on_order_done(4)
    eng.on_venue_reconnect(1, T0 + NS)
    assert eng.check_order(order(3, 0, 100, 2450)).allowed()
    ids = [e.rule_id for e in eng.audit()]
    assert ids[0] == Rules.VENUE_DISCONNECT and eng.audit()[0].decision == int(Decision.KILL)
    assert Rules.VENUE_RECONNECT in ids
    assert eng.snapshot()["venues_down"] == {"1": False}


# ------------------------------------------------------- fat finger / band


def test_fat_finger_qty_boundary(config_doc):
    eng = engine(config_doc)
    assert eng.check_order(order(1, 0, 40_000, 2452)).allowed()  # 981k < 1M
    d = eng.check_order(order(2, 0, 50_001, 2452))
    assert d.rule_id == Rules.FAT_FINGER_QTY
    assert d.reason == "qty 50001 exceeds max_order_qty 50000"


def test_fat_finger_notional_uses_limit_price_or_mid(config_doc):
    eng = engine(config_doc)
    # 45000 * 24.60 = 1,107,000 > 1,000,000
    d = eng.check_order(order(1, 0, 45_000, 2460))
    assert d.rule_id == Rules.FAT_FINGER_NOTIONAL
    assert d.reason == "notional 1107000.00 USD exceeds max_order_notional 1000000.00"
    # unpriced market order valued at the mid: 45000 * 24.51 = 1,102,950
    d = eng.check_order(order(2, 0, 45_000, 0))
    assert d.rule_id == Rules.FAT_FINGER_NOTIONAL
    assert d.reason.startswith("notional 1102950.00 USD")
    assert eng.check_order(order(3, 0, 40_000, 0)).allowed()


def test_price_band_rejects_far_limits_only(config_doc):
    eng = engine(config_doc)
    # 30.00 vs mid 24.51 => ~2240bps > 200bps
    d = eng.check_order(order(1, 0, 30_000, 3000))
    assert d.rule_id == Rules.PRICE_BAND
    assert d.reason == "price deviates 2239.9bps from mid, band 200.0bps"
    # market orders carry no price -> no band check
    assert eng.check_order(order(2, 0, 100, 0)).allowed()
    eng.on_order_done(2)  # venue terminal report (an open MARKET buy would self-match)
    # just inside the band: 24.99 vs 24.51 = ~196bps
    assert eng.check_order(order(3, 1, 100, 2499)).allowed()


# ---------------------------------------------------------------- throttle


def test_throttle_is_an_event_time_token_bucket(config_doc):
    eng = engine(config_doc)
    base = T0 + 100_000_000
    # burst 4: four same-timestamp orders pass, the fifth throttles
    for i in range(4):
        assert eng.check_order(order(10 + i, 0, 10, 0, timestamp=base)).allowed(), f"order {i}"
    d = eng.check_order(order(14, 0, 10, 0, timestamp=base))
    assert d.rule_id == Rules.RATE_THROTTLE
    assert d.reason == "strategy S1 exceeded 500.00 orders/s (burst 4.00)"
    # 2ms of event time refills exactly one token at 500/s
    assert eng.check_order(order(15, 0, 10, 0, timestamp=base + 2_000_000)).allowed()
    assert eng.check_order(order(16, 0, 10, 0, timestamp=base + 2_000_000)).rule_id \
        == Rules.RATE_THROTTLE
    # buckets are per strategy
    assert eng.check_order(order(17, 0, 10, 0, timestamp=base + 2_000_000,
                                 strategy_id="S2")).allowed()
    # a rejected order that reached check 15 consumed a token
    eng2 = engine(config_doc)
    for i in range(3):
        assert eng2.check_order(order(20 + i, 0, 10, 0, timestamp=base)).allowed()
    assert eng2.check_order(order(23, 1, 10, 0, timestamp=base)).rule_id == Rules.SELF_MATCH
    assert eng2.check_order(order(24, 0, 10, 0, timestamp=base)).rule_id == Rules.RATE_THROTTLE
    assert eng2.snapshot()["buckets"]["S1"] == {"tokens": 0.0, "last_ts": base, "primed": True}


def test_throttle_never_refills_backwards_in_time(config_doc):
    eng = engine(config_doc)
    base = T0 + 100_000_000
    for i in range(4):
        assert eng.check_order(order(10 + i, 0, 10, 0, timestamp=base + i)).allowed()
    # an out-of-order earlier timestamp must not mint tokens
    assert eng.check_order(order(20, 0, 10, 0, timestamp=base - 10 * NS)).rule_id \
        == Rules.RATE_THROTTLE


def test_risk_throttle_regression_then_forward(config_doc):
    """Scenario: two threads share a strategy id and one clock runs behind.
    The regressed order must not reset the bucket clock: the next in-order
    order is still throttled."""
    eng = engine(config_doc)
    base = T0 + 100_000_000
    for i in range(4):
        assert eng.check_order(order(10 + i, 0, 10, 0, timestamp=base + i)).allowed()
    assert eng.check_order(order(20, 0, 10, 0, timestamp=base - 10 * NS)).rule_id \
        == Rules.RATE_THROTTLE  # thread B, clock behind
    assert eng.check_order(order(21, 0, 10, 0, timestamp=base + 3)).rule_id \
        == Rules.RATE_THROTTLE  # thread A, in order: no time has elapsed
    assert eng.snapshot()["buckets"]["S1"]["last_ts"] == base + 3
    # and only real elapsed event time refills (2ms = one token at 500/s)
    assert eng.check_order(order(22, 0, 10, 0, timestamp=base + 2_000_003)).allowed()


def test_unprimed_restored_bucket_is_primed_on_first_use(config_doc):
    """A snapshot may carry ``primed: false``; the first order re-fills the
    bucket to the burst at its own timestamp (Rust ``!bucket.primed``)."""
    eng = engine(config_doc)
    snap = eng.snapshot()
    snap["buckets"]["S1"] = {"tokens": 0.0, "last_ts": 0, "primed": False}
    restored = RiskEngine.restore(RiskLimits.from_json(config_doc), equity_refs(ticks()), snap, T0)
    base = T0 + 100_000_000
    for i in range(4):
        assert restored.check_order(order(10 + i, 0, 10, 0, timestamp=base)).allowed()
    assert restored.check_order(order(14, 0, 10, 0, timestamp=base)).rule_id == Rules.RATE_THROTTLE
    assert restored.snapshot()["buckets"]["S1"] == {"tokens": 0.0, "last_ts": base, "primed": True}


# -------------------------------------------------------------- self-match


def test_self_match_priced_and_unpriced_cases(config_doc):
    eng = engine(config_doc)

    # orders spaced 10ms apart so the throttle (checked BEFORE self-match)
    # never binds in this test
    def spaced(oid: int, side: int, qty: int, price: int) -> OrderRequest:
        return order(oid, side, qty, price, timestamp=T0 + 100_000_000 + oid * 10_000_000)

    assert eng.check_order(spaced(1, 0, 100, 2450)).allowed()  # resting buy at 2450
    d = eng.check_order(spaced(2, 1, 50, 2450))  # sell at 2450 would cross own bid
    assert d.rule_id == Rules.SELF_MATCH
    assert d.reason == "would cross own open order 1 at 2450"
    assert eng.check_order(spaced(3, 1, 50, 2455)).allowed()  # rests above own bid
    # buy at/through own ask crosses
    assert eng.check_order(spaced(4, 0, 50, 2455)).rule_id == Rules.SELF_MATCH
    assert eng.check_order(spaced(5, 0, 50, 2460)).rule_id == Rules.SELF_MATCH
    # buy below own ask is fine
    assert eng.check_order(spaced(6, 0, 50, 2451)).allowed()
    # unpriced marketable buy vs any own resting ask: rejected (pinned)
    assert eng.check_order(spaced(7, 0, 50, 0)).rule_id == Rules.SELF_MATCH
    # cancel the resting ask -> market buy passes
    eng.on_order_done(3)
    assert eng.check_order(spaced(8, 0, 50, 0)).allowed()
    # ... and the open MARKET buy blocks any own sell until it is done
    d = eng.check_order(spaced(10, 1, 50, 2455))
    assert d.rule_id == Rules.SELF_MATCH
    assert d.reason == "would cross own open order 8 at 0"
    eng.on_order_done(8)
    # same-side resting never self-matches
    assert eng.check_order(spaced(9, 1, 50, 2455)).allowed()


def test_unpriced_buy_ignores_own_resting_bids(config_doc):
    eng = engine(config_doc)
    expect(eng, order(1, 0, 100, 2450, timestamp=T0 + 1), Rules.ALLOW)  # own bid
    expect(eng, order(2, 0, 50, 0, timestamp=T0 + 2), Rules.ALLOW)


def test_self_match_prevention_variants_with_peg(config_doc):
    step = 10_000_000
    eng = engine(config_doc)
    expect(eng, order(1, 0, 100, 2450, timestamp=T0 + step), Rules.ALLOW)  # bid rests
    expect(eng, order(2, 1, 50, 2450, timestamp=T0 + 2 * step), Rules.SELF_MATCH)
    expect(eng, order(3, 1, 50, 2455, timestamp=T0 + 3 * step), Rules.ALLOW)
    expect(eng, order(4, 0, 50, 0, timestamp=T0 + 4 * step), Rules.SELF_MATCH)
    # PEG orders are checked at their pegged touch (bid 2450 for a buy) —
    # below the own ask at 2455, so no cross
    expect(eng, typed(5, 1, 0, 50, 0, OrderType.PEG, T0 + 5 * step), Rules.ALLOW)
    # ... but a sell at/below the pegged bid would cross it
    expect(eng, order(7, 1, 50, 2450, timestamp=T0 + 5 * step + 1), Rules.SELF_MATCH)
    # once the resting ask is done, the unpriced buy is fine
    eng.on_order_done(3)
    expect(eng, typed(6, 1, 0, 50, 0, OrderType.IOC, T0 + 6 * step), Rules.ALLOW)


def test_fills_release_resting_orders_for_self_match(config_doc):
    eng = engine(config_doc)
    assert eng.check_order(order(1, 1, 100, 2455)).allowed()  # resting ask
    assert eng.check_order(order(2, 0, 10, 2455)).rule_id == Rules.SELF_MATCH
    # a partial fill keeps the remainder open
    eng.on_fill(fill("S1", 1, 1, 40, 2455, order_id=1))
    assert eng.open_order_count() == 1
    assert eng.snapshot()["open"]["1"]["qty"] == 60
    assert eng.check_order(order(4, 0, 10, 2455)).rule_id == Rules.SELF_MATCH
    # full fill of the resting ask removes it
    eng.on_fill(fill("S1", 1, 1, 60, 2455, order_id=1))
    assert eng.open_order_count() == 0
    assert eng.check_order(order(3, 0, 10, 2455)).allowed()
    # a fill naming an unknown order id is applied without touching open orders
    eng.on_fill(fill("S1", 1, 0, 5, 2455, order_id=77))
    assert eng.open_order_count() == 1


def test_self_match_is_firm_wide_across_strategies_and_venues(config_doc):
    eng = engine(config_doc)
    assert eng.check_order(order(1, 0, 100, 2450)).allowed()  # S1 bid @ 2450 on venue 1
    d = eng.check_order(order(2, 1, 50, 2450, strategy_id="S2", venue_id=2,
                              timestamp=T0 + 110_000_000))
    assert d.rule_id == Rules.SELF_MATCH


# ------------------------------------------------- position / notional set


def test_position_limit_projects_open_orders(config_doc):
    eng = engine(config_doc)
    eng.on_fill(fill("S1", 1, 0, 95_000, 2452))
    assert eng.position(1) == 95_000
    assert eng.check_order(order(1, 0, 100, 2450)).allowed()  # resting buy 100
    d = eng.check_order(order(2, 0, 40_000, 2452))  # 95000 + 100 + 40000 > 100000
    assert d.rule_id == Rules.POSITION_LIMIT
    assert d.reason == "projected position 135100 exceeds max_position_qty 100000"
    # sells project the other way: 95000 - 40000 fine
    assert eng.check_order(order(3, 1, 40_000, 2455)).allowed()
    # shorts cap symmetrically
    eng2 = engine(config_doc)
    eng2.on_fill(fill("S1", 1, 1, 95_000, 2452))
    d = eng2.check_order(order(1, 1, 40_000, 2455))
    assert d.rule_id == Rules.POSITION_LIMIT
    assert d.reason == "projected position -135000 exceeds max_position_qty 100000"


def test_open_orders_count_in_position_projection(config_doc):
    eng = engine(config_doc)
    expect(eng, order(1, 0, 40_000, 2450, timestamp=T0 + 1), Rules.ALLOW)
    expect(eng, order(2, 0, 40_000, 2450, timestamp=T0 + 2), Rules.ALLOW)
    expect(eng, order(3, 0, 40_000, 2450, timestamp=T0 + 3), Rules.POSITION_LIMIT)
    # a fill against order 1 moves qty from open to position; the
    # worst-case projection is unchanged and still rejects
    eng.on_fill(fill("S1", 1, 0, 40_000, 2450, ts=T0 + 4, order_id=1))
    expect(eng, order(4, 0, 40_000, 2450, timestamp=T0 + 5), Rules.POSITION_LIMIT)


def test_instrument_notional_limit_binds_before_position_cap(config_doc):
    eng = engine(config_doc)
    eng.on_fill(fill("S1", 1, 0, 95_000, 2452))
    # projected 98,000 * 24.51 = 2,401,980 > 2,400,000 but position ok
    d = eng.check_order(order(1, 0, 3_000, 2452))
    assert d.rule_id == Rules.INSTRUMENT_NOTIONAL
    assert d.reason == "projected notional 2401980.00 exceeds max_instrument_notional 2400000.00"
    assert eng.check_order(order(2, 0, 100, 2452)).allowed()


def test_gross_and_net_notional_limits(config_doc):
    eng = engine(config_doc)
    # long 95,000 @ inst1 (2.33M) and short 70,000 @ inst2 (2.18M):
    # gross 4.51M, net 0.15M
    eng.on_fill(fill("S1", 1, 0, 95_000, 2452))
    eng.on_fill(fill("S2", 2, 1, 70_000, 3120))
    # sell 20,000 inst1 at 24.55 adds 491k gross -> 5.006M > 5M
    d = eng.check_order(order(1, 1, 20_000, 2455))
    assert d.rule_id == Rules.GROSS_NOTIONAL
    assert d.reason == "projected gross notional 5003450.00 exceeds max_gross_notional 5000000.00"
    assert eng.check_order(order(2, 1, 1_000, 2455)).allowed()

    # net: all-long book
    eng = engine(config_doc)
    eng.on_fill(fill("S1", 1, 0, 95_000, 2452))  # net 2.329M
    d = eng.check_order(order(3, 0, 6_000, 3120, instrument_id=2))  # +187k -> 2.517M > 2.5M
    assert d.rule_id == Rules.NET_NOTIONAL
    assert d.reason == "projected net notional 2515650.00 exceeds max_net_notional 2500000.00"


def test_unmarked_position_fails_closed_on_gross_check(config_doc):
    eng = RiskEngine.from_config_ticks(config_doc, {**ticks(), 3: 0.01})
    eng.on_market(1, 2450, 2452, T0)
    # a position in instrument 3, which has never had market data
    eng.on_fill(fill("S1", 3, 0, 1_000, 5000))
    d = eng.check_order(order(1, 0, 100, 2450))
    assert d.rule_id == Rules.GROSS_NOTIONAL
    assert d.reason == "position in instrument 3 has no mark price (fail-closed)"
    # an unpriced open order in an unmarked instrument cannot be valued and
    # is skipped in gross/net (it still counts in the position projection)
    eng2 = RiskEngine.from_config_ticks(config_doc, {**ticks(), 3: 0.01})
    eng2.on_market(1, 2450, 2452, T0)
    eng2.on_market(3, 4999, 5001, T0)
    assert eng2.check_order(order(1, 0, 100, 0, instrument_id=3)).allowed()
    eng2.on_market(3, 0, 0, T0 + 1)  # mark withdrawn
    assert eng2.check_order(order(2, 0, 100, 2450, timestamp=T0 + 2)).allowed()


# -------------------------------------------------------------- loss limits


def test_strategy_loss_limit_kills_the_strategy(config_doc):
    eng = engine(config_doc)
    eng.on_fill(fill("S1", 1, 0, 95_000, 2452))  # avg 24.52
    eng.on_fill(fill("S1", 1, 1, 95_000, 2398))  # realized -51,300
    assert abs(eng.strategy_pnl("S1") - -51_300.0) < 1e-6
    latch = [e for e in eng.audit() if e.rule_id == Rules.STRATEGY_LOSS
             and e.decision == int(Decision.KILL)]
    assert len(latch) == 1
    assert latch[0].reason == "strategy daily pnl -51300.00 breaches loss limit 50000.00"
    assert latch[0].severity == int(Severity.BREACH)
    assert eng.check_order(order(1, 0, 100, 2450)).rule_id == Rules.KILL_STRATEGY
    # other strategies keep trading
    assert eng.check_order(order(2, 0, 100, 2450, strategy_id="S2")).allowed()


def test_global_daily_loss_kills_everything(config_doc):
    eng = engine(config_doc)
    eng.on_fill(fill("S1", 1, 0, 95_000, 2452))
    eng.on_fill(fill("S1", 1, 1, 95_000, 2180))  # realized -258,400
    assert eng.realized_pnl() < -250_000.0
    assert kills(eng) == [Rules.STRATEGY_LOSS, Rules.DAILY_LOSS]
    daily = [e for e in eng.audit() if e.rule_id == Rules.DAILY_LOSS][0]
    assert daily.reason == "global daily pnl -258400.00 breaches daily loss limit 250000.00"
    # every strategy is now rejected by the global switch
    assert eng.check_order(order(1, 0, 100, 2450, strategy_id="S9")).rule_id == Rules.KILL_GLOBAL
    assert eng.metrics.gauge_value("risk_kill_switch_engaged") == 1.0


def test_loss_limits_latch_on_unrealized_at_the_fill(config_doc):
    eng = engine(config_doc)
    eng.on_fill(fill("S1", 1, 0, 95_000, 2452, ts=T0 + 1))
    eng.on_fill(fill("S1", 1, 1, 95_000, 2398, ts=T0 + 2))
    assert not eng.kill_switch_engaged()
    # the buy at 35.00 marked at 31.20 is -380k UNREALIZED and latches the
    # global switch at the fill, before the closing sell
    eng.on_market(2, 3119, 3121, T0 + 3)
    eng.on_fill(fill("S2", 2, 0, 100_000, 3500, ts=T0 + 4))
    assert eng.kill_switch_engaged()
    assert abs(eng.unrealized_pnl() - -380_000.0) < 1e-6
    eng.on_fill(fill("S2", 2, 1, 100_000, 3120, ts=T0 + 5))
    assert abs(eng.global_daily_pnl() - -431_300.0) < 1e-6
    assert eng.unrealized_pnl() == 0.0
    assert eng.metrics.gauge_value("risk_daily_pnl") == eng.global_daily_pnl()
    assert eng.metrics.gauge_value("risk_realized_pnl") == eng.realized_pnl()
    expect(eng, order(2, 0, 10, 3120, instrument_id=2, strategy_id="S3", timestamp=T0 + 6),
           Rules.KILL_GLOBAL)


def test_avg_cost_pnl_accounting_is_pinned(config_doc):
    eng = engine(config_doc)
    # buy 100 @ 24.00, buy 100 @ 26.00 -> avg 25.00
    eng.on_fill(fill("S1", 1, 0, 100, 2400))
    eng.on_fill(fill("S1", 1, 0, 100, 2600))
    # sell 150 @ 25.50 -> realized (25.50 - 25.00) * 150 = +75
    eng.on_fill(fill("S1", 1, 1, 150, 2550))
    assert abs(eng.strategy_pnl("S1") - 75.0) < 1e-9
    assert eng.position(1) == 50
    # sell 100 @ 24.00: closes 50 (-50), opens short 50 @ 24.00
    eng.on_fill(fill("S1", 1, 1, 100, 2400))
    assert abs(eng.strategy_pnl("S1") - 25.0) < 1e-9
    assert eng.position(1) == -50
    assert eng.snapshot()["lots"] == [
        {"strategy_id": "S1", "instrument_id": 1, "pos": -50, "avg_price": 24.0}]
    # buy 50 @ 23.00 closes the short: +50 * (24-23) = +50
    eng.on_fill(fill("S1", 1, 0, 50, 2300))
    assert abs(eng.strategy_pnl("S1") - 75.0) < 1e-9
    assert eng.position(1) == 0
    # short averaging: sell 100 @ 30, sell 100 @ 32 -> avg 31; cover 50 @ 29 -> +100
    eng.on_fill(fill("S2", 2, 1, 100, 3000))
    eng.on_fill(fill("S2", 2, 1, 100, 3200))
    eng.on_fill(fill("S2", 2, 0, 50, 2900))
    assert abs(eng.strategy_pnl("S2") - 100.0) < 1e-9
    assert eng.position(2) == -150
    # lots are per (strategy, instrument); positions aggregate per instrument
    eng.on_fill(fill("S3", 2, 0, 150, 3100))
    assert eng.position(2) == 0
    assert len(eng.snapshot()["lots"]) == 3


# ------------------------------------------------------------------- audit


def test_every_decision_is_audited_and_deterministic(config_doc):
    def run() -> str:
        eng = engine(config_doc)
        eng.check_order(order(1, 0, 100, 2450))
        eng.check_order(order(1, 0, 100, 2450))  # duplicate
        eng.check_order(order(2, 0, 60_000, 2450))  # fat finger
        eng.on_venue_disconnect(2, T0 + NS)
        eng.on_fill(fill("S1", 1, 0, 95_000, 2452))
        eng.on_fill(fill("S1", 1, 1, 95_000, 2398))  # strategy kill
        eng.check_order(order(3, 0, 100, 2450))
        return eng.audit_jsonl()

    log1 = run()
    log2 = run()
    assert log1 == log2, "audit logs must be byte-identical across runs"
    assert len(log1.splitlines()) == 6  # 4 decisions + disconnect + kill
    # and every line round-trips through the schema shape
    for line in log1.splitlines():
        ev = RiskEvent.from_json_line(line)
        assert ev.to_json_line() == line
        assert set(ev.to_dict()) == {"timestamp", "scope", "scope_id", "rule_id",
                                     "severity", "decision", "reason"}


def test_risk_event_json_line_round_trips_with_schema_keys():
    ev = RiskEvent(42, Scope.INSTRUMENT, "1", Rules.PRICE_BAND, Severity.WARN,
                   Decision.REJECT, "price 30 vs mid 24.51")
    line = ev.to_json_line()
    assert list(json.loads(line)) == ["decision", "reason", "rule_id", "scope",
                                      "scope_id", "severity", "timestamp"]
    assert line == ('{"decision":2,"reason":"price 30 vs mid 24.51","rule_id":"PRICE_BAND",'
                    '"scope":"INSTRUMENT","scope_id":"1","severity":2,"timestamp":42}')
    assert RiskEvent.from_json_line(line) == ev
    assert to_canonical_json(ev.to_dict()) == line


def test_risk_event_bad_lines_are_rejected():
    for bad in ("{}", "not json", "[]",
                '{"timestamp":1,"scope":"PLANET","scope_id":"","rule_id":"X","severity":1,"decision":1,"reason":""}',
                '{"timestamp":1,"scope":"GLOBAL","scope_id":"","rule_id":"X","severity":9,"decision":1,"reason":""}',
                '{"timestamp":1.5,"scope":"GLOBAL","scope_id":"","rule_id":"X","severity":1,"decision":1,"reason":""}',
                '{"timestamp":1,"scope":"GLOBAL","scope_id":"","rule_id":"X","severity":1,"decision":1,"reason":7}'):
        with pytest.raises(ValueError):
            RiskEvent.from_json_line(bad)
    # serde's derived deserializer ignores unknown fields
    extra = '{"timestamp":1,"scope":"GLOBAL","scope_id":"","rule_id":"X","severity":1,"decision":1,"reason":"","x":1}'
    assert RiskEvent.from_json_line(extra).rule_id == "X"
    with pytest.raises(ValueError):
        RiskEvent(1, Scope.GLOBAL, "", "X", 0, 1, "")
    with pytest.raises(ValueError):
        RiskEvent(1, "GLOBAL", "", "X", 1, 1, "")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Scope.parse("PLANET")


def test_risk_event_json_escapes_like_serde_json():
    ev = RiskEvent(7, Scope.GLOBAL, "", Rules.KILL_SWITCH_ENGAGED, 3, 3,
                   "a\bb\fc\nd\re\tf\u0001g\"h\\i\u007fé")
    assert ev.to_json_line() == (
        '{"decision":3,"reason":"a\\bb\\fc\\nd\\re\\tf\\u0001g\\"h\\\\i\u007fé",'
        '"rule_id":"KILL_SWITCH_ENGAGED","scope":"GLOBAL","scope_id":"",'
        '"severity":3,"timestamp":7}')
    assert RiskEvent.from_json_line(ev.to_json_line()) == ev


def test_fixed_formatting_is_integer_scaled():
    assert fmt_fixed(2.675, 2) == "2.68"  # 2.675*100 rounds to 267.5 in binary
    assert fmt_fixed(32.4375, 2) == "32.44"  # exact tie: half away
    assert fmt_fixed(-50012.345, 2) == "-50012.35"
    assert fmt_fixed(-0.001, 2) == "0.00"
    assert fmt_fixed(1000000.125, 2) == "1000000.13"
    assert fmt_fixed(0.05, 1) == "0.1"
    assert fmt_fixed(500.0, 0) == "500"
    assert fmt_fixed(-7.0, 2) == "-7.00"
    # ``f64::round`` is exact half-away: 0.49999999999999994 rounds down
    assert fmt_fixed(0.49999999999999994, 0) == "0"
    assert fmt_fixed(-0.0, 2) == "0.00"
    # ``as i64`` saturates and maps NaN to 0
    assert fmt_fixed(1e30, 2) == "92233720368547758.07"
    assert fmt_fixed(-1e30, 0) == "-9223372036854775807"
    assert fmt_fixed(float("nan"), 2) == "0.00"
    assert fmt_fixed(float("-inf"), 1) == "-922337203685477580.7"
    with pytest.raises(ValueError):
        fmt_fixed(1.0, -1)


def test_canonical_json_matches_serde_json():
    assert format_f64(3.0) == "3.0"
    assert format_f64(24.51) == "24.51"
    assert format_f64(0.0) == "0.0" and format_f64(-0.0) == "-0.0"
    assert format_f64(1e16) == "1e+16" and format_f64(1e15) == "1000000000000000.0"
    assert format_f64(1e-5) == "0.00001" and format_f64(1e-6) == "1e-6"
    assert format_f64(1.23456e-7) == "1.23456e-7" and format_f64(1.5e300) == "1.5e+300"
    assert format_f64(0.1 + 0.2) == "0.30000000000000004"
    assert format_f64(float("nan")) == "null" and format_f64(float("inf")) == "null"
    assert rust_display_f64(-5.0) == "-5" and rust_display_f64(1e-7) == "0.0000001"
    assert rust_display_f64(1e21) == "1000000000000000000000"
    assert rust_display_f64(0.5) == "0.5" and rust_display_f64(-0.0) == "-0"
    doc = {"b": [], "a": {}, "c": [1, {"x": 2.0}], "t": True, "n": None}
    assert to_canonical_json(doc) == '{"a":{},"b":[],"c":[1,{"x":2.0}],"n":null,"t":true}'
    assert to_canonical_json(doc, pretty=True) == (
        '{\n  "a": {},\n  "b": [],\n  "c": [\n    1,\n    {\n      "x": 2.0\n    }\n  ],'
        '\n  "n": null,\n  "t": true\n}')
    with pytest.raises(ValueError):
        to_canonical_json({1: 2})
    with pytest.raises(ValueError):
        to_canonical_json({"a": object()})


def test_metrics_count_decisions(config_doc):
    eng = engine(config_doc)
    eng.check_order(order(1, 0, 100, 2450))
    eng.check_order(order(2, 0, 60_000, 2450))
    assert eng.metrics.counter_value("risk_decisions_total") == 2
    assert eng.metrics.counter_value("risk_allowed_total") == 1
    assert eng.metrics.counter_value("risk_rejected_total") == 1
    assert eng.metrics.counter_value("risk_events_total") == 2
    assert eng.metrics.counter_value("never_touched") == 0
    assert eng.metrics.gauge_value("never_set") is None
    assert eng.metrics.gauge_value("risk_kill_switch_engaged") == 0.0
    assert list(eng.metrics.counters()) == sorted(eng.metrics.counters())
    assert set(eng.metrics.gauges()) == {"risk_kill_switch_engaged", "risk_realized_pnl",
                                         "risk_unrealized_pnl", "risk_daily_pnl"}


# ------------------------------------------------ round-3 scenario tests


def test_risk_mtm_loss_latches_without_fill(config_doc):
    """Scenario: a news gap marks a 95k long down 14% with no fill. The
    mark update alone must latch STRATEGY_LOSS then DAILY_LOSS."""
    eng = engine(config_doc)
    eng.on_fill(fill("S1", 1, 0, 95_000, 2452))  # 95k @ 24.52
    assert all(e.decision != int(Decision.KILL) for e in eng.audit())
    eng.on_market(1, 2099, 2101, T0 + 2 * NS)  # mid 21.00 -> -334,400
    assert kills(eng) == [Rules.STRATEGY_LOSS, Rules.DAILY_LOSS]
    assert abs(eng.strategy_daily_pnl("S1") + 334_400.0) < 1e-6
    assert abs(eng.unrealized_pnl() + 334_400.0) < 1e-6
    assert eng.realized_pnl() == 0.0
    assert eng.metrics.gauge_value("risk_unrealized_pnl") == eng.unrealized_pnl()
    assert eng.check_order(order(1, 0, 100, 2100)).rule_id == Rules.KILL_GLOBAL
    # a strategy that never traded is also blocked by the global latch
    assert eng.check_order(order(2, 0, 100, 2100, strategy_id="S9")).rule_id == Rules.KILL_GLOBAL
    # a mark on an instrument nobody holds re-checks only the global scope
    eng2 = engine(config_doc)
    eng2.on_fill(fill("S1", 1, 0, 95_000, 2452))
    eng2.on_market(2, 3119, 3121, T0 + NS)
    assert kills(eng2) == []


def test_market_update_regression_is_dropped(config_doc):
    eng = engine(config_doc)
    eng.on_market(1, 2000, 2002, T0 - NS)  # older than the T0 update
    assert eng.metrics.counter_value("risk_market_regressions_dropped_total") == 1
    # the mid is still 24.51: a 45,000 market buy is 1,102,950 > 1M
    assert eng.check_order(order(1, 0, 45_000, 0)).rule_id == Rules.FAT_FINGER_NOTIONAL
    # an equal timestamp is accepted (>=), a regression never re-evaluates loss limits
    eng.on_market(1, 2000, 2002, T0)
    assert eng.snapshot()["market"]["1"]["bid_ticks"] == 2000


def test_risk_fx_notional_uses_lot_size_and_ccy(config_doc):
    """Scenario: fat-fingered FX ticket. USD/JPY qty is 1,000-USD lots and
    prices are JPY: the notional must be qty * 1000 * price / (USD/JPY
    mid) in USD, so 1,001 lots (1,001,000 USD) rejects and 999 passes."""
    eng = fx_engine(config_doc)
    t = T0 + 100_000_000
    d = eng.check_order(typed(1, 103, 1, 1001, 0, OrderType.MARKET, t))
    assert d.rule_id == Rules.FAT_FINGER_NOTIONAL, d.reason
    assert d.reason.startswith("notional 1001000.00 USD"), d.reason
    assert eng.check_order(typed(2, 103, 1, 999, 0, OrderType.MARKET, t)).allowed()
    d = eng.check_order(typed(3, 103, 1, 50_000, 0, OrderType.MARKET, t))
    assert d.rule_id == Rules.FAT_FINGER_NOTIONAL
    # missing conversion pair (GBP/USD never marked) -> FX_RATE_MISSING
    eng.on_market(108, 85_315, 85_325, t)
    d = eng.check_order(typed(4, 108, 0, 100, 0, OrderType.MARKET, t))
    assert d.rule_id == Rules.FX_RATE_MISSING
    assert d.reason == "no conversion rate for GBP -> USD"
    eng.on_market(102, 127_335, 127_345, t)
    assert eng.check_order(typed(5, 108, 0, 100, 0, OrderType.MARKET, t + 10_000_000)).allowed()
    # a stale conversion rate is as bad as a missing one
    eng.on_market(108, 85_315, 85_325, t + 6 * NS)
    d = eng.check_order(typed(6, 108, 0, 100, 0, OrderType.MARKET, t + 6 * NS))
    assert d.rule_id == Rules.FX_RATE_MISSING
    assert d.reason == f"conversion rate GBP -> USD age {6 * NS}ns exceeds {5 * NS}ns"
    # a one-sided conversion pair is no rate
    eng.on_market(102, 0, 127_345, t + 6 * NS)
    d = eng.check_order(typed(7, 108, 0, 100, 0, OrderType.MARKET, t + 6 * NS))
    assert d.reason == "no conversion rate for GBP -> USD"


def test_fx_pnl_is_converted_before_loss_limits(config_doc):
    """Multi-currency P&L: a JPY loss is converted at the USD/JPY mid
    before the loss limit is evaluated (150,000 JPY at 150 = 1,000 USD)."""
    eng = fx_engine(config_doc)
    eng.on_market(103, 149_995, 150_005, T0)  # mid 150.000
    eng.on_fill(Fill(T0 + NS, "S1", 103, 0, 0, 10, 150_000))
    eng.on_fill(Fill(T0 + NS, "S1", 103, 0, 1, 10, 149_985))
    # realized = -0.015 * 10 * 1000 = -150 JPY = -1.00 USD
    assert abs(eng.strategy_pnl("S1") + 1.0) < 1e-9, eng.strategy_pnl("S1")
    assert eng.snapshot()["realized"][0]["ccy"] == "JPY"
    # a 7.5M JPY loss = 50,000 USD -> strategy latch (limit 50,000)
    eng.on_fill(Fill(T0 + 2 * NS, "S2", 103, 0, 0, 5_000, 150_000))
    eng.on_fill(Fill(T0 + 2 * NS, "S2", 103, 0, 1, 5_000, 148_500))
    assert any(e.rule_id == Rules.STRATEGY_LOSS and e.scope_id == "S2" for e in eng.audit())


def test_missing_rate_makes_loss_checks_undeterminable(config_doc):
    """A P&L bucket whose conversion rate is missing: no latch on the
    fill/mark, the strategy's and the firm's daily P&L are ``None`` and
    every order rejects with FX_RATE_MISSING at check 21/22 (fail-closed);
    the gauges fall back to the convertible realized figure."""
    eng = RiskEngine.from_config(config_doc, fx_refs())
    eng.on_market(1, 2450, 2452, T0)
    eng.on_market(108, 85_315, 85_325, T0)
    eng.on_fill(Fill(T0 + NS, "S2", 108, 0, 0, 500, 85_320))
    eng.on_fill(Fill(T0 + NS, "S2", 108, 0, 1, 500, 75_320))  # -50,000 GBP: unconvertible
    assert kills(eng) == []
    assert eng.strategy_daily_pnl("S2") is None
    assert eng.global_daily_pnl() is None
    assert eng.realized_pnl() == 0.0  # missing rates contribute 0 to the gauge
    assert eng.metrics.gauge_value("risk_daily_pnl") == 0.0
    d = eng.check_order(order(1, 0, 100, 2450))
    assert d.rule_id == Rules.FX_RATE_MISSING
    assert d.reason == "global daily pnl undeterminable: conversion rate missing"
    # the pair arrives: -50,000 GBP at 1.2734 is -63,670 USD, past the
    # strategy limit — but a mark re-checks holders only and S2 is flat, so
    # nothing latches; S2 is rejected at check 22 while S1 trades
    eng.on_market(102, 127_335, 127_345, T0 + 2 * NS)
    assert kills(eng) == []
    d = eng.check_order(order(2, 0, 100, 2450, strategy_id="S2", timestamp=T0 + 2 * NS))
    assert d.rule_id == Rules.STRATEGY_LOSS
    assert d.reason == "strategy daily pnl -63670.00 at loss limit 50000.00"
    assert eng.check_order(order(3, 0, 100, 2450, timestamp=T0 + 2 * NS)).allowed()


def test_strategy_loss_reject_is_only_for_pnl_at_or_below_limit(config_doc):
    doc = config(config_doc)
    doc["per_strategy"]["max_daily_loss"] = 12_733.0
    eng = RiskEngine.from_config(doc, fx_refs())
    eng.on_market(1, 2450, 2452, T0)
    eng.on_market(108, 85_315, 85_325, T0)
    eng.on_market(102, 127_335, 127_345, T0)
    eng.on_fill(Fill(T0 + NS, "S2", 108, 0, 0, 100, 85_320))
    eng.on_fill(Fill(T0 + NS, "S2", 108, 0, 1, 100, 75_320))  # -10,000 GBP = -12,734 USD
    assert kills(eng) == [Rules.STRATEGY_LOSS]
    assert eng.audit()[-1].reason == "strategy daily pnl -12734.00 breaches loss limit 12733.00"
    # the latch is on the fill, converted at the prevailing pair mid
    eng2 = RiskEngine.from_config(config_doc, fx_refs())
    eng2.on_market(1, 2450, 2452, T0)
    eng2.on_market(102, 127_335, 127_345, T0)
    eng2.on_fill(Fill(T0 + NS, "S2", 108, 0, 0, 100, 85_320))
    eng2.on_fill(Fill(T0 + NS, "S2", 108, 0, 1, 100, 75_320))
    assert kills(eng2) == []  # -12,734 USD is above the 50,000 limit
    assert abs(eng2.strategy_daily_pnl("S2") + 12_734.0) < 1e-6


def test_risk_inflight_market_orders_count_in_projection(config_doc):
    """Scenario (Knight-style): 90k long, a burst of MARKET buys during a
    latency spike. In-flight MARKET orders count in the projection."""
    eng = engine(config_doc)
    eng.on_market(1, 1999, 2001, T0)  # mid 20.00: the 2.4M notional cap sits above 100k shares
    eng.on_fill(fill("S1", 1, 0, 90_000, 2000))
    t = T0 + 100_000_000
    assert eng.check_order(typed(1, 1, 0, 5_000, 0, OrderType.MARKET, t)).allowed()
    assert eng.check_order(typed(2, 1, 0, 5_000, 0, OrderType.MARKET, t + 10_000_000)).allowed()
    d = eng.check_order(typed(3, 1, 0, 5_000, 0, OrderType.MARKET, t + 20_000_000))
    assert d.rule_id == Rules.POSITION_LIMIT, d.reason
    assert "105000" in d.reason
    assert eng.open_order_count() == 2
    # IOC / FOK / MID are tracked the same way
    assert eng.check_order(typed(4, 1, 0, 5_000, 0, OrderType.IOC, t + 30_000_000)).rule_id \
        == Rules.POSITION_LIMIT
    assert eng.check_order(typed(6, 1, 0, 5_000, 0, OrderType.FOK, t + 40_000_000)).rule_id \
        == Rules.POSITION_LIMIT
    assert eng.check_order(typed(7, 1, 0, 5_000, 0, OrderType.MID, t + 50_000_000)).rule_id \
        == Rules.POSITION_LIMIT
    # fill of the first (order_id 1) releases it; the second is done at the venue
    eng.on_fill(Fill(t + 60_000_000, "S1", 1, 1, 0, 5_000, 2000))
    assert eng.open_order_count() == 1
    eng.on_order_done(2)
    assert eng.open_order_count() == 0
    assert eng.check_order(typed(5, 1, 0, 5_000, 0, OrderType.MARKET, t + 70_000_000)).allowed()


def test_risk_peg_orders_tracked_for_self_match_and_projection(config_doc):
    """PEG orders rest at the venue: they are tracked at their pegged touch
    for self-match and count in the position projection."""
    eng = engine(config_doc)
    eng.on_market(1, 1999, 2001, T0)  # mid 20.00
    t = T0 + 100_000_000
    assert eng.check_order(typed(1, 1, 0, 100, 0, OrderType.PEG, t)).allowed()
    assert eng.snapshot()["open"]["1"]["price_ticks"] == 1999
    # a MARKET sell would cross the own pegged bid
    d = eng.check_order(typed(2, 1, 1, 50, 0, OrderType.MARKET, t + 10_000_000))
    assert d.rule_id == Rules.SELF_MATCH
    assert "at 1999" in d.reason, f"pegged at the bid: {d.reason}"
    # a limit sell above the pegged bid is fine
    assert eng.check_order(typed(3, 1, 1, 50, 2005, OrderType.LIMIT, t + 20_000_000)).allowed()
    assert eng.snapshot()["open"]["3"]["price_ticks"] == 2005
    # PEG qty counts in open_same: 99,900 + 100 pegged + 100 = 100,100
    eng.on_fill(fill("S1", 1, 0, 99_900, 2000))
    assert eng.check_order(typed(4, 1, 0, 100, 1999, OrderType.LIMIT, t + 30_000_000)).rule_id \
        == Rules.POSITION_LIMIT
    eng.on_order_done(1)
    assert eng.check_order(typed(5, 1, 0, 100, 1999, OrderType.LIMIT, t + 40_000_000)).allowed()
    # a PEG sell pegs to the ask; a PEG in an unmarked instrument is unpriced
    assert eng.check_order(typed(8, 2, 1, 10, 0, OrderType.PEG, t + 50_000_000)).allowed()
    assert eng.snapshot()["open"]["8"]["price_ticks"] == 3121


def test_risk_gross_includes_resting_orders(config_doc):
    """Scenario: resting LIMIT buys across instruments. Gross must include
    every open order: 40 x 120k of resting orders leaves no room for a 41st."""
    refs = {iid: InstrumentRef.equity(0.01) for iid in range(1, 42)}
    eng = RiskEngine.from_config(config_doc, refs)
    for iid in range(1, 42):
        eng.on_market(iid, 2999, 3001, T0)  # mid 30.00
    t = T0 + 100_000_000
    # 4,000 @ 30.00 = 120,000 per resting order; 40 of them = 4.8M gross
    # (alternating sides so net stays flat)
    for i in range(40):
        side = 0 if i % 2 == 0 else 1
        o = typed(i + 1, i + 1, side, 4_000, 3000, OrderType.LIMIT, t + i * 10_000_000,
                  strategy=f"S{i % 4}")
        assert eng.check_order(o).allowed(), f"resting order {i}"
    assert eng.open_order_count() == 40
    # 41st: 4.8M open + 240k = 5.04M > 5M gross while no position exists
    d = eng.check_order(typed(41, 41, 0, 8_000, 3000, OrderType.LIMIT, t + 400_000_000,
                              strategy="S0"))
    assert d.rule_id == Rules.GROSS_NOTIONAL, d.reason
    assert d.reason == "projected gross notional 5040000.00 exceeds max_gross_notional 5000000.00"
    # cancelling one frees the room
    eng.on_order_done(1)
    assert eng.check_order(typed(42, 41, 0, 3_000, 3000, OrderType.LIMIT, t + 410_000_000,
                                 strategy="S0")).allowed()


def test_risk_clear_kill_after_loss_latch_resumes_with_override(config_doc):
    """Scenario: 11:40 latch, root cause fixed, CRO approves a resumed
    session with a raised limit. clear_kill alone still rejects and the
    next mark re-latches; only an approved override plus a clear lets
    orders flow."""
    eng = engine(config_doc)
    eng.on_fill(fill("S1", 1, 0, 95_000, 2452))
    eng.on_fill(fill("S1", 1, 1, 94_900, 2398))  # realized -51,246 -> S1 latched; 100 left
    assert eng.check_order(order(1, 0, 100, 2450)).rule_id == Rules.KILL_STRATEGY
    eng.clear_kill(Scope.STRATEGY, "S1", T0 + 2 * NS, "ops clear")
    d = eng.check_order(order(2, 0, 100, 2450))
    assert d.rule_id == Rules.STRATEGY_LOSS, f"belt-and-braces: {d.reason}"
    assert d.decision == Decision.REJECT
    assert d.severity == Severity.BREACH
    assert d.reason == "strategy daily pnl -51247.00 at loss limit 50000.00"
    # the next mark of a held instrument re-latches (no fill needed)
    eng.on_market(1, 2450, 2452, T0 + 3 * NS)
    assert eng.check_order(order(3, 0, 100, 2450)).rule_id == Rules.KILL_STRATEGY
    n_latches = sum(1 for e in eng.audit() if e.rule_id == Rules.STRATEGY_LOSS
                    and e.decision == int(Decision.KILL))
    assert n_latches == 2
    # override below the loss is legal but ineffective
    eng.override_loss_limit(Scope.STRATEGY, "S1", 51_000.0, T0 + 4 * NS, "cro")
    eng.clear_kill(Scope.STRATEGY, "S1", T0 + 4 * NS, "ops clear #2")
    assert eng.check_order(order(4, 0, 100, 2450)).rule_id == Rules.STRATEGY_LOSS
    # a flat strategy's realized loss cannot re-latch on a mark (no lot to
    # mark) but stays rejected pre-trade: the latch basis is the same P&L
    eng.on_fill(fill("S1", 1, 1, 100, 2450))  # flat; realized -51,448 -> re-latch on the fill
    assert eng.check_order(order(6, 0, 100, 2450)).rule_id == Rules.KILL_STRATEGY
    eng.clear_kill(Scope.STRATEGY, "S1", T0 + 4 * NS + 1, "ops clear #3")
    eng.on_market(1, 2450, 2452, T0 + 4 * NS + 2)
    assert eng.check_order(order(7, 0, 100, 2450)).rule_id == Rules.STRATEGY_LOSS
    # override above the loss + clear -> ALLOW, and marks no longer latch
    eng.override_loss_limit(Scope.STRATEGY, "S1", 75_000.0, T0 + 5 * NS, "cro")
    eng.clear_kill(Scope.STRATEGY, "S1", T0 + 5 * NS, "ops clear INC-1")
    eng.on_market(1, 2450, 2452, T0 + 5 * NS)
    assert eng.check_order(order(5, 0, 100, 2450)).allowed()
    ids = [e.rule_id for e in eng.audit()]
    assert ids.count(Rules.LOSS_LIMIT_OVERRIDE) == 2
    assert ids.count(Rules.KILL_SWITCH_CLEARED) == 4
    ov = next(e for e in eng.audit() if e.rule_id == Rules.LOSS_LIMIT_OVERRIDE)
    assert ov.reason == "daily loss limit 50000.00 -> 51000.00 approved by cro"
    assert ov.severity == int(Severity.WARN) and ov.decision == int(Decision.ALLOW)
    ov2 = [e for e in eng.audit() if e.rule_id == Rules.LOSS_LIMIT_OVERRIDE][1]
    assert ov2.reason == "daily loss limit 51000.00 -> 75000.00 approved by cro"
    assert eng.snapshot()["loss_override_strategy"] == {"S1": 75000.0}
    # invalid overrides are refused without side effects
    before = eng.snapshot_json()
    for bad in ((Scope.STRATEGY, "S1", -1.0), (Scope.INSTRUMENT, "1", 10.0),
                (Scope.GLOBAL, "", float("nan")), (Scope.VENUE, "1", 10.0),
                (Scope.GLOBAL, "", float("inf")), (Scope.GLOBAL, "", 0.0),
                (Scope.GLOBAL, "", True)):
        with pytest.raises(ValueError):
            eng.override_loss_limit(bad[0], bad[1], bad[2], T0, "x")  # type: ignore[arg-type]
    assert eng.snapshot_json() == before
    assert ids == [e.rule_id for e in eng.audit()]


def test_global_override_replaces_the_effective_limit(config_doc):
    eng = engine(config_doc)
    eng.override_loss_limit(Scope.GLOBAL, "", 400_000, T0, "cro")  # an int is a number
    ov = eng.audit()[-1]
    assert ov.reason == "daily loss limit 250000.00 -> 400000.00 approved by cro"
    assert ov.scope == Scope.GLOBAL and ov.scope_id == ""
    assert eng.snapshot()["loss_override_global"] == 400000.0
    eng.on_fill(fill("S1", 1, 0, 95_000, 2452))
    eng.on_fill(fill("S1", 1, 1, 95_000, 2180))  # -258,400: below the default, above the override
    assert kills(eng) == [Rules.STRATEGY_LOSS]
    assert not eng.kill_switch_engaged()
    eng.override_loss_limit(Scope.GLOBAL, "", 200_000.0, T0 + NS, "cro")
    assert eng.audit()[-1].reason == "daily loss limit 400000.00 -> 200000.00 approved by cro"
    d = eng.check_order(order(1, 0, 100, 2450, strategy_id="S2"))
    assert d.rule_id == Rules.DAILY_LOSS
    assert d.reason == "global daily pnl -258400.00 at daily loss limit 200000.00"
    # ... and the next mark of a held instrument latches at the new limit
    eng.on_fill(fill("S2", 2, 0, 100, 3120, ts=T0 + 2 * NS))
    assert kills(eng) == [Rules.STRATEGY_LOSS, Rules.DAILY_LOSS]


def test_scenario_session_roll_rebases_daily_pnl_and_keeps_latches(config_doc):
    """Scenario: FX book rolls at 22:00 Sunday. roll_session zeroes
    realized P&L, re-bases marked lots, clears overrides and keeps kill
    switches."""
    eng = engine(config_doc)
    eng.on_fill(fill("S1", 1, 0, 95_000, 2452))  # unrealized -950 at 24.51
    eng.on_fill(fill("S2", 2, 0, 1_000, 3120))
    eng.on_fill(fill("S2", 2, 1, 1_000, 3110))  # realized -100
    eng.override_loss_limit(Scope.GLOBAL, "", 400_000.0, T0, "cro")
    eng.engage_kill(Scope.STRATEGY, "S2", T0, "manual")
    assert abs(eng.global_daily_pnl() + 1_050.0) < 1e-6
    eng.roll_session(T0 + NS, "roll")
    assert eng.global_daily_pnl() == 0.0
    assert eng.strategy_daily_pnl("S1") == 0.0
    assert eng.position(1) == 95_000, "positions survive the roll"
    snap = eng.snapshot()
    assert snap["realized"] == [] and snap["loss_override_global"] is None
    assert snap["lots"][0] == {"strategy_id": "S1", "instrument_id": 1, "pos": 95_000,
                               "avg_price": 24.51}
    assert eng.check_order(order(1, 0, 100, 2450, strategy_id="S2")).rule_id == Rules.KILL_STRATEGY
    # the override is gone: a 300k loss on the new day latches at 250k
    eng.on_market(1, 2099, 2101, T0 + 2 * NS)  # 95k * (21.00 - 24.51) = -333,450
    assert eng.kill_switch_engaged()
    rolled = next(e for e in eng.audit() if e.rule_id == Rules.SESSION_ROLLED)
    assert rolled.reason == "roll" and rolled.scope == Scope.GLOBAL
    # an unmarked lot keeps its cost through the roll
    eng2 = RiskEngine.from_config_ticks(config_doc, {**ticks(), 3: 0.01})
    eng2.on_fill(fill("S1", 3, 0, 10, 5000))
    eng2.roll_session(T0, "roll")
    assert eng2.snapshot()["lots"][0]["avg_price"] == 50.0


def test_risk_snapshot_restore_roundtrip(config_doc):
    """Scenario: mid-session restart. Snapshot after 30 mixed steps,
    restore into a fresh engine, the next steps produce identical
    decisions and audit lines to the unbroken run; a fresh engine that
    requires a bootstrap rejects everything until positions arrive."""

    def script(eng: RiskEngine, start: int, n: int) -> List[str]:
        lines = []
        for k in range(start, start + n):
            t = T0 + 100_000_000 + k * 20_000_000
            side = 1 if k % 3 == 0 else 0
            px = 0 if k % 5 == 0 else 2450 + (k % 4)
            iid = 1 + (k % 2)
            price = 3120 if k % 2 == 1 else px
            ot = OrderType.MARKET if px == 0 else OrderType.LIMIT
            o = typed(1000 + k, iid, side, 100 + k, price, ot, t, strategy=f"S{k % 3}")
            d = eng.check_order(o)
            lines.append(f"{o.order_id}:{d.rule_id}")
            if k % 4 == 0:
                eng.on_fill(Fill(t, o.strategy_id, o.instrument_id, o.order_id, side, 50,
                                 2451 if o.instrument_id == 1 else 3120))
            if k % 7 == 0:
                eng.on_order_done(o.order_id)
            if k % 9 == 0:
                eng.on_market(1, 2449 + (k % 3), 2452, t)
        return lines

    unbroken = engine(config_doc)
    first = script(unbroken, 0, 30)
    snap = unbroken.snapshot()
    assert snap["x-version"] == 1
    limits = RiskLimits.from_json(config_doc)
    restored = RiskEngine.restore(limits, equity_refs(ticks()), snap, T0 + 5 * NS)
    assert restored.audit_len() == 1
    assert restored.audit()[0].rule_id == Rules.STATE_RESTORED
    assert restored.audit()[0].reason.startswith("restored snapshot v1: ")
    a = script(unbroken, 30, 20)
    b = script(restored, 30, 20)
    assert a == b
    assert len(first) == 30
    full_lines = unbroken.audit_jsonl().splitlines()
    tail = restored.audit_jsonl().splitlines()[1:]
    assert full_lines[len(full_lines) - len(tail):] == tail
    assert restored.snapshot() == unbroken.snapshot()
    assert restored.snapshot_json() == unbroken.snapshot_json()
    # a malformed snapshot is refused as a whole
    bad = copy.deepcopy(snap)
    bad["x-version"] = 99
    with pytest.raises(ValueError, match="bad x-version"):
        RiskEngine.restore(limits, equity_refs(ticks()), bad, T0)
    bad = copy.deepcopy(snap)
    bad["lots"][0]["avg_price"] = "nan"
    with pytest.raises(ValueError, match="bad lots.avg_price"):
        RiskEngine.restore(limits, equity_refs(ticks()), bad, T0)
    # bootstrap gate: fail-closed until positions arrive
    fresh = engine(config_doc)
    fresh.require_bootstrap()
    assert not fresh.is_bootstrapped()
    d = fresh.check_order(order(1, 0, 100, 2450))
    assert d.rule_id == Rules.NOT_BOOTSTRAPPED
    assert d.severity == Severity.BREACH
    assert d.reason == "positions not bootstrapped (fail-closed)"
    assert fresh.snapshot()["bootstrapped"] is False
    bad_count = fresh.bootstrap_positions(
        [fill("S1", 1, 0, 95_000, 2452), fill("S1", 999, 0, 1, 1)], T0 + NS)
    assert bad_count == 1
    assert fresh.is_bootstrapped()
    assert fresh.position(1) == 95_000
    assert fresh.check_order(order(2, 0, 40_000, 2452)).rule_id == Rules.POSITION_LIMIT
    boot = next(e for e in fresh.audit() if e.rule_id == Rules.BOOTSTRAP_COMPLETE)
    assert boot.reason == "bootstrapped from 2 drop-copy fills (1 rejected)"
    # a restore of a not-bootstrapped snapshot keeps the gate closed
    gated = RiskEngine.restore(limits, equity_refs(ticks()), engine_snapshot_unbootstrapped(config_doc), T0)
    assert gated.check_order(order(1, 0, 100, 2450)).rule_id == Rules.NOT_BOOTSTRAPPED


def engine_snapshot_unbootstrapped(config_doc: dict) -> dict:
    eng = engine(config_doc)
    eng.require_bootstrap()
    return eng.snapshot()


def test_restore_is_strict_field_by_field(config_doc):
    eng = engine(config_doc)
    eng.check_order(order(1, 0, 100, 2450))
    eng.on_fill(fill("S1", 1, 0, 10, 2450, order_id=1))
    eng.engage_kill(Scope.INSTRUMENT, "2", T0, "x")
    eng.on_venue_disconnect(2, T0)
    eng.override_loss_limit(Scope.STRATEGY, "S1", 60_000.0, T0, "cro")
    snap = eng.snapshot()
    limits = RiskLimits.from_json(config_doc)
    refs = equity_refs(ticks())

    def refused(mutate, what: str) -> None:
        bad = copy.deepcopy(snap)
        mutate(bad)
        with pytest.raises(ValueError, match=f"risk snapshot: bad {what}"):
            RiskEngine.restore(limits, refs, bad, T0)

    refused(lambda s: s.pop("bootstrapped"), "bootstrapped")
    refused(lambda s: s.__setitem__("kill_global", 1), "kill_global")
    refused(lambda s: s.__setitem__("kill_strategies", []), "kill_strategies")
    refused(lambda s: s["kill_strategies"].__setitem__("S9", "yes"), "kill_strategies")
    refused(lambda s: s["kill_instruments"].__setitem__("x", True), "kill_instruments")
    refused(lambda s: s["kill_venues"].__setitem__("65536", True), "kill_venues")
    refused(lambda s: s["venues_down"].__setitem__("2", None), "venues_down")
    refused(lambda s: s["market"]["1"].__setitem__("bid_ticks", 1.5), "market.bid_ticks")
    refused(lambda s: s["market"]["1"].__setitem__("gaps", -1), "market.gaps")
    refused(lambda s: s["market"]["1"].__setitem__("gated", 0), "market.gated")
    refused(lambda s: s["market"].__setitem__("4294967296", s["market"]["1"]), "market")
    refused(lambda s: s.__setitem__("seen_orders", {}), "seen_orders")
    refused(lambda s: s["seen_orders"].append([1]), "seen_orders")
    refused(lambda s: s["seen_orders"].append([-1, 0]), "seen_orders")
    refused(lambda s: s["buckets"]["S1"].__setitem__("tokens", "3"), "buckets.tokens")
    refused(lambda s: s["buckets"]["S1"].__setitem__("primed", None), "buckets.primed")
    refused(lambda s: s["open"]["1"].__setitem__("side", 2), "open.side")
    refused(lambda s: s["open"]["1"].__setitem__("instrument_id", 1 << 32), "open.instrument_id")
    refused(lambda s: s["open"].__setitem__("-1", s["open"]["1"]), "open")
    refused(lambda s: s["open"]["1"].__setitem__("qty", "10"), "open.qty")
    refused(lambda s: s["positions"].__setitem__("1", 1 << 63), "positions")
    refused(lambda s: s["lots"][0].__setitem__("strategy_id", 1), "lots.strategy_id")
    refused(lambda s: s["lots"][0].__setitem__("instrument_id", -1), "lots.instrument_id")
    refused(lambda s: s["lots"][0].__setitem__("pos", 1.0), "lots.pos")
    refused(lambda s: s["lots"].append({}), "lots.strategy_id")
    refused(lambda s: s.__setitem__("realized", None), "realized")
    refused(lambda s: s["realized"][0].__setitem__("pnl", float("inf")), "realized.pnl")
    refused(lambda s: s["realized"][0].__setitem__("ccy", None), "realized.ccy")
    refused(lambda s: s.__setitem__("loss_override_global", "x"), "loss_override_global")
    refused(lambda s: s["loss_override_strategy"].__setitem__("S1", True), "loss_override_strategy")
    refused(lambda s: s.__setitem__("x-version", 1.0), "x-version")
    with pytest.raises(ValueError, match="bad x-version"):
        RiskEngine.restore(limits, refs, [], T0)
    # the good snapshot restores everything, including the gauges
    restored = RiskEngine.restore(limits, refs, snap, T0 + 1)
    assert restored.snapshot() == snap
    assert restored.metrics.gauge_value("risk_daily_pnl") == eng.metrics.gauge_value("risk_daily_pnl")
    assert restored.audit()[0].reason == "restored snapshot v1: 1 positions, 1 open orders"
    # a restored snapshot with a latched global switch stays latched
    eng.engage_kill(Scope.GLOBAL, "", T0, "x")
    latched = RiskEngine.restore(limits, refs, eng.snapshot(), T0)
    assert latched.kill_switch_engaged()
    assert latched.metrics.gauge_value("risk_kill_switch_engaged") == 1.0
    assert latched.check_order(order(9, 0, 100, 2450)).rule_id == Rules.KILL_GLOBAL


def test_malformed_fills_are_audited_and_dropped(config_doc):
    eng = engine(config_doc)
    assert not eng.on_fill(Fill(T0 + NS, "S1", 1, 5, 0, 0, 2452))
    assert not eng.on_fill(Fill(T0 + NS, "S1", 1, 0, 2, 100, 2452))
    assert not eng.on_fill(Fill(T0 + NS, "S1", 1, 0, 0, 100, 0))
    assert not eng.on_fill(fill("S1", 999, 0, 100, 2452))
    assert eng.position(1) == 0
    assert eng.metrics.counter_value("risk_malformed_fills_total") == 4
    bad = [e for e in eng.audit() if e.rule_id == Rules.MALFORMED_FILL]
    assert [e.reason for e in bad] == [
        "fill for order 5 rejected: qty must be > 0: 0",
        "fill for order 0 rejected: side must be 0 or 1: 2",
        "fill for order 0 rejected: price_ticks must be > 0: 0",
        "fill for order 0 rejected: no reference data for instrument 999",
    ]
    assert all(e.scope == Scope.STRATEGY and e.scope_id == "S1"
               and e.severity == int(Severity.WARN) and e.decision == int(Decision.REJECT)
               for e in bad)
    assert eng.on_fill(fill("S1", 1, 0, 100, 2452))
    assert eng.position(1) == 100
    # a malformed fill through the bootstrap path is counted as rejected
    with pytest.raises(ValueError):
        eng.on_fill("not a fill")  # type: ignore[arg-type]


def test_duplicate_window_expires_and_prunes(config_doc):
    doc = config(config_doc)
    doc["per_order"]["duplicate_order_window_ns"] = NS
    eng = RiskEngine.from_config_ticks(doc, ticks())
    eng.on_market(1, 2450, 2452, T0)
    assert eng.check_order(order(7, 0, 100, 2450, timestamp=T0)).allowed()
    # inside the window (<=)
    assert eng.check_order(order(7, 0, 100, 2450, timestamp=T0 + NS)).rule_id \
        == Rules.DUPLICATE_ORDER_ID
    # expired
    assert eng.check_order(order(7, 0, 100, 2450, timestamp=T0 + NS + 1)).allowed()
    snap = eng.snapshot()
    assert len(snap["seen_orders"]) == 1, "pruned to the window"
    assert snap["seen_orders"] == [[7, T0 + NS + 1]]


# ------------------------------------------------------- integer domain


def test_i64_overflow_raises_like_rust_checked_arithmetic(config_doc):
    """Where the Rust engine's overflow-checked i64 arithmetic would panic
    (a mark whose bid + ask leaves i64, a position pushed past i64), the
    port raises ``OverflowError`` — never a wrapped or bigint result."""
    i64_max = (1 << 63) - 1
    eng = engine(config_doc)
    eng.on_market(1, i64_max, i64_max, T0 + 1)
    with pytest.raises(OverflowError):
        eng.check_order(order(1, 0, 100, 2450, timestamp=T0 + 2))
    with pytest.raises(OverflowError):
        eng.on_fill(fill("S1", 1, 0, 1, 1))  # the mark of the new lot overflows
    eng2 = engine(config_doc)
    eng2.on_fill(fill("S1", 1, 0, i64_max, 2452))
    with pytest.raises(OverflowError):
        eng2.on_fill(fill("S1", 1, 0, 1, 2452))
    # timestamps: an age computation that leaves i64 raises too
    eng3 = RiskEngine.from_config_ticks(config_doc, ticks())
    eng3.on_market(1, 2450, 2452, -(1 << 63))
    with pytest.raises(OverflowError):
        eng3.check_order(order(1, 0, 100, 2450, timestamp=1))
    # and typed arguments are domain-checked at the boundary
    with pytest.raises(ValueError):
        eng.on_market(1 << 32, 1, 1, 0)
    with pytest.raises(ValueError):
        eng.on_venue_disconnect(1 << 16, 0)
    with pytest.raises(ValueError):
        eng.on_order_done(-1)
    with pytest.raises(ValueError):
        eng.engage_kill("GLOBAL", "", 0, "x")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        RiskEngine(config_doc, {})  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        RiskEngine(RiskLimits.from_json(config_doc), {1: 0.01})  # type: ignore[dict-item]


# ------------------------------------------------------- reference data


def test_instrument_refs_from_config_and_reference_data(golden_dir):
    root = golden_dir.parents[1]
    with open(root / "configs" / "instruments" / "instruments.json") as f:
        doc = json.load(f)
    refs = instrument_refs_from_config(doc)
    assert refs[1] == InstrumentRef(0.01, 1.0, "USD")
    assert refs[103] == InstrumentRef(0.001, 1000.0, "JPY")
    assert refs[101].quote_ccy == "USD" and refs[101].qty_unit == 1000.0
    assert list(refs) == sorted(refs)
    assert load_instrument_refs(root / "configs") == refs
    assert instrument_refs_from_reference_data(ReferenceData.load(root / "configs")) == refs
    # the engine built from the real universe prices an FX order in USD
    with open(root / "configs" / "risk" / "risk.json") as f:
        eng = RiskEngine.from_config(json.load(f), refs)
    eng.on_market(103, 147_515, 147_525, T0)
    d = eng.check_order(typed(1, 103, 1, 1001, 0, OrderType.MARKET, T0 + 1))
    assert d.rule_id == Rules.FAT_FINGER_NOTIONAL
    assert d.reason.startswith("notional 1001000.00 USD")
    # fail-closed derivation
    for mutate, msg in (
        (lambda d: d["instruments"][0].pop("currency"), "missing currency for 1"),
        (lambda d: d["instruments"][0].__setitem__("lot_size", 0), "lot_size must be > 0"),
        (lambda d: d["instruments"][0].__setitem__("asset_class", "BOND"), "unknown asset_class"),
        (lambda d: d["instruments"][0].pop("adv"), "instrument missing adv"),
        (lambda d: d["instruments"].append(d["instruments"][0]), "duplicate instrument_id"),
        (lambda d: d.__setitem__("instruments", []), "empty universe"),
        (lambda d: d.pop("instruments"), "missing instruments"),
    ):
        bad = copy.deepcopy(doc)
        mutate(bad)
        with pytest.raises(ValueError, match=msg):
            instrument_refs_from_config(bad)
    with pytest.raises(ValueError):
        InstrumentRef(0.0, 1.0, "USD")
    with pytest.raises(ValueError):
        InstrumentRef(0.01, 1.0, "")
    with pytest.raises(ValueError):
        InstrumentRef(0.01, float("nan"), "USD")


# ------------------------------------------------------- property tests


def _exposure_state(eng: RiskEngine) -> dict:
    """Everything a REJECT must leave untouched: the snapshot minus the
    two bookkeeping maps a rejected order legitimately touches (the seen
    id set at check 7 and the token bucket at check 15 — both pinned)."""
    snap = eng.snapshot()
    del snap["seen_orders"]
    del snap["buckets"]
    return snap


def _random_script(seed: int, n: int) -> List[dict]:
    rng = SplitMix64(seed)
    steps: List[dict] = []
    t = T0 + 100_000_000
    for k in range(n):
        t += rng.randint(0, 4_000_000)
        r = rng.below(100)
        if r < 55:
            iid = 1 + rng.below(2)
            side = rng.below(2)
            ot = [OrderType.LIMIT, OrderType.LIMIT, OrderType.MARKET, OrderType.PEG,
                  OrderType.IOC][rng.below(5)]
            base = 2451 if iid == 1 else 3120
            px = 0 if ot in (OrderType.MARKET, OrderType.PEG) else base + rng.randint(-60, 60)
            if ot == OrderType.IOC and rng.below(2):
                px = 0
            qty = [10, 100, 1_000, 5_000, 20_000, 60_000][rng.below(6)]
            oid = 1 + rng.below(40) if rng.below(10) == 0 else 1000 + k
            steps.append({"type": "order", "order": typed(
                oid, iid, side, qty, px, ot, t, strategy=f"S{rng.below(3)}",
                venue=rng.below(3))})
        elif r < 75:
            iid = 1 + rng.below(2)
            base = 2451 if iid == 1 else 3120
            steps.append({"type": "fill", "fill": Fill(
                t, f"S{rng.below(3)}", iid, 1000 + rng.below(max(k, 1)) if rng.below(2) else 0,
                rng.below(2), [10, 100, 1_000, 5_000][rng.below(4)],
                base + rng.randint(-300, 300))})
        elif r < 88:
            iid = 1 + rng.below(2)
            base = 2451 if iid == 1 else 3120
            move = rng.randint(-200, 200)
            steps.append({"type": "market", "iid": iid, "bid": base + move - 1,
                          "ask": base + move + 1, "ts": t - rng.below(3) * NS})
        elif r < 93:
            steps.append({"type": "done", "oid": 1000 + rng.below(max(k, 1))})
        elif r < 96:
            steps.append({"type": "gap" if rng.below(2) else "recover", "iid": 1 + rng.below(2), "ts": t})
        elif r < 98:
            steps.append({"type": "venue_down" if rng.below(2) else "venue_up",
                          "vid": 1 + rng.below(2), "ts": t})
        else:
            steps.append({"type": "unkill", "scope": [Scope.GLOBAL, Scope.STRATEGY][rng.below(2)],
                          "id": "" if rng.below(2) else "S1", "ts": t})
    return steps


def _apply(eng: RiskEngine, step: dict):
    kind = step["type"]
    if kind == "order":
        return eng.check_order(step["order"])
    if kind == "fill":
        return eng.on_fill(step["fill"])
    if kind == "market":
        return eng.on_market(step["iid"], step["bid"], step["ask"], step["ts"])
    if kind == "done":
        return eng.on_order_done(step["oid"])
    if kind == "gap":
        return eng.on_sequence_gap(step["iid"], step["ts"])
    if kind == "recover":
        return eng.on_feed_recovered(step["iid"], step["ts"])
    if kind == "venue_down":
        return eng.on_venue_disconnect(step["vid"], step["ts"])
    if kind == "venue_up":
        return eng.on_venue_reconnect(step["vid"], step["ts"])
    return eng.clear_kill(step["scope"], step["id"], step["ts"], "ops")


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_property_reject_never_changes_exposure_state(config_doc, seed):
    """A REJECT never changes positions, lots, realized P&L, open orders,
    marks, kill switches, gates, venues or overrides, and appends exactly
    one audit line; an ALLOW changes only the open-order set (plus the
    bookkeeping maps). Every rule id is exercised by the random script."""
    eng = engine(config_doc)
    seen_rules = set()
    for step in _random_script(seed, 600):
        if step["type"] != "order":
            _apply(eng, step)
            continue
        before = _exposure_state(eng)
        n_audit = eng.audit_len()
        d = eng.check_order(step["order"])
        seen_rules.add(d.rule_id)
        after = _exposure_state(eng)
        assert eng.audit_len() == n_audit + 1
        last = eng.audit()[-1]
        assert last.rule_id == d.rule_id and last.reason == d.reason
        assert last.decision == int(d.decision) and last.severity == int(d.severity)
        if d.decision == Decision.REJECT:
            assert after == before, d
            assert last.severity in (int(Severity.WARN), int(Severity.BREACH))
        else:
            assert d.rule_id == Rules.ALLOW and d.reason == "" and d.severity == Severity.INFO
            before["open"][str(step["order"].order_id)] = after["open"][str(step["order"].order_id)]
            assert after == before
    assert len(seen_rules) >= 8, seen_rules
    assert Rules.ALLOW in seen_rules


@pytest.mark.parametrize("seed", [11, 12, 13])
def test_property_applied_decisions_are_replayable(config_doc, seed):
    """Two fresh engines fed the same script produce byte-identical audit
    logs and snapshots, and an engine restored from the snapshot taken
    at any cut reproduces the remaining decisions, audit tail and final
    state bit for bit."""
    script = _random_script(seed, 400)
    a = engine(config_doc)
    b = engine(config_doc)
    snaps = []
    for i, step in enumerate(script):
        da = _apply(a, step)
        db = _apply(b, step)
        assert da == db
        if i % 37 == 0:
            snaps.append((i, a.snapshot_json()))
    assert a.audit_jsonl() == b.audit_jsonl()
    assert a.snapshot_json() == b.snapshot_json()
    full = a.audit_jsonl().splitlines()
    limits = RiskLimits.from_json(config_doc)
    for cut, snap_json in snaps:
        restored = RiskEngine.restore(limits, equity_refs(ticks()), json.loads(snap_json), 0)
        for step in script[cut + 1:]:
            _apply(restored, step)
        tail = restored.audit_jsonl().splitlines()[1:]
        assert tail == full[len(full) - len(tail):], f"cut {cut}"
        assert restored.snapshot_json() == a.snapshot_json(), f"cut {cut}"
        # the snapshot document itself round-trips through canonical JSON
        assert json.loads(snap_json) == restored_snapshot_parse(snap_json)


def restored_snapshot_parse(text: str) -> dict:
    doc = json.loads(text)
    assert to_canonical_json(doc, pretty=True) == text
    return doc


def test_snapshot_is_json_shaped_and_independent_of_the_engine(config_doc):
    eng = engine(config_doc)
    eng.check_order(order(1, 0, 100, 2450))
    snap = eng.snapshot()
    snap["open"]["1"]["qty"] = 999
    assert eng.snapshot()["open"]["1"]["qty"] == 100, "snapshot is a copy"
    assert json.loads(eng.snapshot_json()) == eng.snapshot()
    assert set(eng.snapshot()) == {
        "x-version", "bootstrapped", "kill_global", "kill_strategies", "kill_instruments",
        "kill_venues", "venues_down", "market", "seen_orders", "buckets", "open",
        "positions", "lots", "realized", "loss_override_global", "loss_override_strategy"}
    assert not math.isnan(eng.metrics.gauge_value("risk_daily_pnl"))


# ------------------------------------------- remaining fail-closed branches


def test_positions_and_open_orders_without_a_rate_fail_the_gross_check(config_doc):
    eng = RiskEngine.from_config(config_doc, fx_refs())
    eng.on_market(1, 2450, 2452, T0)
    eng.on_market(108, 85_315, 85_325, T0)
    # a GBP position with no GBP/USD mark: the USD order fails closed
    eng.on_fill(Fill(T0 + 1, "S1", 108, 0, 0, 10, 85_320))
    d = eng.check_order(order(1, 0, 100, 2450))
    assert d.rule_id == Rules.GROSS_NOTIONAL
    assert d.reason == "position in instrument 108 has no GBP conversion rate (fail-closed)"
    # the pair arrives, a GBP order rests, then the pair goes one-sided:
    # the open order cannot be converted either
    eng.on_market(102, 127_335, 127_345, T0 + 2)
    assert eng.check_order(typed(2, 108, 1, 10, 85_330, OrderType.LIMIT, T0 + 3)).allowed()
    eng.on_fill(Fill(T0 + 4, "S1", 108, 0, 1, 10, 85_320))  # flat again
    eng.on_market(102, 0, 127_345, T0 + 5)
    d = eng.check_order(order(3, 0, 100, 2450, timestamp=T0 + 6))
    assert d.rule_id == Rules.GROSS_NOTIONAL
    assert d.reason == "open order in instrument 108 has no GBP conversion rate (fail-closed)"


def test_fx_rate_needs_reference_data_and_a_conversion_entry(config_doc):
    # the conversion pair (102) has market data but no reference data
    refs = fx_refs()
    del refs[102]
    eng = RiskEngine.from_config(config_doc, refs)
    eng.on_market(108, 85_315, 85_325, T0)
    eng.on_market(102, 127_335, 127_345, T0)
    d = eng.check_order(typed(1, 108, 0, 10, 0, OrderType.MARKET, T0 + 1))
    assert d.rule_id == Rules.FX_RATE_MISSING
    assert d.reason == "no conversion rate for GBP -> USD"
    # a quote currency without a currency.conversion entry
    refs = fx_refs()
    refs[109] = InstrumentRef(1e-5, 1000.0, "XXX")
    eng = RiskEngine.from_config(config_doc, refs)
    eng.on_market(109, 100_000, 100_010, T0)
    d = eng.check_order(typed(1, 109, 0, 10, 0, OrderType.MARKET, T0 + 1))
    assert d.reason == "no conversion rate for XXX -> USD"
    # a fail-closed engine holds nothing: its P&L is a determinate 0
    closed = RiskEngine.fail_closed("boom")
    assert closed.global_daily_pnl() == 0.0
    assert closed.realized_pnl() == 0.0
    d = closed.check_order(order(1, 0, 100, 2450))
    assert d.reason == "fail-closed: boom"


def test_with_ticks_and_argument_type_checks(config_doc):
    limits = RiskLimits.from_json(config_doc)
    eng = RiskEngine.with_ticks(limits, {2: 0.01, 1: 0.01})
    eng.on_market(1, 2450, 2452, T0)
    assert eng.check_order(order(1, 0, 100, 2450)).allowed()
    with pytest.raises(ValueError):
        eng.check_order({"order_id": 1})  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        eng.engage_kill(Scope.STRATEGY, 7, T0, "x")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        eng.on_market("1", 1, 1, 0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        RiskEvent(1 << 63, Scope.GLOBAL, "", "X", 1, 1, "")


def test_restore_tolerates_unknown_instruments_fail_closed(config_doc):
    """A snapshot may reference instruments the new reference data lacks
    (restore is not the place to lose state): an unknown-instrument
    position fails the gross check (no mark), an unknown-instrument open
    order is skipped in gross/net but still counts in projections."""
    eng = engine(config_doc)
    eng.engage_kill(Scope.VENUE, "3", T0, "x")
    snap = eng.snapshot()
    snap["open"]["5"] = {"instrument_id": 99, "side": 0, "price_ticks": 0, "qty": 10}
    limits = RiskLimits.from_json(config_doc)
    restored = RiskEngine.restore(limits, equity_refs(ticks()), snap, T0)
    assert restored.snapshot()["kill_venues"] == {"3": True}
    assert restored.check_order(order(1, 0, 100, 2450)).allowed()
    snap["positions"]["99"] = 10
    restored = RiskEngine.restore(limits, equity_refs(ticks()), snap, T0)
    d = restored.check_order(order(1, 0, 100, 2450))
    assert d.rule_id == Rules.GROSS_NOTIONAL
    assert d.reason == "position in instrument 99 has no mark price (fail-closed)"
    bad = copy.deepcopy(snap)
    bad["lots"] = [{"strategy_id": "S1", "instrument_id": 1 << 32, "pos": 1, "avg_price": 1.0}]
    with pytest.raises(ValueError, match="bad lots.instrument_id"):
        RiskEngine.restore(limits, equity_refs(ticks()), bad, T0)


def test_limits_reject_non_finite_and_malformed_conversion_tables(config_doc):
    doc = config(config_doc)
    doc["global"]["max_gross_notional"] = float("inf")
    with pytest.raises(ValueError, match="non-finite global.max_gross_notional"):
        RiskLimits.from_json(doc)
    doc = config(config_doc)
    doc["per_order"]["max_order_qty"] = 0
    with pytest.raises(ValueError, match="per_order.max_order_qty must be > 0, got 0"):
        RiskLimits.from_json(doc)
    doc = config(config_doc)
    doc["currency"]["conversion"] = []
    with pytest.raises(ValueError, match="missing currency.conversion object"):
        RiskLimits.from_json(doc)
    doc = config(config_doc)
    doc["currency"]["conversion"]["JPY"] = 103
    with pytest.raises(ValueError, match="conversion.JPY.instrument_id missing/invalid"):
        RiskLimits.from_json(doc)


def test_reference_builders_reject_malformed_tables():
    with pytest.raises(ValueError, match="must be an object"):
        instrument_refs_from_golden({"1": 0.01})
    with pytest.raises(ValueError, match="must be a u32"):
        instrument_refs_from_golden({"x": {"tick_size": 0.01, "qty_unit": 1, "quote_ccy": "USD"}})
    with pytest.raises(ValueError, match="must be a u32"):
        instrument_refs_from_golden({1 << 32: {"tick_size": 0.01, "qty_unit": 1, "quote_ccy": "USD"}})
    with pytest.raises(ValueError, match="must be a number"):
        InstrumentRef("0.01", 1.0, "USD")  # type: ignore[arg-type]
    for row in ({"instrument_id": 0, "tick_size": 0.01, "lot_size": 1, "adv": 1, "asset_class": "EQUITY"},
                {"instrument_id": 1, "tick_size": 0, "lot_size": 1, "adv": 1, "asset_class": "EQUITY"}):
        with pytest.raises(ValueError, match="bad instrument_id/tick_size"):
            instrument_refs_from_config({"instruments": [row]})
    with pytest.raises(ValueError, match="must be an object"):
        instrument_refs_from_config({"instruments": [1]})
