//! Audit-log emission (hash-chained, G-P0-1) and full-state
//! snapshot/restore (schema x-version 2),
//! split out of `engine.rs` verbatim. This is an `impl RiskEngine`
//! continued from `engine.rs`.

use std::collections::BTreeMap;

use marketdata::IapError;
use serde_json::{json, Value};

use crate::engine::{
    Bucket, InstrumentRef, Lot, MarketState, OpenOrder, Rate, RiskEngine, SNAPSHOT_VERSION,
};
use crate::event::{line_sha256, rules, Decision, RiskEvent, Scope, Severity};
use crate::limits::RiskLimits;

impl RiskEngine {
    /// Full audit log as JSONL (one RiskEvent per line, trailing newline).
    pub fn audit_jsonl(&self) -> String {
        let mut out = String::new();
        for ev in &self.audit {
            out.push_str(&ev.to_json_line());
            out.push('\n');
        }
        out
    }

    pub(crate) fn emit(&mut self, mut ev: RiskEvent) {
        self.metrics.counter("risk_events_total").inc();
        self.audit_seq += 1;
        ev.seq = self.audit_seq;
        ev.prev_sha256 = std::mem::take(&mut self.audit_head);
        self.audit_head = line_sha256(&ev.to_json_line());
        self.audit.push(ev);
    }

    /// Serialize the full mutable state (positions, lots, realized P&L,
    /// kill/latch state, marks, open orders, throttle buckets, seen order
    /// ids, overrides, bootstrap flag) as a schema-versioned JSON document.
    /// The audit log and metrics are NOT part of the snapshot (the audit
    /// log is the external JSONL file; metrics restart).
    pub fn snapshot(&self) -> Value {
        let bool_map = |m: &BTreeMap<String, bool>| -> Value {
            Value::Object(m.iter().map(|(k, v)| (k.clone(), json!(v))).collect())
        };
        json!({
            "x-version": SNAPSHOT_VERSION,
            "bootstrapped": self.bootstrapped,
            "kill_global": self.kill_global,
            "kill_strategies": bool_map(&self.kill_strategies),
            "kill_instruments": Value::Object(self.kill_instruments.iter()
                .map(|(k, v)| (k.to_string(), json!(v))).collect()),
            "kill_venues": Value::Object(self.kill_venues.iter()
                .map(|(k, v)| (k.to_string(), json!(v))).collect()),
            "venues_down": Value::Object(self.venues_down.iter()
                .map(|(k, v)| (k.to_string(), json!(v))).collect()),
            "market": Value::Object(self.market.iter().map(|(k, m)| (k.to_string(), json!({
                "bid_ticks": m.bid_ticks, "ask_ticks": m.ask_ticks, "ts": m.ts,
                "gaps": m.gaps, "gated": m.gated }))).collect()),
            "seen_orders": self.seen_orders.iter()
                .map(|(id, ts)| json!([id, ts])).collect::<Vec<_>>(),
            "buckets": Value::Object(self.buckets.iter().map(|(k, b)| (k.clone(), json!({
                "tokens": b.tokens, "last_ts": b.last_ts, "primed": b.primed }))).collect()),
            "open": Value::Object(self.open.iter().map(|(k, o)| (k.to_string(), json!({
                "instrument_id": o.instrument_id, "side": o.side,
                "price_ticks": o.price_ticks, "qty": o.qty,
                "strategy_id": o.strategy_id }))).collect()),
            "positions": Value::Object(self.positions.iter()
                .map(|(k, v)| (k.to_string(), json!(v))).collect()),
            "lots": self.lots.iter().map(|((sid, iid), lot)| json!({
                "strategy_id": sid, "instrument_id": iid,
                "pos": lot.pos, "avg_price": lot.avg_price })).collect::<Vec<_>>(),
            "realized": self.realized.iter().map(|((sid, ccy), pnl)| json!({
                "strategy_id": sid, "ccy": ccy, "pnl": pnl })).collect::<Vec<_>>(),
            "loss_override_global": self.loss_override_global,
            "loss_override_strategy": Value::Object(self.loss_override_strategy.iter()
                .map(|(k, v)| (k.clone(), json!(v))).collect()),
            "venue_buckets": Value::Object(self.venue_buckets.iter().map(|(k, b)| (k.to_string(), json!({
                "tokens": b.tokens, "last_ts": b.last_ts }))).collect()),
            "cancel_buckets": Value::Object(self.cancel_buckets.iter().map(|(k, b)| (k.clone(), json!({
                "tokens": b.tokens, "last_ts": b.last_ts }))).collect()),
            "market_volume": Value::Object(self.market_volume.iter()
                .map(|(k, v)| (k.to_string(), json!(v))).collect()),
            "executed": Value::Object(self.executed.iter()
                .map(|(k, v)| (k.to_string(), json!(v))).collect()),
        })
    }

    /// Rebuild an engine from `limits`, `instruments` and a
    /// [`RiskEngine::snapshot`] document (strict: unknown version or a
    /// malformed field is an error, nothing is restored). Emits a
    /// `STATE_RESTORED` audit record stamped `ts`, the first record of a new
    /// audit chain (see [`RiskEngine::restore_chained`]).
    pub fn restore(
        limits: RiskLimits,
        instruments: BTreeMap<u32, InstrumentRef>,
        snap: &Value,
        ts: i64,
    ) -> Result<RiskEngine, IapError> {
        RiskEngine::restore_chained(limits, instruments, snap, ts, None)
    }

    /// [`RiskEngine::restore`] continuing the audit chain from `chain`, the
    /// `(seq, sha256)` [`RiskEngine::audit_chain_head`] of the engine that
    /// wrote the snapshot (the session store records it next to the
    /// snapshot). The head is deliberately not part of the snapshot: the
    /// snapshot is the trading state, identical for an unbroken and a
    /// restored engine.
    pub fn restore_chained(
        limits: RiskLimits,
        instruments: BTreeMap<u32, InstrumentRef>,
        snap: &Value,
        ts: i64,
        chain: Option<(u64, String)>,
    ) -> Result<RiskEngine, IapError> {
        let bad = |what: &str| IapError::InvalidArgument(format!("risk snapshot: bad {what}"));
        if snap["x-version"].as_u64() != Some(SNAPSHOT_VERSION) {
            return Err(bad("x-version"));
        }
        let mut eng = RiskEngine::new(limits, instruments);
        eng.bootstrapped = snap["bootstrapped"].as_bool().ok_or_else(|| bad("bootstrapped"))?;
        eng.kill_global = snap["kill_global"].as_bool().ok_or_else(|| bad("kill_global"))?;
        eng.metrics
            .gauge("risk_kill_switch_engaged")
            .set(if eng.kill_global { 1.0 } else { 0.0 });
        for (k, v) in snap["kill_strategies"].as_object().ok_or_else(|| bad("kill_strategies"))? {
            eng.kill_strategies
                .insert(k.clone(), v.as_bool().ok_or_else(|| bad("kill_strategies"))?);
        }
        for (k, v) in snap["kill_instruments"].as_object().ok_or_else(|| bad("kill_instruments"))? {
            eng.kill_instruments.insert(
                k.parse::<u32>().map_err(|_| bad("kill_instruments"))?,
                v.as_bool().ok_or_else(|| bad("kill_instruments"))?,
            );
        }
        for (k, v) in snap["kill_venues"].as_object().ok_or_else(|| bad("kill_venues"))? {
            eng.kill_venues.insert(
                k.parse::<u16>().map_err(|_| bad("kill_venues"))?,
                v.as_bool().ok_or_else(|| bad("kill_venues"))?,
            );
        }
        for (k, v) in snap["venues_down"].as_object().ok_or_else(|| bad("venues_down"))? {
            eng.venues_down.insert(
                k.parse::<u16>().map_err(|_| bad("venues_down"))?,
                v.as_bool().ok_or_else(|| bad("venues_down"))?,
            );
        }
        for (k, m) in snap["market"].as_object().ok_or_else(|| bad("market"))? {
            eng.market.insert(
                k.parse::<u32>().map_err(|_| bad("market"))?,
                MarketState {
                    bid_ticks: m["bid_ticks"].as_i64().ok_or_else(|| bad("market.bid_ticks"))?,
                    ask_ticks: m["ask_ticks"].as_i64().ok_or_else(|| bad("market.ask_ticks"))?,
                    ts: m["ts"].as_i64().ok_or_else(|| bad("market.ts"))?,
                    gaps: m["gaps"].as_u64().ok_or_else(|| bad("market.gaps"))?,
                    gated: m["gated"].as_bool().ok_or_else(|| bad("market.gated"))?,
                },
            );
        }
        for pair in snap["seen_orders"].as_array().ok_or_else(|| bad("seen_orders"))? {
            let id = pair[0].as_u64().ok_or_else(|| bad("seen_orders"))?;
            let t = pair[1].as_i64().ok_or_else(|| bad("seen_orders"))?;
            eng.seen_orders.insert(id, t);
        }
        for (k, b) in snap["buckets"].as_object().ok_or_else(|| bad("buckets"))? {
            eng.buckets.insert(
                k.clone(),
                Bucket {
                    tokens: b["tokens"].as_f64().ok_or_else(|| bad("buckets.tokens"))?,
                    last_ts: b["last_ts"].as_i64().ok_or_else(|| bad("buckets.last_ts"))?,
                    primed: b["primed"].as_bool().ok_or_else(|| bad("buckets.primed"))?,
                },
            );
        }
        for (k, o) in snap["open"].as_object().ok_or_else(|| bad("open"))? {
            let side = o["side"].as_u64().ok_or_else(|| bad("open.side"))?;
            if side > 1 {
                return Err(bad("open.side"));
            }
            eng.open.insert(
                k.parse::<u64>().map_err(|_| bad("open"))?,
                OpenOrder {
                    instrument_id: o["instrument_id"]
                        .as_u64()
                        .and_then(|v| u32::try_from(v).ok())
                        .ok_or_else(|| bad("open.instrument_id"))?,
                    side: side as u8,
                    price_ticks: o["price_ticks"].as_i64().ok_or_else(|| bad("open.price_ticks"))?,
                    qty: o["qty"].as_i64().ok_or_else(|| bad("open.qty"))?,
                    strategy_id: o["strategy_id"]
                        .as_str()
                        .ok_or_else(|| bad("open.strategy_id"))?
                        .to_string(),
                },
            );
        }
        for (k, v) in snap["positions"].as_object().ok_or_else(|| bad("positions"))? {
            eng.positions.insert(
                k.parse::<u32>().map_err(|_| bad("positions"))?,
                v.as_i64().ok_or_else(|| bad("positions"))?,
            );
        }
        for l in snap["lots"].as_array().ok_or_else(|| bad("lots"))? {
            let sid = l["strategy_id"].as_str().ok_or_else(|| bad("lots.strategy_id"))?;
            let iid = l["instrument_id"]
                .as_u64()
                .and_then(|v| u32::try_from(v).ok())
                .ok_or_else(|| bad("lots.instrument_id"))?;
            let avg = l["avg_price"].as_f64().ok_or_else(|| bad("lots.avg_price"))?;
            if !avg.is_finite() {
                return Err(bad("lots.avg_price"));
            }
            eng.lots.insert(
                (sid.to_string(), iid),
                Lot {
                    pos: l["pos"].as_i64().ok_or_else(|| bad("lots.pos"))?,
                    avg_price: avg,
                },
            );
        }
        for r in snap["realized"].as_array().ok_or_else(|| bad("realized"))? {
            let sid = r["strategy_id"].as_str().ok_or_else(|| bad("realized.strategy_id"))?;
            let ccy = r["ccy"].as_str().ok_or_else(|| bad("realized.ccy"))?;
            let pnl = r["pnl"].as_f64().ok_or_else(|| bad("realized.pnl"))?;
            if !pnl.is_finite() {
                return Err(bad("realized.pnl"));
            }
            eng.realized.insert((sid.to_string(), ccy.to_string()), pnl);
        }
        eng.loss_override_global = match &snap["loss_override_global"] {
            Value::Null => None,
            v => Some(v.as_f64().ok_or_else(|| bad("loss_override_global"))?),
        };
        for (k, v) in snap["loss_override_strategy"]
            .as_object()
            .ok_or_else(|| bad("loss_override_strategy"))?
        {
            eng.loss_override_strategy
                .insert(k.clone(), v.as_f64().ok_or_else(|| bad("loss_override_strategy"))?);
        }
        for (k, b) in snap["venue_buckets"].as_object().ok_or_else(|| bad("venue_buckets"))? {
            let tokens = b["tokens"].as_f64().ok_or_else(|| bad("venue_buckets.tokens"))?;
            if !tokens.is_finite() {
                return Err(bad("venue_buckets.tokens"));
            }
            eng.venue_buckets.insert(
                k.parse::<u16>().map_err(|_| bad("venue_buckets"))?,
                Rate {
                    tokens,
                    last_ts: b["last_ts"].as_i64().ok_or_else(|| bad("venue_buckets.last_ts"))?,
                },
            );
        }
        for (k, b) in snap["cancel_buckets"].as_object().ok_or_else(|| bad("cancel_buckets"))? {
            let tokens = b["tokens"].as_f64().ok_or_else(|| bad("cancel_buckets.tokens"))?;
            if !tokens.is_finite() {
                return Err(bad("cancel_buckets.tokens"));
            }
            eng.cancel_buckets.insert(
                k.clone(),
                Rate {
                    tokens,
                    last_ts: b["last_ts"].as_i64().ok_or_else(|| bad("cancel_buckets.last_ts"))?,
                },
            );
        }
        for (k, v) in snap["market_volume"].as_object().ok_or_else(|| bad("market_volume"))? {
            eng.market_volume.insert(
                k.parse::<u32>().map_err(|_| bad("market_volume"))?,
                v.as_i64().ok_or_else(|| bad("market_volume"))?,
            );
        }
        for (k, v) in snap["executed"].as_object().ok_or_else(|| bad("executed"))? {
            eng.executed.insert(
                k.parse::<u32>().map_err(|_| bad("executed"))?,
                v.as_i64().ok_or_else(|| bad("executed"))?,
            );
        }
        if let Some((seq, head)) = chain {
            if head.len() != 64 || !head.bytes().all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c)) {
                return Err(IapError::InvalidArgument(
                    "audit chain head must be 64 lowercase hex digits".to_string(),
                ));
            }
            eng.audit_seq = seq;
            eng.audit_head = head;
        }
        eng.refresh_pnl_gauges();
        eng.emit(RiskEvent {
            seq: 0,
            prev_sha256: String::new(),
            timestamp: ts,
            scope: Scope::Global,
            scope_id: String::new(),
            rule_id: rules::STATE_RESTORED.to_string(),
            severity: Severity::Info as u8,
            decision: Decision::Allow as u8,
            reason: format!(
                "restored snapshot v{SNAPSHOT_VERSION}: {} positions, {} open orders",
                eng.positions.values().filter(|p| **p != 0).count(),
                eng.open.len()
            ),
        });
        Ok(eng)
    }
}
