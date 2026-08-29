//! Deterministic event-time replay driving order books (spec §9/§18).
//!
//! Mirrors `python/src/iap/replay/replay.py`: the engine consumes normalized
//! events in event-time order, routes each to its per-venue book inside a
//! per-instrument `ConsolidatedBook`, emits book-state snapshots every
//! `snapshot_every` events, checkpoints every `checkpoint_every` events, and
//! restores from any checkpoint to bit-identical subsequent state.
//!
//! Determinism: no wall clock; all maps are `BTreeMap`s walked in sorted key
//! order when serializing.

use std::collections::BTreeMap;

use marketdata::{IapError, MarketEvent};
use orderbook::{ConsolidatedBook, ConsolidatedCheckpoint};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

/// Summary statistics returned by [`ReplayEngine::run`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ReplaySummary {
    pub events_processed: u64,
    pub instruments: usize,
    pub time_regressions: u64,
    pub snapshots: usize,
}

/// Full JSON-able engine state; [`ReplayEngine::restore`] rebuilds it
/// identically.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct EngineCheckpoint {
    pub events_processed: u64,
    pub last_exchange_ts: i64,
    pub time_regressions: u64,
    pub checkpoint_every: u64,
    pub snapshot_every: u64,
    pub books: BTreeMap<u32, ConsolidatedCheckpoint>,
}

/// Event-time replay across all instruments/venues in a stream.
#[derive(Debug)]
pub struct ReplayEngine {
    pub books: BTreeMap<u32, ConsolidatedBook>,
    pub events_processed: u64,
    pub checkpoint_every: u64,
    pub snapshot_every: u64,
    pub keep_checkpoints: usize,
    pub checkpoints: Vec<EngineCheckpoint>,
    pub snapshots: Vec<Value>,
    pub time_regressions: u64,
    last_exchange_ts: i64,
}

impl ReplayEngine {
    /// Create an engine. `checkpoint_every` / `snapshot_every` of 0 disable
    /// the respective cadence.
    pub fn new(checkpoint_every: u64, snapshot_every: u64) -> ReplayEngine {
        ReplayEngine {
            books: BTreeMap::new(),
            events_processed: 0,
            checkpoint_every,
            snapshot_every,
            keep_checkpoints: 4,
            checkpoints: Vec::new(),
            snapshots: Vec::new(),
            time_regressions: 0,
            last_exchange_ts: 0,
        }
    }

    /// Get (or lazily create) the per-instrument consolidated book.
    pub fn instrument_book(&mut self, instrument_id: u32) -> &mut ConsolidatedBook {
        self.books
            .entry(instrument_id)
            .or_insert_with(|| ConsolidatedBook::new(instrument_id))
    }

    /// Apply one event; tracks event-time monotonicity.
    pub fn apply(&mut self, ev: &MarketEvent) -> Result<(), IapError> {
        if ev.exchange_ts < self.last_exchange_ts {
            self.time_regressions += 1;
        }
        self.last_exchange_ts = ev.exchange_ts;
        self.instrument_book(ev.instrument_id).apply(ev)?;
        self.events_processed += 1;
        Ok(())
    }

    /// Replay an event stream; returns summary stats.
    pub fn run<'a, I>(&mut self, events: I) -> Result<ReplaySummary, IapError>
    where
        I: IntoIterator<Item = &'a MarketEvent>,
    {
        for ev in events {
            self.apply(ev)?;
            if self.snapshot_every != 0 && self.events_processed % self.snapshot_every == 0 {
                let mut snap = self.book_states();
                if let Some(obj) = snap.as_object_mut() {
                    obj.insert("index".to_string(), json!(self.events_processed));
                }
                self.snapshots.push(snap);
            }
            if self.checkpoint_every != 0 && self.events_processed % self.checkpoint_every == 0 {
                let cp = self.checkpoint();
                self.checkpoints.push(cp);
                if self.checkpoints.len() > self.keep_checkpoints {
                    self.checkpoints.remove(0);
                }
            }
        }
        Ok(ReplaySummary {
            events_processed: self.events_processed,
            instruments: self.books.len(),
            time_regressions: self.time_regressions,
            snapshots: self.snapshots.len(),
        })
    }

    // -------------------------------------------------------- serialization

    /// Exact-integer per-venue state summaries (deterministic key order:
    /// instruments -> venues, both sorted numerically).
    pub fn book_states(&self) -> Value {
        let mut instruments = serde_json::Map::new();
        for (iid, cons) in &self.books {
            let mut venues = serde_json::Map::new();
            for (vid, book) in &cons.books {
                venues.insert(vid.to_string(), book.state_summary());
            }
            instruments.insert(iid.to_string(), Value::Object(venues));
        }
        json!({ "instruments": instruments })
    }

    /// Full engine state (books serialized in sorted key order).
    pub fn checkpoint(&self) -> EngineCheckpoint {
        EngineCheckpoint {
            events_processed: self.events_processed,
            last_exchange_ts: self.last_exchange_ts,
            time_regressions: self.time_regressions,
            checkpoint_every: self.checkpoint_every,
            snapshot_every: self.snapshot_every,
            books: self
                .books
                .iter()
                .map(|(&iid, cons)| (iid, cons.checkpoint()))
                .collect(),
        }
    }

    /// Rebuild an engine from `checkpoint()` output.
    pub fn restore(cp: &EngineCheckpoint) -> Result<ReplayEngine, IapError> {
        let mut engine = ReplayEngine::new(cp.checkpoint_every, cp.snapshot_every);
        engine.events_processed = cp.events_processed;
        engine.last_exchange_ts = cp.last_exchange_ts;
        engine.time_regressions = cp.time_regressions;
        for (&iid, bcp) in &cp.books {
            engine.books.insert(iid, ConsolidatedBook::restore(bcp)?);
        }
        Ok(engine)
    }
}
