//! Per-rule unit tests for the hard risk engine: every rule's allow AND
//! deny path, fail-closed behavior, kill-switch precedence, event-time
//! throttling, self-match cases and audit determinism.

use std::collections::BTreeMap;

use risk::{rules, Decision, Fill, RiskEngine, Scope, Severity};
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
    let mut eng = RiskEngine::from_config(&config(), ticks());
    eng.on_market(1, 2450, 2452, T0); // mid 24.51
    eng.on_market(2, 3119, 3121, T0); // mid 31.20
    eng
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
    let mut eng = RiskEngine::from_config(&doc, ticks());
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
    let mut eng = RiskEngine::from_config(&doc, ticks());
    let d = eng.check_order(&order(1, 0, 100, 2450));
    assert_eq!(d.rule_id, rules::CONFIG_MISSING);
}

#[test]
fn config_kill_switch_engaged_starts_killed() {
    let mut doc = config();
    doc["global"]["kill_switch_engaged"] = serde_json::json!(true);
    let mut eng = RiskEngine::from_config(&doc, ticks());
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
    let mut eng2 = RiskEngine::from_config(&config(), t);
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
    let mut eng = RiskEngine::from_config(&config(), t);
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
