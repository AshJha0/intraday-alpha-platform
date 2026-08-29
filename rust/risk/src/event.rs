//! RiskEvent — the auditable decision record
//! (`schemas/risk_event.schema.json`, exact field set and enum codes).

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
    /// Firm-wide realized daily loss limit breached.
    pub const DAILY_LOSS: &str = "DAILY_LOSS";
    /// Per-strategy realized loss limit breached.
    pub const STRATEGY_LOSS: &str = "STRATEGY_LOSS";
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
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn json_line_round_trips_with_schema_keys() {
        let ev = RiskEvent {
            timestamp: 42,
            scope: Scope::Instrument,
            scope_id: "1".to_string(),
            rule_id: rules::PRICE_BAND.to_string(),
            severity: Severity::Warn as u8,
            decision: Decision::Reject as u8,
            reason: "price 30 vs mid 24.51".to_string(),
        };
        let line = ev.to_json_line();
        // serde_json objects serialize in sorted key order; all 7 schema
        // keys must be present and nothing else.
        let v: serde_json::Value = serde_json::from_str(&line).unwrap();
        let keys: Vec<&str> = v.as_object().unwrap().keys().map(String::as_str).collect();
        assert_eq!(
            keys,
            ["decision", "reason", "rule_id", "scope", "scope_id", "severity", "timestamp"]
        );
        let back = RiskEvent::from_json_line(&line).unwrap();
        assert_eq!(back, ev);
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
