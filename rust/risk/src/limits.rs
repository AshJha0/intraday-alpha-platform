//! Hard-risk limits (`configs/risk.json`, x-version 2 — the complete
//! pinned limit set). Parsing is STRICT: any missing or invalid limit is
//! an error, and the engine built from a failed parse is fail-closed
//! (rejects every order with `CONFIG_MISSING`).

use marketdata::IapError;

/// The complete pinned limit set.
#[derive(Debug, Clone, PartialEq)]
pub struct RiskLimits {
    /// Start with the global kill switch engaged.
    pub kill_switch_engaged: bool,
    /// Firm-wide gross notional cap (currency).
    pub max_gross_notional: f64,
    /// Firm-wide |net| notional cap (currency).
    pub max_net_notional: f64,
    /// Firm-wide realized daily loss limit (positive number).
    pub max_daily_loss: f64,
    /// Token-bucket refill rate per strategy (orders/second).
    pub max_order_rate_per_sec: f64,
    /// Token-bucket capacity per strategy.
    pub order_rate_burst: f64,
    /// Fat-finger quantity cap per order.
    pub max_order_qty: i64,
    /// Fat-finger notional cap per order (currency).
    pub max_order_notional: f64,
    /// Price band around the last mid (bps).
    pub price_band_bps: f64,
    /// Reject orders when the reference price is stale.
    pub stale_book_reject: bool,
    /// Duplicate-order-id window (ns); 0 = the entire session.
    pub duplicate_order_window_ns: i64,
    /// Per-instrument absolute position cap.
    pub max_position_qty: i64,
    /// Per-instrument absolute marked-notional cap (currency).
    pub max_instrument_notional: f64,
    /// Per-strategy realized loss limit (positive number).
    pub strategy_max_daily_loss: f64,
    /// Sequence gaps tolerated before the feed gate closes.
    pub max_sequence_gap_before_halt: u64,
    /// Reference-price staleness timeout (ns).
    pub stale_feed_timeout_ns: i64,
}

fn need_f64(doc: &serde_json::Value, section: &str, key: &str) -> Result<f64, IapError> {
    let v = doc[section][key].as_f64().ok_or_else(|| {
        IapError::InvalidArgument(format!("risk.json: missing/non-numeric {section}.{key}"))
    })?;
    if !v.is_finite() {
        return Err(IapError::InvalidArgument(format!(
            "risk.json: non-finite {section}.{key}"
        )));
    }
    Ok(v)
}

fn need_pos_f64(doc: &serde_json::Value, section: &str, key: &str) -> Result<f64, IapError> {
    let v = need_f64(doc, section, key)?;
    if v <= 0.0 {
        return Err(IapError::InvalidArgument(format!(
            "risk.json: {section}.{key} must be > 0, got {v}"
        )));
    }
    Ok(v)
}

fn need_pos_i64(doc: &serde_json::Value, section: &str, key: &str) -> Result<i64, IapError> {
    let v = doc[section][key].as_i64().ok_or_else(|| {
        IapError::InvalidArgument(format!("risk.json: missing/non-integer {section}.{key}"))
    })?;
    if v <= 0 {
        return Err(IapError::InvalidArgument(format!(
            "risk.json: {section}.{key} must be > 0, got {v}"
        )));
    }
    Ok(v)
}

fn need_bool(doc: &serde_json::Value, section: &str, key: &str) -> Result<bool, IapError> {
    doc[section][key].as_bool().ok_or_else(|| {
        IapError::InvalidArgument(format!("risk.json: missing/non-bool {section}.{key}"))
    })
}

impl RiskLimits {
    /// Strict parse of a `configs/risk.json` document.
    pub fn from_json(doc: &serde_json::Value) -> Result<RiskLimits, IapError> {
        let dup = doc["per_order"]["duplicate_order_window_ns"]
            .as_i64()
            .ok_or_else(|| {
                IapError::InvalidArgument(
                    "risk.json: missing per_order.duplicate_order_window_ns".to_string(),
                )
            })?;
        if dup < 0 {
            return Err(IapError::InvalidArgument(
                "risk.json: duplicate_order_window_ns must be >= 0".to_string(),
            ));
        }
        Ok(RiskLimits {
            kill_switch_engaged: need_bool(doc, "global", "kill_switch_engaged")?,
            max_gross_notional: need_pos_f64(doc, "global", "max_gross_notional")?,
            max_net_notional: need_pos_f64(doc, "global", "max_net_notional")?,
            max_daily_loss: need_pos_f64(doc, "global", "max_daily_loss")?,
            max_order_rate_per_sec: need_pos_f64(doc, "global", "max_order_rate_per_sec")?,
            order_rate_burst: need_pos_f64(doc, "global", "order_rate_burst")?,
            max_order_qty: need_pos_i64(doc, "per_order", "max_order_qty")?,
            max_order_notional: need_pos_f64(doc, "per_order", "max_order_notional")?,
            price_band_bps: need_pos_f64(doc, "per_order", "price_band_bps")?,
            stale_book_reject: need_bool(doc, "per_order", "stale_book_reject")?,
            duplicate_order_window_ns: dup,
            max_position_qty: need_pos_i64(doc, "per_instrument", "max_position_qty")?,
            max_instrument_notional: need_pos_f64(doc, "per_instrument", "max_instrument_notional")?,
            strategy_max_daily_loss: need_pos_f64(doc, "per_strategy", "max_daily_loss")?,
            max_sequence_gap_before_halt: doc["market_data"]["max_sequence_gap_before_halt"]
                .as_u64()
                .ok_or_else(|| {
                    IapError::InvalidArgument(
                        "risk.json: missing market_data.max_sequence_gap_before_halt".to_string(),
                    )
                })?,
            stale_feed_timeout_ns: need_pos_i64(doc, "market_data", "stale_feed_timeout_ns")?,
        })
    }

    /// Load and strictly parse a risk.json file.
    pub fn load<P: AsRef<std::path::Path>>(path: P) -> Result<RiskLimits, IapError> {
        let text = std::fs::read_to_string(path.as_ref())?;
        let doc: serde_json::Value = serde_json::from_str(&text)
            .map_err(|e| IapError::Codec(format!("risk.json: {e}")))?;
        RiskLimits::from_json(&doc)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn repo_risk_json() -> serde_json::Value {
        let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../configs/risk.json");
        serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
    }

    #[test]
    fn repo_config_parses_completely() {
        let limits = RiskLimits::from_json(&repo_risk_json()).expect("complete pinned set");
        assert!(limits.max_order_qty > 0);
        assert!(limits.max_order_notional > 0.0);
        assert!(!limits.kill_switch_engaged);
        assert_eq!(limits.duplicate_order_window_ns, 0); // whole session
    }

    #[test]
    fn missing_limit_is_a_config_error() {
        let mut doc = repo_risk_json();
        doc["per_order"]
            .as_object_mut()
            .unwrap()
            .remove("max_order_qty");
        assert!(RiskLimits::from_json(&doc).is_err());
    }

    #[test]
    fn invalid_limit_is_a_config_error() {
        let mut doc = repo_risk_json();
        doc["global"]["max_daily_loss"] = serde_json::json!(-5.0);
        assert!(RiskLimits::from_json(&doc).is_err());
        let mut doc = repo_risk_json();
        doc["per_order"]["max_order_qty"] = serde_json::json!("many");
        assert!(RiskLimits::from_json(&doc).is_err());
    }
}
