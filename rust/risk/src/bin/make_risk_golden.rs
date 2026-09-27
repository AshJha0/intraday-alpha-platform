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
//!
//! Error handling: this is a dev tool (not part of the library surface, not
//! reachable from feed-controlled runtime input), but a malformed or
//! hand-edited `expected_risk_decisions.json` step script should still fail
//! with a message naming the step and the field, not a bare panic. The
//! platform's Rust dependency set is `serde`/`serde_json` only (no `anyhow`,
//! PLATFORM_CONVENTIONS.md §10 / SECURITY.md §1), so field access goes
//! through the small `field_*` helpers below, which return `Result<_, String>`
//! with the offending step and key in the message; `run()` propagates with
//! `?` and `main()` prints the error and exits 1.

use std::collections::BTreeMap;
use std::fs;

use risk::{Decision, Fill, InstrumentRef, RiskEngine, Scope};
use serde_json::{json, Map, Value};
use venue::OrderRequest;

/// Read a required `u64` field, with a message naming `ctx` and `key`.
fn field_u64(v: &Value, key: &str, ctx: &str) -> Result<u64, String> {
    v.get(key)
        .and_then(Value::as_u64)
        .ok_or_else(|| format!("{ctx}: field `{key}` missing or not a non-negative integer (got {})", v.get(key).cloned().unwrap_or(Value::Null)))
}

/// Read a required `i64` field, with a message naming `ctx` and `key`.
fn field_i64(v: &Value, key: &str, ctx: &str) -> Result<i64, String> {
    v.get(key)
        .and_then(Value::as_i64)
        .ok_or_else(|| format!("{ctx}: field `{key}` missing or not an integer (got {})", v.get(key).cloned().unwrap_or(Value::Null)))
}

/// Read a required `f64` field, with a message naming `ctx` and `key`.
fn field_f64(v: &Value, key: &str, ctx: &str) -> Result<f64, String> {
    v.get(key)
        .and_then(Value::as_f64)
        .ok_or_else(|| format!("{ctx}: field `{key}` missing or not a number (got {})", v.get(key).cloned().unwrap_or(Value::Null)))
}

/// Read a required string field, with a message naming `ctx` and `key`.
fn field_str<'a>(v: &'a Value, key: &str, ctx: &str) -> Result<&'a str, String> {
    v.get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("{ctx}: field `{key}` missing or not a string (got {})", v.get(key).cloned().unwrap_or(Value::Null)))
}

fn parse_order(v: &Value) -> Result<OrderRequest, String> {
    let ctx = "order step";
    Ok(OrderRequest {
        order_id: field_u64(v, "order_id", ctx)?,
        instrument_id: field_u64(v, "instrument_id", ctx)? as u32,
        side: field_u64(v, "side", ctx)? as u8,
        qty: field_i64(v, "qty", ctx)?,
        price_ticks: field_i64(v, "price_ticks", ctx)?,
        order_type: field_u64(v, "order_type", ctx)? as u8,
        venue_id: field_u64(v, "venue_id", ctx)? as u16,
        strategy_id: field_str(v, "strategy_id", ctx)?.to_string(),
        urgency: field_f64(v, "urgency", ctx)?,
        timestamp: field_i64(v, "timestamp", ctx)?,
    })
}

fn parse_scope(s: &str) -> Result<Scope, String> {
    match s {
        "GLOBAL" => Ok(Scope::Global),
        "STRATEGY" => Ok(Scope::Strategy),
        "INSTRUMENT" => Ok(Scope::Instrument),
        "VENUE" => Ok(Scope::Venue),
        other => Err(format!("bad scope `{other}` (expected GLOBAL/STRATEGY/INSTRUMENT/VENUE)")),
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

fn instruments(golden: &Value) -> Result<BTreeMap<u32, InstrumentRef>, String> {
    let mut out = BTreeMap::new();
    let obj = golden
        .get("instruments")
        .and_then(Value::as_object)
        .ok_or_else(|| "golden JSON: top-level `instruments` missing or not an object".to_string())?;
    for (iid, spec) in obj {
        let ctx = format!("instruments[{iid}]");
        let id: u32 = iid
            .parse()
            .map_err(|e| format!("instruments key `{iid}` is not a valid u32 instrument id: {e}"))?;
        out.insert(
            id,
            InstrumentRef::new(
                field_f64(spec, "tick_size", &ctx)?,
                field_f64(spec, "qty_unit", &ctx)?,
                field_str(spec, "quote_ccy", &ctx)?,
            ),
        );
    }
    Ok(out)
}

/// Apply one non-order step to the engine.
pub fn apply_step(eng: &mut RiskEngine, step: &Value, step_index: usize) -> Result<(), String> {
    let ty = field_str(step, "type", &format!("step[{step_index}]"))?.to_string();
    let ctx = format!("step[{step_index}] (type={ty})");
    let ts = || field_i64(step, "ts", &ctx);
    match ty.as_str() {
        "market" => eng.on_market(
            field_u64(step, "instrument_id", &ctx)? as u32,
            field_i64(step, "bid_ticks", &ctx)?,
            field_i64(step, "ask_ticks", &ctx)?,
            ts()?,
        ),
        "fill" => {
            eng.on_fill(&Fill {
                ts: ts()?,
                strategy_id: field_str(step, "strategy_id", &ctx)?.to_string(),
                instrument_id: field_u64(step, "instrument_id", &ctx)? as u32,
                order_id: field_u64(step, "order_id", &ctx)?,
                side: field_u64(step, "side", &ctx)? as u8,
                qty: field_i64(step, "qty", &ctx)?,
                price_ticks: field_i64(step, "price_ticks", &ctx)?,
            });
        }
        "cancel" => eng.on_order_done(field_u64(step, "order_id", &ctx)?),
        "gap" => eng.on_sequence_gap(field_u64(step, "instrument_id", &ctx)? as u32, ts()?),
        "recover" => eng.on_feed_recovered(field_u64(step, "instrument_id", &ctx)? as u32, ts()?),
        "venue_down" => eng.on_venue_disconnect(field_u64(step, "venue_id", &ctx)? as u16, ts()?),
        "venue_up" => eng.on_venue_reconnect(field_u64(step, "venue_id", &ctx)? as u16, ts()?),
        "kill" => eng
            .engage_kill(
                parse_scope(field_str(step, "scope", &ctx)?)?,
                field_str(step, "scope_id", &ctx)?,
                ts()?,
                step.get("reason").and_then(Value::as_str).unwrap_or(""),
            )
            .map_err(|e| format!("{ctx}: engine rejected kill step: {e:?}"))?,
        "unkill" => eng
            .clear_kill(
                parse_scope(field_str(step, "scope", &ctx)?)?,
                field_str(step, "scope_id", &ctx)?,
                ts()?,
                step.get("reason").and_then(Value::as_str).unwrap_or(""),
            )
            .map_err(|e| format!("{ctx}: engine rejected unkill step: {e:?}"))?,
        "override_loss" => eng
            .override_loss_limit(
                parse_scope(field_str(step, "scope", &ctx)?)?,
                field_str(step, "scope_id", &ctx)?,
                field_f64(step, "new_limit", &ctx)?,
                ts()?,
                step.get("approver").and_then(Value::as_str).unwrap_or(""),
            )
            .map_err(|e| format!("{ctx}: engine rejected override_loss step: {e:?}"))?,
        "roll_session" => eng.roll_session(ts()?, step.get("reason").and_then(Value::as_str).unwrap_or("")),
        other => return Err(format!("{ctx}: unknown step type `{other}`")),
    }
    Ok(())
}

fn run() -> Result<(), String> {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 2 {
        return Err(format!("usage: {} <golden_dir> [--force]", args[0]));
    }
    let dir = std::path::PathBuf::from(&args[1]);
    let force = args.iter().any(|a| a == "--force");
    let audit_path = dir.join("expected_risk_audit.jsonl");
    let snap_path = dir.join("expected_risk_snapshot.json");
    if !force && (audit_path.exists() || snap_path.exists()) {
        return Err("golden files exist — pinned; pass --force after a MIGRATIONS.md entry".to_string());
    }
    let dec_path = dir.join("expected_risk_decisions.json");
    let dec_text = fs::read_to_string(&dec_path)
        .map_err(|e| format!("reading step script {}: {e}", dec_path.display()))?;
    let mut golden: Value = serde_json::from_str(&dec_text)
        .map_err(|e| format!("parsing step script {} as JSON: {e}", dec_path.display()))?;
    let config_rel = field_str(&golden, "config", "golden JSON")?;
    let config_path = dir.join("../..").join(config_rel);
    let config_text = fs::read_to_string(&config_path)
        .map_err(|e| format!("reading risk config {}: {e}", config_path.display()))?;
    let config: Value = serde_json::from_str(&config_text)
        .map_err(|e| format!("parsing risk config {} as JSON: {e}", config_path.display()))?;
    let snapshot_after = field_u64(&golden, "snapshot_after_step", "golden JSON")? as usize;
    let mut eng = RiskEngine::from_config(&config, instruments(&golden)?);
    let mut snapshot: Option<Value> = None;
    let steps = golden
        .get_mut("steps")
        .and_then(Value::as_array_mut)
        .ok_or_else(|| "golden JSON: top-level `steps` missing or not an array".to_string())?;
    let mut n_orders = 0;
    for (i, step) in steps.iter_mut().enumerate() {
        if step["type"] == "order" {
            let order = step
                .get("order")
                .ok_or_else(|| format!("step[{i}]: order step missing `order` object"))?;
            let d = eng.check_order(&parse_order(order)?);
            step["expect"] = json!({
                "decision": d.decision as u8,
                "rule_id": d.rule_id,
                "severity": d.severity as u8,
            });
            n_orders += 1;
        } else {
            apply_step(&mut eng, step, i)?;
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
            m.insert(
                "scope".into(),
                serde_json::to_value(e.scope)
                    .unwrap_or_else(|_| Value::String("UNKNOWN".to_string())),
            );
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
    golden
        .as_object_mut()
        .ok_or_else(|| "golden JSON: top level is not an object".to_string())?
        .remove("expected_kill_events");
    let dec_out = serde_json::to_string_pretty(&golden)
        .map_err(|e| format!("serializing {}: {e}", dec_path.display()))?;
    fs::write(&dec_path, dec_out + "\n").map_err(|e| format!("writing {}: {e}", dec_path.display()))?;
    fs::write(&audit_path, eng.audit_jsonl())
        .map_err(|e| format!("writing {}: {e}", audit_path.display()))?;
    let snap_out = serde_json::to_string_pretty(
        &snapshot.ok_or_else(|| format!("snapshot_after_step {snapshot_after} is out of range for {} steps", eng.audit().len()))?,
    )
    .map_err(|e| format!("serializing {}: {e}", snap_path.display()))?;
    fs::write(&snap_path, snap_out + "\n").map_err(|e| format!("writing {}: {e}", snap_path.display()))?;
    println!(
        "wrote {} ({n_orders} orders, {} audit lines, snapshot after step {snapshot_after})",
        dec_path.display(),
        eng.audit().len()
    );
    Ok(())
}

fn main() {
    if let Err(e) = run() {
        eprintln!("make_risk_golden: {e}");
        std::process::exit(1);
    }
}
