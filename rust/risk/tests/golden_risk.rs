//! Golden replay of `tests/golden/expected_risk_decisions.json`.
//!
//! The engine is built from `configs/risk.json` and driven through the
//! pinned step script; every order step's decision, deciding rule and
//! severity must match exactly, the pinned KILL/notification events must
//! appear in order, and the audit log must replay byte-identically.

use std::collections::BTreeMap;

use risk::{Decision, Fill, RiskEngine, Scope};
use venue::OrderRequest;

fn repo_path(rel: &str) -> std::path::PathBuf {
    std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..").join(rel)
}

fn load_json(rel: &str) -> serde_json::Value {
    serde_json::from_str(&std::fs::read_to_string(repo_path(rel)).unwrap())
        .unwrap_or_else(|e| panic!("parsing {rel}: {e}"))
}

fn parse_order(v: &serde_json::Value) -> OrderRequest {
    OrderRequest {
        order_id: v["order_id"].as_u64().unwrap(),
        instrument_id: v["instrument_id"].as_u64().unwrap() as u32,
        side: v["side"].as_u64().unwrap() as u8,
        qty: v["qty"].as_i64().unwrap(),
        price_ticks: v["price_ticks"].as_i64().unwrap(),
        order_type: v["order_type"].as_u64().unwrap() as u8,
        venue_id: v["venue_id"].as_u64().unwrap() as u16,
        strategy_id: v["strategy_id"].as_str().unwrap().to_string(),
        urgency: v["urgency"].as_f64().unwrap(),
        timestamp: v["timestamp"].as_i64().unwrap(),
    }
}

fn parse_scope(s: &str) -> Scope {
    match s {
        "GLOBAL" => Scope::Global,
        "STRATEGY" => Scope::Strategy,
        "INSTRUMENT" => Scope::Instrument,
        "VENUE" => Scope::Venue,
        other => panic!("bad scope {other}"),
    }
}

/// Run the golden script once, asserting per-order expectations when
/// `check` is true. Returns the audit JSONL.
fn replay(golden: &serde_json::Value, check: bool) -> String {
    let config = load_json(golden["config"].as_str().expect("config path"));
    let mut ticks = BTreeMap::new();
    for (iid, spec) in golden["instruments"].as_object().unwrap() {
        ticks.insert(iid.parse::<u32>().unwrap(), spec["tick_size"].as_f64().unwrap());
    }
    let mut eng = RiskEngine::from_config(&config, ticks);
    for (i, step) in golden["steps"].as_array().unwrap().iter().enumerate() {
        match step["type"].as_str().unwrap() {
            "market" => eng.on_market(
                step["instrument_id"].as_u64().unwrap() as u32,
                step["bid_ticks"].as_i64().unwrap(),
                step["ask_ticks"].as_i64().unwrap(),
                step["ts"].as_i64().unwrap(),
            ),
            "fill" => eng.on_fill(&Fill {
                ts: step["ts"].as_i64().unwrap(),
                strategy_id: step["strategy_id"].as_str().unwrap().to_string(),
                instrument_id: step["instrument_id"].as_u64().unwrap() as u32,
                order_id: step["order_id"].as_u64().unwrap(),
                side: step["side"].as_u64().unwrap() as u8,
                qty: step["qty"].as_i64().unwrap(),
                price_ticks: step["price_ticks"].as_i64().unwrap(),
            }),
            "cancel" => eng.on_order_done(step["order_id"].as_u64().unwrap()),
            "gap" => eng.on_sequence_gap(
                step["instrument_id"].as_u64().unwrap() as u32,
                step["ts"].as_i64().unwrap(),
            ),
            "recover" => eng.on_feed_recovered(
                step["instrument_id"].as_u64().unwrap() as u32,
                step["ts"].as_i64().unwrap(),
            ),
            "venue_down" => eng.on_venue_disconnect(
                step["venue_id"].as_u64().unwrap() as u16,
                step["ts"].as_i64().unwrap(),
            ),
            "venue_up" => eng.on_venue_reconnect(
                step["venue_id"].as_u64().unwrap() as u16,
                step["ts"].as_i64().unwrap(),
            ),
            "kill" => eng.engage_kill(
                parse_scope(step["scope"].as_str().unwrap()),
                step["scope_id"].as_str().unwrap(),
                step["ts"].as_i64().unwrap(),
                step["reason"].as_str().unwrap_or(""),
            ),
            "unkill" => eng.clear_kill(
                parse_scope(step["scope"].as_str().unwrap()),
                step["scope_id"].as_str().unwrap(),
                step["ts"].as_i64().unwrap(),
                step["reason"].as_str().unwrap_or(""),
            ),
            "order" => {
                let order = parse_order(&step["order"]);
                let d = eng.check_order(&order);
                if check {
                    let exp = &step["expect"];
                    assert_eq!(
                        d.decision as u8,
                        exp["decision"].as_u64().unwrap() as u8,
                        "step {i} order {}: decision ({} / {})",
                        order.order_id,
                        d.rule_id,
                        d.reason
                    );
                    assert_eq!(
                        d.rule_id,
                        exp["rule_id"].as_str().unwrap(),
                        "step {i} order {}: rule ({})",
                        order.order_id,
                        d.reason
                    );
                    assert_eq!(
                        d.severity as u8,
                        exp["severity"].as_u64().unwrap() as u8,
                        "step {i} order {}: severity",
                        order.order_id
                    );
                }
            }
            other => panic!("unknown step type {other}"),
        }
    }
    eng.audit_jsonl()
}

#[test]
fn golden_decisions_match() {
    let golden = load_json("tests/golden/expected_risk_decisions.json");
    let n_orders = golden["steps"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|s| s["type"] == "order")
        .count();
    assert!(n_orders >= 25, "golden vector must pin at least 25 orders");
    replay(&golden, true);
}

#[test]
fn golden_kill_events_in_order() {
    let golden = load_json("tests/golden/expected_risk_decisions.json");
    let audit = replay(&golden, false);
    let events: Vec<risk::RiskEvent> = audit
        .lines()
        .map(|l| risk::RiskEvent::from_json_line(l).expect("schema-shaped line"))
        .collect();
    // the non-decision (notification / kill) events, in emission order
    let notif: Vec<&risk::RiskEvent> = events
        .iter()
        .filter(|e| {
            matches!(
                e.rule_id.as_str(),
                "VENUE_DISCONNECT"
                    | "VENUE_RECONNECT"
                    | "KILL_SWITCH_ENGAGED"
                    | "KILL_SWITCH_CLEARED"
                    | "STRATEGY_LOSS"
                    | "DAILY_LOSS"
            ) && !e.reason.is_empty() // decision rejects carry reasons too
        })
        .filter(|e| e.decision != Decision::Reject as u8)
        .collect();
    let expected = golden["expected_kill_events"].as_array().unwrap();
    assert_eq!(notif.len(), expected.len(), "kill/notification event count");
    for (got, want) in notif.iter().zip(expected) {
        assert_eq!(got.rule_id, want["rule_id"].as_str().unwrap());
        assert_eq!(got.scope, parse_scope(want["scope"].as_str().unwrap()));
        assert_eq!(got.scope_id, want["scope_id"].as_str().unwrap());
        assert_eq!(got.decision, want["decision"].as_u64().unwrap() as u8);
    }
}

#[test]
fn audit_log_replays_byte_identically() {
    let golden = load_json("tests/golden/expected_risk_decisions.json");
    let a = replay(&golden, false);
    let b = replay(&golden, false);
    assert_eq!(a, b);
    // one audit line per decision + one per pinned notification event
    let n_orders = golden["steps"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|s| s["type"] == "order")
        .count();
    let n_notif = golden["expected_kill_events"].as_array().unwrap().len();
    assert_eq!(a.lines().count(), n_orders + n_notif);
}
