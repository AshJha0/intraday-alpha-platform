//! IAP fail-closed hard risk engine (spec §16; PLATFORM_CONVENTIONS.md §11).
//!
//! Pre-trade checks (fat-finger, price band, staleness, FX conversion,
//! position/notional/gross/net limits incl. every open order, loss limits
//! on realized + mark-to-market P&L, event-time order throttle, duplicate
//! order ids, self-match prevention, sequence-gap and venue-disconnect
//! gates), kill switches at global/strategy/instrument/venue scope with a
//! pinned re-arm precedence, session roll, snapshot/restore, and a
//! deterministic, replayable RiskEvent audit log
//! (`schemas/risk_event.schema.json`, JSONL) that is byte-identical across
//! languages.
//!
//! Limits come from `configs/risk.json` (the complete pinned set, strict
//! parse). Golden vectors: `tests/golden/expected_risk_decisions.json`
//! (decisions), `expected_risk_audit.jsonl` (byte-exact audit log) and
//! `expected_risk_snapshot.json` (state snapshot mid-script).

pub mod engine;
pub mod event;
pub mod limits;

pub use engine::{Fill, InstrumentRef, RiskDecision, RiskEngine, SNAPSHOT_VERSION};
pub use event::{fmt_fixed, rules, Decision, RiskEvent, Scope, Severity};
pub use limits::{FxConversion, RiskLimits};
