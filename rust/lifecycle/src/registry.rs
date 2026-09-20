//! Persistent lifecycle state (`iap.lifecycle.registry`): the alpha registry
//! (`research/alpha_registry.json`, `x-version` 1) and the append-only
//! transition log (`research/lifecycle_transitions.jsonl`).
//!
//! The registry file is Python `json.dumps(sort_keys=True, indent=2,
//! ensure_ascii=True)` plus one trailing newline; [`AlphaRegistry::render`]
//! reproduces it byte-for-byte (identical state ⇒ identical bytes), so a
//! registry written by either language reloads in the other.

use std::collections::BTreeMap;
use std::io::Write;
use std::path::Path;

use contracts::{indented_json, is_generic_id, is_sha256_hex};
use marketdata::IapError;
use serde_json::{json, Map, Value};

use crate::machine::{GateEvaluation, LifecycleTransition};
use crate::state::LifecycleState;

/// `x-version` of `research/alpha_registry.json`.
pub const REGISTRY_VERSION: u64 = 1;

const REGISTRY_DESCRIPTION: &str =
    "Alpha promotion lifecycle registry (iap.lifecycle). One record per alpha: \
current LifecycleState, the event time it was entered, the last transition \
and gate evaluation, the research versions it was registered from and the \
counters the machine resumes from. Deterministic and wall-clock free: an \
identical rerun of `python -m iap.lifecycle bootstrap` reproduces the bytes.";

/// One alpha's lifecycle row. Mutated only by
/// [`crate::machine::AlphaLifecycle`].
#[derive(Debug, Clone, PartialEq)]
pub struct AlphaRecord {
    /// Alpha id.
    pub alpha_id: String,
    /// Current state.
    pub state: LifecycleState,
    /// Event time the state was entered.
    pub since_ts: i64,
    /// Last transition made.
    pub last_transition: Option<LifecycleTransition>,
    /// Last gate evaluation.
    pub last_evaluation: Option<GateEvaluation>,
    /// Experiment the alpha was registered from.
    pub experiment_id: Option<String>,
    /// Dataset hash.
    pub data_version: Option<String>,
    /// Feature-registry hash.
    pub feature_version: Option<String>,
    /// Model hash.
    pub model_version: Option<String>,
    /// Failed evaluations in a row at VALIDATING / PAPER.
    pub consecutive_failures: u64,
    /// Live breach counter (mirrors the tracker).
    pub breach_count: u64,
    /// Live recovery counter (mirrors the tracker).
    pub recovery_count: u64,
}

fn check_optional_sha(value: &Option<String>, name: &str) -> Result<(), IapError> {
    match value {
        Some(s) if !is_sha256_hex(s) => Err(IapError::Validation(format!(
            "{name}: expected a lowercase sha256 hex or null"
        ))),
        _ => Ok(()),
    }
}

impl AlphaRecord {
    /// A freshly registered alpha: RESEARCH, no history, zero counters.
    pub fn new(alpha_id: &str, since_ts: i64) -> Result<AlphaRecord, IapError> {
        let rec = AlphaRecord {
            alpha_id: alpha_id.to_string(),
            state: LifecycleState::Research,
            since_ts,
            last_transition: None,
            last_evaluation: None,
            experiment_id: None,
            data_version: None,
            feature_version: None,
            model_version: None,
            consecutive_failures: 0,
            breach_count: 0,
            recovery_count: 0,
        };
        rec.validate()?;
        Ok(rec)
    }

    /// Id alphabet and hash patterns.
    pub fn validate(&self) -> Result<(), IapError> {
        if !is_generic_id(&self.alpha_id) {
            return Err(IapError::Validation(format!(
                "AlphaRecord: {:?} is not a valid alpha id",
                self.alpha_id
            )));
        }
        if let Some(e) = &self.experiment_id {
            if !is_generic_id(e) {
                return Err(IapError::Validation(format!(
                    "AlphaRecord.experiment_id: {e:?} is not a valid identifier"
                )));
            }
        }
        check_optional_sha(&self.data_version, "AlphaRecord.data_version")?;
        check_optional_sha(&self.feature_version, "AlphaRecord.feature_version")?;
        check_optional_sha(&self.model_version, "AlphaRecord.model_version")?;
        Ok(())
    }

    /// JSON document.
    pub fn to_value(&self) -> Result<Value, IapError> {
        self.validate()?;
        let last_transition = match &self.last_transition {
            Some(t) => t.to_value()?,
            None => Value::Null,
        };
        let last_evaluation = match &self.last_evaluation {
            Some(e) => e.to_value()?,
            None => Value::Null,
        };
        Ok(json!({
            "alpha_id": self.alpha_id,
            "state": self.state.name(),
            "state_index": self.state.index(),
            "since_ts": self.since_ts,
            "last_transition": last_transition,
            "last_evaluation": last_evaluation,
            "experiment_id": self.experiment_id,
            "data_version": self.data_version,
            "feature_version": self.feature_version,
            "model_version": self.model_version,
            "consecutive_failures": self.consecutive_failures,
            "breach_count": self.breach_count,
            "recovery_count": self.recovery_count,
        }))
    }

    /// Strict parse (`state_index` must agree with `state`).
    pub fn from_value(v: &Value) -> Result<AlphaRecord, IapError> {
        let err = |m: String| IapError::Validation(format!("AlphaRecord: {m}"));
        let doc = v
            .as_object()
            .ok_or_else(|| err("expected an object".to_string()))?;
        let expected = [
            "alpha_id",
            "state",
            "state_index",
            "since_ts",
            "last_transition",
            "last_evaluation",
            "experiment_id",
            "data_version",
            "feature_version",
            "model_version",
            "consecutive_failures",
            "breach_count",
            "recovery_count",
        ];
        for key in expected {
            if !doc.contains_key(key) {
                return Err(err(format!("missing key {key:?}")));
            }
        }
        for key in doc.keys() {
            if !expected.contains(&key.as_str()) {
                return Err(err(format!("unknown key {key:?}")));
            }
        }
        let alpha_id = doc["alpha_id"]
            .as_str()
            .ok_or_else(|| err("alpha_id: expected a string".to_string()))?;
        let state = LifecycleState::from_name(
            doc["state"]
                .as_str()
                .ok_or_else(|| err("state: expected a string".to_string()))?,
        )?;
        if doc["state_index"].as_u64() != Some(u64::from(state.index())) {
            return Err(err(format!("{alpha_id}: state_index disagrees with state")));
        }
        let opt_str = |key: &str| -> Result<Option<String>, IapError> {
            match &doc[key] {
                Value::Null => Ok(None),
                Value::String(s) => Ok(Some(s.clone())),
                _ => Err(err(format!("{key}: expected a string or null"))),
            }
        };
        let count = |key: &str| -> Result<u64, IapError> {
            doc[key]
                .as_u64()
                .ok_or_else(|| err(format!("{key}: expected a non-negative integer")))
        };
        let rec = AlphaRecord {
            alpha_id: alpha_id.to_string(),
            state,
            since_ts: doc["since_ts"]
                .as_i64()
                .ok_or_else(|| err("since_ts: expected an integer".to_string()))?,
            last_transition: match &doc["last_transition"] {
                Value::Null => None,
                other => Some(LifecycleTransition::from_value(other)?),
            },
            last_evaluation: match &doc["last_evaluation"] {
                Value::Null => None,
                other => Some(GateEvaluation::from_value(other)?),
            },
            experiment_id: opt_str("experiment_id")?,
            data_version: opt_str("data_version")?,
            feature_version: opt_str("feature_version")?,
            model_version: opt_str("model_version")?,
            consecutive_failures: count("consecutive_failures")?,
            breach_count: count("breach_count")?,
            recovery_count: count("recovery_count")?,
        };
        rec.validate()?;
        Ok(rec)
    }
}

/// The set of [`AlphaRecord`]s keyed by alpha id, with byte-deterministic
/// JSON persistence.
#[derive(Debug, Clone, PartialEq)]
pub struct AlphaRegistry {
    policy: String,
    records: BTreeMap<String, AlphaRecord>,
}

impl AlphaRegistry {
    /// An empty registry under `policy`.
    pub fn new(policy: &str) -> Result<AlphaRegistry, IapError> {
        if policy.is_empty() {
            return Err(IapError::InvalidArgument(
                "AlphaRegistry: policy name must not be empty".to_string(),
            ));
        }
        Ok(AlphaRegistry {
            policy: policy.to_string(),
            records: BTreeMap::new(),
        })
    }

    /// Policy name.
    pub fn policy(&self) -> &str {
        &self.policy
    }

    /// Number of alphas.
    pub fn len(&self) -> usize {
        self.records.len()
    }

    /// True when no alpha is registered.
    pub fn is_empty(&self) -> bool {
        self.records.is_empty()
    }

    /// True when `alpha_id` is registered.
    pub fn contains(&self, alpha_id: &str) -> bool {
        self.records.contains_key(alpha_id)
    }

    /// Sorted alpha ids (the only iteration order the registry offers).
    pub fn alpha_ids(&self) -> Vec<&str> {
        self.records.keys().map(String::as_str).collect()
    }

    /// The record of an alpha.
    pub fn get(&self, alpha_id: &str) -> Result<&AlphaRecord, IapError> {
        self.records
            .get(alpha_id)
            .ok_or_else(|| IapError::InvalidArgument(format!("unknown alpha {alpha_id:?}")))
    }

    /// Mutable record of an alpha.
    pub fn get_mut(&mut self, alpha_id: &str) -> Result<&mut AlphaRecord, IapError> {
        self.records
            .get_mut(alpha_id)
            .ok_or_else(|| IapError::InvalidArgument(format!("unknown alpha {alpha_id:?}")))
    }

    /// Register a record (an already registered id is an error).
    pub fn add(&mut self, record: AlphaRecord) -> Result<&AlphaRecord, IapError> {
        record.validate()?;
        if self.records.contains_key(&record.alpha_id) {
            return Err(IapError::InvalidArgument(format!(
                "alpha {:?} is already registered",
                record.alpha_id
            )));
        }
        let id = record.alpha_id.clone();
        self.records.insert(id.clone(), record);
        self.get(&id)
    }

    /// Records in sorted alpha-id order.
    pub fn records(&self) -> impl Iterator<Item = &AlphaRecord> {
        self.records.values()
    }

    /// The JSON document.
    pub fn to_value(&self) -> Result<Value, IapError> {
        let mut alphas = Map::new();
        for (id, rec) in &self.records {
            alphas.insert(id.clone(), rec.to_value()?);
        }
        Ok(json!({
            "x-version": REGISTRY_VERSION,
            "description": REGISTRY_DESCRIPTION,
            "policy": self.policy,
            "alphas": Value::Object(alphas),
        }))
    }

    /// The exact file text: sorted keys, 2-space indent, ASCII, one trailing
    /// newline.
    pub fn render(&self) -> Result<String, IapError> {
        let mut text = indented_json(&self.to_value()?, 2)?;
        text.push('\n');
        Ok(text)
    }

    /// Write the file (`render`).
    pub fn save(&self, path: &Path) -> Result<(), IapError> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::write(path, self.render()?)?;
        Ok(())
    }

    /// Strict parse of the document.
    pub fn from_value(doc: &Value) -> Result<AlphaRegistry, IapError> {
        let err = |m: String| IapError::Validation(format!("alpha registry: {m}"));
        let map = doc
            .as_object()
            .ok_or_else(|| err("expected an object".to_string()))?;
        if map.get("x-version").and_then(Value::as_u64) != Some(REGISTRY_VERSION) {
            return Err(err(format!(
                "x-version {} != {REGISTRY_VERSION}",
                map.get("x-version").cloned().unwrap_or(Value::Null)
            )));
        }
        let policy = map
            .get("policy")
            .and_then(Value::as_str)
            .ok_or_else(|| err("policy: expected a string".to_string()))?;
        let mut registry = AlphaRegistry::new(policy)?;
        let alphas = map
            .get("alphas")
            .and_then(Value::as_object)
            .ok_or_else(|| err("alphas: expected an object".to_string()))?;
        for (alpha_id, v) in alphas {
            let record = AlphaRecord::from_value(v)?;
            if record.alpha_id != *alpha_id {
                return Err(err(format!(
                    "key {alpha_id:?} != record {:?}",
                    record.alpha_id
                )));
            }
            registry.add(record)?;
        }
        Ok(registry)
    }

    /// Load a registry file.
    pub fn load(path: &Path) -> Result<AlphaRegistry, IapError> {
        let text = std::fs::read_to_string(path)
            .map_err(|e| IapError::Io(format!("{}: {e}", path.display())))?;
        let doc: Value = serde_json::from_str(&text)
            .map_err(|e| IapError::Codec(format!("{}: {e}", path.display())))?;
        AlphaRegistry::from_value(&doc)
    }
}

/// Append-only canonical-JSON-lines log of [`LifecycleTransition`]s
/// (`research/lifecycle_transitions.jsonl`).
#[derive(Debug)]
pub struct TransitionLog<W: Write> {
    sink: W,
    lines: u64,
}

impl<W: Write> TransitionLog<W> {
    /// Wrap a writer opened for append.
    pub fn new(sink: W) -> TransitionLog<W> {
        TransitionLog { sink, lines: 0 }
    }

    /// Validate and append one canonical line.
    pub fn append(&mut self, transition: &LifecycleTransition) -> Result<(), IapError> {
        let line = transition.to_canonical_json()?;
        self.sink.write_all(line.as_bytes())?;
        self.sink.write_all(b"\n")?;
        self.sink.flush()?;
        self.lines += 1;
        Ok(())
    }

    /// Lines written by this instance.
    pub fn lines(&self) -> u64 {
        self.lines
    }

    /// Consume, returning the writer.
    pub fn into_inner(self) -> W {
        self.sink
    }

    /// Parse a log's text back into transitions (file order, strict).
    pub fn read_all(text: &str) -> Result<Vec<LifecycleTransition>, IapError> {
        let mut out = Vec::new();
        for line in text.lines() {
            if line.trim().is_empty() {
                continue;
            }
            let v: Value = serde_json::from_str(line)
                .map_err(|e| IapError::Codec(format!("transition log: {e}")))?;
            out.push(LifecycleTransition::from_value(&v)?);
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_registry_renders_and_reloads() {
        let mut reg = AlphaRegistry::new("lifecycle_v1").expect("policy");
        reg.add(AlphaRecord::new("EQ01", 5).expect("id"))
            .expect("new");
        assert!(reg.add(AlphaRecord::new("EQ01", 5).expect("id")).is_err());
        let text = reg.render().expect("finite");
        assert!(text.ends_with("}\n"));
        assert!(text
            .starts_with("{\n  \"alphas\": {\n    \"EQ01\": {\n      \"alpha_id\": \"EQ01\",\n"));
        let doc: Value = serde_json::from_str(&text).expect("json");
        let back = AlphaRegistry::from_value(&doc).expect("strict");
        assert_eq!(back, reg);
        assert_eq!(back.render().expect("finite"), text);
        assert!(AlphaRecord::new("bad id", 0).is_err());
        assert!(AlphaRegistry::new("").is_err());
        let mut wrong = doc.clone();
        wrong["alphas"]["EQ01"]["state_index"] = json!(3);
        assert!(AlphaRegistry::from_value(&wrong).is_err());
        let mut wrong = doc;
        wrong["x-version"] = json!(2);
        assert!(AlphaRegistry::from_value(&wrong).is_err());
    }
}
