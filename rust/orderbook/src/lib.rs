//! IAP order book crate: pinned MBO/L2/L1 semantics, per-venue and
//! consolidated books, sequencing/staleness QC, checkpoints.
//!
//! Rust port of `python/src/iap/orderbook/book.py` per `API_CORE.md` §4
//! (semantics pinned by PLATFORM_CONVENTIONS.md §4).

pub mod book;

pub use book::{
    synthetic_order_id, ApplyStatus, BookCheckpoint, ConsolidatedBook, ConsolidatedCheckpoint, Counters,
    LevelCheckpoint, OrderBook, PendingRow, CHECKPOINT_VERSION, DEPTH_LEVELS,
    MAX_REORDER_WINDOW, SYNTHETIC_ID_BASE,
};
