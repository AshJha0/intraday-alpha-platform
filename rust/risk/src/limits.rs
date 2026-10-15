//! Hard-risk limits (`configs/risk/risk.json`, x-version 4 — the complete
//! pinned limit set plus the currency block). Parsing is STRICT: any
//! missing or invalid limit is an error, and the engine built from a
//! failed parse is fail-closed (rejects every order with `CONFIG_MISSING`).
//!
//! x-version 4 adds OPTIONAL keys (absent or null = that control is off; a
//! present key is parsed strictly): `market_data.max_mid_jump_ratio`, the
//! SEC 15c3-5 `pre_trade` set and the `overrides` bounds. v1.17 (F8) adds,
//! still optional and parsed last: `pre_trade.venue_types` (the lit / dark /
//! midpoint type of each venue id), `pre_trade.allowed_venue_types`
//! (check 31), `pre_trade.max_dark_participation_bps` +
//! `dark_participation_floor_qty` (check 32) and
//! `pre_trade.max_dark_fill_notional` (check 33).

use std::collections::{BTreeMap, BTreeSet};

use marketdata::IapError;

/// How one quote currency converts into the reporting currency: the last
/// consolidated mid of `instrument_id` (an FX pair), inverted when the
/// pair is quoted as `REPORTING/CCY` (e.g. USD/JPY for JPY -> USD).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FxConversion {
    /// The FX pair whose mid is the conversion rate.
    pub instrument_id: u32,
    /// `true`: rate = 1 / mid (pair quoted REPORTING/CCY); `false`: rate = mid.
    pub invert: bool,
}

/// The type of a venue as the pre-trade controls see it (v1.17 F8, the
/// `VenueSpec.venue_type` vocabulary of the B10 dark-venue model). Every
/// venue is `Lit` unless `pre_trade.venue_types` says otherwise. The
/// declaration order is the pinned order of the type names in reasons.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum VenueType {
    /// A displayed (lit) book.
    Lit,
    /// A non-displayed pool.
    Dark,
    /// A midpoint-matching venue.
    Midpoint,
}

impl VenueType {
    /// The pinned name (`risk.json` value and reason text).
    pub const fn as_str(self) -> &'static str {
        match self {
            VenueType::Lit => "lit",
            VenueType::Dark => "dark",
            VenueType::Midpoint => "midpoint",
        }
    }

    /// Parse the pinned name; `None` for anything else.
    pub fn parse(s: &str) -> Option<VenueType> {
        match s {
            "lit" => Some(VenueType::Lit),
            "dark" => Some(VenueType::Dark),
            "midpoint" => Some(VenueType::Midpoint),
            _ => None,
        }
    }

    /// True for every non-displayed type (dark and midpoint): the venues
    /// the dark-first controls (checks 32 and 33) count.
    pub const fn is_dark(self) -> bool {
        !matches!(self, VenueType::Lit)
    }
}

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
    /// Currency every limit and P&L figure is expressed in (pinned "USD"
    /// in the repo config).
    pub reporting_ccy: String,
    /// Quote-currency -> conversion source (`currency.conversion`).
    /// A quote currency equal to `reporting_ccy` needs no entry.
    pub fx_conversion: BTreeMap<String, FxConversion>,
    /// `market_data.max_mid_jump_ratio` (> 1): a two-sided update whose
    /// `bid + ask` is more than this factor above / below the stored
    /// two-sided one is dropped (G-P2-6). Crossed books are always dropped.
    pub max_mid_jump_ratio: Option<f64>,
    /// `pre_trade.restricted_instruments` (check 23).
    pub restricted_instruments: BTreeSet<u32>,
    /// `pre_trade.max_open_orders` per strategy (check 24).
    pub max_open_orders: Option<i64>,
    /// `pre_trade.max_open_notional` per strategy (check 25).
    pub max_open_notional: Option<f64>,
    /// `pre_trade.max_strategy_position_qty` per (strategy, instrument)
    /// (check 26).
    pub max_strategy_position_qty: Option<i64>,
    /// `pre_trade.max_participation_bps` of session market volume (check 27).
    pub max_participation_bps: Option<f64>,
    /// `pre_trade.participation_floor_qty` (required with the bps).
    pub participation_floor_qty: Option<i64>,
    /// `pre_trade.max_cancels_per_sec` per strategy (check 28).
    pub max_cancels_per_sec: Option<f64>,
    /// `pre_trade.cancel_burst` (required with the rate).
    pub cancel_burst: Option<f64>,
    /// `pre_trade.max_venue_msgs_per_sec` per venue (check 29).
    pub max_venue_msgs_per_sec: Option<f64>,
    /// `pre_trade.venue_msg_burst` (required with the rate).
    pub venue_msg_burst: Option<f64>,
    /// `pre_trade.max_tracked_keys`: bound of every per-key state map
    /// (G-P2-7).
    pub max_tracked_keys: Option<i64>,
    /// `overrides.max_loss_override_multiple` x the configured loss limit
    /// caps an override (G-P2-3).
    pub max_loss_override_multiple: Option<f64>,
    /// `overrides.min_approvers`: distinct comma-separated approvers an
    /// override needs (G-P2-3).
    pub min_approvers: Option<i64>,
    /// `pre_trade.venue_types` (v1.17 F8): venue id -> type; an unmapped
    /// venue is `Lit`. Absent / null = empty.
    pub venue_types: BTreeMap<u16, VenueType>,
    /// `pre_trade.allowed_venue_types` (check 31): the venue types an order
    /// may be routed to; `None` = off.
    pub allowed_venue_types: Option<BTreeSet<VenueType>>,
    /// `pre_trade.max_dark_participation_bps` (check 32): cap of the
    /// firm's session quantity routed to dark / midpoint venues, in bps of
    /// the instrument's session market volume.
    pub max_dark_participation_bps: Option<f64>,
    /// `pre_trade.dark_participation_floor_qty` (required with the bps).
    pub dark_participation_floor_qty: Option<i64>,
    /// `pre_trade.max_dark_fill_notional` (check 33): cap of the firm's
    /// session notional FILLED on dark / midpoint venues plus the order.
    pub max_dark_fill_notional: Option<f64>,
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

fn parse_conversion(
    doc: &serde_json::Value,
) -> Result<BTreeMap<String, FxConversion>, IapError> {
    let table = doc["currency"]["conversion"].as_object().ok_or_else(|| {
        IapError::InvalidArgument("risk.json: missing currency.conversion object".to_string())
    })?;
    let mut out = BTreeMap::new();
    for (ccy, spec) in table {
        let iid = spec["instrument_id"].as_u64().ok_or_else(|| {
            IapError::InvalidArgument(format!(
                "risk.json: currency.conversion.{ccy}.instrument_id missing/invalid"
            ))
        })?;
        if iid == 0 || iid > u64::from(u32::MAX) {
            return Err(IapError::InvalidArgument(format!(
                "risk.json: currency.conversion.{ccy}.instrument_id out of u32 range"
            )));
        }
        let invert = spec["invert"].as_bool().ok_or_else(|| {
            IapError::InvalidArgument(format!(
                "risk.json: currency.conversion.{ccy}.invert missing/non-bool"
            ))
        })?;
        out.insert(
            ccy.clone(),
            FxConversion {
                instrument_id: iid as u32,
                invert,
            },
        );
    }
    Ok(out)
}

/// An optional f64 key: `None` when absent or null, else `need_pos_f64`.
fn opt_pos_f64(doc: &serde_json::Value, section: &str, key: &str) -> Result<Option<f64>, IapError> {
    if doc[section][key].is_null() {
        return Ok(None);
    }
    need_pos_f64(doc, section, key).map(Some)
}

/// An optional i64 key: `None` when absent or null, else `need_pos_i64`.
fn opt_pos_i64(doc: &serde_json::Value, section: &str, key: &str) -> Result<Option<i64>, IapError> {
    if doc[section][key].is_null() {
        return Ok(None);
    }
    need_pos_i64(doc, section, key).map(Some)
}

/// `pre_trade.restricted_instruments`: absent / null = empty; else an array
/// of u32 instrument ids.
fn parse_restricted(doc: &serde_json::Value) -> Result<BTreeSet<u32>, IapError> {
    let v = &doc["pre_trade"]["restricted_instruments"];
    if v.is_null() {
        return Ok(BTreeSet::new());
    }
    let arr = v.as_array().ok_or_else(|| {
        IapError::InvalidArgument(
            "risk.json: pre_trade.restricted_instruments must be an array".to_string(),
        )
    })?;
    let mut out = BTreeSet::new();
    for item in arr {
        let iid = item
            .as_u64()
            .and_then(|x| u32::try_from(x).ok())
            .ok_or_else(|| {
                IapError::InvalidArgument(
                    "risk.json: pre_trade.restricted_instruments entries must be u32 instrument ids"
                        .to_string(),
                )
            })?;
        out.insert(iid);
    }
    Ok(out)
}

/// `pre_trade.venue_types`: absent / null = empty; else an object whose
/// keys are decimal u16 venue ids and whose values are `lit` / `dark` /
/// `midpoint`.
fn parse_venue_types(doc: &serde_json::Value) -> Result<BTreeMap<u16, VenueType>, IapError> {
    let v = &doc["pre_trade"]["venue_types"];
    if v.is_null() {
        return Ok(BTreeMap::new());
    }
    let obj = v.as_object().ok_or_else(|| {
        IapError::InvalidArgument("risk.json: pre_trade.venue_types must be an object".to_string())
    })?;
    let mut out = BTreeMap::new();
    for (k, t) in obj {
        let vid = k.parse::<u16>().map_err(|_| {
            IapError::InvalidArgument(format!(
                "risk.json: pre_trade.venue_types key {k} is not a u16 venue id"
            ))
        })?;
        let ty = t.as_str().and_then(VenueType::parse).ok_or_else(|| {
            IapError::InvalidArgument(format!(
                "risk.json: pre_trade.venue_types.{k} must be one of lit, dark, midpoint"
            ))
        })?;
        out.insert(vid, ty);
    }
    Ok(out)
}

/// `pre_trade.allowed_venue_types`: absent / null = off; else a non-empty
/// array of `lit` / `dark` / `midpoint`.
fn parse_allowed_venue_types(
    doc: &serde_json::Value,
) -> Result<Option<BTreeSet<VenueType>>, IapError> {
    let v = &doc["pre_trade"]["allowed_venue_types"];
    if v.is_null() {
        return Ok(None);
    }
    let bad = || {
        IapError::InvalidArgument(
            "risk.json: pre_trade.allowed_venue_types must be a non-empty array of lit, dark, midpoint"
                .to_string(),
        )
    };
    let arr = v.as_array().ok_or_else(bad)?;
    if arr.is_empty() {
        return Err(bad());
    }
    let mut out = BTreeSet::new();
    for item in arr {
        out.insert(item.as_str().and_then(VenueType::parse).ok_or_else(bad)?);
    }
    Ok(Some(out))
}

fn need_bool(doc: &serde_json::Value, section: &str, key: &str) -> Result<bool, IapError> {
    doc[section][key].as_bool().ok_or_else(|| {
        IapError::InvalidArgument(format!("risk.json: missing/non-bool {section}.{key}"))
    })
}

impl RiskLimits {
    /// Strict parse of a `configs/risk/risk.json` document.
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
        let base = RiskLimits {
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
            reporting_ccy: doc["currency"]["reporting_ccy"]
                .as_str()
                .filter(|s| !s.is_empty())
                .ok_or_else(|| {
                    IapError::InvalidArgument(
                        "risk.json: missing/empty currency.reporting_ccy".to_string(),
                    )
                })?
                .to_string(),
            fx_conversion: parse_conversion(doc)?,
            max_mid_jump_ratio: None,
            restricted_instruments: BTreeSet::new(),
            max_open_orders: None,
            max_open_notional: None,
            max_strategy_position_qty: None,
            max_participation_bps: None,
            participation_floor_qty: None,
            max_cancels_per_sec: None,
            cancel_burst: None,
            max_venue_msgs_per_sec: None,
            venue_msg_burst: None,
            max_tracked_keys: None,
            max_loss_override_multiple: None,
            min_approvers: None,
            venue_types: BTreeMap::new(),
            allowed_venue_types: None,
            max_dark_participation_bps: None,
            dark_participation_floor_qty: None,
            max_dark_fill_notional: None,
        };
        // x-version 4 keys, parsed in this order (the first error names its key)
        let jump = opt_pos_f64(doc, "market_data", "max_mid_jump_ratio")?;
        if let Some(j) = jump {
            if j <= 1.0 {
                return Err(IapError::InvalidArgument(format!(
                    "risk.json: market_data.max_mid_jump_ratio must be > 1, got {j}"
                )));
            }
        }
        let restricted = parse_restricted(doc)?;
        let max_open_orders = opt_pos_i64(doc, "pre_trade", "max_open_orders")?;
        let max_open_notional = opt_pos_f64(doc, "pre_trade", "max_open_notional")?;
        let max_strategy_position_qty = opt_pos_i64(doc, "pre_trade", "max_strategy_position_qty")?;
        let max_participation_bps = opt_pos_f64(doc, "pre_trade", "max_participation_bps")?;
        let participation_floor_qty = if max_participation_bps.is_some() {
            Some(need_pos_i64(doc, "pre_trade", "participation_floor_qty")?)
        } else {
            None
        };
        let max_cancels_per_sec = opt_pos_f64(doc, "pre_trade", "max_cancels_per_sec")?;
        let cancel_burst = if max_cancels_per_sec.is_some() {
            Some(need_pos_f64(doc, "pre_trade", "cancel_burst")?)
        } else {
            None
        };
        let max_venue_msgs_per_sec = opt_pos_f64(doc, "pre_trade", "max_venue_msgs_per_sec")?;
        let venue_msg_burst = if max_venue_msgs_per_sec.is_some() {
            Some(need_pos_f64(doc, "pre_trade", "venue_msg_burst")?)
        } else {
            None
        };
        let max_tracked_keys = opt_pos_i64(doc, "pre_trade", "max_tracked_keys")?;
        let max_loss_override_multiple =
            opt_pos_f64(doc, "overrides", "max_loss_override_multiple")?;
        let min_approvers = opt_pos_i64(doc, "overrides", "min_approvers")?;
        // v1.17 F8 keys, parsed after every x-version 4 key, in this order
        let venue_types = parse_venue_types(doc)?;
        let allowed_venue_types = parse_allowed_venue_types(doc)?;
        let max_dark_participation_bps =
            opt_pos_f64(doc, "pre_trade", "max_dark_participation_bps")?;
        let dark_participation_floor_qty = if max_dark_participation_bps.is_some() {
            Some(need_pos_i64(doc, "pre_trade", "dark_participation_floor_qty")?)
        } else {
            None
        };
        let max_dark_fill_notional = opt_pos_f64(doc, "pre_trade", "max_dark_fill_notional")?;
        Ok(RiskLimits {
            max_mid_jump_ratio: jump,
            restricted_instruments: restricted,
            max_open_orders,
            max_open_notional,
            max_strategy_position_qty,
            max_participation_bps,
            participation_floor_qty,
            max_cancels_per_sec,
            cancel_burst,
            max_venue_msgs_per_sec,
            venue_msg_burst,
            max_tracked_keys,
            max_loss_override_multiple,
            min_approvers,
            venue_types,
            allowed_venue_types,
            max_dark_participation_bps,
            dark_participation_floor_qty,
            max_dark_fill_notional,
            ..base
        })
    }

    /// The type of `venue_id` as the pre-trade controls see it: the
    /// `pre_trade.venue_types` entry, else `Lit`.
    pub fn venue_type(&self, venue_id: u16) -> VenueType {
        self.venue_types.get(&venue_id).copied().unwrap_or(VenueType::Lit)
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
            .join("../../configs/risk/risk.json");
        serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
    }

    #[test]
    fn repo_config_parses_completely() {
        let limits = RiskLimits::from_json(&repo_risk_json()).expect("complete pinned set");
        assert!(limits.max_order_qty > 0);
        assert!(limits.max_order_notional > 0.0);
        assert!(!limits.kill_switch_engaged);
        assert_eq!(limits.duplicate_order_window_ns, 0); // whole session
        assert_eq!(limits.reporting_ccy, "USD");
        assert_eq!(limits.fx_conversion["JPY"].instrument_id, 103);
        assert!(limits.fx_conversion["JPY"].invert);
        assert!(!limits.fx_conversion["EUR"].invert);
    }

    #[test]
    fn f8_controls_are_off_by_default_and_parse_strictly() {
        let limits = RiskLimits::from_json(&repo_risk_json()).unwrap();
        assert!(limits.venue_types.is_empty());
        assert!(limits.allowed_venue_types.is_none());
        assert!(limits.max_dark_participation_bps.is_none());
        assert!(limits.dark_participation_floor_qty.is_none());
        assert!(limits.max_dark_fill_notional.is_none());
        assert_eq!(limits.venue_type(7), VenueType::Lit);
        let mut doc = repo_risk_json();
        doc["pre_trade"]["venue_types"] =
            serde_json::json!({"1": "lit", "2": "dark", "3": "midpoint"});
        doc["pre_trade"]["allowed_venue_types"] = serde_json::json!(["dark", "lit"]);
        doc["pre_trade"]["max_dark_participation_bps"] = serde_json::json!(500.0);
        doc["pre_trade"]["dark_participation_floor_qty"] = serde_json::json!(100);
        doc["pre_trade"]["max_dark_fill_notional"] = serde_json::json!(250000.0);
        let limits = RiskLimits::from_json(&doc).unwrap();
        assert_eq!(limits.venue_type(2), VenueType::Dark);
        assert_eq!(limits.venue_type(3), VenueType::Midpoint);
        assert_eq!(limits.venue_type(9), VenueType::Lit);
        assert!(limits.allowed_venue_types.as_ref().unwrap().contains(&VenueType::Dark));
        assert_eq!(limits.dark_participation_floor_qty, Some(100));
        // strictness: bad key, bad type name, empty allow list, missing floor
        let mut bad = doc.clone();
        bad["pre_trade"]["venue_types"] = serde_json::json!({"x": "lit"});
        assert!(RiskLimits::from_json(&bad).is_err());
        let mut bad = doc.clone();
        bad["pre_trade"]["venue_types"] = serde_json::json!({"1": "grey"});
        assert!(RiskLimits::from_json(&bad).is_err());
        let mut bad = doc.clone();
        bad["pre_trade"]["allowed_venue_types"] = serde_json::json!([]);
        assert!(RiskLimits::from_json(&bad).is_err());
        let mut bad = doc.clone();
        bad["pre_trade"]
            .as_object_mut()
            .unwrap()
            .remove("dark_participation_floor_qty");
        assert!(RiskLimits::from_json(&bad).is_err());
    }

    #[test]
    fn missing_currency_block_is_a_config_error() {
        let mut doc = repo_risk_json();
        doc.as_object_mut().unwrap().remove("currency");
        assert!(RiskLimits::from_json(&doc).is_err());
        let mut doc = repo_risk_json();
        doc["currency"]["conversion"]["JPY"]["invert"] = serde_json::json!("yes");
        assert!(RiskLimits::from_json(&doc).is_err());
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
