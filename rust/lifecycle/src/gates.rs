//! The gate table (`iap.lifecycle.gates.GATE_SPECS`) and the policy
//! configuration (`iap.lifecycle.config.PolicyConfig`).
//!
//! Every gate is one row of [`GATE_SPECS`]: name, the evidence block it
//! reads, the comparison kind and the config key holding its threshold. A
//! gate is a pure function `(spec, config, evidence) -> GateResult`.
//!
//! Comparison kinds (pinned): `min` ⇒ `value >= threshold`; `max` ⇒
//! `value <= threshold`; `gt` ⇒ `value > threshold` (strict); `bool` ⇒ the
//! metric itself with `value = threshold = null`. A gate whose block is absent
//! or whose metric is `None` fails with `value = null`.
//!
//! **The significance threshold (v1.5.0).** `statistical_significance` is the
//! one gate whose threshold is not a config constant. Under the default
//! policy ([`TstatThreshold::Ledger`]) it is
//! `max(min_nw_tstat, evidence.significance_threshold)` — the multiple-testing
//! threshold the research result was judged at, never below the configured
//! floor — and when the evidence carries no threshold the gate FAILS with
//! `threshold = null` (the value is still reported). Under
//! [`TstatThreshold::Fixed`] — the rule up to v1.4.0 — the threshold is
//! `min_nw_tstat` and the evidence field is not read. The gate table (names,
//! blocks, kinds, edges) is unchanged.
//!
//! The policy is loaded from two files, fail-fast with file + key in every
//! error: `configs/strategies/lifecycle.json` (promotion gates, demotion
//! counter and `tstat_threshold`, `x-version` 2) and the `adaptive.lifecycle`
//! block of `configs/strategies/strategies.json` (the live sub-machine: gates
//! and the retirement rule, reused unchanged). [`PolicyConfig::from_value`]
//! reads the merged view embedded in `tests/golden/expected_lifecycle.json`.

use std::path::Path;

use marketdata::IapError;
use serde::de::Deserializer;
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};

use crate::evidence::Evidence;

/// `x-version` of `configs/strategies/lifecycle.json` (2 since v1.5.0:
/// `tstat_threshold`).
pub const LIFECYCLE_CONFIG_VERSION: u64 = 2;

/// The gate whose threshold comes from the evidence under the ledger policy.
pub const SIGNIFICANCE_GATE: &str = "statistical_significance";

/// How `statistical_significance` gets its threshold (module docs).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TstatThreshold {
    /// The default since v1.5.0: `max(min_nw_tstat, evidence threshold)`.
    Ledger,
    /// The legacy rule (the default up to v1.4.0): `min_nw_tstat` alone.
    Fixed,
}

impl TstatThreshold {
    /// Config value (`ledger` / `fixed`).
    pub fn name(self) -> &'static str {
        match self {
            TstatThreshold::Ledger => "ledger",
            TstatThreshold::Fixed => "fixed",
        }
    }

    /// Parse a config value.
    pub fn from_name(name: &str, where_: &str) -> Result<TstatThreshold, IapError> {
        match name {
            "ledger" => Ok(TstatThreshold::Ledger),
            "fixed" => Ok(TstatThreshold::Fixed),
            other => Err(cfg_err(format!(
                "{where_}.tstat_threshold: expected one of [\"ledger\", \"fixed\"], got {other:?}"
            ))),
        }
    }
}

/// How a persistent live breach is recognised
/// (`iap.adaptive.lifecycle`, `breach_rule`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BreachRule {
    /// The default since v1.5.0: a CUSUM of the shortfall below the watch
    /// gate, each reading weighted by its share of new information.
    Cusum,
    /// The legacy rule (the default up to v1.4.0): `retire_breach_evals`
    /// consecutive breaches.
    Consecutive,
}

impl BreachRule {
    /// Config value (`cusum` / `consecutive`).
    pub fn name(self) -> &'static str {
        match self {
            BreachRule::Cusum => "cusum",
            BreachRule::Consecutive => "consecutive",
        }
    }

    /// Parse a config value.
    pub fn from_name(name: &str, where_: &str) -> Result<BreachRule, IapError> {
        match name {
            "cusum" => Ok(BreachRule::Cusum),
            "consecutive" => Ok(BreachRule::Consecutive),
            other => Err(cfg_err(format!(
                "{where_}: unknown breach_rule {other:?}; known: cusum, consecutive"
            ))),
        }
    }
}

/// How a gate compares its metric with its threshold.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GateKind {
    /// `value >= threshold`.
    Min,
    /// `value <= threshold`.
    Max,
    /// `value > threshold` (strict).
    Gt,
    /// The boolean metric itself.
    Bool,
}

impl GateKind {
    /// Wire name (`min` / `max` / `gt` / `bool`).
    pub fn name(self) -> &'static str {
        match self {
            GateKind::Min => "min",
            GateKind::Max => "max",
            GateKind::Gt => "gt",
            GateKind::Bool => "bool",
        }
    }
}

/// The evidence block a gate reads.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Block {
    /// `evidence.research`.
    Research,
    /// `evidence.capacity_usd`.
    Capacity,
    /// `evidence.validation`.
    Validation,
    /// `evidence.paper`.
    Paper,
    /// `evidence.live`.
    Live,
}

impl Block {
    /// Wire name.
    pub fn name(self) -> &'static str {
        match self {
            Block::Research => "research",
            Block::Capacity => "capacity",
            Block::Validation => "validation",
            Block::Paper => "paper",
            Block::Live => "live",
        }
    }
}

/// One row of the gate table.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct GateSpec {
    /// Gate name (wire key of `LifecycleTransition.gates`).
    pub name: &'static str,
    /// Evidence block read.
    pub block: Block,
    /// Comparison kind.
    pub kind: GateKind,
    /// Config key of the threshold (`None` for `bool` gates;
    /// `watch_ic_gate` lives in `strategies.json`, all others in
    /// `lifecycle.json` `gates`).
    pub threshold_key: Option<&'static str>,
}

const fn spec(
    name: &'static str,
    block: Block,
    kind: GateKind,
    key: Option<&'static str>,
) -> GateSpec {
    GateSpec {
        name,
        block,
        kind,
        threshold_key: key,
    }
}

/// The complete gate table, in the pinned order (lifecycle.md §3).
pub const GATE_SPECS: [GateSpec; 18] = [
    spec(
        "ledger_entry_exists",
        Block::Research,
        GateKind::Min,
        Some("min_experiments_in_ledger"),
    ),
    spec("leakage_clean", Block::Research, GateKind::Bool, None),
    spec("oos_ic", Block::Research, GateKind::Min, Some("min_oos_ic")),
    spec(
        "statistical_significance",
        Block::Research,
        GateKind::Min,
        Some("min_nw_tstat"),
    ),
    spec(
        "fold_consistency",
        Block::Research,
        GateKind::Min,
        Some("min_fold_sign_consistency"),
    ),
    spec(
        "fold_count",
        Block::Research,
        GateKind::Min,
        Some("min_folds"),
    ),
    spec("hypothesis_sign", Block::Research, GateKind::Bool, None),
    spec(
        "net_pnl_after_costs",
        Block::Research,
        GateKind::Gt,
        Some("min_net_return_bps"),
    ),
    spec(
        "capacity",
        Block::Capacity,
        GateKind::Min,
        Some("min_capacity_usd"),
    ),
    spec(
        "stability",
        Block::Research,
        GateKind::Max,
        Some("max_ic_rank_gap"),
    ),
    spec(
        "holdout_ic_tracks_research",
        Block::Validation,
        GateKind::Max,
        Some("max_holdout_ic_gap"),
    ),
    spec(
        "replay_reproducible",
        Block::Validation,
        GateKind::Bool,
        None,
    ),
    spec(
        "cross_language_parity",
        Block::Validation,
        GateKind::Bool,
        None,
    ),
    spec(
        "paper_min_sessions",
        Block::Paper,
        GateKind::Min,
        Some("min_paper_sessions"),
    ),
    spec(
        "paper_ic_tracking",
        Block::Paper,
        GateKind::Max,
        Some("max_paper_ic_gap"),
    ),
    spec(
        "paper_net_pnl",
        Block::Paper,
        GateKind::Min,
        Some("min_paper_net_pnl"),
    ),
    spec(
        "no_kill_events",
        Block::Paper,
        GateKind::Max,
        Some("max_kill_events"),
    ),
    spec(
        "rolling_ic",
        Block::Live,
        GateKind::Min,
        Some("watch_ic_gate"),
    ),
];

/// Look a gate up by name.
pub fn spec_by_name(name: &str) -> Option<&'static GateSpec> {
    GATE_SPECS.iter().find(|s| s.name == name)
}

/// `|ic - rank_ic| / max(|ic|, eps)` — the pinned stability statistic.
pub fn ic_rank_gap(ic: f64, rank_ic: f64, eps: f64) -> f64 {
    (ic - rank_ic).abs() / ic.abs().max(eps)
}

// ---------------------------------------------------------------- config ---

/// Every promotion-gate threshold (`lifecycle.json` `gates`, key order).
#[derive(Debug, Clone, PartialEq)]
pub struct GateThresholds {
    /// `ledger_entry_exists` (>= 1).
    pub min_experiments_in_ledger: u64,
    /// `oos_ic`.
    pub min_oos_ic: f64,
    /// `statistical_significance`.
    pub min_nw_tstat: f64,
    /// `fold_consistency` (in `[0, 1]`).
    pub min_fold_sign_consistency: f64,
    /// `fold_count` (>= 1).
    pub min_folds: u64,
    /// `net_pnl_after_costs` (strict).
    pub min_net_return_bps: f64,
    /// `capacity` (>= 0).
    pub min_capacity_usd: f64,
    /// `stability` (>= 0).
    pub max_ic_rank_gap: f64,
    /// Denominator floor of the stability statistic (> 0).
    pub ic_rank_gap_eps: f64,
    /// `holdout_ic_tracks_research` (>= 0).
    pub max_holdout_ic_gap: f64,
    /// `paper_min_sessions` (>= 1).
    pub min_paper_sessions: u64,
    /// `paper_ic_tracking` (>= 0).
    pub max_paper_ic_gap: f64,
    /// `paper_net_pnl`.
    pub min_paper_net_pnl: f64,
    /// `no_kill_events` (>= 0).
    pub max_kill_events: u64,
}

const GATE_KEYS_FLOAT: [&str; 10] = [
    "min_oos_ic",
    "min_nw_tstat",
    "min_fold_sign_consistency",
    "min_net_return_bps",
    "min_capacity_usd",
    "max_ic_rank_gap",
    "ic_rank_gap_eps",
    "max_holdout_ic_gap",
    "max_paper_ic_gap",
    "min_paper_net_pnl",
];
const GATE_KEYS_INT: [&str; 4] = [
    "min_experiments_in_ledger",
    "min_folds",
    "min_paper_sessions",
    "max_kill_events",
];

fn cfg_err(msg: String) -> IapError {
    IapError::InvalidArgument(msg)
}

fn object<'a>(v: &'a Value, where_: &str) -> Result<&'a Map<String, Value>, IapError> {
    v.as_object()
        .ok_or_else(|| cfg_err(format!("{where_}: expected a JSON object")))
}

fn require_keys(
    block: &Map<String, Value>,
    expected: &[&str],
    where_: &str,
) -> Result<(), IapError> {
    let mut missing: Vec<&str> = expected
        .iter()
        .copied()
        .filter(|k| !block.contains_key(*k))
        .collect();
    let mut unknown: Vec<&str> = block
        .keys()
        .map(String::as_str)
        .filter(|k| !expected.contains(k))
        .collect();
    missing.sort_unstable();
    unknown.sort_unstable();
    if !missing.is_empty() {
        return Err(cfg_err(format!("{where_}: missing keys {missing:?}")));
    }
    if !unknown.is_empty() {
        return Err(cfg_err(format!("{where_}: unknown keys {unknown:?}")));
    }
    Ok(())
}

fn as_float(block: &Map<String, Value>, key: &str, where_: &str) -> Result<f64, IapError> {
    match block.get(key) {
        Some(Value::Number(n)) => n
            .as_f64()
            .filter(|x| x.is_finite())
            .ok_or_else(|| cfg_err(format!("{where_}.{key}: non-finite number"))),
        Some(other) => Err(cfg_err(format!(
            "{where_}.{key}: expected a number, got {}",
            type_name(other)
        ))),
        None => Err(cfg_err(format!("{where_}: missing key {key:?}"))),
    }
}

fn as_int(
    block: &Map<String, Value>,
    key: &str,
    where_: &str,
    minimum: i64,
) -> Result<u64, IapError> {
    match block.get(key) {
        Some(Value::Number(n)) if n.is_i64() || n.is_u64() => {
            let v = n
                .as_i64()
                .ok_or_else(|| cfg_err(format!("{where_}.{key}: integer out of range")))?;
            if v < minimum {
                return Err(cfg_err(format!("{where_}.{key}: {v} < {minimum}")));
            }
            Ok(v as u64)
        }
        Some(other) => Err(cfg_err(format!(
            "{where_}.{key}: expected an integer, got {}",
            type_name(other)
        ))),
        None => Err(cfg_err(format!("{where_}: missing key {key:?}"))),
    }
}

fn type_name(v: &Value) -> &'static str {
    match v {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(n) if n.is_f64() => "float",
        Value::Number(_) => "int",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

impl GateThresholds {
    /// Parse the `gates` block (`where_` names the file / path in errors).
    pub fn from_block(v: &Value, where_: &str) -> Result<GateThresholds, IapError> {
        let block = object(v, where_)?;
        let mut expected: Vec<&str> = GATE_KEYS_FLOAT.to_vec();
        expected.extend_from_slice(&GATE_KEYS_INT);
        require_keys(block, &expected, where_)?;
        let t = GateThresholds {
            min_experiments_in_ledger: as_int(block, "min_experiments_in_ledger", where_, 1)?,
            min_oos_ic: as_float(block, "min_oos_ic", where_)?,
            min_nw_tstat: as_float(block, "min_nw_tstat", where_)?,
            min_fold_sign_consistency: as_float(block, "min_fold_sign_consistency", where_)?,
            min_folds: as_int(block, "min_folds", where_, 1)?,
            min_net_return_bps: as_float(block, "min_net_return_bps", where_)?,
            min_capacity_usd: as_float(block, "min_capacity_usd", where_)?,
            max_ic_rank_gap: as_float(block, "max_ic_rank_gap", where_)?,
            ic_rank_gap_eps: as_float(block, "ic_rank_gap_eps", where_)?,
            max_holdout_ic_gap: as_float(block, "max_holdout_ic_gap", where_)?,
            min_paper_sessions: as_int(block, "min_paper_sessions", where_, 1)?,
            max_paper_ic_gap: as_float(block, "max_paper_ic_gap", where_)?,
            min_paper_net_pnl: as_float(block, "min_paper_net_pnl", where_)?,
            max_kill_events: as_int(block, "max_kill_events", where_, 0)?,
        };
        if !(0.0..=1.0).contains(&t.min_fold_sign_consistency) {
            return Err(cfg_err(format!(
                "{where_}.min_fold_sign_consistency must lie in [0, 1]"
            )));
        }
        if t.ic_rank_gap_eps <= 0.0 {
            return Err(cfg_err(format!("{where_}.ic_rank_gap_eps must be > 0")));
        }
        if t.max_ic_rank_gap < 0.0 {
            return Err(cfg_err(format!("{where_}.max_ic_rank_gap must be >= 0")));
        }
        if t.max_holdout_ic_gap < 0.0 || t.max_paper_ic_gap < 0.0 {
            return Err(cfg_err(format!("{where_}.max_*_ic_gap must be >= 0")));
        }
        if t.min_capacity_usd < 0.0 {
            return Err(cfg_err(format!("{where_}.min_capacity_usd must be >= 0")));
        }
        Ok(t)
    }

    /// JSON-ready view in config key order.
    pub fn to_value(&self) -> Value {
        json!({
            "min_experiments_in_ledger": self.min_experiments_in_ledger,
            "min_oos_ic": self.min_oos_ic,
            "min_nw_tstat": self.min_nw_tstat,
            "min_fold_sign_consistency": self.min_fold_sign_consistency,
            "min_folds": self.min_folds,
            "min_net_return_bps": self.min_net_return_bps,
            "min_capacity_usd": self.min_capacity_usd,
            "max_ic_rank_gap": self.max_ic_rank_gap,
            "ic_rank_gap_eps": self.ic_rank_gap_eps,
            "max_holdout_ic_gap": self.max_holdout_ic_gap,
            "min_paper_sessions": self.min_paper_sessions,
            "max_paper_ic_gap": self.max_paper_ic_gap,
            "min_paper_net_pnl": self.min_paper_net_pnl,
            "max_kill_events": self.max_kill_events,
        })
    }

    /// The threshold behind a `lifecycle.json` gate key (as `float`, the way
    /// the gates report it), or `None` for an unknown key.
    pub fn threshold(&self, key: &str) -> Option<f64> {
        Some(match key {
            "min_experiments_in_ledger" => self.min_experiments_in_ledger as f64,
            "min_oos_ic" => self.min_oos_ic,
            "min_nw_tstat" => self.min_nw_tstat,
            "min_fold_sign_consistency" => self.min_fold_sign_consistency,
            "min_folds" => self.min_folds as f64,
            "min_net_return_bps" => self.min_net_return_bps,
            "min_capacity_usd" => self.min_capacity_usd,
            "max_ic_rank_gap" => self.max_ic_rank_gap,
            "ic_rank_gap_eps" => self.ic_rank_gap_eps,
            "max_holdout_ic_gap" => self.max_holdout_ic_gap,
            "min_paper_sessions" => self.min_paper_sessions as f64,
            "max_paper_ic_gap" => self.max_paper_ic_gap,
            "min_paper_net_pnl" => self.min_paper_net_pnl,
            "max_kill_events" => self.max_kill_events as f64,
            _ => return None,
        })
    }
}

/// The live sub-machine gates and retirement rule (`strategies.json`
/// `adaptive.lifecycle`, `iap.adaptive.lifecycle.LifecycleConfig`).
#[derive(Debug, Clone, PartialEq)]
pub struct LiveConfig {
    /// Breach when `rolling_ic < watch_ic_gate` (strict).
    pub watch_ic_gate: f64,
    /// Recovery when `rolling_ic >= reactivate_ic_gate` (>= watch gate).
    pub reactivate_ic_gate: f64,
    /// Consecutive breaches in WATCH that retire (>= 1; read by the legacy
    /// consecutive rule only).
    pub retire_breach_evals: u64,
    /// Consecutive recoveries in WATCH that re-activate (>= 1).
    pub reactivate_evals: u64,
    /// The retirement rule. The block must name it.
    pub breach_rule: BreachRule,
    /// CUSUM slack below the watch gate (>= 0; 0 under the legacy rule when
    /// the block gives none).
    pub cusum_k: f64,
    /// CUSUM decision threshold (> 0 under the CUSUM rule; 0 under the legacy
    /// rule when the block gives none).
    pub cusum_h: f64,
}

const LIVE_GATE_KEYS: [&str; 4] = [
    "watch_ic_gate",
    "reactivate_ic_gate",
    "retire_breach_evals",
    "reactivate_evals",
];

impl LiveConfig {
    /// Parse the block. The four gate keys and `breach_rule` are required;
    /// `cusum_k` and `cusum_h` are required under `"cusum"` and optional
    /// under `"consecutive"`; any other key is an error.
    pub fn from_block(v: &Value, where_: &str) -> Result<LiveConfig, IapError> {
        let block = object(v, where_)?;
        for key in block.keys() {
            let known = LIVE_GATE_KEYS.contains(&key.as_str())
                || matches!(key.as_str(), "breach_rule" | "cusum_k" | "cusum_h");
            if !known {
                return Err(cfg_err(format!("{where_}: unknown key {key:?}")));
            }
        }
        let rule_name = match block.get("breach_rule") {
            Some(Value::String(s)) => s.as_str(),
            Some(other) => {
                return Err(cfg_err(format!(
                    "{where_}.breach_rule: expected a string, got {}",
                    type_name(other)
                )))
            }
            None => {
                return Err(cfg_err(format!(
                    "{where_}: missing key \"breach_rule\" (\"cusum\", the default since \
                     v1.5.0, or \"consecutive\", the rule up to v1.4.0)"
                )))
            }
        };
        let breach_rule = BreachRule::from_name(rule_name, where_)?;
        let optional = |key: &str| -> Result<f64, IapError> {
            if block.contains_key(key) {
                as_float(block, key, where_)
            } else if breach_rule == BreachRule::Cusum {
                Err(cfg_err(format!(
                    "{where_}: breach_rule \"cusum\" needs {key:?}"
                )))
            } else {
                Ok(0.0)
            }
        };
        let cfg = LiveConfig {
            watch_ic_gate: as_float(block, "watch_ic_gate", where_)?,
            reactivate_ic_gate: as_float(block, "reactivate_ic_gate", where_)?,
            retire_breach_evals: as_int(block, "retire_breach_evals", where_, 1)?,
            reactivate_evals: as_int(block, "reactivate_evals", where_, 1)?,
            breach_rule,
            cusum_k: optional("cusum_k")?,
            cusum_h: optional("cusum_h")?,
        };
        cfg.validate(where_)?;
        Ok(cfg)
    }

    /// The gates under the LEGACY consecutive-breach rule, named.
    pub fn legacy_consecutive(
        watch_ic_gate: f64,
        reactivate_ic_gate: f64,
        retire_breach_evals: u64,
        reactivate_evals: u64,
    ) -> LiveConfig {
        LiveConfig {
            watch_ic_gate,
            reactivate_ic_gate,
            retire_breach_evals,
            reactivate_evals,
            breach_rule: BreachRule::Consecutive,
            cusum_k: 0.0,
            cusum_h: 0.0,
        }
    }

    /// Consistency of the gates and the rule's parameters.
    pub fn validate(&self, where_: &str) -> Result<(), IapError> {
        if self.reactivate_ic_gate < self.watch_ic_gate {
            return Err(cfg_err(format!(
                "{where_}: reactivate_ic_gate must be >= watch_ic_gate"
            )));
        }
        if self.cusum_k < 0.0 {
            return Err(cfg_err(format!("{where_}: cusum_k must be >= 0")));
        }
        if self.breach_rule == BreachRule::Cusum && self.cusum_h <= 0.0 {
            return Err(cfg_err(format!(
                "{where_}: breach_rule \"cusum\" needs cusum_h > 0"
            )));
        }
        Ok(())
    }

    /// JSON-ready view (every key, as `LifecycleConfig.to_dict`).
    pub fn to_value(&self) -> Value {
        json!({
            "watch_ic_gate": self.watch_ic_gate,
            "reactivate_ic_gate": self.reactivate_ic_gate,
            "retire_breach_evals": self.retire_breach_evals,
            "reactivate_evals": self.reactivate_evals,
            "breach_rule": self.breach_rule.name(),
            "cusum_k": self.cusum_k,
            "cusum_h": self.cusum_h,
        })
    }
}

/// The merged lifecycle policy.
#[derive(Debug, Clone, PartialEq)]
pub struct PolicyConfig {
    /// Policy name (`lifecycle_v1`).
    pub policy: String,
    /// Promotion-gate thresholds.
    pub gates: GateThresholds,
    /// Failed evaluations at VALIDATING / PAPER that demote (>= 1).
    pub max_consecutive_failures: u64,
    /// Live sub-machine gates.
    pub live: LiveConfig,
    /// How `statistical_significance` gets its threshold.
    pub tstat_threshold: TstatThreshold,
}

fn tstat_policy(doc: &Map<String, Value>, where_: &str) -> Result<TstatThreshold, IapError> {
    match doc.get("tstat_threshold") {
        Some(Value::String(s)) => TstatThreshold::from_name(s, where_),
        Some(other) => Err(cfg_err(format!(
            "{where_}.tstat_threshold: expected a string, got {}",
            type_name(other)
        ))),
        None => Err(cfg_err(format!(
            "{where_}: missing keys [\"tstat_threshold\"]"
        ))),
    }
}

impl PolicyConfig {
    /// Parse the merged view (`{"policy", "tstat_threshold", "gates",
    /// "demotion", "live"}`), the form embedded in
    /// `tests/golden/expected_lifecycle.json`.
    pub fn from_value(v: &Value) -> Result<PolicyConfig, IapError> {
        let doc = object(v, "config")?;
        require_keys(
            doc,
            &["policy", "tstat_threshold", "gates", "demotion", "live"],
            "config",
        )?;
        let policy = doc["policy"]
            .as_str()
            .filter(|s| !s.is_empty())
            .ok_or_else(|| cfg_err("config.policy: expected a non-empty string".to_string()))?;
        let demotion = object(&doc["demotion"], "config.demotion")?;
        require_keys(demotion, &["max_consecutive_failures"], "config.demotion")?;
        Ok(PolicyConfig {
            policy: policy.to_string(),
            gates: GateThresholds::from_block(&doc["gates"], "config.gates")?,
            max_consecutive_failures: as_int(
                demotion,
                "max_consecutive_failures",
                "config.demotion",
                1,
            )?,
            live: LiveConfig::from_block(&doc["live"], "config.live")?,
            tstat_threshold: tstat_policy(doc, "config")?,
        })
    }

    /// JSON-ready merged view (the golden's `config`).
    pub fn to_value(&self) -> Value {
        json!({
            "policy": self.policy,
            "tstat_threshold": self.tstat_threshold.name(),
            "gates": self.gates.to_value(),
            "demotion": {"max_consecutive_failures": self.max_consecutive_failures},
            "live": self.live.to_value(),
        })
    }

    /// Load and validate the policy from the two pinned config files; every
    /// error names the file and the key.
    pub fn load(lifecycle_path: &Path, strategies_path: &Path) -> Result<PolicyConfig, IapError> {
        let lc = read_json(lifecycle_path)?;
        let where_ = lifecycle_path.display().to_string();
        let doc = object(&lc, &where_)?;
        let version = doc.get("x-version").cloned().unwrap_or(Value::Null);
        if version.as_u64() != Some(LIFECYCLE_CONFIG_VERSION) {
            return Err(cfg_err(format!(
                "{where_}: x-version {version} != {LIFECYCLE_CONFIG_VERSION}"
            )));
        }
        require_keys(
            doc,
            &[
                "x-version",
                "description",
                "policy",
                "tstat_threshold",
                "gates",
                "demotion",
            ],
            &where_,
        )?;
        let tstat_threshold = tstat_policy(doc, &where_)?;
        let policy = doc["policy"]
            .as_str()
            .filter(|s| !s.is_empty())
            .ok_or_else(|| cfg_err(format!("{where_}.policy: expected a non-empty string")))?;
        let demotion = object(&doc["demotion"], &format!("{where_}.demotion"))?;
        require_keys(
            demotion,
            &["max_consecutive_failures"],
            &format!("{where_}.demotion"),
        )?;
        let gates = GateThresholds::from_block(&doc["gates"], &format!("{where_}.gates"))?;
        let max_failures = as_int(
            demotion,
            "max_consecutive_failures",
            &format!("{where_}.demotion"),
            1,
        )?;

        let st = read_json(strategies_path)?;
        let st_where = strategies_path.display().to_string();
        let live_block = st
            .get("adaptive")
            .and_then(|a| a.get("lifecycle"))
            .ok_or_else(|| cfg_err(format!("{st_where}: missing adaptive.lifecycle block")))?;
        let live = LiveConfig::from_block(live_block, &format!("{st_where}: adaptive.lifecycle"))?;
        Ok(PolicyConfig {
            policy: policy.to_string(),
            gates,
            max_consecutive_failures: max_failures,
            live,
            tstat_threshold,
        })
    }

    /// The configured threshold of a gate (`None` for `bool` gates). For
    /// `statistical_significance` this is the floor `min_nw_tstat`;
    /// [`PolicyConfig::threshold_for`] gives the threshold applied to an
    /// evidence.
    pub fn threshold(&self, spec: &GateSpec) -> Option<f64> {
        match spec.threshold_key {
            None => None,
            Some("watch_ic_gate") => Some(self.live.watch_ic_gate),
            Some(key) => self.gates.threshold(key),
        }
    }

    /// The threshold a gate applies to `ev` (module docs, "The significance
    /// threshold"): `None` for `bool` gates, and for the ledger policy when
    /// the evidence carries no significance threshold.
    pub fn threshold_for(&self, spec: &GateSpec, ev: &Evidence) -> Option<f64> {
        let configured = self.threshold(spec);
        if spec.name != SIGNIFICANCE_GATE || self.tstat_threshold == TstatThreshold::Fixed {
            return configured;
        }
        match (configured, ev.significance_threshold) {
            (Some(floor), Some(carried)) => Some(if carried > floor { carried } else { floor }),
            _ => None,
        }
    }
}

fn read_json(path: &Path) -> Result<Value, IapError> {
    if !path.is_file() {
        return Err(cfg_err(format!(
            "{}: config file not found",
            path.display()
        )));
    }
    let text = std::fs::read_to_string(path)
        .map_err(|e| IapError::Io(format!("{}: {e}", path.display())))?;
    serde_json::from_str(&text).map_err(|e| cfg_err(format!("{}: {e}", path.display())))
}

// ------------------------------------------------------------- evaluation ---

/// What a gate extracted from the evidence.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Metric {
    /// Block absent or metric null.
    Absent,
    /// A numeric metric.
    Number(f64),
    /// A boolean metric.
    Bool(bool),
}

/// One gate's outcome (`lifecycle_transition#/$defs/GateResult`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GateResult {
    /// Passed.
    pub passed: bool,
    /// Observed value (null for a bool gate or a missing metric).
    #[serde(deserialize_with = "required_option")]
    pub value: Option<f64>,
    /// Threshold compared against (null for a bool gate).
    #[serde(deserialize_with = "required_option")]
    pub threshold: Option<f64>,
}

fn required_option<'de, D, T>(d: D) -> Result<Option<T>, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de>,
{
    Option::<T>::deserialize(d)
}

impl GateResult {
    /// JSON form (`{"passed", "value", "threshold"}`).
    pub fn to_value(&self) -> Value {
        json!({"passed": self.passed, "value": self.value, "threshold": self.threshold})
    }

    /// Strict parse.
    pub fn from_value(v: &Value) -> Result<GateResult, IapError> {
        serde_json::from_value(v.clone())
            .map_err(|e| IapError::Validation(format!("GateResult: {e}")))
    }
}

/// Extract a gate's metric from the evidence (the `metric` column of the
/// table).
pub fn metric(spec: &GateSpec, config: &PolicyConfig, ev: &Evidence) -> Metric {
    let research = ev.research.as_ref();
    match spec.name {
        "ledger_entry_exists" => research.map_or(Metric::Absent, |r| {
            Metric::Number(r.n_experiments_in_ledger as f64)
        }),
        "leakage_clean" => research.map_or(Metric::Absent, |r| Metric::Bool(r.leakage_passed)),
        "oos_ic" => research.map_or(Metric::Absent, |r| Metric::Number(r.ic)),
        "statistical_significance" => research.map_or(Metric::Absent, |r| Metric::Number(r.t_stat)),
        "fold_consistency" => {
            research.map_or(Metric::Absent, |r| Metric::Number(r.fold_consistency))
        }
        "fold_count" => research.map_or(Metric::Absent, |r| Metric::Number(r.n_folds as f64)),
        "hypothesis_sign" => research.map_or(Metric::Absent, |r| {
            Metric::Bool(r.hypothesis_sign_confirmed == Some(true))
        }),
        "net_pnl_after_costs" => {
            research.map_or(Metric::Absent, |r| Metric::Number(r.net_return_bps))
        }
        "capacity" => ev.capacity_usd.map_or(Metric::Absent, Metric::Number),
        "stability" => research.map_or(Metric::Absent, |r| {
            Metric::Number(ic_rank_gap(r.ic, r.rank_ic, config.gates.ic_rank_gap_eps))
        }),
        "holdout_ic_tracks_research" => ev.validation.as_ref().map_or(Metric::Absent, |v| {
            Metric::Number((v.holdout_ic - v.research_ic).abs())
        }),
        "replay_reproducible" => ev
            .validation
            .as_ref()
            .map_or(Metric::Absent, |v| Metric::Bool(v.replay_hash_match)),
        "cross_language_parity" => ev
            .validation
            .as_ref()
            .map_or(Metric::Absent, |v| Metric::Bool(v.parity)),
        "paper_min_sessions" => ev
            .paper
            .as_ref()
            .map_or(Metric::Absent, |p| Metric::Number(p.n_sessions as f64)),
        "paper_ic_tracking" => ev.paper.as_ref().map_or(Metric::Absent, |p| {
            Metric::Number((p.realized_ic - p.research_ic).abs())
        }),
        "paper_net_pnl" => ev
            .paper
            .as_ref()
            .map_or(Metric::Absent, |p| Metric::Number(p.net_pnl)),
        "no_kill_events" => ev
            .paper
            .as_ref()
            .map_or(Metric::Absent, |p| Metric::Number(p.n_kill_events as f64)),
        "rolling_ic" => match &ev.live {
            Some(l) if l.informative => l.rolling_ic.map_or(Metric::Absent, Metric::Number),
            _ => Metric::Absent,
        },
        _ => Metric::Absent,
    }
}

/// Evaluate one gate: pure, the same evidence always yields the same result.
pub fn evaluate(spec: &GateSpec, config: &PolicyConfig, ev: &Evidence) -> GateResult {
    let m = metric(spec, config, ev);
    let threshold = config.threshold_for(spec, ev);
    match spec.kind {
        GateKind::Bool => GateResult {
            passed: m == Metric::Bool(true),
            value: None,
            threshold: None,
        },
        GateKind::Min | GateKind::Max | GateKind::Gt => {
            let value = match m {
                Metric::Number(x) => x,
                Metric::Bool(b) => {
                    if b {
                        1.0
                    } else {
                        0.0
                    }
                }
                Metric::Absent => {
                    return GateResult {
                        passed: false,
                        value: None,
                        threshold,
                    }
                }
            };
            let passed = match (spec.kind, threshold) {
                (GateKind::Min, Some(t)) => value >= t,
                (GateKind::Max, Some(t)) => value <= t,
                (GateKind::Gt, Some(t)) => value > t,
                (GateKind::Bool, Some(_)) | (_, None) => false,
            };
            GateResult {
                passed,
                value: Some(value),
                threshold,
            }
        }
    }
}

/// Evaluate a gate by name (`None` for an unknown gate).
pub fn evaluate_named(name: &str, config: &PolicyConfig, ev: &Evidence) -> Option<GateResult> {
    spec_by_name(name).map(|s| evaluate(s, config, ev))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config() -> PolicyConfig {
        PolicyConfig::from_value(&json!({
            "policy": "lifecycle_v1",
            "tstat_threshold": "ledger",
            "gates": {
                "min_experiments_in_ledger": 1, "min_oos_ic": 0.01, "min_nw_tstat": 3.0,
                "min_fold_sign_consistency": 0.7, "min_folds": 3, "min_net_return_bps": 0.0,
                "min_capacity_usd": 1000000.0, "max_ic_rank_gap": 1.0, "ic_rank_gap_eps": 1e-12,
                "max_holdout_ic_gap": 0.01, "min_paper_sessions": 5, "max_paper_ic_gap": 0.01,
                "min_paper_net_pnl": 0.0, "max_kill_events": 0
            },
            "demotion": {"max_consecutive_failures": 3},
            "live": {"watch_ic_gate": 0.0, "reactivate_ic_gate": 0.005, "retire_breach_evals": 6,
                     "reactivate_evals": 3, "breach_rule": "cusum", "cusum_k": 0.0025, "cusum_h": 0.01}
        }))
        .expect("valid config")
    }

    fn research(t_stat: f64) -> crate::evidence::ExperimentResult {
        crate::evidence::ExperimentResult {
            experiment_id: "g-research".to_string(),
            alpha_id: "G".to_string(),
            dataset_version: "0123456789abcdef".repeat(4),
            feature_version: "fedcba9876543210".repeat(4),
            model_version: None,
            ic: 0.02,
            rank_ic: 0.03,
            t_stat,
            nw_lags: 2,
            hit_rate: 0.55,
            turnover: 40.0,
            gross_return_bps: 12.0,
            transaction_cost_bps: 7.0,
            net_return_bps: 5.0,
            max_drawdown_bps: 20.0,
            sharpe: 1.5,
            fold_consistency: 1.0,
            n_folds: 4,
            leakage_passed: true,
            leakage_detail: json!({}),
            hypothesis_sign_confirmed: Some(true),
            verdict: crate::evidence::Verdict::Promote,
            n_experiments_in_ledger: 10,
            git_commit: "test".to_string(),
            created_ts: 0,
        }
    }

    #[test]
    fn significance_threshold_comes_from_the_evidence_under_the_ledger_policy() {
        let cfg = config();
        let gate = spec_by_name(SIGNIFICANCE_GATE).expect("known");
        let mut ev = Evidence::empty();
        ev.research = Some(research(4.0));
        // no threshold in the evidence: fails, threshold null, value kept
        let r = evaluate(gate, &cfg, &ev);
        assert_eq!((r.passed, r.value, r.threshold), (false, Some(4.0), None));
        // a threshold above the floor applies
        ev.significance_threshold = Some(4.5);
        let r = evaluate(gate, &cfg, &ev);
        assert_eq!((r.passed, r.threshold), (false, Some(4.5)));
        ev.significance_threshold = Some(4.0);
        assert!(evaluate(gate, &cfg, &ev).passed, "min is inclusive");
        // one below the floor is replaced by the floor
        ev.significance_threshold = Some(2.0);
        let r = evaluate(gate, &cfg, &ev);
        assert_eq!((r.passed, r.threshold), (true, Some(3.0)));
        // no research block: value null; the threshold is what the evidence gives
        let mut bare = Evidence::empty();
        let r = evaluate(gate, &cfg, &bare);
        assert_eq!((r.passed, r.value, r.threshold), (false, None, None));
        bare.significance_threshold = Some(4.5);
        assert_eq!(evaluate(gate, &cfg, &bare).threshold, Some(4.5));
        // the legacy fixed policy reads the config alone
        let mut fixed = cfg.clone();
        fixed.tstat_threshold = TstatThreshold::Fixed;
        ev.significance_threshold = None;
        let r = evaluate(gate, &fixed, &ev);
        assert_eq!((r.passed, r.threshold), (true, Some(3.0)));
        ev.significance_threshold = Some(9.0);
        assert_eq!(evaluate(gate, &fixed, &ev).threshold, Some(3.0));
        // every other gate keeps its configured threshold
        let oos = spec_by_name("oos_ic").expect("known");
        assert_eq!(cfg.threshold_for(oos, &ev), cfg.threshold(oos));
    }

    #[test]
    fn table_is_unique_and_bool_gates_have_no_key() {
        let mut names: Vec<&str> = GATE_SPECS.iter().map(|s| s.name).collect();
        names.sort_unstable();
        names.dedup();
        assert_eq!(names.len(), GATE_SPECS.len());
        for s in &GATE_SPECS {
            assert_eq!(
                s.kind == GateKind::Bool,
                s.threshold_key.is_none(),
                "{}",
                s.name
            );
        }
        let cfg = config();
        for s in &GATE_SPECS {
            assert_eq!(
                cfg.threshold(s).is_some(),
                s.threshold_key.is_some(),
                "{}",
                s.name
            );
        }
    }

    #[test]
    fn absent_blocks_fail_with_null_value() {
        let cfg = config();
        let ev = Evidence::empty();
        for s in &GATE_SPECS {
            let r = evaluate(s, &cfg, &ev);
            assert!(!r.passed, "{}", s.name);
            assert_eq!(r.value, None, "{}", s.name);
            // the significance gate has no threshold without one in the
            // evidence (ledger policy); every other numeric gate has its own
            assert_eq!(
                r.threshold.is_some(),
                s.kind != GateKind::Bool && s.name != SIGNIFICANCE_GATE,
                "{}",
                s.name
            );
        }
    }

    #[test]
    fn comparison_kinds() {
        let cfg = config();
        let mut ev = Evidence::empty();
        ev.capacity_usd = Some(1_000_000.0);
        let r = evaluate(spec_by_name("capacity").expect("known"), &cfg, &ev);
        assert!(r.passed, "min is inclusive");
        assert_eq!(r.value, Some(1_000_000.0));
        ev.paper = Some(crate::evidence::PaperEvidence {
            n_sessions: 5,
            realized_ic: 0.04,
            research_ic: 0.01,
            net_pnl: 0.0,
            n_kill_events: 0,
            tracking_error: 0.0,
        });
        assert!(!evaluate(spec_by_name("paper_ic_tracking").expect("known"), &cfg, &ev).passed);
        assert!(evaluate(spec_by_name("paper_net_pnl").expect("known"), &cfg, &ev).passed);
        assert!(evaluate(spec_by_name("no_kill_events").expect("known"), &cfg, &ev).passed);
        ev.live = Some(crate::evidence::LiveEvidence {
            rolling_ic: Some(0.0),
            n_buckets: 8,
            eval_index: 1,
            informative: false,
            new_fraction: 0.125,
        });
        let r = evaluate(spec_by_name("rolling_ic").expect("known"), &cfg, &ev);
        assert!(
            !r.passed && r.value.is_none(),
            "uninformative reading is absent"
        );
    }

    #[test]
    fn config_errors_name_the_key() {
        let mut doc = config().to_value();
        doc["gates"]["min_folds"] = json!(0);
        let err = PolicyConfig::from_value(&doc).expect_err("min_folds 0");
        assert!(err.to_string().contains("min_folds"), "{err}");
        let mut doc = config().to_value();
        doc["gates"]["extra"] = json!(1);
        assert!(PolicyConfig::from_value(&doc)
            .expect_err("unknown key")
            .to_string()
            .contains("extra"));
        let mut doc = config().to_value();
        doc["gates"]["min_oos_ic"] = json!(true);
        assert!(PolicyConfig::from_value(&doc)
            .expect_err("bool is not a number")
            .to_string()
            .contains("min_oos_ic"));
        let mut doc = config().to_value();
        doc["live"]["reactivate_ic_gate"] = json!(-1.0);
        assert!(PolicyConfig::from_value(&doc).is_err());
        // the live block must name its rule, and the CUSUM rule its parameters
        let mut doc = config().to_value();
        doc["live"]
            .as_object_mut()
            .expect("object")
            .remove("breach_rule");
        assert!(PolicyConfig::from_value(&doc)
            .expect_err("no rule named")
            .to_string()
            .contains("breach_rule"));
        let mut doc = config().to_value();
        doc["live"].as_object_mut().expect("object").remove("cusum_h");
        assert!(PolicyConfig::from_value(&doc)
            .expect_err("cusum without a threshold")
            .to_string()
            .contains("cusum_h"));
        let mut doc = config().to_value();
        doc["live"]["breach_rule"] = json!("consecutive");
        doc["live"].as_object_mut().expect("object").remove("cusum_h");
        doc["live"].as_object_mut().expect("object").remove("cusum_k");
        let legacy = PolicyConfig::from_value(&doc).expect("the legacy rule needs no cusum keys");
        assert_eq!(legacy.live, LiveConfig::legacy_consecutive(0.0, 0.005, 6, 3));
        let mut doc = config().to_value();
        doc["tstat_threshold"] = json!("bonferroni");
        assert!(PolicyConfig::from_value(&doc)
            .expect_err("unknown policy")
            .to_string()
            .contains("tstat_threshold"));
        // the merged view round-trips with every key
        assert_eq!(
            PolicyConfig::from_value(&config().to_value()).expect("round trip"),
            config()
        );
        assert!(PolicyConfig::load(
            Path::new("/nonexistent/lifecycle.json"),
            Path::new("/nonexistent/s.json")
        )
        .is_err());
    }
}
