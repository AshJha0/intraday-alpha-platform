//! IAP alpha promotion lifecycle (Python reference: `iap.lifecycle`; notes:
//! `lifecycle.md`). Rust owns the state machine that will gate allocation in
//! the risk engine, so it reproduces the reference exactly:
//!
//! - [`state`] — the seven ordered states RESEARCH(0) … RETIRED(6);
//! - [`evidence`] — the typed, finite-checked evidence blocks;
//! - [`gates`] — the 18-row gate table, the policy config
//!   (`configs/strategies/lifecycle.json` + `strategies.json`
//!   `adaptive.lifecycle`) and gate evaluation;
//! - [`tracker`] — the live ACTIVE / WATCH / RETIRED rolling-IC rules
//!   (exact port of `iap.adaptive.lifecycle.LifecycleTracker`);
//! - [`machine`] — the 17-edge transition table as data, `advance` /
//!   `retire` / `reset_to_research`, `LifecycleTransition`, `GateEvaluation`;
//! - [`registry`] — `research/alpha_registry.json` read / written
//!   byte-identically, and the transition log.
//!
//! Golden: `tests/golden/expected_lifecycle.json` (`tests/golden_lifecycle.rs`).

pub mod evidence;
pub mod gates;
pub mod machine;
pub mod registry;
pub mod state;
pub mod tracker;

pub use evidence::{
    Evidence, ExperimentResult, LiveEvidence, PaperEvidence, ValidationEvidence, Verdict,
};
pub use gates::{
    evaluate, evaluate_named, ic_rank_gap, metric, spec_by_name, Block, GateKind, GateResult,
    GateSpec, GateThresholds, LiveConfig, Metric, PolicyConfig, GATE_SPECS,
    LIFECYCLE_CONFIG_VERSION,
};
pub use machine::{
    edge_for, promotion_edge, transition_table, Actor, AlphaLifecycle, Edge, EdgeKind,
    GateEvaluation, LifecycleTransition, Outcome, ALLOWED_TRANSITIONS,
};
pub use marketdata::IapError;
pub use registry::{AlphaRecord, AlphaRegistry, TransitionLog, REGISTRY_VERSION};
pub use state::LifecycleState;
pub use tracker::{LiveTracker, TrackerTransition};
