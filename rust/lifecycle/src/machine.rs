//! The alpha promotion state machine (`iap.lifecycle.machine.AlphaLifecycle`).
//!
//! The transition table [`ALLOWED_TRANSITIONS`] is data: one [`Edge`] per
//! allowed move with its kind, the actor that may take it and the gates it
//! evaluates, in pinned order. [`AlphaLifecycle::advance`] looks up the
//! SYSTEM edge leaving the current state, evaluates its gates against the
//! [`Evidence`] and applies the outcome:
//!
//! * **promotion** (RESEARCH..PAPER): every gate passes ⇒ one state up,
//!   failure counter reset;
//! * **demotion**: at CANDIDATE a failed `leakage_clean` demotes to RESEARCH
//!   at once; at VALIDATING / PAPER each failed evaluation increments
//!   `consecutive_failures` and the `max_consecutive_failures`-th one demotes
//!   to CANDIDATE; a passing evaluation resets the counter;
//! * **silence**: an absent evidence block for the edge (`research` at
//!   CANDIDATE, `validation` at VALIDATING, `paper` at PAPER, `live` / a null
//!   or uninformative rolling IC at ACTIVE / WATCH) evaluates nothing and
//!   moves nothing — not even the failure counter (`NO_EVIDENCE`). RESEARCH is
//!   the exception by construction: `ledger_entry_exists` IS the presence check;
//! * **live** (ACTIVE / WATCH): delegated unchanged to the
//!   [`LiveTracker`] rules; its transition is wrapped into a
//!   [`LifecycleTransition`] with `gates = {"rolling_ic": …}`;
//! * **RETIRED is terminal for SYSTEM**: `advance` records `TERMINAL` and
//!   moves nothing; re-entry is the HUMAN [`AlphaLifecycle::reset_to_research`].
//!
//! Manual edges (`retire`, `reset_to_research`) require `Actor::Human` and a
//! non-empty reason. Every call is a pure function of the registry state and
//! its arguments: no wall clock, no RNG, sorted iteration only.

use std::collections::BTreeMap;

use contracts::{canonical_json, is_generic_id};
use marketdata::IapError;
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};

use crate::evidence::Evidence;
use crate::gates::{evaluate_named, GateResult, PolicyConfig};
use crate::registry::{AlphaRecord, AlphaRegistry};
use crate::state::LifecycleState;
use crate::tracker::{LiveTracker, TrackerTransition};

/// Who took a transition (`lifecycle_transition.schema.json` `actor`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum Actor {
    /// The automatic policy.
    #[serde(rename = "SYSTEM")]
    System,
    /// A human override.
    #[serde(rename = "HUMAN")]
    Human,
}

impl Actor {
    /// Wire name.
    pub fn name(self) -> &'static str {
        match self {
            Actor::System => "SYSTEM",
            Actor::Human => "HUMAN",
        }
    }
}

/// Why an edge exists.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum EdgeKind {
    /// SYSTEM promotion on all gates passing.
    Promotion,
    /// SYSTEM demotion (leakage at CANDIDATE, repeated failure at VALIDATING / PAPER).
    Demotion,
    /// SYSTEM live rolling-IC rule.
    Live,
    /// HUMAN retire / reset.
    Manual,
}

impl EdgeKind {
    /// Wire name.
    pub fn name(self) -> &'static str {
        match self {
            EdgeKind::Promotion => "PROMOTION",
            EdgeKind::Demotion => "DEMOTION",
            EdgeKind::Live => "LIVE",
            EdgeKind::Manual => "MANUAL",
        }
    }
}

/// One allowed transition: who may take it and which gates it evaluates.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Edge {
    /// State before.
    pub from_state: LifecycleState,
    /// State after.
    pub to_state: LifecycleState,
    /// Kind.
    pub kind: EdgeKind,
    /// Actor allowed to take it.
    pub actor: Actor,
    /// Gates, in evaluation order.
    pub gates: &'static [&'static str],
}

const fn edge(
    from_state: LifecycleState,
    to_state: LifecycleState,
    kind: EdgeKind,
    actor: Actor,
    gates: &'static [&'static str],
) -> Edge {
    Edge {
        from_state,
        to_state,
        kind,
        actor,
        gates,
    }
}

const LIVE_GATES: &[&str] = &["rolling_ic"];

/// The transition table (pinned order, lifecycle.md §2).
pub const ALLOWED_TRANSITIONS: [Edge; 17] = [
    edge(
        LifecycleState::Research,
        LifecycleState::Candidate,
        EdgeKind::Promotion,
        Actor::System,
        &["ledger_entry_exists", "leakage_clean"],
    ),
    edge(
        LifecycleState::Candidate,
        LifecycleState::Validating,
        EdgeKind::Promotion,
        Actor::System,
        &[
            "leakage_clean",
            "oos_ic",
            "statistical_significance",
            "fold_consistency",
            "fold_count",
            "hypothesis_sign",
            "net_pnl_after_costs",
            "capacity",
            "stability",
        ],
    ),
    edge(
        LifecycleState::Candidate,
        LifecycleState::Research,
        EdgeKind::Demotion,
        Actor::System,
        &["leakage_clean"],
    ),
    edge(
        LifecycleState::Validating,
        LifecycleState::Paper,
        EdgeKind::Promotion,
        Actor::System,
        &[
            "holdout_ic_tracks_research",
            "replay_reproducible",
            "cross_language_parity",
        ],
    ),
    edge(
        LifecycleState::Validating,
        LifecycleState::Candidate,
        EdgeKind::Demotion,
        Actor::System,
        &[],
    ),
    edge(
        LifecycleState::Paper,
        LifecycleState::Active,
        EdgeKind::Promotion,
        Actor::System,
        &[
            "paper_min_sessions",
            "paper_ic_tracking",
            "paper_net_pnl",
            "no_kill_events",
        ],
    ),
    edge(
        LifecycleState::Paper,
        LifecycleState::Candidate,
        EdgeKind::Demotion,
        Actor::System,
        &[],
    ),
    edge(
        LifecycleState::Active,
        LifecycleState::Watch,
        EdgeKind::Live,
        Actor::System,
        LIVE_GATES,
    ),
    edge(
        LifecycleState::Watch,
        LifecycleState::Active,
        EdgeKind::Live,
        Actor::System,
        LIVE_GATES,
    ),
    edge(
        LifecycleState::Watch,
        LifecycleState::Retired,
        EdgeKind::Live,
        Actor::System,
        LIVE_GATES,
    ),
    edge(
        LifecycleState::Research,
        LifecycleState::Retired,
        EdgeKind::Manual,
        Actor::Human,
        &[],
    ),
    edge(
        LifecycleState::Candidate,
        LifecycleState::Retired,
        EdgeKind::Manual,
        Actor::Human,
        &[],
    ),
    edge(
        LifecycleState::Validating,
        LifecycleState::Retired,
        EdgeKind::Manual,
        Actor::Human,
        &[],
    ),
    edge(
        LifecycleState::Paper,
        LifecycleState::Retired,
        EdgeKind::Manual,
        Actor::Human,
        &[],
    ),
    edge(
        LifecycleState::Active,
        LifecycleState::Retired,
        EdgeKind::Manual,
        Actor::Human,
        &[],
    ),
    edge(
        LifecycleState::Watch,
        LifecycleState::Retired,
        EdgeKind::Manual,
        Actor::Human,
        &[],
    ),
    edge(
        LifecycleState::Retired,
        LifecycleState::Research,
        EdgeKind::Manual,
        Actor::Human,
        &[],
    ),
];

/// The table entry for `(from, to, kind)`.
pub fn edge_for(
    from_state: LifecycleState,
    to_state: LifecycleState,
    kind: EdgeKind,
) -> Result<&'static Edge, IapError> {
    ALLOWED_TRANSITIONS
        .iter()
        .find(|e| e.from_state == from_state && e.to_state == to_state && e.kind == kind)
        .ok_or_else(|| {
            IapError::InvalidArgument(format!(
                "no {} edge {} -> {}",
                kind.name(),
                from_state.name(),
                to_state.name()
            ))
        })
}

/// The SYSTEM promotion edge leaving a pre-live state.
pub fn promotion_edge(state: LifecycleState) -> Option<&'static Edge> {
    ALLOWED_TRANSITIONS
        .iter()
        .find(|e| e.from_state == state && e.kind == EdgeKind::Promotion)
}

/// The evidence block a promotion edge needs before its gates are evaluated
/// (RESEARCH has none: its presence gate does the checking).
fn required_block_present(state: LifecycleState, ev: &Evidence) -> bool {
    match state {
        LifecycleState::Research => true,
        LifecycleState::Candidate => ev.research.is_some(),
        LifecycleState::Validating => ev.validation.is_some(),
        LifecycleState::Paper => ev.paper.is_some(),
        LifecycleState::Active | LifecycleState::Watch | LifecycleState::Retired => false,
    }
}

/// JSON-ready copy of [`ALLOWED_TRANSITIONS`] (the golden's `transition_table`).
pub fn transition_table() -> Value {
    Value::Array(
        ALLOWED_TRANSITIONS
            .iter()
            .map(|e| {
                json!({
                    "from_state": e.from_state.name(),
                    "to_state": e.to_state.name(),
                    "kind": e.kind.name(),
                    "actor": e.actor.name(),
                    "gates": e.gates,
                })
            })
            .collect(),
    )
}

// ------------------------------------------------------------ transition ---

/// An alpha moving between states (`schemas/alpha/lifecycle_transition.schema.json`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LifecycleTransition {
    /// Alpha.
    pub alpha_id: String,
    /// State before.
    pub from_state: LifecycleState,
    /// State after (differs).
    pub to_state: LifecycleState,
    /// Event time, ns.
    pub event_ts: i64,
    /// Reason text.
    pub reason: String,
    /// Gate results by name.
    pub gates: BTreeMap<String, GateResult>,
    /// Policy name.
    pub policy: String,
    /// SYSTEM or HUMAN.
    pub actor: Actor,
}

fn is_gate_name(s: &str) -> bool {
    let b = s.as_bytes();
    !b.is_empty()
        && (b[0].is_ascii_alphabetic() || b[0] == b'_')
        && b[1..]
            .iter()
            .all(|c| c.is_ascii_alphanumeric() || *c == b'_')
}

impl LifecycleTransition {
    /// Schema invariants: id alphabet, `from != to`, non-empty policy, gate
    /// name pattern, finite gate numbers.
    pub fn validate(&self) -> Result<(), IapError> {
        if !is_generic_id(&self.alpha_id) {
            return Err(IapError::Validation(format!(
                "LifecycleTransition.alpha_id: {:?} is not a valid identifier",
                self.alpha_id
            )));
        }
        if self.from_state == self.to_state {
            return Err(IapError::Validation(
                "LifecycleTransition: from_state == to_state".to_string(),
            ));
        }
        if self.policy.is_empty() {
            return Err(IapError::Validation(
                "LifecycleTransition.policy must not be empty".to_string(),
            ));
        }
        for (name, g) in &self.gates {
            if !is_gate_name(name) {
                return Err(IapError::Validation(format!(
                    "LifecycleTransition.gates: bad gate name {name:?}"
                )));
            }
            for x in [g.value, g.threshold].into_iter().flatten() {
                if !x.is_finite() {
                    return Err(IapError::Validation(format!(
                        "LifecycleTransition.gates.{name}: non-finite number"
                    )));
                }
            }
        }
        Ok(())
    }

    /// JSON document (`to_dict()`), validated.
    pub fn to_value(&self) -> Result<Value, IapError> {
        self.validate()?;
        serde_json::to_value(self).map_err(|e| IapError::Codec(format!("LifecycleTransition: {e}")))
    }

    /// Strict parse.
    pub fn from_value(v: &Value) -> Result<LifecycleTransition, IapError> {
        let t: LifecycleTransition = serde_json::from_value(v.clone())
            .map_err(|e| IapError::Validation(format!("LifecycleTransition: {e}")))?;
        t.validate()?;
        Ok(t)
    }

    /// The canonical JSON line — byte-identical to Python's
    /// `canonical_json(transition.to_dict())`.
    pub fn to_canonical_json(&self) -> Result<String, IapError> {
        canonical_json(&self.to_value()?)
    }
}

// ------------------------------------------------------------ evaluation ---

/// Outcome codes of one gate evaluation.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Outcome {
    /// The alpha moved.
    Transition,
    /// Gates evaluated, no move.
    Hold,
    /// The edge's evidence block was absent: nothing evaluated.
    NoEvidence,
    /// SYSTEM `advance` on a RETIRED alpha.
    Terminal,
}

impl Outcome {
    /// Wire name.
    pub fn name(self) -> &'static str {
        match self {
            Outcome::Transition => "TRANSITION",
            Outcome::Hold => "HOLD",
            Outcome::NoEvidence => "NO_EVIDENCE",
            Outcome::Terminal => "TERMINAL",
        }
    }

    /// Parse a wire name.
    pub fn from_name(name: &str) -> Result<Outcome, IapError> {
        match name {
            "TRANSITION" => Ok(Outcome::Transition),
            "HOLD" => Ok(Outcome::Hold),
            "NO_EVIDENCE" => Ok(Outcome::NoEvidence),
            "TERMINAL" => Ok(Outcome::Terminal),
            other => Err(IapError::InvalidArgument(format!(
                "GateEvaluation: unknown outcome {other:?}"
            ))),
        }
    }
}

/// One `advance` call: the gates it evaluated (edge order) and what it did.
#[derive(Debug, Clone, PartialEq)]
pub struct GateEvaluation {
    /// Alpha.
    pub alpha_id: String,
    /// Event time, ns.
    pub event_ts: i64,
    /// State the evaluation started from.
    pub state: LifecycleState,
    /// Outcome.
    pub outcome: Outcome,
    /// Gate results in evaluation order (empty for NO_EVIDENCE / TERMINAL).
    pub gates: Vec<(String, GateResult)>,
    /// Failure counter after the evaluation.
    pub consecutive_failures: u64,
    /// The transition made (iff outcome is TRANSITION).
    pub transition: Option<LifecycleTransition>,
}

impl GateEvaluation {
    /// True when every evaluated gate passed (vacuously with none).
    pub fn passed(&self) -> bool {
        self.gates.iter().all(|(_, g)| g.passed)
    }

    /// Names of the failed gates in evaluation order.
    pub fn failed_gates(&self) -> Vec<&str> {
        self.gates
            .iter()
            .filter(|(_, g)| !g.passed)
            .map(|(n, _)| n.as_str())
            .collect()
    }

    /// The gate results as a map (for a transition record).
    pub fn gate_map(&self) -> BTreeMap<String, GateResult> {
        self.gates.iter().cloned().collect()
    }

    /// JSON-ready; `gate_order` carries the pinned evaluation order that a
    /// sorted-key serialisation of `gates` would lose.
    pub fn to_value(&self) -> Result<Value, IapError> {
        let mut gates = Map::new();
        for (name, g) in &self.gates {
            gates.insert(name.clone(), g.to_value());
        }
        let transition = match &self.transition {
            Some(t) => t.to_value()?,
            None => Value::Null,
        };
        Ok(json!({
            "alpha_id": self.alpha_id,
            "event_ts": self.event_ts,
            "state": self.state.name(),
            "outcome": self.outcome.name(),
            "gate_order": self.gates.iter().map(|(n, _)| n.as_str()).collect::<Vec<_>>(),
            "gates": Value::Object(gates),
            "failed_gates": self.failed_gates(),
            "consecutive_failures": self.consecutive_failures,
            "transition": transition,
        }))
    }

    /// Strict parse (gate order / failed gates must agree with `gates`).
    pub fn from_value(v: &Value) -> Result<GateEvaluation, IapError> {
        let err = |m: String| IapError::Validation(format!("GateEvaluation: {m}"));
        let doc = v
            .as_object()
            .ok_or_else(|| err("expected an object".to_string()))?;
        let expected = [
            "alpha_id",
            "event_ts",
            "state",
            "outcome",
            "gate_order",
            "gates",
            "failed_gates",
            "consecutive_failures",
            "transition",
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
        let order: Vec<String> = serde_json::from_value(doc["gate_order"].clone())
            .map_err(|e| err(format!("gate_order: {e}")))?;
        let gates_doc = doc["gates"]
            .as_object()
            .ok_or_else(|| err("gates: expected an object".to_string()))?;
        let mut sorted = order.clone();
        sorted.sort_unstable();
        sorted.dedup();
        let mut keys: Vec<String> = gates_doc.keys().cloned().collect();
        keys.sort_unstable();
        if sorted != keys || sorted.len() != order.len() {
            return Err(err("gate_order does not match gates".to_string()));
        }
        let mut gates = Vec::with_capacity(order.len());
        for name in &order {
            gates.push((name.clone(), GateResult::from_value(&gates_doc[name])?));
        }
        let transition = match &doc["transition"] {
            Value::Null => None,
            other => Some(LifecycleTransition::from_value(other)?),
        };
        let outcome = Outcome::from_name(
            doc["outcome"]
                .as_str()
                .ok_or_else(|| err("outcome: expected a string".to_string()))?,
        )?;
        if (outcome == Outcome::Transition) != transition.is_some() {
            return Err(err("TRANSITION outcome iff a transition".to_string()));
        }
        let evaluation = GateEvaluation {
            alpha_id: doc["alpha_id"]
                .as_str()
                .ok_or_else(|| err("alpha_id: expected a string".to_string()))?
                .to_string(),
            event_ts: doc["event_ts"]
                .as_i64()
                .ok_or_else(|| err("event_ts: expected an integer".to_string()))?,
            state: LifecycleState::from_name(
                doc["state"]
                    .as_str()
                    .ok_or_else(|| err("state: expected a string".to_string()))?,
            )?,
            outcome,
            gates,
            consecutive_failures: doc["consecutive_failures"].as_u64().ok_or_else(|| {
                err("consecutive_failures: expected a non-negative integer".to_string())
            })?,
            transition,
        };
        let failed: Vec<String> = serde_json::from_value(doc["failed_gates"].clone())
            .map_err(|e| err(format!("failed_gates: {e}")))?;
        if failed != evaluation.failed_gates() {
            return Err(err("failed_gates does not match gates".to_string()));
        }
        Ok(evaluation)
    }
}

// --------------------------------------------------------------- machine ---

/// The machine over an [`AlphaRegistry`] (see module docs).
#[derive(Debug)]
pub struct AlphaLifecycle {
    config: PolicyConfig,
    registry: AlphaRegistry,
    evaluations: Vec<GateEvaluation>,
    transitions: Vec<LifecycleTransition>,
    trackers: BTreeMap<String, LiveTracker>,
}

impl AlphaLifecycle {
    /// Bind a policy to a registry (their policy names must agree).
    pub fn new(config: PolicyConfig, registry: AlphaRegistry) -> Result<AlphaLifecycle, IapError> {
        if registry.policy() != config.policy {
            return Err(IapError::InvalidArgument(format!(
                "registry policy {:?} != config policy {:?}",
                registry.policy(),
                config.policy
            )));
        }
        for e in &ALLOWED_TRANSITIONS {
            for name in e.gates {
                if crate::gates::spec_by_name(name).is_none() {
                    return Err(IapError::InvalidArgument(format!(
                        "edge names unknown gate {name:?}"
                    )));
                }
            }
        }
        Ok(AlphaLifecycle {
            config,
            registry,
            evaluations: Vec::new(),
            transitions: Vec::new(),
            trackers: BTreeMap::new(),
        })
    }

    /// The policy.
    pub fn config(&self) -> &PolicyConfig {
        &self.config
    }

    /// The registry.
    pub fn registry(&self) -> &AlphaRegistry {
        &self.registry
    }

    /// Consume the machine, returning the registry.
    pub fn into_registry(self) -> AlphaRegistry {
        self.registry
    }

    /// Every evaluation of this instance, in call order.
    pub fn evaluations(&self) -> &[GateEvaluation] {
        &self.evaluations
    }

    /// Every transition of this instance, in call order.
    pub fn transitions(&self) -> &[LifecycleTransition] {
        &self.transitions
    }

    /// Enter `alpha_id` at RESEARCH as of `event_ts`.
    pub fn register(&mut self, alpha_id: &str, event_ts: i64) -> Result<&AlphaRecord, IapError> {
        self.registry.add(AlphaRecord::new(alpha_id, event_ts)?)
    }

    /// Current state of an alpha.
    pub fn state(&self, alpha_id: &str) -> Result<LifecycleState, IapError> {
        Ok(self.registry.get(alpha_id)?.state)
    }

    /// The alpha's record.
    pub fn record(&self, alpha_id: &str) -> Result<&AlphaRecord, IapError> {
        self.registry.get(alpha_id)
    }

    fn make_transition(
        alpha_id: &str,
        edge: &Edge,
        event_ts: i64,
        reason: String,
        gates: BTreeMap<String, GateResult>,
        actor: Actor,
        policy: &str,
    ) -> LifecycleTransition {
        LifecycleTransition {
            alpha_id: alpha_id.to_string(),
            from_state: edge.from_state,
            to_state: edge.to_state,
            event_ts,
            reason,
            gates,
            policy: policy.to_string(),
            actor,
        }
    }

    fn apply(
        &mut self,
        alpha_id: &str,
        transition: LifecycleTransition,
    ) -> Result<LifecycleTransition, IapError> {
        transition.validate()?;
        let rec = self.registry.get_mut(alpha_id)?;
        rec.state = transition.to_state;
        rec.since_ts = transition.event_ts;
        rec.last_transition = Some(transition.clone());
        rec.consecutive_failures = 0;
        rec.breach_count = 0;
        rec.recovery_count = 0;
        self.trackers.remove(alpha_id);
        self.transitions.push(transition.clone());
        Ok(transition)
    }

    fn record_evaluation(
        &mut self,
        alpha_id: &str,
        evaluation: GateEvaluation,
    ) -> Result<(), IapError> {
        let rec = self.registry.get_mut(alpha_id)?;
        rec.last_evaluation = Some(evaluation.clone());
        self.evaluations.push(evaluation);
        Ok(())
    }

    /// Evaluate the SYSTEM edge leaving the alpha's current state. Returns
    /// the transition made, or `None` (a [`GateEvaluation`] is recorded
    /// either way).
    pub fn advance(
        &mut self,
        alpha_id: &str,
        event_ts: i64,
        evidence: &Evidence,
    ) -> Result<Option<LifecycleTransition>, IapError> {
        evidence.validate()?;
        let rec = self.registry.get(alpha_id)?;
        let state = rec.state;
        let failures = rec.consecutive_failures;
        match state {
            LifecycleState::Retired => {
                self.record_evaluation(
                    alpha_id,
                    GateEvaluation {
                        alpha_id: alpha_id.to_string(),
                        event_ts,
                        state,
                        outcome: Outcome::Terminal,
                        gates: Vec::new(),
                        consecutive_failures: failures,
                        transition: None,
                    },
                )?;
                Ok(None)
            }
            LifecycleState::Active | LifecycleState::Watch => {
                self.advance_live(alpha_id, event_ts, evidence)
            }
            LifecycleState::Research
            | LifecycleState::Candidate
            | LifecycleState::Validating
            | LifecycleState::Paper => self.advance_promotion(alpha_id, event_ts, evidence),
        }
    }

    fn advance_promotion(
        &mut self,
        alpha_id: &str,
        event_ts: i64,
        evidence: &Evidence,
    ) -> Result<Option<LifecycleTransition>, IapError> {
        let rec = self.registry.get(alpha_id)?;
        let state = rec.state;
        let mut failures = rec.consecutive_failures;
        let edge = promotion_edge(state).ok_or_else(|| {
            IapError::InvalidArgument(format!("no promotion edge leaves {}", state.name()))
        })?;
        if !required_block_present(state, evidence) {
            self.record_evaluation(
                alpha_id,
                GateEvaluation {
                    alpha_id: alpha_id.to_string(),
                    event_ts,
                    state,
                    outcome: Outcome::NoEvidence,
                    gates: Vec::new(),
                    consecutive_failures: failures,
                    transition: None,
                },
            )?;
            return Ok(None);
        }

        let mut results: Vec<(String, GateResult)> = Vec::with_capacity(edge.gates.len());
        for name in edge.gates {
            let r = evaluate_named(name, &self.config, evidence)
                .ok_or_else(|| IapError::InvalidArgument(format!("unknown gate {name:?}")))?;
            results.push((name.to_string(), r));
        }
        let failed: Vec<&str> = results
            .iter()
            .filter(|(_, g)| !g.passed)
            .map(|(n, _)| n.as_str())
            .collect();
        let gate_map: BTreeMap<String, GateResult> = results.iter().cloned().collect();

        let transition = if failed.is_empty() {
            Some(Self::make_transition(
                alpha_id,
                edge,
                event_ts,
                format!(
                    "all {} gates passed: {} -> {}",
                    edge.gates.len(),
                    state.name(),
                    edge.to_state.name()
                ),
                gate_map,
                Actor::System,
                &self.config.policy,
            ))
        } else if state == LifecycleState::Candidate && failed.contains(&"leakage_clean") {
            let demote = edge_for(
                LifecycleState::Candidate,
                LifecycleState::Research,
                EdgeKind::Demotion,
            )?;
            Some(Self::make_transition(
                alpha_id,
                demote,
                event_ts,
                "leakage_clean failed: a leaking alpha is not a candidate".to_string(),
                gate_map,
                Actor::System,
                &self.config.policy,
            ))
        } else if matches!(state, LifecycleState::Validating | LifecycleState::Paper) {
            failures += 1;
            if failures >= self.config.max_consecutive_failures {
                let demote = edge_for(state, LifecycleState::Candidate, EdgeKind::Demotion)?;
                Some(Self::make_transition(
                    alpha_id,
                    demote,
                    event_ts,
                    format!(
                        "{failures} consecutive failed evaluations (max {}); failed gates: {}",
                        self.config.max_consecutive_failures,
                        failed.join(", ")
                    ),
                    gate_map,
                    Actor::System,
                    &self.config.policy,
                ))
            } else {
                self.registry.get_mut(alpha_id)?.consecutive_failures = failures;
                None
            }
        } else {
            None
        };

        match transition {
            Some(t) => {
                let applied = self.apply(alpha_id, t)?;
                self.record_evaluation(
                    alpha_id,
                    GateEvaluation {
                        alpha_id: alpha_id.to_string(),
                        event_ts,
                        state,
                        outcome: Outcome::Transition,
                        gates: results,
                        consecutive_failures: 0,
                        transition: Some(applied.clone()),
                    },
                )?;
                Ok(Some(applied))
            }
            None => {
                self.record_evaluation(
                    alpha_id,
                    GateEvaluation {
                        alpha_id: alpha_id.to_string(),
                        event_ts,
                        state,
                        outcome: Outcome::Hold,
                        gates: results,
                        consecutive_failures: failures,
                        transition: None,
                    },
                )?;
                Ok(None)
            }
        }
    }

    fn tracker(&mut self, alpha_id: &str) -> Result<&mut LiveTracker, IapError> {
        if !self.trackers.contains_key(alpha_id) {
            let rec = self.registry.get(alpha_id)?;
            let tracker = LiveTracker::new(
                alpha_id,
                self.config.live.clone(),
                &self.config.policy,
                rec.state,
                rec.breach_count,
                rec.recovery_count,
            )?;
            self.trackers.insert(alpha_id.to_string(), tracker);
        }
        self.trackers
            .get_mut(alpha_id)
            .ok_or_else(|| IapError::InvalidArgument(format!("no tracker for {alpha_id}")))
    }

    /// The `rolling_ic` gate as it decided a tracker transition: a breach
    /// transition (-> WATCH, -> RETIRED) failed the watch gate; a
    /// re-activation (-> ACTIVE) passed the reactivate gate.
    fn live_gate_result(tr: &TrackerTransition, config: &PolicyConfig) -> GateResult {
        if tr.to_state == LifecycleState::Active {
            GateResult {
                passed: true,
                value: tr.rolling_ic,
                threshold: Some(config.live.reactivate_ic_gate),
            }
        } else {
            GateResult {
                passed: false,
                value: tr.rolling_ic,
                threshold: Some(config.live.watch_ic_gate),
            }
        }
    }

    fn advance_live(
        &mut self,
        alpha_id: &str,
        event_ts: i64,
        evidence: &Evidence,
    ) -> Result<Option<LifecycleTransition>, IapError> {
        let rec = self.registry.get(alpha_id)?;
        let state = rec.state;
        let failures = rec.consecutive_failures;
        let (rolling_ic, informative) = match &evidence.live {
            Some(l) if l.informative && l.rolling_ic.is_some() => (l.rolling_ic, l.informative),
            _ => {
                self.record_evaluation(
                    alpha_id,
                    GateEvaluation {
                        alpha_id: alpha_id.to_string(),
                        event_ts,
                        state,
                        outcome: Outcome::NoEvidence,
                        gates: Vec::new(),
                        consecutive_failures: failures,
                        transition: None,
                    },
                )?;
                return Ok(None);
            }
        };
        let gate = evaluate_named("rolling_ic", &self.config, evidence)
            .ok_or_else(|| IapError::InvalidArgument("unknown gate rolling_ic".to_string()))?;
        let gates = vec![("rolling_ic".to_string(), gate)];

        let tracker = self.tracker(alpha_id)?;
        let n_before = tracker.transitions().len();
        tracker.update(event_ts, rolling_ic, informative);
        let breach = tracker.breach_count();
        let recovery = tracker.recovery_count();
        let made = tracker.transitions().get(n_before).cloned();
        let policy = tracker.policy().to_string();

        match made {
            None => {
                let rec = self.registry.get_mut(alpha_id)?;
                rec.breach_count = breach;
                rec.recovery_count = recovery;
                self.record_evaluation(
                    alpha_id,
                    GateEvaluation {
                        alpha_id: alpha_id.to_string(),
                        event_ts,
                        state,
                        outcome: Outcome::Hold,
                        gates,
                        consecutive_failures: failures,
                        transition: None,
                    },
                )?;
                Ok(None)
            }
            Some(tr) => {
                let edge = edge_for(state, tr.to_state, EdgeKind::Live)?;
                let mut gate_map = BTreeMap::new();
                gate_map.insert(
                    "rolling_ic".to_string(),
                    Self::live_gate_result(&tr, &self.config),
                );
                let transition = Self::make_transition(
                    alpha_id,
                    edge,
                    event_ts,
                    tr.reason.clone(),
                    gate_map,
                    Actor::System,
                    &policy,
                );
                // `apply` drops the tracker; keep it — the tracker keeps
                // counting across its own transition (the breach that
                // enters WATCH is breach #1, pinned) — and mirror its
                // counters on the record so a reload resumes exactly.
                let kept = self.trackers.remove(alpha_id);
                let applied = self.apply(alpha_id, transition)?;
                if let Some(t) = kept {
                    self.trackers.insert(alpha_id.to_string(), t);
                }
                let rec = self.registry.get_mut(alpha_id)?;
                rec.breach_count = breach;
                rec.recovery_count = recovery;
                self.record_evaluation(
                    alpha_id,
                    GateEvaluation {
                        alpha_id: alpha_id.to_string(),
                        event_ts,
                        state,
                        outcome: Outcome::Transition,
                        gates,
                        consecutive_failures: 0,
                        transition: Some(applied.clone()),
                    },
                )?;
                Ok(Some(applied))
            }
        }
    }

    fn check_manual(actor: Actor, reason: &str, what: &str) -> Result<(), IapError> {
        if actor != Actor::Human {
            return Err(IapError::InvalidArgument(format!(
                "{what}: requires Actor::Human, got {}",
                actor.name()
            )));
        }
        if reason.trim().is_empty() {
            return Err(IapError::InvalidArgument(format!(
                "{what}: a non-empty reason is required"
            )));
        }
        Ok(())
    }

    /// Manual retirement from any non-retired state (HUMAN only).
    pub fn retire(
        &mut self,
        alpha_id: &str,
        event_ts: i64,
        reason: &str,
        actor: Actor,
    ) -> Result<LifecycleTransition, IapError> {
        Self::check_manual(actor, reason, "retire")?;
        let state = self.registry.get(alpha_id)?.state;
        if state == LifecycleState::Retired {
            return Err(IapError::InvalidArgument(format!(
                "retire: {alpha_id} is already RETIRED"
            )));
        }
        let edge = edge_for(state, LifecycleState::Retired, EdgeKind::Manual)?;
        let t = Self::make_transition(
            alpha_id,
            edge,
            event_ts,
            reason.to_string(),
            BTreeMap::new(),
            actor,
            &self.config.policy,
        );
        self.apply(alpha_id, t)
    }

    /// Manual re-research: RETIRED -> RESEARCH (HUMAN only).
    pub fn reset_to_research(
        &mut self,
        alpha_id: &str,
        event_ts: i64,
        reason: &str,
        actor: Actor,
    ) -> Result<LifecycleTransition, IapError> {
        Self::check_manual(actor, reason, "reset_to_research")?;
        let state = self.registry.get(alpha_id)?.state;
        if state != LifecycleState::Retired {
            return Err(IapError::InvalidArgument(format!(
                "reset_to_research: {alpha_id} is {}, not RETIRED",
                state.name()
            )));
        }
        let edge = edge_for(
            LifecycleState::Retired,
            LifecycleState::Research,
            EdgeKind::Manual,
        )?;
        let t = Self::make_transition(
            alpha_id,
            edge,
            event_ts,
            reason.to_string(),
            BTreeMap::new(),
            actor,
            &self.config.policy,
        );
        self.apply(alpha_id, t)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn table_has_17_unique_edges_and_known_gates() {
        assert_eq!(ALLOWED_TRANSITIONS.len(), 17);
        for (i, a) in ALLOWED_TRANSITIONS.iter().enumerate() {
            assert_ne!(a.from_state, a.to_state);
            assert_eq!(a.actor == Actor::Human, a.kind == EdgeKind::Manual);
            for name in a.gates {
                assert!(crate::gates::spec_by_name(name).is_some(), "{name}");
            }
            for b in &ALLOWED_TRANSITIONS[i + 1..] {
                assert!(
                    !(a.from_state == b.from_state && a.to_state == b.to_state && a.kind == b.kind)
                );
            }
        }
        assert!(edge_for(
            LifecycleState::Research,
            LifecycleState::Paper,
            EdgeKind::Promotion
        )
        .is_err());
        assert!(promotion_edge(LifecycleState::Active).is_none());
        let manual: Vec<&Edge> = ALLOWED_TRANSITIONS
            .iter()
            .filter(|e| e.kind == EdgeKind::Manual)
            .collect();
        assert_eq!(manual.len(), 7);
        assert_eq!(transition_table().as_array().map(Vec::len), Some(17));
    }

    #[test]
    fn transition_canonical_json_and_strictness() {
        let mut gates = BTreeMap::new();
        gates.insert(
            "rolling_ic".to_string(),
            GateResult {
                passed: false,
                value: Some(-0.01),
                threshold: Some(0.0),
            },
        );
        let t = LifecycleTransition {
            alpha_id: "LC01".to_string(),
            from_state: LifecycleState::Active,
            to_state: LifecycleState::Watch,
            event_ts: 1700007200000000000,
            reason: "rolling_ic -0.010000 < watch gate 0.0".to_string(),
            gates,
            policy: "lifecycle_v1".to_string(),
            actor: Actor::System,
        };
        assert_eq!(
            t.to_canonical_json().expect("valid"),
            "{\"actor\":\"SYSTEM\",\"alpha_id\":\"LC01\",\"event_ts\":1700007200000000000,\"from_state\":\"ACTIVE\",\"gates\":{\"rolling_ic\":{\"passed\":false,\"threshold\":0.0,\"value\":-0.01}},\"policy\":\"lifecycle_v1\",\"reason\":\"rolling_ic -0.010000 < watch gate 0.0\",\"to_state\":\"WATCH\"}"
        );
        let back = LifecycleTransition::from_value(&t.to_value().expect("valid")).expect("parses");
        assert_eq!(back, t);
        let mut same = t.clone();
        same.to_state = LifecycleState::Active;
        assert!(same.validate().is_err());
        let mut v = t.to_value().expect("valid");
        v["extra"] = json!(1);
        assert!(LifecycleTransition::from_value(&v).is_err());
    }
}
