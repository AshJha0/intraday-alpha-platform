//! Deterministic event-time replay driving order books (spec §9/§18).
//!
//! Mirrors `python/src/iap/replay/replay.py`: the engine consumes normalized
//! events in event-time order, validates ids against an optional universe
//! (instrument -> venue ids; unknown ids are dropped + counted), routes each
//! to its per-venue book inside a per-instrument `ConsolidatedBook`, emits
//! book-state snapshots every `snapshot_every` events (retaining the latest
//! `keep_snapshots`), checkpoints every `checkpoint_every` events (retaining
//! the latest `keep_checkpoints`), and restores from any checkpoint to
//! bit-identical subsequent state. [`EngineCheckpoint`] serializes to the
//! cross-language JSON document of API_CORE §5 (x-version 2).
//!
//! Determinism: no wall clock; all maps are `BTreeMap`s walked in sorted key
//! order when serializing.

use std::collections::{BTreeMap, BTreeSet};

use marketdata::{IapError, MarketEvent};
use orderbook::{ConsolidatedBook, ConsolidatedCheckpoint, MAX_REORDER_WINDOW};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

/// Engine checkpoint schema version (API_CORE §5).
pub const ENGINE_CHECKPOINT_VERSION: i64 = 2;

/// instrument_id -> allowed venue ids.
pub type Universe = BTreeMap<u32, BTreeSet<u16>>;

/// Summary statistics returned by [`ReplayEngine::run`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ReplaySummary {
    pub events_processed: u64,
    pub instruments: usize,
    pub time_regressions: u64,
    pub snapshots: u64,
    pub unknown_instrument_dropped: u64,
    pub unknown_venue_dropped: u64,
}

/// Full JSON-able engine state; [`ReplayEngine::restore`] rebuilds it
/// identically. Field names/order are the cross-language checkpoint shape.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EngineCheckpoint {
    #[serde(rename = "x-version")]
    pub x_version: i64,
    pub events_processed: u64,
    pub last_exchange_ts: i64,
    pub time_regressions: u64,
    pub unknown_instrument_dropped: u64,
    pub unknown_venue_dropped: u64,
    pub checkpoint_every: u64,
    pub snapshot_every: u64,
    pub keep_checkpoints: u64,
    pub keep_snapshots: u64,
    pub snapshots_emitted: u64,
    pub reorder_window: u64,
    pub universe: Option<BTreeMap<u32, Vec<u16>>>,
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
    pub keep_snapshots: usize,
    pub reorder_window: usize,
    pub universe: Option<Universe>,
    pub checkpoints: Vec<EngineCheckpoint>,
    pub snapshots: Vec<Value>,
    pub snapshots_emitted: u64,
    pub time_regressions: u64,
    pub unknown_instrument_dropped: u64,
    pub unknown_venue_dropped: u64,
    last_exchange_ts: i64,
}

impl ReplayEngine {
    /// Create an engine. `checkpoint_every` / `snapshot_every` of 0 disable
    /// the respective cadence; retention defaults to 4 each, no reorder
    /// window, no universe validation.
    pub fn new(checkpoint_every: u64, snapshot_every: u64) -> ReplayEngine {
        ReplayEngine {
            books: BTreeMap::new(),
            events_processed: 0,
            checkpoint_every,
            snapshot_every,
            keep_checkpoints: 4,
            keep_snapshots: 4,
            reorder_window: 0,
            universe: None,
            checkpoints: Vec::new(),
            snapshots: Vec::new(),
            snapshots_emitted: 0,
            time_regressions: 0,
            unknown_instrument_dropped: 0,
            unknown_venue_dropped: 0,
            last_exchange_ts: 0,
        }
    }

    /// Builder: hold-back buffer for every venue book (0..4096).
    pub fn with_reorder_window(mut self, reorder_window: usize) -> Result<ReplayEngine, IapError> {
        if reorder_window > MAX_REORDER_WINDOW {
            return Err(IapError::InvalidArgument(format!(
                "reorder_window must be <= {MAX_REORDER_WINDOW}: {reorder_window}"
            )));
        }
        self.reorder_window = reorder_window;
        Ok(self)
    }

    /// Builder: validate instrument/venue ids against `universe`.
    pub fn with_universe(mut self, universe: Universe) -> ReplayEngine {
        self.universe = Some(universe);
        self
    }

    /// Builder: retention of periodic checkpoints and snapshots.
    pub fn with_retention(mut self, keep_checkpoints: usize, keep_snapshots: usize) -> ReplayEngine {
        self.keep_checkpoints = keep_checkpoints;
        self.keep_snapshots = keep_snapshots;
        self
    }

    /// Get (or lazily create) the per-instrument consolidated book.
    pub fn instrument_book(&mut self, instrument_id: u32) -> &mut ConsolidatedBook {
        let window = self.reorder_window;
        self.books.entry(instrument_id).or_insert_with(|| {
            ConsolidatedBook::with_reorder_window(instrument_id, window)
                .expect("validated at construction")
        })
    }

    /// Apply one event; counts it, validates it against the universe, tracks
    /// event-time monotonicity.
    pub fn apply(&mut self, ev: &MarketEvent) -> Result<(), IapError> {
        if ev.exchange_ts < self.last_exchange_ts {
            self.time_regressions += 1;
        }
        self.last_exchange_ts = ev.exchange_ts;
        self.events_processed += 1;
        if let Some(universe) = &self.universe {
            match universe.get(&ev.instrument_id) {
                None => {
                    self.unknown_instrument_dropped += 1;
                    return Ok(());
                }
                Some(venues) if !venues.contains(&ev.venue_id) => {
                    self.unknown_venue_dropped += 1;
                    return Ok(());
                }
                Some(_) => {}
            }
        }
        self.instrument_book(ev.instrument_id).apply(ev)?;
        Ok(())
    }

    /// Explicit sequence reset on every book (session roll).
    pub fn reset_sequences(&mut self) {
        for cons in self.books.values_mut() {
            cons.reset_sequences();
        }
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
                self.snapshots_emitted += 1;
                self.snapshots.push(snap);
                if self.snapshots.len() > self.keep_snapshots {
                    self.snapshots.remove(0);
                }
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
            snapshots: self.snapshots_emitted,
            unknown_instrument_dropped: self.unknown_instrument_dropped,
            unknown_venue_dropped: self.unknown_venue_dropped,
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
            x_version: ENGINE_CHECKPOINT_VERSION,
            events_processed: self.events_processed,
            last_exchange_ts: self.last_exchange_ts,
            time_regressions: self.time_regressions,
            unknown_instrument_dropped: self.unknown_instrument_dropped,
            unknown_venue_dropped: self.unknown_venue_dropped,
            checkpoint_every: self.checkpoint_every,
            snapshot_every: self.snapshot_every,
            keep_checkpoints: self.keep_checkpoints as u64,
            keep_snapshots: self.keep_snapshots as u64,
            snapshots_emitted: self.snapshots_emitted,
            reorder_window: self.reorder_window as u64,
            universe: self.universe.as_ref().map(|u| {
                u.iter()
                    .map(|(&iid, venues)| (iid, venues.iter().copied().collect()))
                    .collect()
            }),
            books: self
                .books
                .iter()
                .map(|(&iid, cons)| (iid, cons.checkpoint()))
                .collect(),
        }
    }

    /// Rebuild an engine from `checkpoint()` output.
    pub fn restore(cp: &EngineCheckpoint) -> Result<ReplayEngine, IapError> {
        if cp.x_version != ENGINE_CHECKPOINT_VERSION {
            return Err(IapError::Checkpoint(format!(
                "unsupported engine checkpoint x-version: {}",
                cp.x_version
            )));
        }
        if cp.reorder_window > MAX_REORDER_WINDOW as u64 {
            return Err(IapError::Checkpoint(
                "checkpoint reorder_window out of range".to_string(),
            ));
        }
        let mut engine = ReplayEngine::new(cp.checkpoint_every, cp.snapshot_every)
            .with_reorder_window(cp.reorder_window as usize)?
            .with_retention(cp.keep_checkpoints as usize, cp.keep_snapshots as usize);
        engine.universe = cp.universe.as_ref().map(|u| {
            u.iter()
                .map(|(&iid, venues)| (iid, venues.iter().copied().collect()))
                .collect()
        });
        engine.events_processed = cp.events_processed;
        engine.last_exchange_ts = cp.last_exchange_ts;
        engine.time_regressions = cp.time_regressions;
        engine.unknown_instrument_dropped = cp.unknown_instrument_dropped;
        engine.unknown_venue_dropped = cp.unknown_venue_dropped;
        engine.snapshots_emitted = cp.snapshots_emitted;
        for (&iid, bcp) in &cp.books {
            engine.books.insert(iid, ConsolidatedBook::restore(bcp)?);
        }
        Ok(engine)
    }
}
