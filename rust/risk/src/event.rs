//! RiskEvent — the auditable decision record
//! (`schemas/risk/risk_event.schema.json` x-version 2, exact field set and
//! enum codes).
//!
//! Tamper evidence (review item G-P0-1): every record an engine emits
//! carries `seq` (1, 2, 3, ... per engine, continued across a restore when
//! the caller passes the chain head) and `prev_sha256`, the lowercase hex
//! SHA-256 of the previous record's JSONL line (UTF-8, no newline), or
//! [`GENESIS_SHA256`] for the first record. Editing, dropping, inserting or
//! reordering a line breaks the chain ([`verify_audit_chain`]).

use marketdata::IapError;
use serde::{Deserialize, Serialize};

/// Decision scope (schema enum).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Scope {
    /// Whole-firm scope (`scope_id` is "").
    #[serde(rename = "GLOBAL")]
    Global,
    /// One strategy (`scope_id` = strategy id).
    #[serde(rename = "STRATEGY")]
    Strategy,
    /// One instrument (`scope_id` = decimal instrument_id).
    #[serde(rename = "INSTRUMENT")]
    Instrument,
    /// One venue (`scope_id` = decimal venue_id).
    #[serde(rename = "VENUE")]
    Venue,
}

/// Severity codes (schema: INFO=1 WARN=2 BREACH=3).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
#[repr(u8)]
pub enum Severity {
    /// Routine (allowed orders, switch clears).
    Info = 1,
    /// A rejected order / degraded state.
    Warn = 2,
    /// A limit breach or kill-switch action.
    Breach = 3,
}

/// Decision codes (schema: ALLOW=1 REJECT=2 KILL=3).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum Decision {
    /// Order may proceed.
    Allow = 1,
    /// Order rejected.
    Reject = 2,
    /// A kill switch engaged / trading stopped in scope.
    Kill = 3,
}

/// `prev_sha256` of the first record of a chain (64 zeros).
pub const GENESIS_SHA256: &str =
    "0000000000000000000000000000000000000000000000000000000000000000";

/// One audit-log record. Field order matches the schema's canonical key
/// order and is preserved by the JSONL writer.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RiskEvent {
    /// Event time (ns) of the decision.
    pub timestamp: i64,
    /// Decision scope.
    pub scope: Scope,
    /// Identifier within scope ("" for GLOBAL).
    pub scope_id: String,
    /// Pinned rule identifier (see `rules` module docs).
    pub rule_id: String,
    /// INFO=1 WARN=2 BREACH=3.
    pub severity: u8,
    /// ALLOW=1 REJECT=2 KILL=3.
    pub decision: u8,
    /// Human-readable reason.
    pub reason: String,
    /// Position in the engine's audit chain (1-based; 0 = not yet emitted).
    #[serde(default)]
    pub seq: u64,
    /// Hex SHA-256 of the previous record's JSONL line ("" = not yet
    /// emitted).
    #[serde(default)]
    pub prev_sha256: String,
}

impl RiskEvent {
    /// Serialize as one JSONL line (schema key order, no trailing newline).
    pub fn to_json_line(&self) -> String {
        serde_json::json!({
            "timestamp": self.timestamp,
            "scope": match self.scope {
                Scope::Global => "GLOBAL",
                Scope::Strategy => "STRATEGY",
                Scope::Instrument => "INSTRUMENT",
                Scope::Venue => "VENUE",
            },
            "scope_id": self.scope_id,
            "rule_id": self.rule_id,
            "severity": self.severity,
            "decision": self.decision,
            "reason": self.reason,
            "seq": self.seq,
            "prev_sha256": self.prev_sha256,
        })
        .to_string()
    }

    /// Parse one JSONL audit line back into a RiskEvent.
    pub fn from_json_line(line: &str) -> Result<RiskEvent, IapError> {
        let ev: RiskEvent = serde_json::from_str(line)
            .map_err(|e| IapError::Codec(format!("bad RiskEvent line: {e}")))?;
        if !(1..=3).contains(&ev.severity) || !(1..=3).contains(&ev.decision) {
            return Err(IapError::Codec(format!(
                "RiskEvent enum out of domain: severity {} decision {}",
                ev.severity, ev.decision
            )));
        }
        Ok(ev)
    }
}

/// Lowercase hex SHA-256 of one audit line (UTF-8, without newline).
pub fn line_sha256(line: &str) -> String {
    contracts::sha256::sha256_hex(line.as_bytes())
}

/// Verify an audit JSONL chain: consecutive `seq` from `first_seq`, each
/// `prev_sha256` the hash of the line before (`prev_sha256` for the first).
/// Returns `(last seq, head sha256)`; the error names the first broken
/// line. Lines are split on LF only.
pub fn verify_audit_chain(
    text: &str,
    first_seq: u64,
    prev_sha256: &str,
) -> Result<(u64, String), String> {
    let mut seq = first_seq.wrapping_sub(1);
    let mut head = prev_sha256.to_string();
    let body = text.strip_suffix('\n').unwrap_or(text);
    if body.is_empty() {
        return Ok((seq, head));
    }
    for (n, line) in body.split('\n').enumerate() {
        let ev = RiskEvent::from_json_line(line).map_err(|e| e.to_string())?;
        if ev.seq != seq.wrapping_add(1) {
            return Err(format!(
                "audit chain broken at line {}: seq {}, want {}",
                n + 1,
                ev.seq,
                seq.wrapping_add(1)
            ));
        }
        if ev.prev_sha256 != head {
            return Err(format!(
                "audit chain broken at line {}: prev_sha256 mismatch",
                n + 1
            ));
        }
        seq = ev.seq;
        head = line_sha256(line);
    }
    Ok((seq, head))
}

/// `line` without its chain fields: the record's content, comparable between
/// an unbroken run and a restored one (whose chain positions are shifted by
/// the `STATE_RESTORED` record). A line that does not parse is returned
/// unchanged.
pub fn unchained_line(line: &str) -> String {
    match RiskEvent::from_json_line(line) {
        Ok(ev) => line
            .replacen(&format!(",\"prev_sha256\":\"{}\"", ev.prev_sha256), "", 1)
            .replacen(&format!(",\"seq\":{},", ev.seq), ",", 1),
        Err(_) => line.to_string(),
    }
}

/// Pinned rule identifiers, in pre-trade evaluation order. The FIRST
/// failing rule decides (deterministic; kill switches always take
/// precedence, global before strategy before instrument before venue).
pub mod rules {
    /// Global kill switch engaged.
    pub const KILL_GLOBAL: &str = "KILL_GLOBAL";
    /// Strategy kill switch engaged.
    pub const KILL_STRATEGY: &str = "KILL_STRATEGY";
    /// Instrument kill switch engaged.
    pub const KILL_INSTRUMENT: &str = "KILL_INSTRUMENT";
    /// Venue kill switch engaged.
    pub const KILL_VENUE: &str = "KILL_VENUE";
    /// Schema-level order validation failed.
    pub const MALFORMED_ORDER: &str = "MALFORMED_ORDER";
    /// No tick size / reference data for the instrument.
    pub const UNKNOWN_INSTRUMENT: &str = "UNKNOWN_INSTRUMENT";
    /// order_id already used inside the duplicate window.
    pub const DUPLICATE_ORDER_ID: &str = "DUPLICATE_ORDER_ID";
    /// Target venue is disconnected.
    pub const VENUE_DISCONNECTED: &str = "VENUE_DISCONNECTED";
    /// Market-data feed has an unrecovered sequence gap.
    pub const SEQUENCE_GAP: &str = "SEQUENCE_GAP";
    /// Reference price missing or older than the stale timeout.
    pub const STALE_PRICE: &str = "STALE_PRICE";
    /// Order quantity above the fat-finger cap.
    pub const FAT_FINGER_QTY: &str = "FAT_FINGER_QTY";
    /// Order notional above the fat-finger cap.
    pub const FAT_FINGER_NOTIONAL: &str = "FAT_FINGER_NOTIONAL";
    /// Limit price outside the band around the last mid.
    pub const PRICE_BAND: &str = "PRICE_BAND";
    /// Per-strategy event-time token bucket exhausted.
    pub const RATE_THROTTLE: &str = "RATE_THROTTLE";
    /// Order would cross an own resting order.
    pub const SELF_MATCH: &str = "SELF_MATCH";
    /// Projected position beyond the per-instrument cap.
    pub const POSITION_LIMIT: &str = "POSITION_LIMIT";
    /// Projected per-instrument notional beyond the cap.
    pub const INSTRUMENT_NOTIONAL: &str = "INSTRUMENT_NOTIONAL";
    /// Projected gross notional beyond the cap (fail-closed on missing marks).
    pub const GROSS_NOTIONAL: &str = "GROSS_NOTIONAL";
    /// Projected net notional beyond the cap.
    pub const NET_NOTIONAL: &str = "NET_NOTIONAL";
    /// Firm-wide daily loss limit (realized + unrealized) breached.
    pub const DAILY_LOSS: &str = "DAILY_LOSS";
    /// Per-strategy daily loss limit (realized + unrealized) breached.
    pub const STRATEGY_LOSS: &str = "STRATEGY_LOSS";
    /// Quote->reporting currency conversion rate missing or stale
    /// (fail-closed: notionals cannot be expressed in the reporting ccy).
    pub const FX_RATE_MISSING: &str = "FX_RATE_MISSING";
    /// Engine awaits a position bootstrap (drop-copy) or state restore.
    pub const NOT_BOOTSTRAPPED: &str = "NOT_BOOTSTRAPPED";
    /// Order passed every check.
    pub const ALLOW: &str = "ALLOW";
    /// Engine is fail-closed (missing/invalid configuration).
    pub const CONFIG_MISSING: &str = "CONFIG_MISSING";
    /// Manual kill switch engaged (audit record).
    pub const KILL_SWITCH_ENGAGED: &str = "KILL_SWITCH_ENGAGED";
    /// Manual kill switch cleared (audit record).
    pub const KILL_SWITCH_CLEARED: &str = "KILL_SWITCH_CLEARED";
    /// Venue disconnect notification (audit record).
    pub const VENUE_DISCONNECT: &str = "VENUE_DISCONNECT";
    /// Venue reconnect notification (audit record).
    pub const VENUE_RECONNECT: &str = "VENUE_RECONNECT";
    /// A fill was rejected as malformed / unpriceable (audit record; the
    /// fill is NOT applied and `risk_malformed_fills_total` increments).
    pub const MALFORMED_FILL: &str = "MALFORMED_FILL";
    /// A kill-switch command named a scope id the engine cannot resolve
    /// (audit record; `engage_kill`/`clear_kill` return an error and
    /// `risk_malformed_kills_total` increments). NEVER accompanied by a
    /// KILL_SWITCH_ENGAGED / KILL_SWITCH_CLEARED record — that pairing is
    /// exactly the phantom-halt defect this record exists to make visible.
    pub const MALFORMED_KILL: &str = "MALFORMED_KILL";
    /// A loss limit was overridden with approval (audit record).
    pub const LOSS_LIMIT_OVERRIDE: &str = "LOSS_LIMIT_OVERRIDE";
    /// The trading session rolled: daily P&L re-based (audit record).
    pub const SESSION_ROLLED: &str = "SESSION_ROLLED";
    /// Position bootstrap completed (audit record).
    pub const BOOTSTRAP_COMPLETE: &str = "BOOTSTRAP_COMPLETE";
    /// Engine state restored from a snapshot (audit record).
    pub const STATE_RESTORED: &str = "STATE_RESTORED";
    /// A loss-limit override was refused: too few distinct approvers, or
    /// above `overrides.max_loss_override_multiple` x the configured limit
    /// (audit record; the call returns an error).
    pub const LOSS_LIMIT_OVERRIDE_REJECTED: &str = "LOSS_LIMIT_OVERRIDE_REJECTED";
    /// Check 23: instrument on `pre_trade.restricted_instruments`.
    pub const RESTRICTED_INSTRUMENT: &str = "RESTRICTED_INSTRUMENT";
    /// Check 24: the strategy would exceed `pre_trade.max_open_orders`.
    pub const MAX_OPEN_ORDERS: &str = "MAX_OPEN_ORDERS";
    /// Check 25: the strategy's open notional would exceed
    /// `pre_trade.max_open_notional`.
    pub const MAX_OPEN_NOTIONAL: &str = "MAX_OPEN_NOTIONAL";
    /// Check 26: the strategy's projected position would exceed
    /// `pre_trade.max_strategy_position_qty`.
    pub const STRATEGY_POSITION: &str = "STRATEGY_POSITION";
    /// Check 27: the firm's projected session quantity would exceed the
    /// participation cap.
    pub const PARTICIPATION: &str = "PARTICIPATION";
    /// Check 28: the strategy's cancel budget is spent.
    pub const CANCEL_RATE: &str = "CANCEL_RATE";
    /// Check 29: the venue's message budget is spent.
    pub const VENUE_MESSAGE_RATE: &str = "VENUE_MESSAGE_RATE";
    /// Check 7: a bounded per-key state map is full
    /// (`pre_trade.max_tracked_keys`).
    pub const STATE_CAPACITY: &str = "STATE_CAPACITY";
    /// Check 31 (v1.17 F8): the order's venue type is not in
    /// `pre_trade.allowed_venue_types`.
    pub const VENUE_TYPE_RESTRICTED: &str = "VENUE_TYPE_RESTRICTED";
    /// Check 32 (v1.17 F8): the firm's projected session quantity routed to
    /// dark / midpoint venues would exceed the dark participation cap.
    pub const DARK_PARTICIPATION: &str = "DARK_PARTICIPATION";
    /// Check 33 (v1.17 F8): the firm's session dark-fill notional plus the
    /// order would exceed `pre_trade.max_dark_fill_notional`.
    pub const DARK_FILL_NOTIONAL: &str = "DARK_FILL_NOTIONAL";
}

/// Fixed-decimal formatting for audit reasons, PINNED for cross-language
/// byte parity (no floating-point formatting anywhere in a reason): the
/// value is scaled by `10^decimals`, rounded half away from zero to an
/// integer, and printed as `[-]int.frac` with exactly `decimals` fraction
/// digits. A value that rounds to zero prints without a sign. The Java
/// port (`RiskEngine.fmtFixed`) computes the identical integer from the
/// identical IEEE-754 product, so both engines emit identical text at
/// decimal ties (e.g. 2.675 -> "2.67" in BOTH, because 2.675*100 is
/// 267.49999999999997 in binary).
pub fn fmt_fixed(v: f64, decimals: u32) -> String {
    let scale_i: i64 = 10i64.pow(decimals);
    let scale = scale_i as f64;
    let units = (v.abs() * scale).round() as i64;
    let sign = if v < 0.0 && units > 0 { "-" } else { "" };
    if decimals == 0 {
        return format!("{sign}{units}");
    }
    format!(
        "{sign}{}.{:0width$}",
        units / scale_i,
        units % scale_i,
        width = decimals as usize
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn json_line_round_trips_with_schema_keys() {
        let ev = RiskEvent {
            seq: 0,
            prev_sha256: String::new(),
            timestamp: 42,
            scope: Scope::Instrument,
            scope_id: "1".to_string(),
            rule_id: rules::PRICE_BAND.to_string(),
            severity: Severity::Warn as u8,
            decision: Decision::Reject as u8,
            reason: "price 30 vs mid 24.51".to_string(),
        };
        let line = ev.to_json_line();
        // serde_json objects serialize in sorted key order; all 9 schema
        // keys must be present and nothing else.
        let v: serde_json::Value = serde_json::from_str(&line).unwrap();
        let keys: Vec<&str> = v.as_object().unwrap().keys().map(String::as_str).collect();
        assert_eq!(
            keys,
            [
                "decision",
                "prev_sha256",
                "reason",
                "rule_id",
                "scope",
                "scope_id",
                "seq",
                "severity",
                "timestamp"
            ]
        );
        let back = RiskEvent::from_json_line(&line).unwrap();
        assert_eq!(back, ev);
    }

    #[test]
    fn fixed_formatting_is_integer_scaled() {
        assert_eq!(fmt_fixed(2.675, 2), "2.68"); // 2.675*100 rounds to 267.5 in binary
        assert_eq!(fmt_fixed(32.4375, 2), "32.44"); // exact tie: half away
        assert_eq!(fmt_fixed(-50012.345, 2), "-50012.35");
        assert_eq!(fmt_fixed(-0.001, 2), "0.00");
        assert_eq!(fmt_fixed(1000000.125, 2), "1000000.13");
        assert_eq!(fmt_fixed(0.05, 1), "0.1");
        assert_eq!(fmt_fixed(500.0, 0), "500");
        assert_eq!(fmt_fixed(-7.0, 2), "-7.00");
    }

    #[test]
    fn bad_lines_are_rejected() {
        assert!(RiskEvent::from_json_line("{}").is_err());
        assert!(RiskEvent::from_json_line("not json").is_err());
        let bad_scope = r#"{"timestamp":1,"scope":"PLANET","scope_id":"","rule_id":"X","severity":1,"decision":1,"reason":""}"#;
        assert!(RiskEvent::from_json_line(bad_scope).is_err());
        let bad_sev = r#"{"timestamp":1,"scope":"GLOBAL","scope_id":"","rule_id":"X","severity":9,"decision":1,"reason":""}"#;
        assert!(RiskEvent::from_json_line(bad_sev).is_err());
    }
}
