//! Per-rule unit tests for the hard risk engine: every rule's allow AND
//! deny path, fail-closed behavior, kill-switch precedence and re-arm,
//! event-time throttling, self-match cases, mark-to-market loss latching,
//! FX notionals, open-order projections, snapshot/restore and audit
//! determinism. Scenario tests are named after the real-world sequence
//! they pin.

use std::collections::BTreeMap;

use risk::{rules, Decision, Fill, InstrumentRef, RiskEngine, RiskLimits, Scope, Severity};
use venue::{OrderRequest, OrderType};

const T0: i64 = 1_700_000_000_000_000_000;
const NS: i64 = 1_000_000_000;

fn config() -> serde_json::Value {
    let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../configs/risk.json");
    serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
}

fn ticks() -> BTreeMap<u32, f64> {
    let mut t = BTreeMap::new();
    t.insert(1u32, 0.01);
    t.insert(2u32, 0.01);
    t
}

/// Engine with fresh two-sided market data on instruments 1 and 2.
fn engine() -> RiskEngine {
    let mut eng = RiskEngine::from_config_ticks(&config(), ticks());
    eng.on_market(1, 2450, 2452, T0); // mid 24.51
    eng.on_market(2, 3119, 3121, T0); // mid 31.20
    eng
}

/// Instruments 1/2 (USD equities) plus USD/JPY (103, qty_unit 1000, JPY)
/// and EUR/GBP (108, GBP) with GBP/USD (102) as the GBP conversion pair.
fn fx_refs() -> BTreeMap<u32, InstrumentRef> {
    let mut m = BTreeMap::new();
    m.insert(1u32, InstrumentRef::equity(0.01));
    m.insert(2u32, InstrumentRef::equity(0.01));
    m.insert(102u32, InstrumentRef::new(1e-5, 1000.0, "USD"));
    m.insert(103u32, InstrumentRef::new(0.001, 1000.0, "JPY"));
    m.insert(108u32, InstrumentRef::new(1e-5, 1000.0, "GBP"));
    m
}

fn fx_engine() -> RiskEngine {
    let mut eng = RiskEngine::from_config(&config(), fx_refs());
    eng.on_market(1, 2450, 2452, T0);
    eng.on_market(103, 147_515, 147_525, T0); // mid 147.520 JPY
    eng
}

fn typed(id: u64, iid: u32, side: u8, qty: i64, price: i64, ot: OrderType, ts: i64) -> OrderRequest {
    OrderRequest {
        order_id: id,
        instrument_id: iid,
        side,
        qty,
        price_ticks: price,
        order_type: ot.as_u8(),
        venue_id: 1,
        strategy_id: "S1".to_string(),
        urgency: 0.5,
        timestamp: ts,
    }
}

fn order(id: u64, side: u8, qty: i64, price: i64) -> OrderRequest {
    OrderRequest {
        order_id: id,
        instrument_id: 1,
        side,
        qty,
        price_ticks: price,
        order_type: if price > 0 {
            OrderType::Limit.as_u8()
        } else {
            OrderType::Market.as_u8()
        },
        venue_id: 1,
        strategy_id: "S1".to_string(),
        urgency: 0.5,
        timestamp: T0 + 100_000_000,
    }
}

// ------------------------------------------------------------- fail-closed

#[test]
fn missing_limit_fails_closed() {
    let mut doc = config();
    doc["per_order"].as_object_mut().unwrap().remove("max_order_qty");
    let mut eng = RiskEngine::from_config_ticks(&doc, ticks());
    eng.on_market(1, 2450, 2452, T0);
    let d = eng.check_order(&order(1, 0, 100, 2450));
    assert_eq!(d.decision, Decision::Reject);
    assert_eq!(d.rule_id, rules::CONFIG_MISSING);
    assert_eq!(d.severity, Severity::Breach);
    // and it stays closed for every order
    let d = eng.check_order(&order(2, 1, 1, 2452));
    assert_eq!(d.rule_id, rules::CONFIG_MISSING);
}

#[test]
fn invalid_config_value_fails_closed() {
    let mut doc = config();
    doc["global"]["max_daily_loss"] = serde_json::json!(0.0);
    let mut eng = RiskEngine::from_config_ticks(&doc, ticks());
    let d = eng.check_order(&order(1, 0, 100, 2450));
    assert_eq!(d.rule_id, rules::CONFIG_MISSING);
}

#[test]
fn config_kill_switch_engaged_starts_killed() {
    let mut doc = config();
    doc["global"]["kill_switch_engaged"] = serde_json::json!(true);
    let mut eng = RiskEngine::from_config_ticks(&doc, ticks());
    eng.on_market(1, 2450, 2452, T0);
    let d = eng.check_order(&order(1, 0, 100, 2450));
    assert_eq!(d.rule_id, rules::KILL_GLOBAL);
}

// ------------------------------------------------------------ kill switches

#[test]
fn kill_switch_scopes_and_precedence() {
    let mut eng = engine();
    assert!(eng.check_order(&order(1, 0, 100, 2450)).allowed());

    eng.engage_kill(Scope::Venue, "1", T0, "test");
    let d = eng.check_order(&order(2, 0, 100, 2450));
    assert_eq!(d.rule_id, rules::KILL_VENUE);

    eng.engage_kill(Scope::Instrument, "1", T0, "test");
    let d = eng.check_order(&order(3, 0, 100, 2450));
    assert_eq!(d.rule_id, rules::KILL_INSTRUMENT); // instrument > venue

    eng.engage_kill(Scope::Strategy, "S1", T0, "test");
    let d = eng.check_order(&order(4, 0, 100, 2450));
    assert_eq!(d.rule_id, rules::KILL_STRATEGY); // strategy > instrument

    eng.engage_kill(Scope::Global, "", T0, "test");
    let d = eng.check_order(&order(5, 0, 100, 2450));
    assert_eq!(d.rule_id, rules::KILL_GLOBAL); // global beats everything
    assert_eq!(d.severity, Severity::Breach);

    // clearing restores, innermost first
    eng.clear_kill(Scope::Global, "", T0, "clear");
    let d = eng.check_order(&order(6, 0, 100, 2450));
    assert_eq!(d.rule_id, rules::KILL_STRATEGY);
    eng.clear_kill(Scope::Strategy, "S1", T0, "clear");
    eng.clear_kill(Scope::Instrument, "1", T0, "clear");
    eng.clear_kill(Scope::Venue, "1", T0, "clear");
    assert!(eng.check_order(&order(7, 0, 100, 2450)).allowed());
}

#[test]
fn strategy_kill_only_hits_that_strategy() {
    let mut eng = engine();
    eng.engage_kill(Scope::Strategy, "S1", T0, "test");
    let d = eng.check_order(&order(1, 0, 100, 2450));
    assert_eq!(d.rule_id, rules::KILL_STRATEGY);
    let mut o = order(2, 0, 100, 2450);
    o.strategy_id = "S2".to_string();
    assert!(eng.check_order(&o).allowed());
}

// ------------------------------------------------- malformed / reference

#[test]
fn malformed_orders_reject() {
    let mut eng = engine();
    let d = eng.check_order(&order(1, 0, 0, 2450)); // qty 0
    assert_eq!(d.rule_id, rules::MALFORMED_ORDER);
    let mut o = order(2, 0, 100, 2450);
    o.side = 3;
    assert_eq!(eng.check_order(&o).rule_id, rules::MALFORMED_ORDER);
    let mut o = order(3, 0, 100, 0);
    o.order_type = OrderType::Limit.as_u8(); // LIMIT without a price
    assert_eq!(eng.check_order(&o).rule_id, rules::MALFORMED_ORDER);
    let mut o = order(4, 0, 100, 2450);
    o.urgency = 2.0;
    assert_eq!(eng.check_order(&o).rule_id, rules::MALFORMED_ORDER);
}

#[test]
fn unknown_instrument_rejects() {
    let mut eng = engine();
    let mut o = order(1, 0, 100, 2450);
    o.instrument_id = 999;
    assert_eq!(eng.check_order(&o).rule_id, rules::UNKNOWN_INSTRUMENT);
}

#[test]
fn duplicate_order_id_rejects_even_after_reject() {
    let mut eng = engine();
    assert!(eng.check_order(&order(7, 0, 100, 2450)).allowed());
    let d = eng.check_order(&order(7, 1, 50, 2455));
    assert_eq!(d.rule_id, rules::DUPLICATE_ORDER_ID);
    // an id consumed by a rejected order is consumed too (pinned)
    assert_eq!(eng.check_order(&order(8, 0, 0, 2450)).rule_id, rules::MALFORMED_ORDER);
    // ... but only ids that reached the duplicate check are recorded:
    // order 8 failed schema validation BEFORE registration
    let d = eng.check_order(&order(8, 0, 100, 2450));
    assert!(d.allowed(), "id of a malformed order was not consumed");
    let d = eng.check_order(&order(8, 0, 100, 2450));
    assert_eq!(d.rule_id, rules::DUPLICATE_ORDER_ID);
}

// -------------------------------------------------------- market-data gates

#[test]
fn sequence_gap_gates_until_recovery() {
    let mut eng = engine();
    eng.on_sequence_gap(1, T0);
    let d = eng.check_order(&order(1, 0, 100, 2450));
    assert_eq!(d.rule_id, rules::SEQUENCE_GAP);
    // other instruments unaffected
    let mut o = order(2, 0, 100, 3120);
    o.instrument_id = 2;
    assert!(eng.check_order(&o).allowed());
    eng.on_feed_recovered(1, T0 + NS);
    assert!(eng.check_order(&order(3, 0, 100, 2450)).allowed());
}

#[test]
fn stale_price_rejects_and_recovers() {
    let mut eng = engine();
    // no market data at all for a fresh instrument id
    let mut t = ticks();
    t.insert(3u32, 0.01);
    let mut eng2 = RiskEngine::from_config_ticks(&config(), t);
    let mut o = order(1, 0, 100, 2450);
    o.instrument_id = 3;
    assert_eq!(eng2.check_order(&o).rule_id, rules::STALE_PRICE);
    // age beyond the 5s timeout
    let mut o = order(1, 0, 100, 2450);
    o.timestamp = T0 + 6 * NS;
    assert_eq!(eng.check_order(&o).rule_id, rules::STALE_PRICE);
    // refresh -> allowed
    eng.on_market(1, 2450, 2452, T0 + 6 * NS);
    let mut o = order(2, 0, 100, 2450);
    o.timestamp = T0 + 6 * NS + 1;
    assert!(eng.check_order(&o).allowed());
}

#[test]
fn venue_disconnect_rejects_until_reconnect() {
    let mut eng = engine();
    eng.on_venue_disconnect(1, T0);
    let d = eng.check_order(&order(1, 0, 100, 2450));
    assert_eq!(d.rule_id, rules::VENUE_DISCONNECTED);
    // a different venue still works
    let mut o = order(2, 0, 100, 2450);
    o.venue_id = 2;
    assert!(eng.check_order(&o).allowed());
    eng.on_venue_reconnect(1, T0 + NS);
    assert!(eng.check_order(&order(3, 0, 100, 2450)).allowed());
}

// ------------------------------------------------------- fat finger / band

#[test]
fn fat_finger_qty_boundary() {
    let mut eng = engine();
    assert!(eng.check_order(&order(1, 0, 40_000, 2452)).allowed()); // 981k < 1M
    let d = eng.check_order(&order(2, 0, 50_001, 2452));
    assert_eq!(d.rule_id, rules::FAT_FINGER_QTY);
}

#[test]
fn fat_finger_notional_uses_limit_price_or_mid() {
    let mut eng = engine();
    // 45000 * 24.60 = 1,107,000 > 1,000,000
    let d = eng.check_order(&order(1, 0, 45_000, 2460));
    assert_eq!(d.rule_id, rules::FAT_FINGER_NOTIONAL);
    // unpriced market order valued at the mid: 45000 * 24.51 = 1,102,950
    let d = eng.check_order(&order(2, 0, 45_000, 0));
    assert_eq!(d.rule_id, rules::FAT_FINGER_NOTIONAL);
    assert!(eng.check_order(&order(3, 0, 40_000, 0)).allowed());
}

#[test]
fn price_band_rejects_far_limits_only() {
    let mut eng = engine();
    // 30.00 vs mid 24.51 => ~2240bps > 200bps
    let d = eng.check_order(&order(1, 0, 30_000, 3000));
    assert_eq!(d.rule_id, rules::PRICE_BAND);
    // market orders carry no price -> no band check
    assert!(eng.check_order(&order(2, 0, 100, 0)).allowed());
    eng.on_order_done(2); // venue terminal report (an open MARKET buy would self-match)
    // just inside the band: 24.99 vs 24.51 = ~196bps
    assert!(eng.check_order(&order(3, 1, 100, 2499)).allowed());
}

// ---------------------------------------------------------------- throttle

#[test]
fn throttle_is_an_event_time_token_bucket() {
    let mut eng = engine();
    let base = T0 + 100_000_000;
    // burst 4: four same-timestamp orders pass, the fifth throttles
    for i in 0..4 {
        let mut o = order(10 + i, 0, 10, 0);
        o.timestamp = base;
        assert!(eng.check_order(&o).allowed(), "order {i} within burst");
    }
    let mut o = order(14, 0, 10, 0);
    o.timestamp = base;
    assert_eq!(eng.check_order(&o).rule_id, rules::RATE_THROTTLE);
    // 2ms of event time refills exactly one token at 500/s
    let mut o = order(15, 0, 10, 0);
    o.timestamp = base + 2_000_000;
    assert!(eng.check_order(&o).allowed());
    let mut o = order(16, 0, 10, 0);
    o.timestamp = base + 2_000_000;
    assert_eq!(eng.check_order(&o).rule_id, rules::RATE_THROTTLE);
    // buckets are per strategy
    let mut o = order(17, 0, 10, 0);
    o.timestamp = base + 2_000_000;
    o.strategy_id = "S2".to_string();
    assert!(eng.check_order(&o).allowed());
}

#[test]
fn throttle_never_refills_backwards_in_time() {
    let mut eng = engine();
    let base = T0 + 100_000_000;
    for i in 0..4 {
        let mut o = order(10 + i, 0, 10, 0);
        o.timestamp = base + i as i64;
        assert!(eng.check_order(&o).allowed());
    }
    // an out-of-order earlier timestamp must not mint tokens
    let mut o = order(20, 0, 10, 0);
    o.timestamp = base - 10 * NS;
    assert_eq!(eng.check_order(&o).rule_id, rules::RATE_THROTTLE);
}

/// Scenario: two threads share a strategy id and one clock runs behind.
/// The regressed order must not reset the bucket clock: the next in-order
/// order is still throttled (before the fix it got a full refill).
#[test]
fn risk_throttle_regression_then_forward() {
    let mut eng = engine();
    let base = T0 + 100_000_000;
    for i in 0..4 {
        let mut o = order(10 + i, 0, 10, 0);
        o.timestamp = base + i as i64;
        assert!(eng.check_order(&o).allowed());
    }
    let mut o = order(20, 0, 10, 0);
    o.timestamp = base - 10 * NS; // thread B, clock behind
    assert_eq!(eng.check_order(&o).rule_id, rules::RATE_THROTTLE);
    let mut o = order(21, 0, 10, 0);
    o.timestamp = base + 3; // thread A, in order: no time has elapsed
    assert_eq!(eng.check_order(&o).rule_id, rules::RATE_THROTTLE);
    // and only real elapsed event time refills (2ms = one token at 500/s)
    let mut o = order(22, 0, 10, 0);
    o.timestamp = base + 2_000_003;
    assert!(eng.check_order(&o).allowed());
}

// -------------------------------------------------------------- self-match

#[test]
fn self_match_priced_and_unpriced_cases() {
    let mut eng = engine();
    // orders spaced 10ms apart so the throttle (checked BEFORE self-match)
    // never binds in this test
    let spaced = |id: u64, side: u8, qty: i64, price: i64| {
        let mut o = order(id, side, qty, price);
        o.timestamp = T0 + 100_000_000 + id as i64 * 10_000_000;
        o
    };
    // resting buy at 2450
    assert!(eng.check_order(&spaced(1, 0, 100, 2450)).allowed());
    // sell at 2450 would cross own bid
    let d = eng.check_order(&spaced(2, 1, 50, 2450));
    assert_eq!(d.rule_id, rules::SELF_MATCH);
    // sell above own bid is fine (rests at 2455)
    assert!(eng.check_order(&spaced(3, 1, 50, 2455)).allowed());
    // buy at/through own ask crosses
    assert_eq!(eng.check_order(&spaced(4, 0, 50, 2455)).rule_id, rules::SELF_MATCH);
    assert_eq!(eng.check_order(&spaced(5, 0, 50, 2460)).rule_id, rules::SELF_MATCH);
    // buy below own ask is fine
    assert!(eng.check_order(&spaced(6, 0, 50, 2451)).allowed());
    // unpriced marketable buy vs any own resting ask: rejected (pinned)
    assert_eq!(eng.check_order(&spaced(7, 0, 50, 0)).rule_id, rules::SELF_MATCH);
    // cancel the resting ask -> market buy passes
    eng.on_order_done(3);
    assert!(eng.check_order(&spaced(8, 0, 50, 0)).allowed());
    // ... and the open MARKET buy blocks any own sell until it is done
    assert_eq!(eng.check_order(&spaced(10, 1, 50, 2455)).rule_id, rules::SELF_MATCH);
    eng.on_order_done(8);
    // same-side resting never self-matches
    assert!(eng.check_order(&spaced(9, 1, 50, 2455)).allowed());
}

#[test]
fn fills_release_resting_orders_for_self_match() {
    let mut eng = engine();
    assert!(eng.check_order(&order(1, 1, 100, 2455)).allowed()); // resting ask
    assert_eq!(eng.check_order(&order(2, 0, 10, 2455)).rule_id, rules::SELF_MATCH);
    // full fill of the resting ask removes it
    eng.on_fill(&Fill {
        ts: T0 + NS,
        strategy_id: "S1".to_string(),
        instrument_id: 1,
        order_id: 1,
        side: 1,
        qty: 100,
        price_ticks: 2455,
    });
    assert!(eng.check_order(&order(3, 0, 10, 2455)).allowed());
}

// ------------------------------------------------- position / notional set

fn fill(strategy: &str, iid: u32, side: u8, qty: i64, price: i64) -> Fill {
    Fill {
        ts: T0 + NS,
        strategy_id: strategy.to_string(),
        instrument_id: iid,
        order_id: 0,
        side,
        qty,
        price_ticks: price,
    }
}

#[test]
fn position_limit_projects_open_orders() {
    let mut eng = engine();
    eng.on_fill(&fill("S1", 1, 0, 95_000, 2452));
    assert_eq!(eng.position(1), 95_000);
    // resting buy 100
    assert!(eng.check_order(&order(1, 0, 100, 2450)).allowed());
    // 95000 + 100 + 40000 > 100000
    let d = eng.check_order(&order(2, 0, 40_000, 2452));
    assert_eq!(d.rule_id, rules::POSITION_LIMIT);
    // sells project the other way: 95000 - 40000 fine
    assert!(eng.check_order(&order(3, 1, 40_000, 2455)).allowed());
    // shorts cap symmetrically
    let mut eng2 = engine();
    eng2.on_fill(&fill("S1", 1, 1, 95_000, 2452));
    let d = eng2.check_order(&order(1, 1, 40_000, 2455));
    assert_eq!(d.rule_id, rules::POSITION_LIMIT);
}

#[test]
fn instrument_notional_limit_binds_before_position_cap() {
    let mut eng = engine();
    eng.on_fill(&fill("S1", 1, 0, 95_000, 2452));
    // projected 98,000 * 24.51 = 2,401,980 > 2,400,000 but position ok
    let d = eng.check_order(&order(1, 0, 3_000, 2452));
    assert_eq!(d.rule_id, rules::INSTRUMENT_NOTIONAL);
    // small order stays under
    assert!(eng.check_order(&order(2, 0, 100, 2452)).allowed());
}

#[test]
fn gross_and_net_notional_limits() {
    let mut eng = engine();
    // long 95,000 @ inst1 (2.33M) and short 70,000 @ inst2 (2.18M):
    // gross 4.51M, net 0.15M
    eng.on_fill(&fill("S1", 1, 0, 95_000, 2452));
    eng.on_fill(&fill("S2", 2, 1, 70_000, 3120));
    // sell 20,000 inst1 at 24.55 adds 491k gross -> 5.006M > 5M
    let d = eng.check_order(&order(1, 1, 20_000, 2455));
    assert_eq!(d.rule_id, rules::GROSS_NOTIONAL);
    // small order passes both
    assert!(eng.check_order(&order(2, 1, 1_000, 2455)).allowed());

    // net: hedged book -> unwind the short and buy more: all-long book
    let mut eng = engine();
    eng.on_fill(&fill("S1", 1, 0, 95_000, 2452)); // net 2.329M
    let mut o = order(3, 0, 6_000, 3120); // +187k -> net 2.517M > 2.5M
    o.instrument_id = 2;
    let d = eng.check_order(&o);
    assert_eq!(d.rule_id, rules::NET_NOTIONAL);
}

#[test]
fn unmarked_position_fails_closed_on_gross_check() {
    let mut t = ticks();
    t.insert(3u32, 0.01);
    let mut eng = RiskEngine::from_config_ticks(&config(), t);
    eng.on_market(1, 2450, 2452, T0);
    // a position in instrument 3, which has never had market data
    eng.on_fill(&fill("S1", 3, 0, 1_000, 5000));
    let d = eng.check_order(&order(1, 0, 100, 2450));
    assert_eq!(d.rule_id, rules::GROSS_NOTIONAL);
    assert!(d.reason.contains("fail-closed"));
}

// -------------------------------------------------------------- loss limits

#[test]
fn strategy_loss_limit_kills_the_strategy() {
    let mut eng = engine();
    eng.on_fill(&fill("S1", 1, 0, 95_000, 2452)); // avg 24.52
    eng.on_fill(&fill("S1", 1, 1, 95_000, 2398)); // realized -51,300
    assert!((eng.strategy_pnl("S1") - -51_300.0).abs() < 1e-6);
    // the kill event is in the audit log
    assert!(eng
        .audit()
        .iter()
        .any(|e| e.rule_id == rules::STRATEGY_LOSS && e.decision == Decision::Kill as u8));
    let d = eng.check_order(&order(1, 0, 100, 2450));
    assert_eq!(d.rule_id, rules::KILL_STRATEGY);
    // other strategies keep trading
    let mut o = order(2, 0, 100, 2450);
    o.strategy_id = "S2".to_string();
    assert!(eng.check_order(&o).allowed());
}

#[test]
fn global_daily_loss_kills_everything() {
    let mut eng = engine();
    eng.on_fill(&fill("S1", 1, 0, 95_000, 2452));
    eng.on_fill(&fill("S1", 1, 1, 95_000, 2180)); // realized -258,400
    assert!(eng.realized_pnl() < -250_000.0);
    assert!(eng
        .audit()
        .iter()
        .any(|e| e.rule_id == rules::DAILY_LOSS && e.decision == Decision::Kill as u8));
    // every strategy is now rejected by the global switch
    let mut o = order(1, 0, 100, 2450);
    o.strategy_id = "S9".to_string();
    assert_eq!(eng.check_order(&o).rule_id, rules::KILL_GLOBAL);
}

#[test]
fn avg_cost_pnl_accounting_is_pinned() {
    let mut eng = engine();
    // buy 100 @ 24.00, buy 100 @ 26.00 -> avg 25.00
    eng.on_fill(&fill("S1", 1, 0, 100, 2400));
    eng.on_fill(&fill("S1", 1, 0, 100, 2600));
    // sell 150 @ 25.50 -> realized (25.50 - 25.00) * 150 = +75
    eng.on_fill(&fill("S1", 1, 1, 150, 2550));
    assert!((eng.strategy_pnl("S1") - 75.0).abs() < 1e-9);
    assert_eq!(eng.position(1), 50);
    // sell 100 @ 24.00: closes 50 (-50), opens short 50 @ 24.00
    eng.on_fill(&fill("S1", 1, 1, 100, 2400));
    assert!((eng.strategy_pnl("S1") - 25.0).abs() < 1e-9);
    assert_eq!(eng.position(1), -50);
    // buy 50 @ 23.00 closes the short: +50 * (24-23) = +50
    eng.on_fill(&fill("S1", 1, 0, 50, 2300));
    assert!((eng.strategy_pnl("S1") - 75.0).abs() < 1e-9);
    assert_eq!(eng.position(1), 0);
}

// ------------------------------------------------------------------- audit

#[test]
fn every_decision_is_audited_and_deterministic() {
    let run = || {
        let mut eng = engine();
        eng.check_order(&order(1, 0, 100, 2450));
        eng.check_order(&order(1, 0, 100, 2450)); // duplicate
        eng.check_order(&order(2, 0, 60_000, 2450)); // fat finger
        eng.on_venue_disconnect(2, T0 + NS);
        eng.on_fill(&fill("S1", 1, 0, 95_000, 2452));
        eng.on_fill(&fill("S1", 1, 1, 95_000, 2398)); // strategy kill
        eng.check_order(&order(3, 0, 100, 2450));
        eng.audit_jsonl()
    };
    let log1 = run();
    let log2 = run();
    assert_eq!(log1, log2, "audit logs must be byte-identical across runs");
    assert_eq!(log1.lines().count(), 6); // 4 decisions + disconnect + kill
    // and every line round-trips through the schema shape
    for line in log1.lines() {
        risk::RiskEvent::from_json_line(line).expect("schema-shaped audit line");
    }
}

#[test]
fn metrics_count_decisions() {
    let mut eng = engine();
    eng.check_order(&order(1, 0, 100, 2450));
    eng.check_order(&order(2, 0, 60_000, 2450));
    assert_eq!(eng.metrics.counter_value("risk_decisions_total"), 2);
    assert_eq!(eng.metrics.counter_value("risk_allowed_total"), 1);
    assert_eq!(eng.metrics.counter_value("risk_rejected_total"), 1);
}

// ------------------------------------------------ round-3 scenario tests

/// Scenario: a news gap marks a 95k long down 14% with no fill. The
/// mark update alone must latch STRATEGY_LOSS then DAILY_LOSS.
#[test]
fn risk_mtm_loss_latches_without_fill() {
    let mut eng = engine();
    eng.on_fill(&fill("S1", 1, 0, 95_000, 2452)); // 95k @ 24.52
    assert!(eng.audit().iter().all(|e| e.decision != Decision::Kill as u8));
    eng.on_market(1, 2099, 2101, T0 + 2 * NS); // mid 21.00 -> -334,400
    let kills: Vec<&str> = eng
        .audit()
        .iter()
        .filter(|e| e.decision == Decision::Kill as u8)
        .map(|e| e.rule_id.as_str())
        .collect();
    assert_eq!(kills, [rules::STRATEGY_LOSS, rules::DAILY_LOSS]);
    assert!((eng.strategy_daily_pnl("S1").unwrap() + 334_400.0).abs() < 1e-6);
    assert!((eng.unrealized_pnl() + 334_400.0).abs() < 1e-6);
    assert_eq!(eng.realized_pnl(), 0.0);
    assert_eq!(eng.metrics.gauge_value("risk_unrealized_pnl").unwrap(), eng.unrealized_pnl());
    let d = eng.check_order(&order(1, 0, 100, 2100));
    assert_eq!(d.rule_id, rules::KILL_GLOBAL);
    // a strategy that never traded is also blocked by the global latch
    let mut o = order(2, 0, 100, 2100);
    o.strategy_id = "S9".to_string();
    assert_eq!(eng.check_order(&o).rule_id, rules::KILL_GLOBAL);
}

/// The MTM mark is the last consolidated mid: a regressed (older) update
/// is dropped and counted, never overwriting a newer mark.
#[test]
fn market_update_regression_is_dropped() {
    let mut eng = engine();
    eng.on_market(1, 2000, 2002, T0 - NS); // older than the T0 update
    assert_eq!(eng.metrics.counter_value("risk_market_regressions_dropped_total"), 1);
    // the mid is still 24.51: a 45,000 market buy is 1,102,950 > 1M
    let d = eng.check_order(&order(1, 0, 45_000, 0));
    assert_eq!(d.rule_id, rules::FAT_FINGER_NOTIONAL);
}

/// Scenario: fat-fingered FX ticket. USD/JPY qty is 1,000-USD lots and
/// prices are JPY: the notional must be qty * 1000 * price / (USD/JPY mid)
/// in USD, so 1,001 lots (1,001,000 USD) rejects and 999 lots passes.
#[test]
fn risk_fx_notional_uses_lot_size_and_ccy() {
    let mut eng = fx_engine();
    let t = T0 + 100_000_000;
    let d = eng.check_order(&typed(1, 103, 1, 1001, 0, OrderType::Market, t));
    assert_eq!(d.rule_id, rules::FAT_FINGER_NOTIONAL, "{}", d.reason);
    assert!(d.reason.starts_with("notional 1001000.00 USD"), "{}", d.reason);
    assert!(eng.check_order(&typed(2, 103, 1, 999, 0, OrderType::Market, t)).allowed());
    // before the fix 50,000 lots (50M USD) computed as 50,000 * 147.52 JPY
    let d = eng.check_order(&typed(3, 103, 1, 50_000, 0, OrderType::Market, t));
    assert_eq!(d.rule_id, rules::FAT_FINGER_NOTIONAL);
    // missing conversion pair (GBP/USD never marked) -> FX_RATE_MISSING
    eng.on_market(108, 85_315, 85_325, t);
    let d = eng.check_order(&typed(4, 108, 0, 100, 0, OrderType::Market, t));
    assert_eq!(d.rule_id, rules::FX_RATE_MISSING);
    eng.on_market(102, 127_335, 127_345, t);
    assert!(eng.check_order(&typed(5, 108, 0, 100, 0, OrderType::Market, t + 10_000_000)).allowed());
    // a stale conversion rate is as bad as a missing one
    eng.on_market(108, 85_315, 85_325, t + 6 * NS);
    let d = eng.check_order(&typed(6, 108, 0, 100, 0, OrderType::Market, t + 6 * NS));
    assert_eq!(d.rule_id, rules::FX_RATE_MISSING);
    assert!(d.reason.contains("age"), "{}", d.reason);
}

/// Multi-currency P&L: a JPY loss is converted at the USD/JPY mid before
/// the loss limit is evaluated (150,000 JPY at 150 = 1,000 USD).
#[test]
fn fx_pnl_is_converted_before_loss_limits() {
    let mut eng = fx_engine();
    eng.on_market(103, 149_995, 150_005, T0); // mid 150.000
    eng.on_fill(&Fill { ts: T0 + NS, strategy_id: "S1".into(), instrument_id: 103, order_id: 0, side: 0, qty: 10, price_ticks: 150_000 });
    eng.on_fill(&Fill { ts: T0 + NS, strategy_id: "S1".into(), instrument_id: 103, order_id: 0, side: 1, qty: 10, price_ticks: 149_985 });
    // realized = -0.015 * 10 * 1000 = -150 JPY = -1.00 USD
    assert!((eng.strategy_pnl("S1") + 1.0).abs() < 1e-9, "{}", eng.strategy_pnl("S1"));
    // a 7.5M JPY loss = 50,000 USD -> strategy latch (limit 50,000)
    eng.on_fill(&Fill { ts: T0 + 2 * NS, strategy_id: "S2".into(), instrument_id: 103, order_id: 0, side: 0, qty: 5_000, price_ticks: 150_000 });
    eng.on_fill(&Fill { ts: T0 + 2 * NS, strategy_id: "S2".into(), instrument_id: 103, order_id: 0, side: 1, qty: 5_000, price_ticks: 148_500 });
    assert!(eng
        .audit()
        .iter()
        .any(|e| e.rule_id == rules::STRATEGY_LOSS && e.scope_id == "S2"));
}

/// Scenario (Knight-style): 90k long, a burst of MARKET buys during a
/// latency spike. In-flight MARKET orders count in the projection: the
/// third is rejected before any fill; after the fills and the venue's
/// terminal reports the next one passes.
#[test]
fn risk_inflight_market_orders_count_in_projection() {
    let mut eng = engine();
    eng.on_market(1, 1999, 2001, T0); // mid 20.00: the 2.4M notional cap sits above 100k shares
    eng.on_fill(&fill("S1", 1, 0, 90_000, 2000));
    let t = T0 + 100_000_000;
    // orders spaced 10ms so the throttle never binds
    assert!(eng.check_order(&typed(1, 1, 0, 5_000, 0, OrderType::Market, t)).allowed());
    assert!(eng.check_order(&typed(2, 1, 0, 5_000, 0, OrderType::Market, t + 10_000_000)).allowed());
    let d = eng.check_order(&typed(3, 1, 0, 5_000, 0, OrderType::Market, t + 20_000_000));
    assert_eq!(d.rule_id, rules::POSITION_LIMIT, "{}", d.reason);
    assert!(d.reason.contains("105000"));
    assert_eq!(eng.open_order_count(), 2);
    // IOC / FOK are tracked the same way
    let d = eng.check_order(&typed(4, 1, 0, 5_000, 0, OrderType::Ioc, t + 30_000_000));
    assert_eq!(d.rule_id, rules::POSITION_LIMIT);
    // fill of the first (order_id 1) releases it; the second is done at the venue
    eng.on_fill(&Fill { ts: t + 40_000_000, strategy_id: "S1".into(), instrument_id: 1, order_id: 1, side: 0, qty: 5_000, price_ticks: 2000 });
    assert_eq!(eng.open_order_count(), 1);
    eng.on_order_done(2);
    assert_eq!(eng.open_order_count(), 0);
    assert!(eng.check_order(&typed(5, 1, 0, 5_000, 0, OrderType::Market, t + 50_000_000)).allowed());
}

/// PEG orders rest at the venue: they are tracked at their pegged touch
/// for self-match and count in the position projection.
#[test]
fn risk_peg_orders_tracked_for_self_match_and_projection() {
    let mut eng = engine();
    eng.on_market(1, 1999, 2001, T0); // mid 20.00
    let t = T0 + 100_000_000;
    assert!(eng.check_order(&typed(1, 1, 0, 100, 0, OrderType::Peg, t)).allowed());
    // a MARKET sell would cross the own pegged bid
    let d = eng.check_order(&typed(2, 1, 1, 50, 0, OrderType::Market, t + 10_000_000));
    assert_eq!(d.rule_id, rules::SELF_MATCH);
    assert!(d.reason.contains("at 1999"), "pegged at the bid: {}", d.reason);
    // a limit sell above the pegged bid is fine
    assert!(eng.check_order(&typed(3, 1, 1, 50, 2005, OrderType::Limit, t + 20_000_000)).allowed());
    // PEG qty counts in open_same: 99,900 + 100 pegged + 100 = 100,100
    eng.on_fill(&fill("S1", 1, 0, 99_900, 2000));
    let d = eng.check_order(&typed(4, 1, 0, 100, 1999, OrderType::Limit, t + 30_000_000));
    assert_eq!(d.rule_id, rules::POSITION_LIMIT);
    eng.on_order_done(1);
    assert!(eng.check_order(&typed(5, 1, 0, 100, 1999, OrderType::Limit, t + 40_000_000)).allowed());
}

/// Scenario: resting LIMIT buys across instruments. Gross must include
/// every open order: 40 x 120k of resting buys leaves no room for a 41st.
#[test]
fn risk_gross_includes_resting_orders() {
    let mut refs = BTreeMap::new();
    for iid in 1..=41u32 {
        refs.insert(iid, InstrumentRef::equity(0.01));
    }
    let mut eng = RiskEngine::from_config(&config(), refs);
    for iid in 1..=41u32 {
        eng.on_market(iid, 2999, 3001, T0); // mid 30.00
    }
    let t = T0 + 100_000_000;
    // 4,000 @ 30.00 = 120,000 per resting buy; 40 of them = 4.8M (< 5M,
    // < 2.5M net? no: net also binds — so alternate buys and sells)
    for i in 0..40u32 {
        let side = if i % 2 == 0 { 0 } else { 1 };
        let mut o = typed(u64::from(i) + 1, i + 1, side, 4_000, 3000, OrderType::Limit, t + i64::from(i) * 10_000_000);
        o.strategy_id = format!("S{}", i % 4);
        assert!(eng.check_order(&o).allowed(), "resting order {i}");
    }
    assert_eq!(eng.open_order_count(), 40);
    // 41st: 4.8M open + 240k = 5.04M > 5M gross while no position exists
    let mut o = typed(41, 41, 0, 8_000, 3000, OrderType::Limit, t + 400_000_000);
    o.strategy_id = "S0".to_string();
    let d = eng.check_order(&o);
    assert_eq!(d.rule_id, rules::GROSS_NOTIONAL, "{}", d.reason);
    // cancelling one frees the room
    eng.on_order_done(1);
    let mut o = typed(42, 41, 0, 3_000, 3000, OrderType::Limit, t + 410_000_000);
    o.strategy_id = "S0".to_string();
    assert!(eng.check_order(&o).allowed());
}

/// Scenario: 11:40 latch, root cause fixed, CRO approves a resumed session
/// with a raised limit. clear_kill alone still rejects (the P&L is still
/// at the limit) and the next mark re-latches; only an approved override
/// plus a clear lets orders flow. Both records are in the audit log.
#[test]
fn risk_clear_kill_after_loss_latch_resumes_with_override() {
    let mut eng = engine();
    eng.on_fill(&fill("S1", 1, 0, 95_000, 2452));
    eng.on_fill(&fill("S1", 1, 1, 94_900, 2398)); // realized -51,246 -> S1 latched; 100 left
    assert_eq!(eng.check_order(&order(1, 0, 100, 2450)).rule_id, rules::KILL_STRATEGY);
    eng.clear_kill(Scope::Strategy, "S1", T0 + 2 * NS, "ops clear");
    let d = eng.check_order(&order(2, 0, 100, 2450));
    assert_eq!(d.rule_id, rules::STRATEGY_LOSS, "belt-and-braces: {}", d.reason);
    assert_eq!(d.decision, Decision::Reject);
    // the next mark of a held instrument re-latches (no fill needed)
    eng.on_market(1, 2450, 2452, T0 + 3 * NS);
    assert_eq!(eng.check_order(&order(3, 0, 100, 2450)).rule_id, rules::KILL_STRATEGY);
    let n_latches = eng.audit().iter().filter(|e| e.rule_id == rules::STRATEGY_LOSS && e.decision == Decision::Kill as u8).count();
    assert_eq!(n_latches, 2);
    // override below the loss is legal but ineffective
    eng.override_loss_limit(Scope::Strategy, "S1", 51_000.0, T0 + 4 * NS, "cro").unwrap();
    eng.clear_kill(Scope::Strategy, "S1", T0 + 4 * NS, "ops clear #2");
    assert_eq!(eng.check_order(&order(4, 0, 100, 2450)).rule_id, rules::STRATEGY_LOSS);
    // a flat strategy's realized loss cannot re-latch on a mark (no lot to
    // mark) but stays rejected pre-trade: the latch basis is the same P&L
    eng.on_fill(&fill("S1", 1, 1, 100, 2450)); // flat; realized -51,448 -> re-latch on the fill
    assert_eq!(eng.check_order(&order(6, 0, 100, 2450)).rule_id, rules::KILL_STRATEGY);
    eng.clear_kill(Scope::Strategy, "S1", T0 + 4 * NS + 1, "ops clear #3");
    eng.on_market(1, 2450, 2452, T0 + 4 * NS + 2);
    assert_eq!(eng.check_order(&order(7, 0, 100, 2450)).rule_id, rules::STRATEGY_LOSS);
    // override above the loss + clear -> ALLOW, and marks no longer latch
    eng.override_loss_limit(Scope::Strategy, "S1", 75_000.0, T0 + 5 * NS, "cro").unwrap();
    eng.clear_kill(Scope::Strategy, "S1", T0 + 5 * NS, "ops clear INC-1");
    eng.on_market(1, 2450, 2452, T0 + 5 * NS);
    assert!(eng.check_order(&order(5, 0, 100, 2450)).allowed());
    let ids: Vec<&str> = eng.audit().iter().map(|e| e.rule_id.as_str()).collect();
    assert_eq!(ids.iter().filter(|r| **r == rules::LOSS_LIMIT_OVERRIDE).count(), 2);
    assert_eq!(ids.iter().filter(|r| **r == rules::KILL_SWITCH_CLEARED).count(), 4);
    let ov = eng.audit().iter().find(|e| e.rule_id == rules::LOSS_LIMIT_OVERRIDE).unwrap();
    assert_eq!(ov.reason, "daily loss limit 50000.00 -> 51000.00 approved by cro");
    // invalid overrides are refused without side effects
    assert!(eng.override_loss_limit(Scope::Strategy, "S1", -1.0, T0, "x").is_err());
    assert!(eng.override_loss_limit(Scope::Instrument, "1", 10.0, T0, "x").is_err());
    assert!(eng.override_loss_limit(Scope::Global, "", f64::NAN, T0, "x").is_err());
}

/// Scenario: FX book rolls at 22:00 Sunday. roll_session zeroes realized
/// P&L, re-bases marked lots, clears overrides and keeps kill switches.
#[test]
fn scenario_session_roll_rebases_daily_pnl_and_keeps_latches() {
    let mut eng = engine();
    eng.on_fill(&fill("S1", 1, 0, 95_000, 2452)); // unrealized -950 at 24.51
    eng.on_fill(&fill("S2", 2, 0, 1_000, 3120));
    eng.on_fill(&fill("S2", 2, 1, 1_000, 3110)); // realized -100
    eng.override_loss_limit(Scope::Global, "", 400_000.0, T0, "cro").unwrap();
    eng.engage_kill(Scope::Strategy, "S2", T0, "manual");
    assert!((eng.global_daily_pnl().unwrap() + 1_050.0).abs() < 1e-6);
    eng.roll_session(T0 + NS, "roll");
    assert_eq!(eng.global_daily_pnl().unwrap(), 0.0);
    assert_eq!(eng.strategy_daily_pnl("S1").unwrap(), 0.0);
    assert_eq!(eng.position(1), 95_000, "positions survive the roll");
    let mut o = order(1, 0, 100, 2450);
    o.strategy_id = "S2".into();
    assert_eq!(eng.check_order(&o).rule_id, rules::KILL_STRATEGY);
    // the override is gone: a 300k loss on the new day latches at 250k
    eng.on_market(1, 2099, 2101, T0 + 2 * NS); // 95k * (21.00 - 24.51) = -333,450
    assert!(eng.kill_switch_engaged());
    assert!(eng.audit().iter().any(|e| e.rule_id == rules::SESSION_ROLLED));
}

/// Scenario: mid-session restart. Snapshot after 30 mixed steps, restore
/// into a fresh engine, the next steps produce identical decisions and
/// audit lines to the unbroken run; a fresh engine that requires a
/// bootstrap rejects everything until positions arrive.
#[test]
fn risk_snapshot_restore_roundtrip() {
    let script = |eng: &mut RiskEngine, from: i64, n: i64| {
        let mut lines = Vec::new();
        for k in from..from + n {
            let t = T0 + 100_000_000 + k * 20_000_000;
            let side = if k % 3 == 0 { 1 } else { 0 };
            let px = if k % 5 == 0 { 0 } else { 2450 + (k % 4) };
            let mut o = typed(1000 + k as u64, 1 + (k % 2) as u32, side, 100 + k, if k % 2 == 1 { 3120 } else { px }, if px == 0 { OrderType::Market } else { OrderType::Limit }, t);
            o.strategy_id = format!("S{}", k % 3);
            let d = eng.check_order(&o);
            lines.push(format!("{}:{}", o.order_id, d.rule_id));
            if k % 4 == 0 {
                eng.on_fill(&Fill { ts: t, strategy_id: o.strategy_id.clone(), instrument_id: o.instrument_id, order_id: o.order_id, side, qty: 50, price_ticks: if o.instrument_id == 1 { 2451 } else { 3120 } });
            }
            if k % 7 == 0 {
                eng.on_order_done(o.order_id);
            }
            if k % 9 == 0 {
                eng.on_market(1, 2449 + (k % 3), 2452, t);
            }
        }
        lines
    };
    let mut unbroken = engine();
    let first = script(&mut unbroken, 0, 30);
    let snap = unbroken.snapshot();
    assert_eq!(snap["x-version"].as_u64(), Some(1));
    let limits = RiskLimits::from_json(&config()).unwrap();
    let mut restored = RiskEngine::restore(limits, unbroken_refs(), &snap, T0 + 5 * NS).unwrap();
    assert_eq!(restored.audit().len(), 1);
    assert_eq!(restored.audit()[0].rule_id, rules::STATE_RESTORED);
    let a = script(&mut unbroken, 30, 20);
    let b = script(&mut restored, 30, 20);
    assert_eq!(a, b);
    assert_eq!(first.len(), 30);
    let full = unbroken.audit_jsonl();
    let full_lines: Vec<&str> = full.lines().collect();
    let restored_audit = restored.audit_jsonl();
    let tail: Vec<&str> = restored_audit.lines().skip(1).collect();
    assert_eq!(&full_lines[full_lines.len() - tail.len()..], &tail[..]);
    assert_eq!(restored.snapshot(), unbroken.snapshot());
    // a malformed snapshot is refused as a whole
    let mut bad = snap.clone();
    bad["x-version"] = serde_json::json!(99);
    assert!(RiskEngine::restore(RiskLimits::from_json(&config()).unwrap(), unbroken_refs(), &bad, T0).is_err());
    let mut bad = snap.clone();
    bad["lots"][0]["avg_price"] = serde_json::json!("nan");
    assert!(RiskEngine::restore(RiskLimits::from_json(&config()).unwrap(), unbroken_refs(), &bad, T0).is_err());
    // bootstrap gate: fail-closed until positions arrive
    let mut fresh = engine();
    fresh.require_bootstrap();
    let d = fresh.check_order(&order(1, 0, 100, 2450));
    assert_eq!(d.rule_id, rules::NOT_BOOTSTRAPPED);
    assert_eq!(d.severity, Severity::Breach);
    let bad = fresh.bootstrap_positions(&[fill("S1", 1, 0, 95_000, 2452), fill("S1", 999, 0, 1, 1)], T0 + NS);
    assert_eq!(bad, 1);
    assert_eq!(fresh.position(1), 95_000);
    assert_eq!(fresh.check_order(&order(2, 0, 40_000, 2452)).rule_id, rules::POSITION_LIMIT);
    assert!(fresh.audit().iter().any(|e| e.rule_id == rules::BOOTSTRAP_COMPLETE));
}

fn unbroken_refs() -> BTreeMap<u32, InstrumentRef> {
    ticks().into_iter().map(|(i, t)| (i, InstrumentRef::equity(t))).collect()
}

/// Malformed fills are audited and dropped, never applied.
#[test]
fn malformed_fills_are_audited_and_dropped() {
    let mut eng = engine();
    let mut f = fill("S1", 1, 0, 100, 2452);
    f.qty = 0;
    assert!(!eng.on_fill(&f));
    let mut f = fill("S1", 1, 0, 100, 2452);
    f.side = 2;
    assert!(!eng.on_fill(&f));
    let mut f = fill("S1", 1, 0, 100, 2452);
    f.price_ticks = 0;
    assert!(!eng.on_fill(&f));
    assert!(!eng.on_fill(&fill("S1", 999, 0, 100, 2452)));
    assert_eq!(eng.position(1), 0);
    assert_eq!(eng.metrics.counter_value("risk_malformed_fills_total"), 4);
    assert_eq!(eng.audit().iter().filter(|e| e.rule_id == rules::MALFORMED_FILL).count(), 4);
    assert!(eng.on_fill(&fill("S1", 1, 0, 100, 2452)));
    assert_eq!(eng.position(1), 100);
}

/// Duplicate-id window > 0: ids expire and the seen set is pruned.
#[test]
fn duplicate_window_expires_and_prunes() {
    let mut doc = config();
    doc["per_order"]["duplicate_order_window_ns"] = serde_json::json!(NS);
    let mut eng = RiskEngine::from_config_ticks(&doc, ticks());
    eng.on_market(1, 2450, 2452, T0);
    let mut o = order(7, 0, 100, 2450);
    o.timestamp = T0;
    assert!(eng.check_order(&o).allowed());
    let mut o = order(7, 0, 100, 2450);
    o.timestamp = T0 + NS; // inside the window (<=)
    assert_eq!(eng.check_order(&o).rule_id, rules::DUPLICATE_ORDER_ID);
    let mut o = order(7, 0, 100, 2450);
    o.timestamp = T0 + NS + 1; // expired
    assert!(eng.check_order(&o).allowed());
    let snap = eng.snapshot();
    assert_eq!(snap["seen_orders"].as_array().unwrap().len(), 1, "pruned to the window");
}

/// Self-match is a firm-level (any strategy, any venue) wash-trade control.
#[test]
fn self_match_is_firm_wide_across_strategies_and_venues() {
    let mut eng = engine();
    assert!(eng.check_order(&order(1, 0, 100, 2450)).allowed()); // S1 bid @ 2450 on venue 1
    let mut o = order(2, 1, 50, 2450);
    o.strategy_id = "S2".into();
    o.venue_id = 2;
    o.timestamp += 10_000_000;
    assert_eq!(eng.check_order(&o).rule_id, rules::SELF_MATCH);
}
