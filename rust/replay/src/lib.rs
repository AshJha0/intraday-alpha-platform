//! IAP replay crate: deterministic event-time replay driving order books,
//! with snapshot emission and checkpoint/restart (API_CORE.md §5).

pub mod engine;

pub use engine::{EngineCheckpoint, ReplayEngine, ReplaySummary, Universe, ENGINE_CHECKPOINT_VERSION};
