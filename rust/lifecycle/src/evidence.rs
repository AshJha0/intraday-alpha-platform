//! Typed evidence handed to the gates (`iap.lifecycle.evidence`).
//!
//! One [`Evidence`] carries at most one block per stage: `research`
//! (an [`ExperimentResult`], `schemas/research/experiment_result.schema.json`)
//! plus `capacity_usd` for the CANDIDATE gates, `validation` for VALIDATING,
//! `paper` for PAPER and `live` for ACTIVE / WATCH. Every scalar is finite
//! (NaN / ±Inf are rejected by [`Evidence::validate`], which every strict
//! constructor runs); an absent block is `None` and, except for the RESEARCH
//! presence gate, moves nothing (silence is not evidence).
//!
//! `significance_threshold` (v1.5.0) is the PROMOTE t threshold the research
//! result was judged at; it sits beside `research` as `capacity_usd` does and
//! is what the `statistical_significance` gate reads under the default
//! `tstat_threshold = "ledger"` policy (`crate::gates`). The key is required
//! and may be `null`. `live.new_fraction` (v1.5.0) is the share of a live
//! reading's window that is new since the last counted reading, in `(0, 1]` —
//! the weight of the reading under the CUSUM retirement rule
//! (`crate::tracker`); the key is required whichever rule is configured.

use marketdata::IapError;
use serde::de::Deserializer;
use serde::{Deserialize, Serialize};
use serde_json::Value;

/// Research verdict (`experiment_result.schema.json`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Verdict {
    /// Promote to the next stage.
    #[serde(rename = "PROMOTE")]
    Promote,
    /// Iterate on the hypothesis.
    #[serde(rename = "ITERATE")]
    Iterate,
    /// Reject.
    #[serde(rename = "REJECT")]
    Reject,
}

/// A required key whose value may be `null`.
fn required_option<'de, D, T>(d: D) -> Result<Option<T>, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de>,
{
    Option::<T>::deserialize(d)
}

/// One experiment's result (`research/experiment_result.schema.json`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExperimentResult {
    /// Experiment id.
    pub experiment_id: String,
    /// Alpha id.
    pub alpha_id: String,
    /// Dataset hash.
    pub dataset_version: String,
    /// Feature-registry hash.
    pub feature_version: String,
    /// Model hash (may be null).
    #[serde(deserialize_with = "required_option")]
    pub model_version: Option<String>,
    /// Out-of-sample IC.
    pub ic: f64,
    /// Out-of-sample rank IC.
    pub rank_ic: f64,
    /// Newey-West t statistic.
    pub t_stat: f64,
    /// Newey-West lags.
    pub nw_lags: u32,
    /// Hit rate in `[0, 1]`.
    pub hit_rate: f64,
    /// Turnover (>= 0).
    pub turnover: f64,
    /// Gross return, bps.
    pub gross_return_bps: f64,
    /// Transaction cost, bps (>= 0).
    pub transaction_cost_bps: f64,
    /// Net return, bps (= gross - cost).
    pub net_return_bps: f64,
    /// Max drawdown, bps (>= 0).
    pub max_drawdown_bps: f64,
    /// Sharpe ratio.
    pub sharpe: f64,
    /// Fold sign consistency in `[0, 1]`.
    pub fold_consistency: f64,
    /// Number of folds.
    pub n_folds: u32,
    /// Leakage test passed.
    pub leakage_passed: bool,
    /// Free-form leakage detail (JSON object).
    pub leakage_detail: Value,
    /// Hypothesis sign confirmed (null = untested).
    #[serde(deserialize_with = "required_option")]
    pub hypothesis_sign_confirmed: Option<bool>,
    /// Verdict.
    pub verdict: Verdict,
    /// Experiments in the ledger for this alpha.
    pub n_experiments_in_ledger: u64,
    /// Git commit.
    pub git_commit: String,
    /// Ledger / event time (>= 0, never wall clock).
    pub created_ts: i64,
}

impl ExperimentResult {
    /// Finite floats, non-negative counts, `leakage_passed == false ⇒ REJECT`.
    pub fn validate(&self) -> Result<(), IapError> {
        for (name, x) in [
            ("ic", self.ic),
            ("rank_ic", self.rank_ic),
            ("t_stat", self.t_stat),
            ("hit_rate", self.hit_rate),
            ("turnover", self.turnover),
            ("gross_return_bps", self.gross_return_bps),
            ("transaction_cost_bps", self.transaction_cost_bps),
            ("net_return_bps", self.net_return_bps),
            ("max_drawdown_bps", self.max_drawdown_bps),
            ("sharpe", self.sharpe),
            ("fold_consistency", self.fold_consistency),
        ] {
            check_finite(&format!("research.{name}"), x)?;
        }
        if !self.leakage_detail.is_object() {
            return Err(IapError::Validation(
                "research.leakage_detail: expected an object".to_string(),
            ));
        }
        if self.created_ts < 0 {
            return Err(IapError::Validation(format!(
                "research.created_ts: {} < 0",
                self.created_ts
            )));
        }
        if !self.leakage_passed && self.verdict != Verdict::Reject {
            return Err(IapError::Validation(
                "research: leakage_passed == false requires verdict REJECT".to_string(),
            ));
        }
        Ok(())
    }
}

/// VALIDATING-stage evidence: the alpha replayed on held-out data.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ValidationEvidence {
    /// Realised IC of the held-out replay.
    pub holdout_ic: f64,
    /// IC the research result claimed.
    pub research_ic: f64,
    /// Replay reproduced the research signal stream byte-for-byte.
    pub replay_hash_match: bool,
    /// The language ports agree on the replay.
    pub parity: bool,
}

/// PAPER-stage evidence: the alpha traded in the paper environment.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PaperEvidence {
    /// Completed paper sessions.
    pub n_sessions: u64,
    /// Realised IC over them.
    pub realized_ic: f64,
    /// IC the research result claimed.
    pub research_ic: f64,
    /// Net P&L after modelled costs.
    pub net_pnl: f64,
    /// Hard-risk KILL decisions the alpha caused.
    pub n_kill_events: u64,
    /// Std of (paper - backtest) P&L per session (diagnostic, >= 0).
    pub tracking_error: f64,
}

/// One live rolling-IC evaluation (API_ADAPTIVE.md §4/§6).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LiveEvidence {
    /// Mean matured bucket IC, or null when too few buckets exist.
    #[serde(deserialize_with = "required_option")]
    pub rolling_ic: Option<f64>,
    /// Buckets behind it.
    pub n_buckets: u64,
    /// Adaptive block index of the evaluation.
    pub eval_index: u64,
    /// False when the matured set gained no new rows since the last
    /// counted evaluation (a re-read moves nothing).
    pub informative: bool,
    /// Share of the reading's window that is new since the last counted
    /// one, in `(0, 1]`.
    pub new_fraction: f64,
}

/// Everything the gates may read for one `advance` call.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Evidence {
    /// Research result.
    #[serde(deserialize_with = "required_option")]
    pub research: Option<ExperimentResult>,
    /// Aggregate deployable notional proxy, USD (>= 0).
    #[serde(deserialize_with = "required_option")]
    pub capacity_usd: Option<f64>,
    /// The t threshold the research result was judged at (> 0), or null.
    #[serde(deserialize_with = "required_option")]
    pub significance_threshold: Option<f64>,
    /// Held-out replay evidence.
    #[serde(deserialize_with = "required_option")]
    pub validation: Option<ValidationEvidence>,
    /// Paper-trading evidence.
    #[serde(deserialize_with = "required_option")]
    pub paper: Option<PaperEvidence>,
    /// Live rolling-IC evidence.
    #[serde(deserialize_with = "required_option")]
    pub live: Option<LiveEvidence>,
}

fn check_finite(path: &str, x: f64) -> Result<(), IapError> {
    if x.is_finite() {
        Ok(())
    } else {
        Err(IapError::Validation(format!("{path}: non-finite number")))
    }
}

impl Evidence {
    /// No evidence at all (every block absent).
    pub fn empty() -> Evidence {
        Evidence {
            research: None,
            capacity_usd: None,
            significance_threshold: None,
            validation: None,
            paper: None,
            live: None,
        }
    }

    /// Every scalar finite, non-negative where the contract says so.
    pub fn validate(&self) -> Result<(), IapError> {
        if let Some(r) = &self.research {
            r.validate()?;
        }
        if let Some(c) = self.capacity_usd {
            check_finite("evidence.capacity_usd", c)?;
            if c < 0.0 {
                return Err(IapError::Validation(
                    "evidence.capacity_usd must be >= 0".to_string(),
                ));
            }
        }
        if let Some(t) = self.significance_threshold {
            check_finite("evidence.significance_threshold", t)?;
            if t <= 0.0 {
                return Err(IapError::Validation(
                    "evidence.significance_threshold must be > 0".to_string(),
                ));
            }
        }
        if let Some(v) = &self.validation {
            check_finite("validation.holdout_ic", v.holdout_ic)?;
            check_finite("validation.research_ic", v.research_ic)?;
        }
        if let Some(p) = &self.paper {
            check_finite("paper.realized_ic", p.realized_ic)?;
            check_finite("paper.research_ic", p.research_ic)?;
            check_finite("paper.net_pnl", p.net_pnl)?;
            check_finite("paper.tracking_error", p.tracking_error)?;
            if p.tracking_error < 0.0 {
                return Err(IapError::Validation(
                    "paper.tracking_error must be >= 0".to_string(),
                ));
            }
        }
        if let Some(l) = &self.live {
            if let Some(ic) = l.rolling_ic {
                check_finite("live.rolling_ic", ic)?;
            }
            check_finite("live.new_fraction", l.new_fraction)?;
            if !(l.new_fraction > 0.0 && l.new_fraction <= 1.0) {
                return Err(IapError::Validation(
                    "live.new_fraction must be in (0, 1]".to_string(),
                ));
            }
        }
        Ok(())
    }

    /// Strict parse of the JSON document (`Evidence.to_dict()` form):
    /// unknown / missing keys, wrong types and non-finite values are errors.
    pub fn from_value(v: &Value) -> Result<Evidence, IapError> {
        let ev: Evidence = serde_json::from_value(v.clone())
            .map_err(|e| IapError::Validation(format!("evidence: {e}")))?;
        ev.validate()?;
        Ok(ev)
    }

    /// JSON-ready document; absent blocks are `null`.
    pub fn to_value(&self) -> Result<Value, IapError> {
        self.validate()?;
        serde_json::to_value(self).map_err(|e| IapError::Codec(format!("evidence: {e}")))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn empty_document_round_trips() {
        let doc = json!({"research": null, "capacity_usd": null, "significance_threshold": null,
                         "validation": null, "paper": null, "live": null});
        let ev = Evidence::from_value(&doc).expect("strict parse");
        assert_eq!(ev, Evidence::empty());
        assert_eq!(ev.to_value().expect("finite"), doc);
    }

    #[test]
    fn strictness() {
        let base = json!({"research": null, "capacity_usd": null, "significance_threshold": null,
                          "validation": null, "paper": null, "live": null});
        let mut missing = base.clone();
        missing.as_object_mut().expect("object").remove("live");
        assert!(Evidence::from_value(&missing).is_err(), "missing key");
        // a v1.4.0 document (no significance_threshold) is rejected, not defaulted
        let mut old = base.clone();
        old.as_object_mut()
            .expect("object")
            .remove("significance_threshold");
        assert!(Evidence::from_value(&old).is_err(), "v1.4.0 document");
        let mut unknown = base.clone();
        unknown["x"] = json!(1);
        assert!(Evidence::from_value(&unknown).is_err(), "unknown key");
        let mut negative = base.clone();
        negative["capacity_usd"] = json!(-1.0);
        assert!(
            Evidence::from_value(&negative).is_err(),
            "negative capacity"
        );
        let mut threshold = base.clone();
        threshold["significance_threshold"] = json!(4.365);
        assert_eq!(
            Evidence::from_value(&threshold)
                .expect("a positive threshold")
                .significance_threshold,
            Some(4.365)
        );
        threshold["significance_threshold"] = json!(0.0);
        assert!(Evidence::from_value(&threshold).is_err(), "threshold <= 0");
        let mut live = base.clone();
        live["live"] = json!({"rolling_ic": null, "n_buckets": 2, "eval_index": 1,
                              "informative": true, "new_fraction": 0.125});
        let ev = Evidence::from_value(&live).expect("null rolling ic is allowed");
        assert_eq!(ev.live.as_ref().and_then(|l| l.rolling_ic), None);
        for bad in [0.0, 1.5, -0.1] {
            live["live"]["new_fraction"] = json!(bad);
            assert!(Evidence::from_value(&live).is_err(), "new_fraction {bad}");
        }
        live["live"]
            .as_object_mut()
            .expect("object")
            .remove("new_fraction");
        assert!(Evidence::from_value(&live).is_err(), "missing new_fraction");
        let mut nan = Evidence::empty();
        nan.capacity_usd = Some(f64::NAN);
        assert!(nan.validate().is_err());
        assert!(nan.to_value().is_err());
    }
}
