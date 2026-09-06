//! Golden replay of `tests/golden/expected_risk_decisions.json` plus the
//! byte-exact audit golden (`expected_risk_audit.jsonl`) and the snapshot
//! golden (`expected_risk_snapshot.json`).
//!
//! The engine is built from `configs/risk.json` and driven through the
//! pinned step script; every order step's decision, deciding rule and
//! severity must match exactly, the pinned notification events must appear
//! in order, the audit log must be byte-identical to the golden file, and
//! restoring the mid-script snapshot must reproduce the audit tail.

use std::collections::BTreeMap;

use risk::{Decision, Fill, InstrumentRef, RiskEngine, RiskLimits, Scope};
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

fn instruments(golden: &serde_json::Value) -> BTreeMap<u32, InstrumentRef> {
    let mut out = BTreeMap::new();
    for (iid, spec) in golden["instruments"].as_object().unwrap() {
        out.insert(
            iid.parse::<u32>().unwrap(),
            InstrumentRef::new(
                spec["tick_size"].as_f64().unwrap(),
                spec["qty_unit"].as_f64().unwrap(),
                spec["quote_ccy"].as_str().unwrap(),
            ),
        );
    }
    out
}

/// Apply one step (order steps are checked when `check`).
fn apply(eng: &mut RiskEngine, i: usize, step: &serde_json::Value, check: bool) {
    let ts = || step["ts"].as_i64().unwrap();
    match step["type"].as_str().unwrap() {
        "market" => eng.on_market(
            step["instrument_id"].as_u64().unwrap() as u32,
            step["bid_ticks"].as_i64().unwrap(),
            step["ask_ticks"].as_i64().unwrap(),
            ts(),
        ),
        "fill" => {
            eng.on_fill(&Fill {
                ts: ts(),
                strategy_id: step["strategy_id"].as_str().unwrap().to_string(),
                instrument_id: step["instrument_id"].as_u64().unwrap() as u32,
                order_id: step["order_id"].as_u64().unwrap(),
                side: step["side"].as_u64().unwrap() as u8,
                qty: step["qty"].as_i64().unwrap(),
                price_ticks: step["price_ticks"].as_i64().unwrap(),
            });
        }
        "cancel" => eng.on_order_done(step["order_id"].as_u64().unwrap()),
        "gap" => eng.on_sequence_gap(step["instrument_id"].as_u64().unwrap() as u32, ts()),
        "recover" => eng.on_feed_recovered(step["instrument_id"].as_u64().unwrap() as u32, ts()),
        "venue_down" => eng.on_venue_disconnect(step["venue_id"].as_u64().unwrap() as u16, ts()),
        "venue_up" => eng.on_venue_reconnect(step["venue_id"].as_u64().unwrap() as u16, ts()),
        "kill" => eng.engage_kill(
            parse_scope(step["scope"].as_str().unwrap()),
            step["scope_id"].as_str().unwrap(),
            ts(),
            step["reason"].as_str().unwrap_or(""),
        ),
        "unkill" => eng.clear_kill(
            parse_scope(step["scope"].as_str().unwrap()),
            step["scope_id"].as_str().unwrap(),
            ts(),
            step["reason"].as_str().unwrap_or(""),
        ),
        "override_loss" => eng
            .override_loss_limit(
                parse_scope(step["scope"].as_str().unwrap()),
                step["scope_id"].as_str().unwrap(),
                step["new_limit"].as_f64().unwrap(),
                ts(),
                step["approver"].as_str().unwrap_or(""),
            )
            .unwrap(),
        "roll_session" => eng.roll_session(ts(), step["reason"].as_str().unwrap_or("")),
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

fn build(golden: &serde_json::Value) -> RiskEngine {
    let config = load_json(golden["config"].as_str().expect("config path"));
    RiskEngine::from_config(&config, instruments(golden))
}

/// Run the golden script once, asserting per-order expectations when
/// `check` is true. Returns the audit JSONL.
fn replay(golden: &serde_json::Value, check: bool) -> String {
    let mut eng = build(golden);
    for (i, step) in golden["steps"].as_array().unwrap().iter().enumerate() {
        apply(&mut eng, i, step, check);
    }
    eng.audit_jsonl()
}

fn is_notification(rule_id: &str, decision: u8) -> bool {
    matches!(
        rule_id,
        "VENUE_DISCONNECT"
            | "VENUE_RECONNECT"
            | "KILL_SWITCH_ENGAGED"
            | "KILL_SWITCH_CLEARED"
            | "LOSS_LIMIT_OVERRIDE"
            | "SESSION_ROLLED"
            | "BOOTSTRAP_COMPLETE"
            | "STATE_RESTORED"
            | "MALFORMED_FILL"
    ) || (matches!(rule_id, "STRATEGY_LOSS" | "DAILY_LOSS")
        && decision == Decision::Kill as u8)
}

#[test]
fn golden_decisions_match() {
    let golden = load_json("tests/golden/expected_risk_decisions.json");
    assert_eq!(golden["x-version"].as_u64(), Some(3));
    let n_orders = golden["steps"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|s| s["type"] == "order")
        .count();
    assert!(n_orders >= 55, "golden vector must pin at least 55 orders");
    replay(&golden, true);
}

#[test]
fn golden_notification_events_in_order() {
    let golden = load_json("tests/golden/expected_risk_decisions.json");
    let audit = replay(&golden, false);
    let events: Vec<risk::RiskEvent> = audit
        .lines()
        .map(|l| risk::RiskEvent::from_json_line(l).expect("schema-shaped line"))
        .collect();
    let notif: Vec<&risk::RiskEvent> = events
        .iter()
        .filter(|e| is_notification(&e.rule_id, e.decision))
        .collect();
    let expected = golden["expected_notification_events"].as_array().unwrap();
    assert_eq!(notif.len(), expected.len(), "notification event count");
    for (got, want) in notif.iter().zip(expected) {
        assert_eq!(got.rule_id, want["rule_id"].as_str().unwrap());
        assert_eq!(got.scope, parse_scope(want["scope"].as_str().unwrap()));
        assert_eq!(got.scope_id, want["scope_id"].as_str().unwrap());
        assert_eq!(got.decision, want["decision"].as_u64().unwrap() as u8);
    }
    // the script exercises: a mark-driven latch, a re-latch after a clear
    // without override, an override, a session roll and a malformed fill
    let ids: Vec<&str> = notif.iter().map(|e| e.rule_id.as_str()).collect();
    for must in [
        "LOSS_LIMIT_OVERRIDE",
        "SESSION_ROLLED",
        "MALFORMED_FILL",
        "DAILY_LOSS",
        "STRATEGY_LOSS",
    ] {
        assert!(ids.contains(&must), "golden must pin {must}");
    }
}

#[test]
fn audit_log_matches_golden_byte_for_byte() {
    let golden = load_json("tests/golden/expected_risk_decisions.json");
    let a = replay(&golden, false);
    let b = replay(&golden, false);
    assert_eq!(a, b, "audit must be deterministic");
    let want = std::fs::read_to_string(repo_path("tests/golden/expected_risk_audit.jsonl")).unwrap();
    assert_eq!(a, want, "audit JSONL must be byte-identical to the golden");
    // one audit line per decision + one per pinned notification event
    let n_orders = golden["steps"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|s| s["type"] == "order")
        .count();
    let n_notif = golden["expected_notification_events"].as_array().unwrap().len();
    assert_eq!(a.lines().count(), n_orders + n_notif);
}

#[test]
fn snapshot_restore_reproduces_the_audit_tail() {
    let golden = load_json("tests/golden/expected_risk_decisions.json");
    let steps = golden["steps"].as_array().unwrap();
    let k = golden["snapshot_after_step"].as_u64().unwrap() as usize;
    // 1. the engine's own snapshot after step k equals the golden snapshot
    let mut eng = build(&golden);
    for (i, step) in steps.iter().enumerate().take(k + 1) {
        apply(&mut eng, i, step, true);
    }
    let snap = eng.snapshot();
    let want = load_json("tests/golden/expected_risk_snapshot.json");
    assert_eq!(snap, want, "snapshot after step {k} must equal the golden");
    // 2. restoring the golden snapshot and replaying the rest reproduces
    //    the unbroken run's audit tail exactly (after the STATE_RESTORED line)
    let config = load_json(golden["config"].as_str().unwrap());
    let limits = RiskLimits::from_json(&config).unwrap();
    let mut restored = RiskEngine::restore(limits, instruments(&golden), &want, 0).unwrap();
    assert_eq!(restored.audit().len(), 1);
    assert_eq!(restored.audit()[0].rule_id, "STATE_RESTORED");
    for (i, step) in steps.iter().enumerate().skip(k + 1) {
        apply(&mut restored, i, step, true);
        apply(&mut eng, i, step, true);
    }
    let full = eng.audit_jsonl();
    let restored_audit = restored.audit_jsonl();
    let tail: Vec<&str> = restored_audit.lines().skip(1).collect();
    let full_lines: Vec<&str> = full.lines().collect();
    let expected_tail = &full_lines[full_lines.len() - tail.len()..];
    assert_eq!(tail, expected_tail);
    assert_eq!(restored.snapshot(), eng.snapshot(), "state converges");
}

#[test]
fn fixed_format_golden_cases() {
    let golden = load_json("tests/golden/expected_risk_decisions.json");
    let cases = golden["fixed_format_cases"].as_array().unwrap();
    assert!(cases.len() >= 10);
    for c in cases {
        let v = c[0].as_f64().unwrap();
        let d = c[1].as_u64().unwrap() as u32;
        assert_eq!(risk::fmt_fixed(v, d), c[2].as_str().unwrap(), "fmt_fixed({v}, {d})");
    }
}
