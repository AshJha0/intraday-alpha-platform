//! Reference L1/L2/MBO order book (PLATFORM_CONVENTIONS.md §4 — pinned).
//!
//! Semantics mirrored exactly from the Python reference (`iap/orderbook/book.py`):
//!
//! - ADD: new order at its price level, FIFO tail. A limit ADD that crosses
//!   the opposite side executes against the book (marketable) from the best
//!   opposite level's FIFO head; any leftover posts at its price (pinned).
//! - MODIFY: qty change only. Decrease keeps queue position; increase moves
//!   the order to the tail of its level (pinned). Event price is ignored.
//! - CANCEL: remove by order_id.
//! - EXECUTE: fill the referenced order (FIFO head under valid flow); partial
//!   supported; order removed when qty reaches 0. Never touches trade_flow.
//! - TRADE: cumulative signed trade_flow only (+qty for BID aggressor).
//! - QUOTE (FX): replaces the venue's whole side at L1 with one synthetic order.
//! - SNAPSHOT: recovery burst; first record clears both sides; the record with
//!   trade_id == 0 completes the burst and clears `stale` — unless a sequence
//!   gap occurred INSIDE the burst, which marks the burst broken: a broken
//!   burst still ends at its trade_id == 0 record but leaves `stale` set;
//!   only a later complete burst with no interior gap clears it (pinned).
//! - Side domain: for side-indexed event types (ADD, QUOTE, SNAPSHOT, TRADE)
//!   `side` must be BID (0) or ASK (1). An event with side > 1 is malformed:
//!   dropped + counted (`invalid_side_dropped`) through the same
//!   drop-don't-raise path as duplicates / unknown-order events — never an
//!   error mid-stream — after its sequence number is consumed (pinned).
//! - Sequence: duplicate (<= last) dropped + counted; gap => stale + counted;
//!   while stale only SNAPSHOT/STATUS/TRADE/HEARTBEAT apply.
//!
//! Determinism: levels live in a `BTreeMap` keyed by (side, price) so every
//! serialized path iterates in sorted order; the order-id index is a `HashMap`
//! used for lookups only, never iterated on a serialized path.

use std::collections::{BTreeMap, HashMap};

use marketdata::{EventType, IapError, MarketEvent, SessionStatus, Side};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

/// Default depth reported by `depth()` / `order_count()`.
pub const DEPTH_LEVELS: usize = 10;

const BID: u8 = Side::Bid as u8;

/// One price level: FIFO queue of (order_id, qty) plus cached total.
#[derive(Debug, Clone)]
struct Level {
    price: i64,
    orders: Vec<(u64, i64)>, // FIFO: head at index 0, tail at the end
    total_qty: i64,
}

impl Level {
    fn new(price: i64) -> Level {
        Level {
            price,
            orders: Vec::new(),
            total_qty: 0,
        }
    }

    fn order_count(&self) -> usize {
        self.orders.len()
    }
}

/// QC counters (exactly the Python reference's counter set).
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Counters {
    pub duplicates_dropped: u64,
    pub gaps_detected: u64,
    pub dropped_while_stale: u64,
    pub unknown_order_events: u64,
    pub invalid_side_dropped: u64,
    pub events_applied: u64,
}

/// One serialized price level inside a [`BookCheckpoint`].
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LevelCheckpoint {
    pub side: u8,
    pub price_ticks: i64,
    /// FIFO order list: `[order_id, qty]` pairs, head first.
    pub orders: Vec<(u64, i64)>,
}

/// Full deterministic book serialization (levels in sorted (side, price) order).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BookCheckpoint {
    pub instrument_id: u32,
    pub venue_id: u16,
    pub levels: Vec<LevelCheckpoint>,
    /// Global arrival order of resting order ids — `restore()` rebuilds it so
    /// `resting_orders()` round-trips checkpoints exactly (levels alone only
    /// pin per-level FIFO).
    pub arrival_order: Vec<u64>,
    pub last_sequence: u64,
    pub exchange_ts: i64,
    pub receive_ts: i64,
    pub trade_flow: i64,
    pub status: i64,
    pub stale: bool,
    pub snapshot_active: bool,
    pub snapshot_broken: bool,
    pub counters: Counters,
}

/// MBO order book for one instrument on one venue (venue_id = 0: synthetic,
/// accepts any venue).
#[derive(Debug, Clone)]
pub struct OrderBook {
    pub instrument_id: u32,
    pub venue_id: u16,
    levels: BTreeMap<(u8, i64), Level>,
    orders: HashMap<u64, (u8, i64)>, // order_id -> (side, price); lookups only
    arrival: Vec<u64>,               // resting order ids in global arrival order
    pub last_sequence: u64,
    pub exchange_ts: i64,
    pub receive_ts: i64,
    pub trade_flow: i64,
    pub status: i64,
    pub stale: bool,
    snapshot_active: bool,
    snapshot_broken: bool,
    pub counters: Counters,
}

impl OrderBook {
    /// Create an empty book (status starts at TRADING, like the reference).
    pub fn new(instrument_id: u32, venue_id: u16) -> OrderBook {
        OrderBook {
            instrument_id,
            venue_id,
            levels: BTreeMap::new(),
            orders: HashMap::new(),
            arrival: Vec::new(),
            last_sequence: 0,
            exchange_ts: 0,
            receive_ts: 0,
            trade_flow: 0,
            status: SessionStatus::Trading as i64,
            stale: false,
            snapshot_active: false,
            snapshot_broken: false,
            counters: Counters::default(),
        }
    }

    // ---------------------------------------------------------- application

    /// Apply one event (sequence-checked). Errors on routing/unknown-type.
    pub fn apply(&mut self, ev: &MarketEvent) -> Result<(), IapError> {
        if ev.instrument_id != self.instrument_id
            || (self.venue_id != 0 && ev.venue_id != self.venue_id)
        {
            return Err(IapError::Book(format!(
                "event routed to wrong book: event {}@{}, book {}@{}",
                ev.instrument_id, ev.venue_id, self.instrument_id, self.venue_id
            )));
        }
        // Sequence handling (duplicates dropped, gaps => stale).
        if ev.sequence <= self.last_sequence {
            self.counters.duplicates_dropped += 1;
            return Ok(());
        }
        if ev.sequence > self.last_sequence + 1 && self.last_sequence != 0 {
            self.counters.gaps_detected += 1;
            self.stale = true;
            if self.snapshot_active {
                // Gap inside an active SNAPSHOT burst: the burst is broken —
                // its completion record must NOT clear `stale` (records are
                // missing). Only a later complete gap-free burst recovers.
                self.snapshot_broken = true;
            }
        }
        self.last_sequence = ev.sequence;
        self.exchange_ts = ev.exchange_ts;
        self.receive_ts = ev.receive_ts;

        let et = EventType::from_u8(ev.event_type);
        // Side-domain validation for side-indexed event types: malformed
        // side => dropped + counted (never an error mid-stream), same path
        // as other malformed events; the sequence number above is consumed.
        if ev.side > 1
            && matches!(
                et,
                Some(EventType::Add)
                    | Some(EventType::Quote)
                    | Some(EventType::Snapshot)
                    | Some(EventType::Trade)
            )
        {
            self.counters.invalid_side_dropped += 1;
            return Ok(());
        }
        if self.stale
            && !matches!(
                et,
                Some(EventType::Snapshot)
                    | Some(EventType::Status)
                    | Some(EventType::Trade)
                    | Some(EventType::Heartbeat)
            )
        {
            self.counters.dropped_while_stale += 1;
            return Ok(());
        }

        match et {
            Some(EventType::Add) => self.apply_add(ev)?,
            Some(EventType::Modify) => self.apply_modify(ev)?,
            Some(EventType::Cancel) => self.apply_cancel(ev)?,
            Some(EventType::Execute) => self.apply_execute(ev)?,
            Some(EventType::Trade) => {
                if ev.side == BID {
                    self.trade_flow += ev.qty;
                } else {
                    self.trade_flow -= ev.qty;
                }
            }
            Some(EventType::Quote) => self.apply_quote(ev)?,
            Some(EventType::Snapshot) => self.apply_snapshot(ev)?,
            Some(EventType::Status) => self.status = ev.qty,
            Some(EventType::Heartbeat) => {}
            None => {
                return Err(IapError::Book(format!(
                    "unknown event_type {} (event_id={})",
                    ev.event_type, ev.event_id
                )));
            }
        }
        self.counters.events_applied += 1;
        Ok(())
    }

    // ----------------------------------------------------------- primitives

    fn insert_order(&mut self, side: u8, price: i64, order_id: u64, qty: i64) {
        let level = self
            .levels
            .entry((side, price))
            .or_insert_with(|| Level::new(price));
        level.orders.push((order_id, qty));
        level.total_qty += qty;
        self.orders.insert(order_id, (side, price));
        self.arrival.push(order_id);
    }

    fn remove_order(&mut self, order_id: u64) -> Result<(), IapError> {
        let key = self.orders.remove(&order_id).ok_or_else(|| {
            IapError::Book(format!("internal: removing unknown order_id {order_id}"))
        })?;
        let level = self.levels.get_mut(&key).ok_or_else(|| {
            IapError::Book(format!("internal: order_id {order_id} maps to missing level"))
        })?;
        let pos = level
            .orders
            .iter()
            .position(|&(oid, _)| oid == order_id)
            .ok_or_else(|| {
                IapError::Book(format!("internal: order_id {order_id} missing from level"))
            })?;
        let (_, qty) = level.orders.remove(pos);
        level.total_qty -= qty;
        if level.orders.is_empty() {
            self.levels.remove(&key);
        }
        if let Some(apos) = self.arrival.iter().position(|&oid| oid == order_id) {
            self.arrival.remove(apos);
        }
        Ok(())
    }

    fn apply_add(&mut self, ev: &MarketEvent) -> Result<(), IapError> {
        if self.orders.contains_key(&ev.order_id) {
            self.counters.unknown_order_events += 1; // duplicate order id: drop, count
            return Ok(());
        }
        let remaining = self.match_marketable(ev.side, ev.price_ticks, ev.qty)?;
        if remaining > 0 {
            self.insert_order(ev.side, ev.price_ticks, ev.order_id, remaining);
        }
        Ok(())
    }

    /// Execute a crossing limit against the opposite side; return leftover qty.
    fn match_marketable(&mut self, side: u8, price: i64, mut qty: i64) -> Result<i64, IapError> {
        let opp = 1 - side;
        while qty > 0 {
            // Best opposite level: max price for bids, min price for asks.
            let best = {
                let mut range = self.levels.range((opp, i64::MIN)..=(opp, i64::MAX));
                let entry = if opp == BID {
                    range.next_back()
                } else {
                    range.next()
                };
                match entry {
                    Some((&key, level)) => {
                        let &(head_id, head_qty) = level.orders.first().ok_or_else(|| {
                            IapError::Book("internal: empty level in book".to_string())
                        })?;
                        Some((key, level.price, head_id, head_qty))
                    }
                    None => None,
                }
            };
            let Some((key, best_price, head_id, head_qty)) = best else {
                break;
            };
            let crosses = if side == BID {
                price >= best_price
            } else {
                price <= best_price
            };
            if !crosses {
                break;
            }
            // Fill from the FIFO head of the best opposite level.
            let fill = qty.min(head_qty);
            qty -= fill;
            if fill == head_qty {
                self.remove_order(head_id)?;
            } else {
                let level = self.levels.get_mut(&key).ok_or_else(|| {
                    IapError::Book("internal: best level vanished during match".to_string())
                })?;
                if let Some(head) = level.orders.first_mut() {
                    head.1 = head_qty - fill;
                }
                level.total_qty -= fill;
            }
        }
        Ok(qty)
    }

    fn apply_modify(&mut self, ev: &MarketEvent) -> Result<(), IapError> {
        let Some(&key) = self.orders.get(&ev.order_id) else {
            self.counters.unknown_order_events += 1;
            return Ok(());
        };
        let new_qty = ev.qty;
        if new_qty <= 0 {
            return self.remove_order(ev.order_id);
        }
        let level = self.levels.get_mut(&key).ok_or_else(|| {
            IapError::Book(format!("internal: order_id {} maps to missing level", ev.order_id))
        })?;
        let pos = level
            .orders
            .iter()
            .position(|&(oid, _)| oid == ev.order_id)
            .ok_or_else(|| {
                IapError::Book(format!("internal: order_id {} missing from level", ev.order_id))
            })?;
        let old_qty = level.orders[pos].1;
        if new_qty <= old_qty {
            // Decrease: keep queue position.
            level.orders[pos].1 = new_qty;
        } else {
            // Increase: move to the tail of the level.
            level.orders.remove(pos);
            level.orders.push((ev.order_id, new_qty));
        }
        level.total_qty += new_qty - old_qty;
        Ok(())
    }

    fn apply_cancel(&mut self, ev: &MarketEvent) -> Result<(), IapError> {
        if !self.orders.contains_key(&ev.order_id) {
            self.counters.unknown_order_events += 1;
            return Ok(());
        }
        self.remove_order(ev.order_id)
    }

    fn apply_execute(&mut self, ev: &MarketEvent) -> Result<(), IapError> {
        let Some(&key) = self.orders.get(&ev.order_id) else {
            self.counters.unknown_order_events += 1;
            return Ok(());
        };
        let level = self.levels.get_mut(&key).ok_or_else(|| {
            IapError::Book(format!("internal: order_id {} maps to missing level", ev.order_id))
        })?;
        let pos = level
            .orders
            .iter()
            .position(|&(oid, _)| oid == ev.order_id)
            .ok_or_else(|| {
                IapError::Book(format!("internal: order_id {} missing from level", ev.order_id))
            })?;
        let old_qty = level.orders[pos].1;
        let fill = ev.qty.min(old_qty);
        if fill >= old_qty {
            self.remove_order(ev.order_id)
        } else {
            level.orders[pos].1 = old_qty - fill;
            level.total_qty -= fill;
            Ok(())
        }
    }

    /// FX QUOTE: replace this venue's whole side at L1.
    fn apply_quote(&mut self, ev: &MarketEvent) -> Result<(), IapError> {
        let side = ev.side;
        let ids: Vec<u64> = self
            .levels
            .range((side, i64::MIN)..=(side, i64::MAX))
            .flat_map(|(_, level)| level.orders.iter().map(|&(oid, _)| oid))
            .collect();
        for oid in ids {
            self.remove_order(oid)?;
        }
        self.insert_order(side, ev.price_ticks, ev.order_id, ev.qty);
        Ok(())
    }

    fn apply_snapshot(&mut self, ev: &MarketEvent) -> Result<(), IapError> {
        if !self.snapshot_active {
            // Burst start: clear the whole book state (levels + orders).
            self.levels.clear();
            self.orders.clear();
            self.arrival.clear();
            self.snapshot_active = true;
            self.snapshot_broken = false;
        }
        if self.orders.contains_key(&ev.order_id) {
            self.remove_order(ev.order_id)?;
        }
        self.insert_order(ev.side, ev.price_ticks, ev.order_id, ev.qty);
        if ev.trade_id == 0 {
            // Last record of the burst; a broken burst never clears `stale`.
            self.snapshot_active = false;
            if !self.snapshot_broken {
                self.stale = false;
            }
            self.snapshot_broken = false;
        }
        Ok(())
    }

    // -------------------------------------------------------- derived state

    /// All resting orders `(order_id, side, price_ticks, qty)` in
    /// deterministic global arrival (insertion) order — exactly the Python
    /// reference's `resting_orders()` iteration order.
    pub fn resting_orders(&self, side: Option<u8>) -> Vec<(u64, u8, i64, i64)> {
        let mut out = Vec::new();
        for &oid in &self.arrival {
            let Some(&(s, price)) = self.orders.get(&oid) else {
                continue;
            };
            if side.is_some() && side != Some(s) {
                continue;
            }
            let qty = self
                .levels
                .get(&(s, price))
                .and_then(|level| {
                    level
                        .orders
                        .iter()
                        .find(|&&(o, _)| o == oid)
                        .map(|&(_, q)| q)
                })
                .unwrap_or(0);
            out.push((oid, s, price, qty));
        }
        out
    }

    /// Total number of resting orders in the book.
    pub fn order_count_total(&self) -> usize {
        self.orders.len()
    }

    fn best_level(&self, side: u8) -> Option<&Level> {
        let mut range = self.levels.range((side, i64::MIN)..=(side, i64::MAX));
        let entry = if side == BID {
            range.next_back()
        } else {
            range.next()
        };
        entry.map(|(_, level)| level)
    }

    /// `(price_ticks, total_size)` of the best bid, or `None`.
    pub fn best_bid(&self) -> Option<(i64, i64)> {
        self.best_level(BID).map(|l| (l.price, l.total_qty))
    }

    /// `(price_ticks, total_size)` of the best ask, or `None`.
    pub fn best_ask(&self) -> Option<(i64, i64)> {
        self.best_level(Side::Ask as u8)
            .map(|l| (l.price, l.total_qty))
    }

    /// Levels of one side, best-first (bids: descending price; asks: ascending).
    fn sorted_levels(&self, side: u8) -> Vec<&Level> {
        let iter = self.levels.range((side, i64::MIN)..=(side, i64::MAX));
        if side == BID {
            iter.rev().map(|(_, l)| l).collect()
        } else {
            iter.map(|(_, l)| l).collect()
        }
    }

    /// Top-N `(price_ticks, total_size)` best-first.
    pub fn depth(&self, side: u8, levels: usize) -> Vec<(i64, i64)> {
        self.sorted_levels(side)
            .into_iter()
            .take(levels)
            .map(|l| (l.price, l.total_qty))
            .collect()
    }

    /// Top-N `(price_ticks, order_count)` best-first.
    pub fn order_count(&self, side: u8, levels: usize) -> Vec<(i64, i64)> {
        self.sorted_levels(side)
            .into_iter()
            .take(levels)
            .map(|l| (l.price, l.order_count() as i64))
            .collect()
    }

    /// Golden-comparable exact-integer state (shape of
    /// `tests/golden/expected_book_states.json` entries).
    pub fn state_summary(&self) -> Value {
        let bb = self.best_bid();
        let ba = self.best_ask();
        let pairs = |v: Vec<(i64, i64)>| -> Vec<Value> {
            v.into_iter().map(|(a, b)| json!([a, b])).collect()
        };
        json!({
            "best_bid_ticks": bb.map_or(0, |t| t.0),
            "best_bid_size": bb.map_or(0, |t| t.1),
            "best_ask_ticks": ba.map_or(0, |t| t.0),
            "best_ask_size": ba.map_or(0, |t| t.1),
            "depth_bid_top5": pairs(self.depth(BID, 5)),
            "depth_ask_top5": pairs(self.depth(Side::Ask as u8, 5)),
            "order_count_bid_top3": pairs(self.order_count(BID, 3)),
            "order_count_ask_top3": pairs(self.order_count(Side::Ask as u8, 3)),
            "trade_flow": self.trade_flow,
            "sequence": self.last_sequence,
        })
    }

    // ---------------------------------------------------------- checkpoints

    /// Full deterministic serialization (levels sorted by (side, price), FIFO
    /// order lists preserved).
    pub fn checkpoint(&self) -> BookCheckpoint {
        BookCheckpoint {
            instrument_id: self.instrument_id,
            venue_id: self.venue_id,
            levels: self
                .levels
                .iter()
                .map(|(&(side, price), level)| LevelCheckpoint {
                    side,
                    price_ticks: price,
                    orders: level.orders.clone(),
                })
                .collect(),
            arrival_order: self.arrival.clone(),
            last_sequence: self.last_sequence,
            exchange_ts: self.exchange_ts,
            receive_ts: self.receive_ts,
            trade_flow: self.trade_flow,
            status: self.status,
            stale: self.stale,
            snapshot_active: self.snapshot_active,
            snapshot_broken: self.snapshot_broken,
            counters: self.counters,
        }
    }

    /// Rebuild an identical book from `checkpoint()` output.
    pub fn restore(cp: &BookCheckpoint) -> Result<OrderBook, IapError> {
        let mut book = OrderBook::new(cp.instrument_id, cp.venue_id);
        for lvl in &cp.levels {
            if lvl.side > 1 {
                return Err(IapError::Checkpoint(format!(
                    "invalid side {} in checkpoint level",
                    lvl.side
                )));
            }
            for &(oid, qty) in &lvl.orders {
                if book.orders.contains_key(&oid) {
                    return Err(IapError::Checkpoint(format!(
                        "duplicate order_id {oid} in checkpoint"
                    )));
                }
                book.insert_order(lvl.side, lvl.price_ticks, oid, qty);
            }
        }
        // Rebuild the global arrival order (levels above fixed per-level FIFO
        // order; `arrival` must iterate in original insertion order so e.g.
        // resting_orders() is checkpoint-round-trip exact).
        if cp.arrival_order.len() != book.orders.len()
            || cp
                .arrival_order
                .iter()
                .any(|oid| !book.orders.contains_key(oid))
        {
            return Err(IapError::Checkpoint(
                "checkpoint arrival_order inconsistent with levels".to_string(),
            ));
        }
        book.arrival = cp.arrival_order.clone();
        book.last_sequence = cp.last_sequence;
        book.exchange_ts = cp.exchange_ts;
        book.receive_ts = cp.receive_ts;
        book.trade_flow = cp.trade_flow;
        book.status = cp.status;
        book.stale = cp.stale;
        book.snapshot_active = cp.snapshot_active;
        book.snapshot_broken = cp.snapshot_broken;
        book.counters = cp.counters;
        Ok(book)
    }
}

/// Checkpoint of a [`ConsolidatedBook`]: per-venue book checkpoints in sorted
/// venue order.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ConsolidatedCheckpoint {
    pub instrument_id: u32,
    pub venues: BTreeMap<u16, BookCheckpoint>,
}

/// Consolidated view over per-venue books of one instrument.
///
/// Routes events to per-venue books by venue_id and merges derived state:
/// same price across venues => sizes and order counts summed; best = best
/// across venues. Sequence/staleness remain per venue.
#[derive(Debug, Clone)]
pub struct ConsolidatedBook {
    pub instrument_id: u32,
    pub books: BTreeMap<u16, OrderBook>,
}

impl ConsolidatedBook {
    /// Create an empty consolidated book.
    pub fn new(instrument_id: u32) -> ConsolidatedBook {
        ConsolidatedBook {
            instrument_id,
            books: BTreeMap::new(),
        }
    }

    /// Get (or lazily create) the per-venue book.
    pub fn venue_book(&mut self, venue_id: u16) -> &mut OrderBook {
        self.books
            .entry(venue_id)
            .or_insert_with(|| OrderBook::new(self.instrument_id, venue_id))
    }

    /// Route one event to its venue book.
    pub fn apply(&mut self, ev: &MarketEvent) -> Result<(), IapError> {
        self.venue_book(ev.venue_id).apply(ev)
    }

    /// Merged `(price, total_size, order_count)` best-first for one side.
    fn merged(&self, side: u8) -> Vec<(i64, i64, i64)> {
        let mut agg: BTreeMap<i64, (i64, i64)> = BTreeMap::new();
        for book in self.books.values() {
            for level in book.sorted_levels(side) {
                let slot = agg.entry(level.price).or_insert((0, 0));
                slot.0 += level.total_qty;
                slot.1 += level.order_count() as i64;
            }
        }
        let iter = agg.into_iter().map(|(p, (sq, oc))| (p, sq, oc));
        if side == BID {
            iter.rev().collect()
        } else {
            iter.collect()
        }
    }

    /// Best merged bid `(price_ticks, total_size)`, or `None`.
    pub fn best_bid(&self) -> Option<(i64, i64)> {
        self.merged(BID).first().map(|&(p, sq, _)| (p, sq))
    }

    /// Best merged ask `(price_ticks, total_size)`, or `None`.
    pub fn best_ask(&self) -> Option<(i64, i64)> {
        self.merged(Side::Ask as u8)
            .first()
            .map(|&(p, sq, _)| (p, sq))
    }

    /// Merged top-N `(price_ticks, total_size)` best-first.
    pub fn depth(&self, side: u8, levels: usize) -> Vec<(i64, i64)> {
        self.merged(side)
            .into_iter()
            .take(levels)
            .map(|(p, sq, _)| (p, sq))
            .collect()
    }

    /// Merged top-N `(price_ticks, order_count)` best-first.
    pub fn order_count(&self, side: u8, levels: usize) -> Vec<(i64, i64)> {
        self.merged(side)
            .into_iter()
            .take(levels)
            .map(|(p, _, oc)| (p, oc))
            .collect()
    }

    /// Sum of per-venue cumulative signed trade flow.
    pub fn trade_flow(&self) -> i64 {
        self.books.values().map(|b| b.trade_flow).sum()
    }

    /// Serialize every venue book (sorted venue order).
    pub fn checkpoint(&self) -> ConsolidatedCheckpoint {
        ConsolidatedCheckpoint {
            instrument_id: self.instrument_id,
            venues: self
                .books
                .iter()
                .map(|(&vid, book)| (vid, book.checkpoint()))
                .collect(),
        }
    }

    /// Rebuild from `checkpoint()` output.
    pub fn restore(cp: &ConsolidatedCheckpoint) -> Result<ConsolidatedBook, IapError> {
        let mut cons = ConsolidatedBook::new(cp.instrument_id);
        for (&vid, bcp) in &cp.venues {
            cons.books.insert(vid, OrderBook::restore(bcp)?);
        }
        Ok(cons)
    }
}
