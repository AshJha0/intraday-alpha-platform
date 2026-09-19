//! IAP cross-language contracts (Python reference: `iap.contracts`,
//! `iap.trace`; notes: `contracts.md`, `store_trace.md`).
//!
//! - [`canonical`] — canonical JSON with byte parity to Python's
//!   `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True)`
//!   (own writer: Python float layout, code-point key order, ASCII escapes),
//!   the `indent=2` artefact layout, `content_hash`, `make_trace_id`.
//! - [`sha256`] — dependency-free streaming SHA-256.
//! - [`trace`] — `DecisionTrace` and every nested stage record as strict
//!   serde structs, the JSONL sink, the stream digest and `explain()`.
//!
//! Goldens: `tests/golden/expected_canonical_json.json`
//! (`tests/golden_canonical_json.rs`) and
//! `tests/golden/expected_contracts_examples.json` (`tests/golden_trace.rs`).

pub mod canonical;
pub mod sha256;
pub mod trace;

pub use canonical::{
    canonical_json, content_hash, float_value, format_float, indented_json, is_generic_id,
    is_sha256_hex, is_trace_id, make_trace_id, write_canonical, write_string,
};
pub use marketdata::IapError;
pub use sha256::{sha256_hex, Sha256};
pub use trace::{
    explain, Algo, AlphaSignal, Attribution, ChildOrder, Decision, DecisionTrace, Direction,
    ExecStatus, ExecutionReport, JsonlTraceSink, LatencyStats, OrderType, ParentOrder,
    PortfolioLeg, PortfolioTarget, RiskDecision, Side, SolverStatus, TcaResult, TraceDigest,
    TraceStages, VenueDecision, VenueScore,
};
