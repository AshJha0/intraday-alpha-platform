//! IAP fail-closed hard risk engine (spec §16).
//!
//! Pre-trade checks (fat-finger, price band, staleness, position/notional/
//! gross/net limits, loss limits, event-time order throttle, duplicate
//! order ids, self-match prevention, sequence-gap and venue-disconnect
//! gates), kill switches at global/strategy/instrument/venue scope, and a
//! deterministic, replayable RiskEvent audit log
//! (`schemas/risk_event.schema.json`, JSONL).
//!
//! Limits come from `configs/risk.json` (the complete pinned set, strict
//! parse). Golden decision vector: `tests/golden/expected_risk_decisions.json`.

pub mod engine;
pub mod event;
pub mod limits;

pub use engine::{Fill, RiskDecision, RiskEngine};
pub use event::{rules, Decision, RiskEvent, Scope, Severity};
pub use limits::RiskLimits;
