//! IAP order book crate: pinned MBO/L2/L1 semantics, per-venue and
//! consolidated books, sequencing/staleness QC, checkpoints.
//!
//! Rust port of `python/src/iap/orderbook/book.py` per `API_CORE.md` §4
//! (semantics pinned by PLATFORM_CONVENTIONS.md §4).

pub mod book;

pub use book::{
    BookCheckpoint, ConsolidatedBook, ConsolidatedCheckpoint, Counters, LevelCheckpoint,
    OrderBook, DEPTH_LEVELS,
};
