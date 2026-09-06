//! Generator for the hard-risk goldens (run manually; the files are pinned
//! and regeneration requires a schemas/MIGRATIONS.md entry):
//!
//! - `tests/golden/expected_risk_decisions.json` — the step script with
//!   every order step's `expect` filled in from the normative Rust engine
//!   and `expected_notification_events` (the non-decision audit records)
//!   regenerated;
//! - `tests/golden/expected_risk_audit.jsonl` — the byte-exact audit log
//!   of the replay (every port must reproduce it byte for byte);
//! - `tests/golden/expected_risk_snapshot.json` — the engine snapshot
//!   taken after step `snapshot_after_step` (every port must restore it
//!   and produce the identical audit tail).
//!
//! Usage: make_risk_golden <golden_dir> [--force]
//!
//! Without `--force` the tool refuses to overwrite an existing audit /
//! snapshot golden. The decisions file is read as the step SCRIPT (its
//! `expect` blocks are recomputed) — so editing the script and re-running
//! is the workflow; every changed expectation must be hand-verified against
//! the pinned rules before the result is committed.

use std::collections::BTreeMap;
use std::fs;

use risk::{Decision, Fill, InstrumentRef, RiskEngine, Scope};
use serde_json::{json, Map, Value};
use venue::OrderRequest;

fn parse_order(v: &Value) -> OrderRequest {
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

/// Rule ids that are notification (non-decision) audit records.
pub fn is_notification(rule_id: &str, decision: u8) -> bool {
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

fn instruments(golden: &Value) -> BTreeMap<u32, InstrumentRef> {
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

/// Apply one non-order step to the engine.
pub fn apply_step(eng: &mut RiskEngine, step: &Value) {
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
            .expect("valid override step"),
        "roll_session" => eng.roll_session(ts(), step["reason"].as_str().unwrap_or("")),
        other => panic!("unknown step type {other}"),
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 2 {
        eprintln!("usage: {} <golden_dir> [--force]", args[0]);
        std::process::exit(2);
    }
    let dir = std::path::PathBuf::from(&args[1]);
    let force = args.iter().any(|a| a == "--force");
    let audit_path = dir.join("expected_risk_audit.jsonl");
    let snap_path = dir.join("expected_risk_snapshot.json");
    if !force && (audit_path.exists() || snap_path.exists()) {
        eprintln!("golden files exist — pinned; pass --force after a MIGRATIONS.md entry");
        std::process::exit(1);
    }
    let dec_path = dir.join("expected_risk_decisions.json");
    let mut golden: Value =
        serde_json::from_str(&fs::read_to_string(&dec_path).expect("read step script"))
            .expect("parse step script");
    let config: Value = serde_json::from_str(
        &fs::read_to_string(dir.join("../..").join(golden["config"].as_str().unwrap())).unwrap(),
    )
    .unwrap();
    let snapshot_after = golden["snapshot_after_step"].as_u64().expect("snapshot_after_step") as usize;
    let mut eng = RiskEngine::from_config(&config, instruments(&golden));
    let mut snapshot: Option<Value> = None;
    let steps = golden["steps"].as_array_mut().unwrap();
    let mut n_orders = 0;
    for (i, step) in steps.iter_mut().enumerate() {
        if step["type"] == "order" {
            let d = eng.check_order(&parse_order(&step["order"]));
            step["expect"] = json!({
                "decision": d.decision as u8,
                "rule_id": d.rule_id,
                "severity": d.severity as u8,
            });
            n_orders += 1;
        } else {
            apply_step(&mut eng, step);
        }
        if i == snapshot_after {
            snapshot = Some(eng.snapshot());
        }
    }
    let notif: Vec<Value> = eng
        .audit()
        .iter()
        .filter(|e| is_notification(&e.rule_id, e.decision))
        .map(|e| {
            let mut m = Map::new();
            m.insert("decision".into(), json!(e.decision));
            m.insert("rule_id".into(), json!(e.rule_id));
            m.insert("scope".into(), serde_json::to_value(e.scope).unwrap());
            m.insert("scope_id".into(), json!(e.scope_id));
            Value::Object(m)
        })
        .collect();
    golden["expected_notification_events"] = Value::Array(notif);
    // Decimal-tie formatting cases: pinned inputs, outputs computed by the
    // reference fmt_fixed (every port must reproduce them exactly).
    let tie_inputs: [(f64, u32); 12] = [
        (2.675, 2),
        (32.4375, 2),
        (-50012.345, 2),
        (1000000.125, 2),
        (0.125, 2),
        (-0.125, 2),
        (2.5, 0),
        (-2.5, 0),
        (-0.001, 2),
        (0.05, 1),
        (24.505, 2),
        (999000.0000000001, 2),
    ];
    golden["fixed_format_cases"] = Value::Array(
        tie_inputs
            .iter()
            .map(|(v, d)| json!([v, d, risk::fmt_fixed(*v, *d)]))
            .collect(),
    );
    golden.as_object_mut().unwrap().remove("expected_kill_events");
    fs::write(&dec_path, serde_json::to_string_pretty(&golden).unwrap() + "\n").unwrap();
    fs::write(&audit_path, eng.audit_jsonl()).unwrap();
    fs::write(
        &snap_path,
        serde_json::to_string_pretty(&snapshot.expect("snapshot step in range")).unwrap() + "\n",
    )
    .unwrap();
    println!(
        "wrote {} ({n_orders} orders, {} audit lines, snapshot after step {snapshot_after})",
        dec_path.display(),
        eng.audit().len()
    );
}
