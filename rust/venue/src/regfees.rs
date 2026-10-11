//! Dated US equity regulatory fee schedule (v1.15, X-P1-1): SEC Section 31
//! transaction fee + FINRA Trading Activity Fee, charged on SELL fills only.
//! Port of `python/src/iap/execution/regulatory.py`; the arithmetic is
//! pinned and checked against `tests/golden/expected_regulatory_fees.json`:
//!
//! ```text
//! sec   = usd_per_million * notional / 1e6
//! taf   = min(per_share * qty, max_per_trade)
//! total = sec + taf            (0.0 on a buy)
//! ```
//!
//! A period covers fills with `from_ns <= ts < until_ns` (`until_ns`
//! absent = open-ended); a fill outside every period is an error (fail
//! closed). Off by default: a [`crate::SimulatedVenue`] charges it only
//! after [`crate::SimulatedVenue::with_regulatory_fees`].

use marketdata::IapError;
use serde_json::Value;

/// One dated period of a fee table.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct FeePeriod {
    /// First covered timestamp (ns since the epoch, UTC).
    pub from_ns: i64,
    /// First timestamp no longer covered (`i64::MAX` = open-ended).
    pub until_ns: i64,
    /// SEC: USD per million of notional; TAF: USD per share.
    pub rate: f64,
    /// TAF cap per trade (0 for SEC rows).
    pub max_per_trade: f64,
}

/// The two dated tables of `configs/reference/regulatory_fee_schedule.json`.
#[derive(Debug, Clone, PartialEq)]
pub struct RegulatoryFeeSchedule {
    /// SEC Section 31 periods.
    pub sec: Vec<FeePeriod>,
    /// FINRA TAF periods.
    pub taf: Vec<FeePeriod>,
}

fn bad(msg: String) -> IapError {
    IapError::InvalidArgument(msg)
}

fn num(row: &Value, key: &str) -> Result<f64, IapError> {
    row.get(key)
        .and_then(Value::as_f64)
        .ok_or_else(|| bad(format!("regulatory fee row missing {key}")))
}

fn period(row: &Value, rate_key: &str, cap_key: Option<&str>) -> Result<FeePeriod, IapError> {
    let from_ns = row
        .get("from_ns")
        .and_then(Value::as_i64)
        .ok_or_else(|| bad("regulatory fee row missing from_ns".to_string()))?;
    let until_ns = match row.get("until_ns") {
        None | Some(Value::Null) => i64::MAX,
        Some(v) => v.as_i64().ok_or_else(|| bad("bad until_ns".to_string()))?,
    };
    let max_per_trade = match cap_key {
        Some(k) => num(row, k)?,
        None => 0.0,
    };
    Ok(FeePeriod { from_ns, until_ns, rate: num(row, rate_key)?, max_per_trade })
}

fn check(ps: &[FeePeriod], what: &str) -> Result<(), IapError> {
    if ps.is_empty() {
        return Err(bad(format!("empty {what} table")));
    }
    for (i, p) in ps.iter().enumerate() {
        let overlaps = i > 0 && p.from_ns < ps[i - 1].until_ns;
        if p.until_ns <= p.from_ns || overlaps || p.rate < 0.0 || p.max_per_trade < 0.0 {
            return Err(bad(format!("bad {what} period {i}")));
        }
    }
    Ok(())
}

fn find<'a>(ps: &'a [FeePeriod], ts: i64, what: &str) -> Result<&'a FeePeriod, IapError> {
    ps.iter()
        .find(|p| p.from_ns <= ts && ts < p.until_ns)
        .ok_or_else(|| bad(format!("no {what} period covers exchange_ts {ts}")))
}

impl RegulatoryFeeSchedule {
    /// Parse the schedule JSON document.
    pub fn from_json(root: &Value) -> Result<RegulatoryFeeSchedule, IapError> {
        let rows = |key: &str| {
            root.get(key)
                .and_then(Value::as_array)
                .ok_or_else(|| bad(format!("regulatory fee schedule missing {key}")))
        };
        let sec = rows("sec_section31")?
            .iter()
            .map(|r| period(r, "usd_per_million", None))
            .collect::<Result<Vec<_>, _>>()?;
        let taf = rows("finra_taf")?
            .iter()
            .map(|r| period(r, "per_share", Some("max_per_trade")))
            .collect::<Result<Vec<_>, _>>()?;
        check(&sec, "sec_section31")?;
        check(&taf, "finra_taf")?;
        Ok(RegulatoryFeeSchedule { sec, taf })
    }

    /// Load the schedule from a file.
    pub fn load(path: &std::path::Path) -> Result<RegulatoryFeeSchedule, IapError> {
        let text = std::fs::read_to_string(path)
            .map_err(|e| bad(format!("cannot read {}: {e}", path.display())))?;
        let root: Value = serde_json::from_str(&text)
            .map_err(|e| bad(format!("bad JSON {}: {e}", path.display())))?;
        Self::from_json(&root)
    }

    /// Regulatory fee of one equity fill (`side` 0 = buy, 1 = sell).
    pub fn fee(&self, side: u8, qty: f64, notional: f64, ts: i64) -> Result<f64, IapError> {
        if side != 1 {
            return Ok(0.0);
        }
        let s = find(&self.sec, ts, "sec_section31")?;
        let t = find(&self.taf, ts, "finra_taf")?;
        let sec = s.rate * notional / 1e6;
        let taf = (t.rate * qty).min(t.max_per_trade);
        Ok(sec + taf)
    }
}
