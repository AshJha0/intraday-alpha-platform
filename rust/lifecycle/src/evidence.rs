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
//!
//! `cross_alpha` (v1.5.0) carries what the `cross_alpha_correlation` gate
//! needs: one [`PeerCorrelation`] per OTHER registered alpha — its id, its
//! lifecycle state when the evidence was built and the correlation of the two
//! alphas' out-of-sample signals, in `[-1, 1]`. The peers are strictly
//! increasing by `alpha_id` (sorted, unique). The gate filters them by state
//! itself, so one document can be judged under another
//! `cross_alpha_min_state`. The key is required and may be `null` — nobody
//! measured the correlations, and the gate fails; an empty peer list is a
//! statement (there is no other alpha) and passes vacuously.
//!
//! `pnl_bootstrap` (v1.5.0) carries the confidence interval the
//! `net_pnl_bootstrap_ci` gate reads, with everything that pins it, so that no
//! port resamples: `ci_low` / `ci_high` (both `null` when the series was too
//! short for an interval), `level`, `n_resamples`, `seed`, `mean_block`,
//! `n_bars` and `n_trades`. The key is required and may be `null` — no
//! interval was computed, and the gate fails.

use marketdata::IapError;
use serde::de::Deserializer;
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::state::LifecycleState;

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

/// The bootstrap interval of the pooled net P&L at 1x costs, with what pins
/// it (module docs).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PnlBootstrapEvidence {
    /// Lower bound, or null when no interval exists.
    #[serde(deserialize_with = "required_option")]
    pub ci_low: Option<f64>,
    /// Upper bound (>= `ci_low`), null exactly when `ci_low` is.
    #[serde(deserialize_with = "required_option")]
    pub ci_high: Option<f64>,
    /// Confidence level the interval was taken at, in `(0, 1)`.
    pub level: f64,
    /// Bootstrap resamples (>= 1).
    pub n_resamples: u64,
    /// Seed of the resampling RNG.
    pub seed: u64,
    /// Mean block length of the stationary bootstrap (>= 1).
    pub mean_block: f64,
    /// Length of the resampled bar series.
    pub n_bars: u64,
    /// Trades at 1x costs over the same folds.
    pub n_trades: u64,
}

impl PnlBootstrapEvidence {
    /// What the gate compares: `ci_low` — or `None` (no usable interval: the
    /// gate fails closed) when the alpha made no trade, when the interval was
    /// not taken at exactly `level`, or when it has no bounds.
    pub fn gate_value(&self, level: f64) -> Option<f64> {
        if self.n_trades == 0 || self.level != level {
            return None;
        }
        self.ci_low
    }

    /// Bounds both present or both null and ordered, finite numbers, level in
    /// `(0, 1)`, at least one resample, mean block >= 1.
    pub fn validate(&self) -> Result<(), IapError> {
        match (self.ci_low, self.ci_high) {
            (None, None) => {}
            (Some(low), Some(high)) => {
                check_finite("pnl_bootstrap.ci_low", low)?;
                check_finite("pnl_bootstrap.ci_high", high)?;
                if high < low {
                    return Err(IapError::Validation(
                        "pnl_bootstrap: ci_high < ci_low".to_string(),
                    ));
                }
            }
            _ => {
                return Err(IapError::Validation(
                    "pnl_bootstrap: ci_low and ci_high are both numbers or both null".to_string(),
                ))
            }
        }
        check_finite("pnl_bootstrap.level", self.level)?;
        if !(self.level > 0.0 && self.level < 1.0) {
            return Err(IapError::Validation(
                "pnl_bootstrap.level must lie in (0, 1)".to_string(),
            ));
        }
        if self.n_resamples < 1 {
            return Err(IapError::Validation(
                "pnl_bootstrap.n_resamples must be >= 1".to_string(),
            ));
        }
        check_finite("pnl_bootstrap.mean_block", self.mean_block)?;
        if self.mean_block < 1.0 {
            return Err(IapError::Validation(
                "pnl_bootstrap.mean_block must be >= 1".to_string(),
            ));
        }
        Ok(())
    }
}

/// One other alpha as the correlation gate sees it.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PeerCorrelation {
    /// The peer's alpha id (non-empty).
    pub alpha_id: String,
    /// The peer's lifecycle state when the evidence was built.
    pub state: LifecycleState,
    /// Signal correlation with the alpha under evaluation, in `[-1, 1]`.
    pub correlation: f64,
}

impl PeerCorrelation {
    /// True when the gate counts this peer under `min_state`: its state is at
    /// or beyond `min_state` and it is not RETIRED.
    pub fn is_eligible(&self, min_state: LifecycleState) -> bool {
        min_state.index() <= self.state.index()
            && self.state.index() < LifecycleState::Retired.index()
    }
}

/// The signal correlation of one alpha with every other registered alpha
/// (module docs).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CrossAlphaEvidence {
    /// One entry per other alpha, strictly increasing by `alpha_id`.
    pub peers: Vec<PeerCorrelation>,
}

impl CrossAlphaEvidence {
    /// The peers the gate counts under `min_state`, in `alpha_id` order.
    pub fn eligible(&self, min_state: LifecycleState) -> Vec<&PeerCorrelation> {
        self.peers
            .iter()
            .filter(|p| p.is_eligible(min_state))
            .collect()
    }

    /// The gate statistic: the largest `|correlation|` over the eligible
    /// peers, `0.0` when there is none (the vacuous case).
    pub fn max_abs_correlation(&self, min_state: LifecycleState) -> f64 {
        self.peers
            .iter()
            .filter(|p| p.is_eligible(min_state))
            .map(|p| p.correlation.abs())
            .fold(0.0, f64::max)
    }

    /// Non-empty ids, finite correlations in `[-1, 1]`, peers strictly
    /// increasing by `alpha_id` (no duplicates).
    pub fn validate(&self) -> Result<(), IapError> {
        for p in &self.peers {
            if p.alpha_id.is_empty() {
                return Err(IapError::Validation(
                    "cross_alpha.peers[].alpha_id: expected a non-empty string".to_string(),
                ));
            }
            check_finite("cross_alpha.peers[].correlation", p.correlation)?;
            if !(-1.0..=1.0).contains(&p.correlation) {
                return Err(IapError::Validation(
                    "cross_alpha.peers[].correlation must lie in [-1, 1]".to_string(),
                ));
            }
        }
        if self
            .peers
            .windows(2)
            .any(|w| w[1].alpha_id <= w[0].alpha_id)
        {
            return Err(IapError::Validation(
                "cross_alpha.peers: must be sorted by alpha_id, without duplicates".to_string(),
            ));
        }
        Ok(())
    }
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
    /// Bootstrap interval of the pooled net P&L at 1x costs, or null when
    /// none was computed.
    #[serde(deserialize_with = "required_option")]
    pub pnl_bootstrap: Option<PnlBootstrapEvidence>,
    /// Signal correlation with every other registered alpha, or null when
    /// nobody measured it.
    #[serde(deserialize_with = "required_option")]
    pub cross_alpha: Option<CrossAlphaEvidence>,
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
            pnl_bootstrap: None,
            cross_alpha: None,
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
        if let Some(b) = &self.pnl_bootstrap {
            b.validate()?;
        }
        if let Some(c) = &self.cross_alpha {
            c.validate()?;
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
                         "pnl_bootstrap": null, "cross_alpha": null, "validation": null, "paper": null, "live": null});
        let ev = Evidence::from_value(&doc).expect("strict parse");
        assert_eq!(ev, Evidence::empty());
        assert_eq!(ev.to_value().expect("finite"), doc);
    }

    #[test]
    fn strictness() {
        let base = json!({"research": null, "capacity_usd": null, "significance_threshold": null,
                          "pnl_bootstrap": null, "cross_alpha": null, "validation": null, "paper": null, "live": null});
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

    fn base_doc() -> Value {
        json!({"research": null, "capacity_usd": null, "significance_threshold": null,
               "pnl_bootstrap": null, "cross_alpha": null, "validation": null, "paper": null, "live": null})
    }

    fn peer(alpha_id: &str, state: LifecycleState, correlation: f64) -> PeerCorrelation {
        PeerCorrelation {
            alpha_id: alpha_id.to_string(),
            state,
            correlation,
        }
    }

    #[test]
    fn cross_alpha_round_trips_and_is_strict() {
        let mut doc = base_doc();
        doc["cross_alpha"] = json!({"peers": [
            {"alpha_id": "A1", "state": "ACTIVE", "correlation": -0.9},
            {"alpha_id": "A2", "state": "CANDIDATE", "correlation": 0.95}
        ]});
        let ev = Evidence::from_value(&doc).expect("strict parse");
        let cross = ev.cross_alpha.as_ref().expect("present");
        assert_eq!(
            cross.peers,
            vec![
                peer("A1", LifecycleState::Active, -0.9),
                peer("A2", LifecycleState::Candidate, 0.95)
            ]
        );
        assert_eq!(ev.to_value().expect("finite"), doc);
        // an empty peer list is a statement and round-trips as such
        let mut empty = base_doc();
        empty["cross_alpha"] = json!({"peers": []});
        let ev = Evidence::from_value(&empty).expect("empty peers");
        assert_eq!(ev.cross_alpha, Some(CrossAlphaEvidence { peers: vec![] }));
        assert_eq!(ev.to_value().expect("finite"), empty);
        // a document without the key is rejected, not defaulted
        let mut old = base_doc();
        old.as_object_mut().expect("object").remove("cross_alpha");
        assert!(Evidence::from_value(&old).is_err(), "missing cross_alpha");
        let rejected: Vec<(&str, Value)> = vec![
            ("not an object", json!([])),
            ("missing peers", json!({})),
            ("unknown key", json!({"peers": [], "x": 1})),
            ("peers not an array", json!({"peers": {}})),
            (
                "missing peer key",
                json!({"peers": [{"alpha_id": "A1", "state": "ACTIVE"}]}),
            ),
            (
                "unknown peer key",
                json!({"peers": [
                    {"alpha_id": "A1", "state": "ACTIVE", "correlation": 0.1, "x": 1}
                ]}),
            ),
            (
                "unknown state",
                json!({"peers": [{"alpha_id": "A1", "state": "LIVE", "correlation": 0.1}]}),
            ),
            (
                "state by index",
                json!({"peers": [{"alpha_id": "A1", "state": 4, "correlation": 0.1}]}),
            ),
            (
                "empty id",
                json!({"peers": [{"alpha_id": "", "state": "ACTIVE", "correlation": 0.1}]}),
            ),
            (
                "correlation above 1",
                json!({"peers": [{"alpha_id": "A1", "state": "ACTIVE", "correlation": 1.5}]}),
            ),
            (
                "correlation below -1",
                json!({"peers": [{"alpha_id": "A1", "state": "ACTIVE", "correlation": -1.01}]}),
            ),
            (
                "correlation not a number",
                json!({"peers": [{"alpha_id": "A1", "state": "ACTIVE", "correlation": "0.1"}]}),
            ),
            (
                "unsorted",
                json!({"peers": [
                    {"alpha_id": "B", "state": "ACTIVE", "correlation": 0.1},
                    {"alpha_id": "A", "state": "ACTIVE", "correlation": 0.1}
                ]}),
            ),
            (
                "duplicate",
                json!({"peers": [
                    {"alpha_id": "A", "state": "ACTIVE", "correlation": 0.1},
                    {"alpha_id": "A", "state": "PAPER", "correlation": 0.2}
                ]}),
            ),
        ];
        for (what, bad) in rejected {
            let mut doc = base_doc();
            doc["cross_alpha"] = bad;
            assert!(Evidence::from_value(&doc).is_err(), "{what}");
        }
        // the bounds themselves are allowed
        let mut bounds = base_doc();
        bounds["cross_alpha"] = json!({"peers": [
            {"alpha_id": "A", "state": "ACTIVE", "correlation": -1.0},
            {"alpha_id": "B", "state": "ACTIVE", "correlation": 1.0}
        ]});
        assert!(Evidence::from_value(&bounds).is_ok());
        let mut nan = Evidence::empty();
        nan.cross_alpha = Some(CrossAlphaEvidence {
            peers: vec![peer("A", LifecycleState::Active, f64::NAN)],
        });
        assert!(nan.validate().is_err());
        assert!(nan.to_value().is_err());
    }

    fn boot_doc() -> Value {
        json!({"ci_low": 12.5, "ci_high": 90.0, "level": 0.95, "n_resamples": 1000,
               "seed": 20260829, "mean_block": 9.0, "n_bars": 626, "n_trades": 40})
    }

    #[test]
    fn pnl_bootstrap_round_trips_and_is_strict() {
        let mut doc = base_doc();
        doc["pnl_bootstrap"] = boot_doc();
        let ev = Evidence::from_value(&doc).expect("strict parse");
        let boot = ev.pnl_bootstrap.clone().expect("present");
        assert_eq!(
            boot,
            PnlBootstrapEvidence {
                ci_low: Some(12.5),
                ci_high: Some(90.0),
                level: 0.95,
                n_resamples: 1000,
                seed: 20260829,
                mean_block: 9.0,
                n_bars: 626,
                n_trades: 40,
            }
        );
        assert_eq!(ev.to_value().expect("finite"), doc);
        // an interval without bounds is a valid document
        let mut unbounded = base_doc();
        unbounded["pnl_bootstrap"] = boot_doc();
        unbounded["pnl_bootstrap"]["ci_low"] = Value::Null;
        unbounded["pnl_bootstrap"]["ci_high"] = Value::Null;
        let ev = Evidence::from_value(&unbounded).expect("null bounds");
        assert_eq!(ev.to_value().expect("finite"), unbounded);
        // a document without the key is rejected, not defaulted
        let mut old = base_doc();
        old.as_object_mut().expect("object").remove("pnl_bootstrap");
        assert!(Evidence::from_value(&old).is_err(), "missing pnl_bootstrap");
        let rejected: Vec<(&str, &str, Value)> = vec![
            ("one bound null", "ci_low", Value::Null),
            ("the other bound null", "ci_high", Value::Null),
            ("ci_high < ci_low", "ci_high", json!(12.0)),
            ("level 1.0", "level", json!(1.0)),
            ("level 0.0", "level", json!(0.0)),
            ("no resample", "n_resamples", json!(0)),
            ("fractional resamples", "n_resamples", json!(1000.5)),
            ("negative seed", "seed", json!(-1)),
            ("mean block below 1", "mean_block", json!(0.5)),
            ("negative bars", "n_bars", json!(-1)),
            ("negative trades", "n_trades", json!(-1)),
            ("bound not a number", "ci_low", json!("12.5")),
        ];
        for (what, key, bad) in rejected {
            let mut doc = base_doc();
            doc["pnl_bootstrap"] = boot_doc();
            doc["pnl_bootstrap"][key] = bad;
            assert!(Evidence::from_value(&doc).is_err(), "{what}");
        }
        for key in [
            "ci_low",
            "ci_high",
            "level",
            "n_resamples",
            "seed",
            "mean_block",
            "n_bars",
            "n_trades",
        ] {
            let mut doc = base_doc();
            doc["pnl_bootstrap"] = boot_doc();
            doc["pnl_bootstrap"]
                .as_object_mut()
                .expect("object")
                .remove(key);
            assert!(Evidence::from_value(&doc).is_err(), "missing {key}");
        }
        let mut unknown = base_doc();
        unknown["pnl_bootstrap"] = boot_doc();
        unknown["pnl_bootstrap"]["x"] = json!(1);
        assert!(Evidence::from_value(&unknown).is_err(), "unknown key");
        let mut nan = Evidence::empty();
        nan.pnl_bootstrap = Some(PnlBootstrapEvidence {
            ci_low: Some(f64::NAN),
            ..boot
        });
        assert!(nan.validate().is_err());
        assert!(nan.to_value().is_err());
    }

    #[test]
    fn pnl_bootstrap_gate_value_needs_a_traded_right_level_interval() {
        let boot = PnlBootstrapEvidence {
            ci_low: Some(12.5),
            ci_high: Some(90.0),
            level: 0.95,
            n_resamples: 1000,
            seed: 20260829,
            mean_block: 9.0,
            n_bars: 626,
            n_trades: 40,
        };
        assert_eq!(boot.gate_value(0.95), Some(12.5));
        assert_eq!(boot.gate_value(0.9), None, "another level");
        let untraded = PnlBootstrapEvidence {
            n_trades: 0,
            ..boot.clone()
        };
        assert_eq!(untraded.gate_value(0.95), None, "no trade");
        let unbounded = PnlBootstrapEvidence {
            ci_low: None,
            ci_high: None,
            ..boot
        };
        assert_eq!(unbounded.gate_value(0.95), None, "no bounds");
        assert!(unbounded.validate().is_ok());
    }

    #[test]
    fn cross_alpha_statistic_counts_eligible_peers_only() {
        let cross = CrossAlphaEvidence {
            peers: vec![
                peer("A", LifecycleState::Research, 0.99),
                peer("B", LifecycleState::Candidate, 0.98),
                peer("C", LifecycleState::Validating, 0.3),
                peer("D", LifecycleState::Paper, -0.5),
                peer("E", LifecycleState::Active, 0.2),
                peer("F", LifecycleState::Watch, -0.1),
                peer("G", LifecycleState::Retired, 0.97),
            ],
        };
        let ids = |min_state: LifecycleState| -> Vec<String> {
            cross
                .eligible(min_state)
                .into_iter()
                .map(|p| p.alpha_id.clone())
                .collect()
        };
        assert_eq!(ids(LifecycleState::Validating), ["C", "D", "E", "F"]);
        assert_eq!(ids(LifecycleState::Paper), ["D", "E", "F"]);
        assert_eq!(ids(LifecycleState::Active), ["E", "F"]);
        assert_eq!(cross.max_abs_correlation(LifecycleState::Validating), 0.5);
        assert_eq!(cross.max_abs_correlation(LifecycleState::Paper), 0.5);
        assert_eq!(cross.max_abs_correlation(LifecycleState::Active), 0.2);
        // no eligible peer, or no peer at all: 0.0
        let none = CrossAlphaEvidence {
            peers: vec![
                peer("A", LifecycleState::Candidate, 0.9),
                peer("B", LifecycleState::Retired, -0.9),
            ],
        };
        assert_eq!(none.max_abs_correlation(LifecycleState::Validating), 0.0);
        assert!(none.eligible(LifecycleState::Validating).is_empty());
        let empty = CrossAlphaEvidence { peers: vec![] };
        assert_eq!(empty.max_abs_correlation(LifecycleState::Validating), 0.0);
    }
}
