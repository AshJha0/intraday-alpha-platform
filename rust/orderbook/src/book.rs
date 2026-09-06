//! Reference L1/L2/MBO order book (PLATFORM_CONVENTIONS.md §4 — pinned).
//!
//! Semantics mirrored exactly from the Python reference (`iap/orderbook/book.py`,
//! API_CORE.md §4):
//!
//! - ADD: new order at its price level, FIFO tail. While `status == TRADING`
//!   a limit ADD that crosses the opposite side executes against the book
//!   (marketable) from the best opposite level's FIFO head; any leftover
//!   posts at its price. While HALT / AUCTION / CLOSE nothing matches: the
//!   ADD rests and the book may be crossed (call phase).
//! - MODIFY: qty change only. Decrease keeps queue position; increase moves
//!   the order to the tail of its level (pinned). A non-zero price that
//!   differs from the resting price is dropped + counted
//!   (`modify_price_mismatch`).
//! - CANCEL: remove by order_id.
//! - EXECUTE: fill the referenced order (FIFO head under valid flow); partial
//!   supported; order removed when qty reaches 0. Never touches trade_flow.
//! - TRADE: cumulative signed trade_flow only (+qty for BID aggressor), checked.
//! - QUOTE (FX): replaces the venue's whole side at L1 with one order;
//!   `order_id == 0` uses the synthetic id `synthetic_order_id(side, 0)`; an
//!   explicit id resting on the other side is malformed (drop + count).
//! - SNAPSHOT: recovery burst; first record clears both sides; the record
//!   with trade_id == 0 completes the burst and clears `stale` — unless a
//!   sequence gap occurred INSIDE the burst (broken burst). The countdown is
//!   validated (`snapshot_restarts`; skipped records break the burst);
//!   id-less records get synthetic ids; a repeated id is malformed.
//! - Malformed-event policy: unknown event_type, side domain, payload domain
//!   and i64 overflow are dropped + counted (`unknown_type_dropped`,
//!   `invalid_side_dropped`, `invalid_payload_dropped`) — never an error
//!   mid-stream — after the sequence number is consumed (pinned).
//! - Sequencing: first event of an epoch accepted whatever its sequence;
//!   duplicates (<= last) dropped + counted; gap => stale + counted unless a
//!   `reorder_window` holds the event back until the hole fills
//!   (`late_recovered`); a SNAPSHOT burst starting below `last_sequence` is a
//!   venue sequence reset (`sequence_resets`, `sequence_epoch`); while stale
//!   only SNAPSHOT/STATUS/TRADE/HEARTBEAT apply.
//!
//! Hot-path design (no allocation after warmup, O(1) per order operation):
//! order and level slabs with free lists, intrusive per-level FIFO lists, an
//! intrusive global arrival list, per-side `BTreeMap<price, level slot>` for
//! best-first iteration, and a `HashMap<order_id, slot>` index used for
//! lookups only — never iterated on a serialized path.

use std::collections::{BTreeMap, HashMap};

use marketdata::{EventType, IapError, MarketEvent, SessionStatus, Side};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

/// Default depth reported by `depth()` / `order_count()`.
pub const DEPTH_LEVELS: usize = 10;

/// Per-event verdict returned by [`OrderBook::apply`] (pinned).
///
/// `applied + dropped + held == events fed` for every book; downstream
/// consumers (feature engine, API_FEATURES.md §2) MUST ignore every event
/// that is not `Applied`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ApplyStatus {
    /// The event changed book state (or was a valid no-op: HEARTBEAT/STATUS).
    Applied,
    /// The event was rejected and counted in exactly one drop counter.
    Dropped,
    /// The event is buffered behind a sequence hole (`reorder_window > 0`);
    /// it is reported Applied/Dropped when the buffer drains.
    Held,
}

/// Checkpoint schema version (API_CORE §4/§5).
pub const CHECKPOINT_VERSION: i64 = 2;

/// Largest accepted `reorder_window` (hold-back buffer, events) — pinned.
pub const MAX_REORDER_WINDOW: usize = 4096;

/// Reserved order-id range (top 16 bits set): synthetic ids for id-less
/// QUOTE/SNAPSHOT records = `SYNTHETIC_ID_BASE | side << 40 | ordinal`.
pub const SYNTHETIC_ID_BASE: u64 = 0xFFFF_0000_0000_0000;

/// Deterministic synthetic order id for id-less QUOTE/SNAPSHOT records.
pub const fn synthetic_order_id(side: u8, ordinal: u64) -> u64 {
    SYNTHETIC_ID_BASE | ((side as u64) << 40) | (ordinal & ((1u64 << 40) - 1))
}

const BID: u8 = Side::Bid as u8;
const NIL: u32 = u32::MAX;

#[derive(Debug, Clone, Copy, Default)]
struct OrderNode {
    id: u64,
    qty: i64,
    prev: u32, // level FIFO links
    next: u32,
    level: u32,
    arr_prev: u32, // global arrival-order links
    arr_next: u32,
}

#[derive(Debug, Clone, Copy, Default)]
struct LevelNode {
    side: u8,
    price: i64,
    total_qty: i64,
    head: u32,
    tail: u32,
    count: u32,
}

/// QC counters (exactly the Python reference's 12 counters, pinned order).
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Counters {
    pub duplicates_dropped: u64,
    pub gaps_detected: u64,
    pub dropped_while_stale: u64,
    pub unknown_order_events: u64,
    pub invalid_side_dropped: u64,
    pub invalid_payload_dropped: u64,
    pub unknown_type_dropped: u64,
    pub modify_price_mismatch: u64,
    pub snapshot_restarts: u64,
    pub sequence_resets: u64,
    pub late_recovered: u64,
    pub events_applied: u64,
}

impl Counters {
    /// Sum of the drop counters (accounting invariant: applied + drops +
    /// pending == events fed).
    pub fn drops(&self) -> u64 {
        self.duplicates_dropped
            + self.dropped_while_stale
            + self.unknown_order_events
            + self.invalid_side_dropped
            + self.invalid_payload_dropped
            + self.unknown_type_dropped
            + self.modify_price_mismatch
    }
}

/// One serialized price level inside a [`BookCheckpoint`].
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LevelCheckpoint {
    pub side: u8,
    pub price_ticks: i64,
    /// FIFO order list: `[order_id, qty]` pairs, head first.
    pub orders: Vec<(u64, i64)>,
}

/// One held-back event in a checkpoint: the 12 canonical fields as an array.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct PendingRow(
    pub u64,
    pub u32,
    pub u16,
    pub i64,
    pub i64,
    pub u64,
    pub u8,
    pub u8,
    pub i64,
    pub i64,
    pub u64,
    pub u64,
);

impl From<&MarketEvent> for PendingRow {
    fn from(ev: &MarketEvent) -> PendingRow {
        PendingRow(
            ev.event_id,
            ev.instrument_id,
            ev.venue_id,
            ev.exchange_ts,
            ev.receive_ts,
            ev.sequence,
            ev.event_type,
            ev.side,
            ev.price_ticks,
            ev.qty,
            ev.order_id,
            ev.trade_id,
        )
    }
}

impl From<&PendingRow> for MarketEvent {
    fn from(r: &PendingRow) -> MarketEvent {
        MarketEvent {
            event_id: r.0,
            instrument_id: r.1,
            venue_id: r.2,
            exchange_ts: r.3,
            receive_ts: r.4,
            sequence: r.5,
            event_type: r.6,
            side: r.7,
            price_ticks: r.8,
            qty: r.9,
            order_id: r.10,
            trade_id: r.11,
        }
    }
}

/// Full deterministic book serialization (levels in sorted (side, price)
/// order) — the cross-language JSON shape of API_CORE §4 (x-version 2).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BookCheckpoint {
    #[serde(rename = "x-version")]
    pub x_version: i64,
    pub instrument_id: u32,
    pub venue_id: u16,
    pub levels: Vec<LevelCheckpoint>,
    /// Global arrival order of resting order ids — `restore()` rebuilds it so
    /// `resting_orders()` round-trips checkpoints exactly.
    pub arrival_order: Vec<u64>,
    pub last_sequence: u64,
    pub has_sequence: bool,
    pub sequence_epoch: u64,
    pub exchange_ts: i64,
    pub receive_ts: i64,
    pub trade_flow: i64,
    pub status: i64,
    pub stale: bool,
    pub snapshot_active: bool,
    pub snapshot_broken: bool,
    pub snapshot_countdown: u64,
    pub snapshot_synthetic_next: [u64; 2],
    pub reorder_window: u64,
    pub reorder_pending: Vec<PendingRow>,
    pub counters: Counters,
}

/// MBO order book for one instrument on one venue (venue_id = 0: synthetic,
/// accepts any venue).
#[derive(Debug, Clone)]
pub struct OrderBook {
    pub instrument_id: u32,
    pub venue_id: u16,
    reorder_window: usize,
    orders: Vec<OrderNode>,
    free_orders: Vec<u32>,
    levels: Vec<LevelNode>,
    free_levels: Vec<u32>,
    side_levels: [BTreeMap<i64, u32>; 2],
    index: HashMap<u64, u32>, // order_id -> slot; lookups only
    arrival_head: u32,
    arrival_tail: u32,
    pub last_sequence: u64,
    pub has_sequence: bool,
    pub sequence_epoch: u64,
    pub exchange_ts: i64,
    pub receive_ts: i64,
    pub trade_flow: i64,
    pub status: i64,
    pub stale: bool,
    snapshot_active: bool,
    snapshot_broken: bool,
    snapshot_countdown: u64,
    snapshot_synthetic_next: [u64; 2],
    pending: BTreeMap<u64, MarketEvent>,
    pub counters: Counters,
}

impl OrderBook {
    /// Create an empty book (status starts at TRADING, like the reference).
    pub fn new(instrument_id: u32, venue_id: u16) -> OrderBook {
        OrderBook::with_reorder_window(instrument_id, venue_id, 0)
            .expect("reorder_window 0 is always valid")
    }

    /// Create an empty book with a hold-back buffer of `reorder_window`
    /// events (0 = off; `Err` above [`MAX_REORDER_WINDOW`]).
    pub fn with_reorder_window(
        instrument_id: u32,
        venue_id: u16,
        reorder_window: usize,
    ) -> Result<OrderBook, IapError> {
        if reorder_window > MAX_REORDER_WINDOW {
            return Err(IapError::InvalidArgument(format!(
                "reorder_window must be <= {MAX_REORDER_WINDOW}: {reorder_window}"
            )));
        }
        let mut book = OrderBook {
            instrument_id,
            venue_id,
            reorder_window,
            orders: Vec::new(),
            free_orders: Vec::new(),
            levels: Vec::new(),
            free_levels: Vec::new(),
            side_levels: [BTreeMap::new(), BTreeMap::new()],
            index: HashMap::new(),
            arrival_head: NIL,
            arrival_tail: NIL,
            last_sequence: 0,
            has_sequence: false,
            sequence_epoch: 0,
            exchange_ts: 0,
            receive_ts: 0,
            trade_flow: 0,
            status: SessionStatus::Trading as i64,
            stale: false,
            snapshot_active: false,
            snapshot_broken: false,
            snapshot_countdown: 0,
            snapshot_synthetic_next: [0, 0],
            pending: BTreeMap::new(),
            counters: Counters::default(),
        };
        book.reserve(1024, 256);
        Ok(book)
    }

    /// Pre-size the pools (never shrinks).
    pub fn reserve(&mut self, orders: usize, levels: usize) {
        self.orders.reserve(orders);
        self.free_orders.reserve(orders);
        self.levels.reserve(levels);
        self.free_levels.reserve(levels);
        self.index.reserve(orders);
    }

    /// Hold-back buffer size.
    pub fn reorder_window(&self) -> usize {
        self.reorder_window
    }

    // ---------------------------------------------------------- application

    /// Apply one event (sequence-checked). Errors ONLY on routing (wrong
    /// instrument/venue); every malformed event is dropped + counted.
    ///
    /// Returns the pinned per-event verdict ([`ApplyStatus`]): downstream
    /// consumers (the feature engine, API_FEATURES.md §2) fold ONLY
    /// `Applied` events into rolling state.
    pub fn apply(&mut self, ev: &MarketEvent) -> Result<ApplyStatus, IapError> {
        if ev.instrument_id != self.instrument_id
            || (self.venue_id != 0 && ev.venue_id != self.venue_id)
        {
            return Err(IapError::Book(format!(
                "event routed to wrong book: event {}@{}, book {}@{}",
                ev.instrument_id, ev.venue_id, self.instrument_id, self.venue_id
            )));
        }
        if self.reorder_window != 0
            && self.has_sequence
            && ev.sequence > self.last_sequence
            && ev.sequence - self.last_sequence > 1
        {
            // Out-of-sequence event ahead of a hole: hold it back until the
            // missing sequences arrive (bounded by reorder_window).
            if self.pending.contains_key(&ev.sequence) {
                self.counters.duplicates_dropped += 1;
                return Ok(ApplyStatus::Dropped);
            }
            if self.pending.len() < self.reorder_window {
                self.pending.insert(ev.sequence, *ev);
                return Ok(ApplyStatus::Held);
            }
            // Buffer full: give up on the hole, declare the gap and apply
            // everything held so far in sequence order.
            let seq = ev.sequence;
            self.pending.insert(seq, *ev);
            return Ok(self.flush_pending(Some(seq)));
        }
        let status = self.apply_sequenced(ev, false);
        if !self.pending.is_empty() {
            self.drain_pending();
        }
        Ok(status)
    }

    /// Apply every held-back event in sequence order (gap declared);
    /// returns the verdict of `target` when the caller tracks one.
    fn flush_pending(&mut self, target: Option<u64>) -> ApplyStatus {
        let pending = std::mem::take(&mut self.pending);
        let mut status = ApplyStatus::Applied;
        for (seq, pev) in pending.iter() {
            let st = self.apply_sequenced(pev, true);
            if Some(*seq) == target {
                status = st;
            }
        }
        status
    }

    fn drain_pending(&mut self) {
        while let Some(pev) = self.pending.remove(&(self.last_sequence.wrapping_add(1))) {
            self.apply_sequenced(&pev, true);
        }
    }

    /// Explicit venue sequence reset (session roll known out of band): held
    /// events are flushed, then a new epoch starts (next event accepted
    /// whatever its sequence) with the book stale until a complete burst.
    pub fn reset_sequence(&mut self) {
        if !self.pending.is_empty() {
            self.flush_pending(None);
        }
        self.has_sequence = false;
        self.sequence_epoch += 1;
        self.counters.sequence_resets += 1;
        self.stale = true;
        self.snapshot_active = false;
        self.snapshot_broken = false;
        self.snapshot_countdown = 0;
    }

    fn payload_ok(ev: &MarketEvent, et: EventType) -> bool {
        match et {
            EventType::Add => {
                ev.order_id != 0
                    && ev.qty > 0
                    && ev.price_ticks > 0
                    && ev.order_id < SYNTHETIC_ID_BASE
            }
            EventType::Modify | EventType::Cancel => ev.order_id != 0,
            EventType::Execute => ev.order_id != 0 && ev.qty > 0,
            EventType::Quote | EventType::Snapshot => {
                ev.qty > 0 && ev.price_ticks > 0 && ev.order_id < SYNTHETIC_ID_BASE
            }
            EventType::Trade => ev.qty > 0 && ev.price_ticks > 0,
            EventType::Status => SessionStatus::from_i64(ev.qty).is_some(),
            EventType::Heartbeat => true,
        }
    }

    fn apply_sequenced(&mut self, ev: &MarketEvent, from_buffer: bool) -> ApplyStatus {
        let et = EventType::from_u8(ev.event_type);
        if self.has_sequence {
            if ev.sequence <= self.last_sequence {
                if et == Some(EventType::Snapshot)
                    && !self.snapshot_active
                    && ev.sequence < self.last_sequence
                {
                    // Venue sequence reset (daily restart / fail-over): the
                    // SNAPSHOT burst starting the new epoch recovers the book.
                    if !self.pending.is_empty() {
                        self.flush_pending(None);
                    }
                    self.sequence_epoch += 1;
                    self.counters.sequence_resets += 1;
                    self.stale = true;
                } else {
                    self.counters.duplicates_dropped += 1;
                    return ApplyStatus::Dropped;
                }
            } else if ev.sequence - self.last_sequence > 1 {
                self.counters.gaps_detected += 1;
                self.stale = true;
                if self.snapshot_active {
                    // Gap inside an active SNAPSHOT burst: the burst is broken.
                    self.snapshot_broken = true;
                }
            } else if !self.pending.is_empty() && !from_buffer {
                self.counters.late_recovered += 1; // a gap filler arrived late
            }
        }
        self.has_sequence = true;
        self.last_sequence = ev.sequence;
        self.exchange_ts = ev.exchange_ts;
        self.receive_ts = ev.receive_ts;

        // Malformed-event classes: dropped + counted (never an error), after
        // the sequence number above is consumed (pinned).
        let Some(et) = et else {
            self.counters.unknown_type_dropped += 1;
            return ApplyStatus::Dropped;
        };
        if ev.side > 1
            && matches!(
                et,
                EventType::Add | EventType::Quote | EventType::Snapshot | EventType::Trade
            )
        {
            self.counters.invalid_side_dropped += 1;
            return ApplyStatus::Dropped;
        }
        if !Self::payload_ok(ev, et) {
            self.counters.invalid_payload_dropped += 1;
            return ApplyStatus::Dropped;
        }
        if self.stale
            && !matches!(
                et,
                EventType::Snapshot | EventType::Status | EventType::Trade | EventType::Heartbeat
            )
        {
            self.counters.dropped_while_stale += 1;
            return ApplyStatus::Dropped;
        }

        let applied = match et {
            EventType::Add => self.apply_add(ev),
            EventType::Modify => self.apply_modify(ev),
            EventType::Cancel => self.apply_cancel(ev),
            EventType::Execute => self.apply_execute(ev),
            EventType::Trade => {
                let flow = if ev.side == BID {
                    self.trade_flow.checked_add(ev.qty)
                } else {
                    self.trade_flow.checked_sub(ev.qty)
                };
                match flow {
                    Some(f) => {
                        self.trade_flow = f;
                        true
                    }
                    None => {
                        self.counters.invalid_payload_dropped += 1;
                        false
                    }
                }
            }
            EventType::Quote => self.apply_quote(ev),
            EventType::Snapshot => self.apply_snapshot(ev),
            EventType::Status => {
                self.status = ev.qty;
                true
            }
            EventType::Heartbeat => true,
        };
        if applied {
            self.counters.events_applied += 1;
            ApplyStatus::Applied
        } else {
            ApplyStatus::Dropped
        }
    }

    // ----------------------------------------------------------- primitives

    fn alloc_order(&mut self) -> u32 {
        if let Some(oi) = self.free_orders.pop() {
            return oi;
        }
        self.orders.push(OrderNode::default());
        (self.orders.len() - 1) as u32
    }

    fn alloc_level(&mut self) -> u32 {
        if let Some(li) = self.free_levels.pop() {
            return li;
        }
        self.levels.push(LevelNode::default());
        (self.levels.len() - 1) as u32
    }

    fn arrival_append(&mut self, oi: u32) {
        let tail = self.arrival_tail;
        {
            let o = &mut self.orders[oi as usize];
            o.arr_prev = tail;
            o.arr_next = NIL;
        }
        if tail != NIL {
            self.orders[tail as usize].arr_next = oi;
        } else {
            self.arrival_head = oi;
        }
        self.arrival_tail = oi;
    }

    fn arrival_unlink(&mut self, oi: u32) {
        let (prev, next) = {
            let o = &self.orders[oi as usize];
            (o.arr_prev, o.arr_next)
        };
        if prev != NIL {
            self.orders[prev as usize].arr_next = next;
        } else {
            self.arrival_head = next;
        }
        if next != NIL {
            self.orders[next as usize].arr_prev = prev;
        } else {
            self.arrival_tail = prev;
        }
    }

    fn find_level(&self, side: u8, price: i64) -> Option<u32> {
        self.side_levels[side as usize].get(&price).copied()
    }

    fn level_total(&self, side: u8, price: i64) -> i64 {
        self.find_level(side, price)
            .map_or(0, |li| self.levels[li as usize].total_qty)
    }

    fn insert_order(&mut self, side: u8, price: i64, order_id: u64, qty: i64) {
        let li = match self.find_level(side, price) {
            Some(li) => li,
            None => {
                let li = self.alloc_level();
                self.levels[li as usize] = LevelNode {
                    side,
                    price,
                    total_qty: 0,
                    head: NIL,
                    tail: NIL,
                    count: 0,
                };
                self.side_levels[side as usize].insert(price, li);
                li
            }
        };
        let oi = self.alloc_order();
        let tail = self.levels[li as usize].tail;
        self.orders[oi as usize] = OrderNode {
            id: order_id,
            qty,
            prev: tail,
            next: NIL,
            level: li,
            arr_prev: NIL,
            arr_next: NIL,
        };
        if tail != NIL {
            self.orders[tail as usize].next = oi;
        } else {
            self.levels[li as usize].head = oi;
        }
        let lvl = &mut self.levels[li as usize];
        lvl.tail = oi;
        lvl.total_qty += qty;
        lvl.count += 1;
        self.index.insert(order_id, oi);
        self.arrival_append(oi);
    }

    fn remove_order(&mut self, oi: u32) {
        self.arrival_unlink(oi);
        let o = self.orders[oi as usize];
        let li = o.level;
        if o.prev != NIL {
            self.orders[o.prev as usize].next = o.next;
        } else {
            self.levels[li as usize].head = o.next;
        }
        if o.next != NIL {
            self.orders[o.next as usize].prev = o.prev;
        } else {
            self.levels[li as usize].tail = o.prev;
        }
        let lvl = &mut self.levels[li as usize];
        lvl.total_qty -= o.qty;
        lvl.count -= 1;
        self.index.remove(&o.id);
        if lvl.count == 0 {
            let (side, price) = (lvl.side, lvl.price);
            self.side_levels[side as usize].remove(&price);
            self.free_levels.push(li);
        }
        self.free_orders.push(oi);
    }

    fn clear_side(&mut self, side: u8) {
        let lis: Vec<u32> = self.side_levels[side as usize].values().copied().collect();
        for li in lis {
            let mut oi = self.levels[li as usize].head;
            while oi != NIL {
                let next = self.orders[oi as usize].next;
                self.arrival_unlink(oi);
                self.index.remove(&self.orders[oi as usize].id);
                self.free_orders.push(oi);
                oi = next;
            }
            self.free_levels.push(li);
        }
        self.side_levels[side as usize].clear();
    }

    fn clear_book(&mut self) {
        self.orders.clear();
        self.free_orders.clear();
        self.levels.clear();
        self.free_levels.clear();
        self.side_levels[0].clear();
        self.side_levels[1].clear();
        self.index.clear();
        self.arrival_head = NIL;
        self.arrival_tail = NIL;
        self.snapshot_synthetic_next = [0, 0];
    }

    fn best_slot(&self, side: u8) -> Option<u32> {
        let map = &self.side_levels[side as usize];
        if side == BID {
            map.iter().next_back().map(|(_, &li)| li)
        } else {
            map.iter().next().map(|(_, &li)| li)
        }
    }

    /// Execute a crossing limit against the opposite side; return leftover qty.
    fn match_marketable(&mut self, side: u8, price: i64, mut qty: i64) -> i64 {
        let opp = 1 - side;
        while qty > 0 {
            let Some(li) = self.best_slot(opp) else { break };
            let best_price = self.levels[li as usize].price;
            let crosses = if side == BID {
                price >= best_price
            } else {
                price <= best_price
            };
            if !crosses {
                break;
            }
            // Fill from the FIFO head of the best opposite level.
            let head = self.levels[li as usize].head;
            let head_qty = self.orders[head as usize].qty;
            let fill = qty.min(head_qty);
            qty -= fill;
            if fill == head_qty {
                self.remove_order(head);
            } else {
                self.orders[head as usize].qty -= fill;
                self.levels[li as usize].total_qty -= fill;
            }
        }
        qty
    }

    // ------------------------------------------------------- event handlers

    fn apply_add(&mut self, ev: &MarketEvent) -> bool {
        if self.index.contains_key(&ev.order_id) {
            self.counters.unknown_order_events += 1; // duplicate order id
            return false;
        }
        if self
            .level_total(ev.side, ev.price_ticks)
            .checked_add(ev.qty)
            .is_none()
        {
            self.counters.invalid_payload_dropped += 1;
            return false;
        }
        let remaining = if self.status == SessionStatus::Trading as i64 {
            self.match_marketable(ev.side, ev.price_ticks, ev.qty)
        } else {
            ev.qty
        };
        if remaining > 0 {
            self.insert_order(ev.side, ev.price_ticks, ev.order_id, remaining);
        }
        true
    }

    fn apply_modify(&mut self, ev: &MarketEvent) -> bool {
        let Some(&oi) = self.index.get(&ev.order_id) else {
            self.counters.unknown_order_events += 1;
            return false;
        };
        let li = self.orders[oi as usize].level;
        if ev.price_ticks != 0 && ev.price_ticks != self.levels[li as usize].price {
            self.counters.modify_price_mismatch += 1; // price change must be CANCEL+ADD
            return false;
        }
        let old_qty = self.orders[oi as usize].qty;
        let new_qty = ev.qty;
        if new_qty <= 0 {
            self.remove_order(oi);
            return true;
        }
        if new_qty <= old_qty {
            // Decrease: keep queue position.
            self.orders[oi as usize].qty = new_qty;
        } else {
            let total = self.levels[li as usize].total_qty;
            if total.checked_add(new_qty - old_qty).is_none() {
                self.counters.invalid_payload_dropped += 1;
                return false;
            }
            // Increase: move to the tail of the level.
            let tail = self.levels[li as usize].tail;
            if tail != oi {
                let (prev, next) = {
                    let o = &self.orders[oi as usize];
                    (o.prev, o.next)
                };
                if prev != NIL {
                    self.orders[prev as usize].next = next;
                } else {
                    self.levels[li as usize].head = next;
                }
                self.orders[next as usize].prev = prev; // next != NIL since not tail
                self.orders[oi as usize].prev = tail;
                self.orders[oi as usize].next = NIL;
                self.orders[tail as usize].next = oi;
                self.levels[li as usize].tail = oi;
            }
            self.orders[oi as usize].qty = new_qty;
        }
        self.levels[li as usize].total_qty += new_qty - old_qty;
        true
    }

    fn apply_cancel(&mut self, ev: &MarketEvent) -> bool {
        let Some(&oi) = self.index.get(&ev.order_id) else {
            self.counters.unknown_order_events += 1;
            return false;
        };
        self.remove_order(oi);
        true
    }

    fn apply_execute(&mut self, ev: &MarketEvent) -> bool {
        let Some(&oi) = self.index.get(&ev.order_id) else {
            self.counters.unknown_order_events += 1;
            return false;
        };
        let old_qty = self.orders[oi as usize].qty;
        let fill = ev.qty.min(old_qty);
        if fill >= old_qty {
            self.remove_order(oi);
        } else {
            let li = self.orders[oi as usize].level;
            self.orders[oi as usize].qty = old_qty - fill;
            self.levels[li as usize].total_qty -= fill;
        }
        true
    }

    /// FX QUOTE: replace this venue's whole side at L1.
    fn apply_quote(&mut self, ev: &MarketEvent) -> bool {
        let side = ev.side;
        let oid = if ev.order_id != 0 {
            ev.order_id
        } else {
            synthetic_order_id(side, 0)
        };
        if let Some(&oi) = self.index.get(&oid) {
            let li = self.orders[oi as usize].level;
            if self.levels[li as usize].side != side {
                self.counters.unknown_order_events += 1; // id rests on the other side
                return false;
            }
        }
        self.clear_side(side);
        self.insert_order(side, ev.price_ticks, oid, ev.qty);
        true
    }

    fn apply_snapshot(&mut self, ev: &MarketEvent) -> bool {
        if self.snapshot_active {
            if ev.trade_id >= self.snapshot_countdown {
                // Countdown went up (or repeated): the previous burst was
                // interrupted and this record starts a new burst.
                self.snapshot_active = false;
                self.counters.snapshot_restarts += 1;
            } else if ev.trade_id != self.snapshot_countdown - 1 {
                // Countdown skipped ahead: records missing — burst broken.
                self.snapshot_broken = true;
            }
        }
        if !self.snapshot_active {
            // Burst start: clear the whole book state (levels + orders).
            self.clear_book();
            self.snapshot_active = true;
            self.snapshot_broken = false;
        }
        self.snapshot_countdown = ev.trade_id;
        let oid = if ev.order_id != 0 {
            ev.order_id
        } else {
            let ordinal = self.snapshot_synthetic_next[ev.side as usize];
            self.snapshot_synthetic_next[ev.side as usize] = ordinal + 1;
            synthetic_order_id(ev.side, ordinal)
        };
        let ok = if self.index.contains_key(&oid) {
            self.counters.unknown_order_events += 1; // repeated id inside a burst
            false
        } else if self
            .level_total(ev.side, ev.price_ticks)
            .checked_add(ev.qty)
            .is_none()
        {
            self.counters.invalid_payload_dropped += 1;
            false
        } else {
            self.insert_order(ev.side, ev.price_ticks, oid, ev.qty);
            true
        };
        if ev.trade_id == 0 {
            // Last record of the burst; a broken burst never clears `stale`.
            self.snapshot_active = false;
            if !self.snapshot_broken {
                self.stale = false;
            }
            self.snapshot_broken = false;
        }
        ok
    }

    // -------------------------------------------------------- derived state

    /// All resting orders `(order_id, side, price_ticks, qty)` in
    /// deterministic global arrival (insertion) order — exactly the Python
    /// reference's `resting_orders()` iteration order.
    pub fn resting_orders(&self, side: Option<u8>) -> Vec<(u64, u8, i64, i64)> {
        let mut out = Vec::with_capacity(self.index.len());
        let mut oi = self.arrival_head;
        while oi != NIL {
            let o = &self.orders[oi as usize];
            let lvl = &self.levels[o.level as usize];
            if side.is_none() || side == Some(lvl.side) {
                out.push((o.id, lvl.side, lvl.price, o.qty));
            }
            oi = o.arr_next;
        }
        out
    }

    /// Total number of resting orders in the book.
    pub fn order_count_total(&self) -> usize {
        self.index.len()
    }

    /// Number of events currently held back in the reorder buffer.
    pub fn pending_count(&self) -> usize {
        self.pending.len()
    }

    /// `(price_ticks, total_size)` of the best bid, or `None`.
    pub fn best_bid(&self) -> Option<(i64, i64)> {
        self.best_slot(BID).map(|li| {
            let l = &self.levels[li as usize];
            (l.price, l.total_qty)
        })
    }

    /// `(price_ticks, total_size)` of the best ask, or `None`.
    pub fn best_ask(&self) -> Option<(i64, i64)> {
        self.best_slot(Side::Ask as u8).map(|li| {
            let l = &self.levels[li as usize];
            (l.price, l.total_qty)
        })
    }

    /// True when best bid > best ask (call phase / malformed feed).
    pub fn is_crossed(&self) -> bool {
        matches!((self.best_bid(), self.best_ask()), (Some((b, _)), Some((a, _))) if b > a)
    }

    /// True when best bid == best ask.
    pub fn is_locked(&self) -> bool {
        matches!((self.best_bid(), self.best_ask()), (Some((b, _)), Some((a, _))) if b == a)
    }

    /// Not stale and the last event was received within `max_age_ns` of `now_ns`.
    pub fn is_fresh(&self, now_ns: i64, max_age_ns: i64) -> bool {
        !self.stale && self.has_sequence && now_ns.saturating_sub(self.receive_ts) <= max_age_ns
    }

    /// Level slots of one side, best-first (bids: descending price; asks: ascending).
    fn sorted_slots(&self, side: u8) -> Vec<u32> {
        let map = &self.side_levels[side as usize];
        if side == BID {
            map.values().rev().copied().collect()
        } else {
            map.values().copied().collect()
        }
    }

    /// Top-N `(price_ticks, total_size)` best-first.
    pub fn depth(&self, side: u8, levels: usize) -> Vec<(i64, i64)> {
        self.sorted_slots(side)
            .into_iter()
            .take(levels)
            .map(|li| {
                let l = &self.levels[li as usize];
                (l.price, l.total_qty)
            })
            .collect()
    }

    /// Top-N `(price_ticks, order_count)` best-first.
    pub fn order_count(&self, side: u8, levels: usize) -> Vec<(i64, i64)> {
        self.sorted_slots(side)
            .into_iter()
            .take(levels)
            .map(|li| {
                let l = &self.levels[li as usize];
                (l.price, l.count as i64)
            })
            .collect()
    }

    /// Every level of one side, best-first: `(price, total_size, order_count)`.
    pub fn side_levels(&self, side: u8) -> Vec<(i64, i64, i64)> {
        self.sorted_slots(side)
            .into_iter()
            .map(|li| {
                let l = &self.levels[li as usize];
                (l.price, l.total_qty, l.count as i64)
            })
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
    /// order lists preserved, pending buffer in sequence order).
    pub fn checkpoint(&self) -> BookCheckpoint {
        let mut levels = Vec::with_capacity(self.side_levels[0].len() + self.side_levels[1].len());
        for side in 0..2u8 {
            for &li in self.side_levels[side as usize].values() {
                let l = &self.levels[li as usize];
                let mut orders = Vec::with_capacity(l.count as usize);
                let mut oi = l.head;
                while oi != NIL {
                    let o = &self.orders[oi as usize];
                    orders.push((o.id, o.qty));
                    oi = o.next;
                }
                levels.push(LevelCheckpoint {
                    side,
                    price_ticks: l.price,
                    orders,
                });
            }
        }
        BookCheckpoint {
            x_version: CHECKPOINT_VERSION,
            instrument_id: self.instrument_id,
            venue_id: self.venue_id,
            levels,
            arrival_order: self.resting_orders(None).into_iter().map(|o| o.0).collect(),
            last_sequence: self.last_sequence,
            has_sequence: self.has_sequence,
            sequence_epoch: self.sequence_epoch,
            exchange_ts: self.exchange_ts,
            receive_ts: self.receive_ts,
            trade_flow: self.trade_flow,
            status: self.status,
            stale: self.stale,
            snapshot_active: self.snapshot_active,
            snapshot_broken: self.snapshot_broken,
            snapshot_countdown: self.snapshot_countdown,
            snapshot_synthetic_next: self.snapshot_synthetic_next,
            reorder_window: self.reorder_window as u64,
            reorder_pending: self.pending.values().map(PendingRow::from).collect(),
            counters: self.counters,
        }
    }

    /// Rebuild an identical book from `checkpoint()` output.
    pub fn restore(cp: &BookCheckpoint) -> Result<OrderBook, IapError> {
        if cp.x_version != CHECKPOINT_VERSION {
            return Err(IapError::Checkpoint(format!(
                "unsupported book checkpoint x-version: {}",
                cp.x_version
            )));
        }
        if cp.reorder_window > MAX_REORDER_WINDOW as u64 {
            return Err(IapError::Checkpoint(
                "checkpoint reorder_window out of range".to_string(),
            ));
        }
        let mut book =
            OrderBook::with_reorder_window(cp.instrument_id, cp.venue_id, cp.reorder_window as usize)?;
        for lvl in &cp.levels {
            if lvl.side > 1 {
                return Err(IapError::Checkpoint(format!(
                    "invalid side {} in checkpoint level",
                    lvl.side
                )));
            }
            for &(oid, qty) in &lvl.orders {
                if book.index.contains_key(&oid) {
                    return Err(IapError::Checkpoint(format!(
                        "duplicate order_id {oid} in checkpoint"
                    )));
                }
                book.insert_order(lvl.side, lvl.price_ticks, oid, qty);
            }
        }
        // Rebuild the global arrival order (levels above fixed per-level FIFO
        // order; the arrival list must iterate in original insertion order).
        if cp.arrival_order.len() != book.index.len() {
            return Err(IapError::Checkpoint(
                "checkpoint arrival_order inconsistent with levels".to_string(),
            ));
        }
        book.arrival_head = NIL;
        book.arrival_tail = NIL;
        let mut seen = vec![false; book.orders.len()];
        for oid in &cp.arrival_order {
            let Some(&oi) = book.index.get(oid) else {
                return Err(IapError::Checkpoint(
                    "checkpoint arrival_order inconsistent with levels".to_string(),
                ));
            };
            if seen[oi as usize] {
                return Err(IapError::Checkpoint(
                    "checkpoint arrival_order inconsistent with levels".to_string(),
                ));
            }
            seen[oi as usize] = true;
            book.arrival_append(oi);
        }
        book.last_sequence = cp.last_sequence;
        book.has_sequence = cp.has_sequence;
        book.sequence_epoch = cp.sequence_epoch;
        book.exchange_ts = cp.exchange_ts;
        book.receive_ts = cp.receive_ts;
        book.trade_flow = cp.trade_flow;
        book.status = cp.status;
        book.stale = cp.stale;
        book.snapshot_active = cp.snapshot_active;
        book.snapshot_broken = cp.snapshot_broken;
        book.snapshot_countdown = cp.snapshot_countdown;
        book.snapshot_synthetic_next = cp.snapshot_synthetic_next;
        if cp.reorder_pending.len() > book.reorder_window {
            return Err(IapError::Checkpoint(
                "checkpoint reorder_pending exceeds reorder_window".to_string(),
            ));
        }
        for row in &cp.reorder_pending {
            let ev = MarketEvent::from(row);
            if book.pending.insert(ev.sequence, ev).is_some() {
                return Err(IapError::Checkpoint(format!(
                    "duplicate pending sequence {} in checkpoint",
                    ev.sequence
                )));
            }
        }
        book.counters = cp.counters;
        Ok(book)
    }
}

/// Checkpoint of a [`ConsolidatedBook`]: per-venue book checkpoints in sorted
/// venue order.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ConsolidatedCheckpoint {
    pub instrument_id: u32,
    pub reorder_window: u64,
    pub venues: BTreeMap<u16, BookCheckpoint>,
}

/// Consolidated view over per-venue books of one instrument.
///
/// Routes events to per-venue books by venue_id and merges derived state
/// over the NON-STALE venues only (pinned): same price across venues =>
/// sizes and order counts summed; best = best across venues. A venue whose
/// book is stale contributes nothing until a complete SNAPSHOT burst
/// recovers it. Sequence/staleness/status remain per venue.
#[derive(Debug, Clone)]
pub struct ConsolidatedBook {
    pub instrument_id: u32,
    reorder_window: usize,
    pub books: BTreeMap<u16, OrderBook>,
}

impl ConsolidatedBook {
    /// Create an empty consolidated book (no hold-back buffer).
    pub fn new(instrument_id: u32) -> ConsolidatedBook {
        ConsolidatedBook {
            instrument_id,
            reorder_window: 0,
            books: BTreeMap::new(),
        }
    }

    /// Create an empty consolidated book whose venue books use a hold-back
    /// buffer of `reorder_window` events.
    pub fn with_reorder_window(
        instrument_id: u32,
        reorder_window: usize,
    ) -> Result<ConsolidatedBook, IapError> {
        if reorder_window > MAX_REORDER_WINDOW {
            return Err(IapError::InvalidArgument(format!(
                "reorder_window must be <= {MAX_REORDER_WINDOW}: {reorder_window}"
            )));
        }
        Ok(ConsolidatedBook {
            instrument_id,
            reorder_window,
            books: BTreeMap::new(),
        })
    }

    /// Hold-back buffer size used for venue books.
    pub fn reorder_window(&self) -> usize {
        self.reorder_window
    }

    /// Get (or lazily create) the per-venue book.
    pub fn venue_book(&mut self, venue_id: u16) -> &mut OrderBook {
        let (instrument_id, window) = (self.instrument_id, self.reorder_window);
        self.books.entry(venue_id).or_insert_with(|| {
            OrderBook::with_reorder_window(instrument_id, venue_id, window)
                .expect("validated at construction")
        })
    }

    /// Route one event to its venue book; returns the pinned verdict.
    pub fn apply(&mut self, ev: &MarketEvent) -> Result<ApplyStatus, IapError> {
        self.venue_book(ev.venue_id).apply(ev)
    }

    /// Explicit sequence reset on every venue book (session roll).
    pub fn reset_sequences(&mut self) {
        for book in self.books.values_mut() {
            book.reset_sequence();
        }
    }

    /// Sorted venue ids whose books are not stale (merged into the view).
    pub fn active_venues(&self) -> Vec<u16> {
        self.books
            .iter()
            .filter(|(_, b)| !b.stale)
            .map(|(&v, _)| v)
            .collect()
    }

    /// Sorted venue ids whose books are stale (excluded from the view).
    pub fn stale_venues(&self) -> Vec<u16> {
        self.books
            .iter()
            .filter(|(_, b)| b.stale)
            .map(|(&v, _)| v)
            .collect()
    }

    /// Session status of one venue's book (`None` when the venue is unknown).
    pub fn venue_status(&self, venue_id: u16) -> Option<i64> {
        self.books.get(&venue_id).map(|b| b.status)
    }

    /// Merged `(price, total_size, order_count)` best-first for one side.
    fn merged(&self, side: u8) -> Vec<(i64, i64, i64)> {
        let mut agg: BTreeMap<i64, (i64, i64)> = BTreeMap::new();
        for book in self.books.values() {
            if book.stale {
                continue; // non-stale venues only (pinned)
            }
            for (price, size, count) in book.side_levels(side) {
                let slot = agg.entry(price).or_insert((0, 0));
                slot.0 += size;
                slot.1 += count;
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

    /// True when the merged best bid > merged best ask.
    pub fn is_crossed(&self) -> bool {
        matches!((self.best_bid(), self.best_ask()), (Some((b, _)), Some((a, _))) if b > a)
    }

    /// True when the merged best bid == merged best ask.
    pub fn is_locked(&self) -> bool {
        matches!((self.best_bid(), self.best_ask()), (Some((b, _)), Some((a, _))) if b == a)
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

    /// Sum of per-venue cumulative signed trade flow (all venues), saturated
    /// to i64 (pinned).
    pub fn trade_flow(&self) -> i64 {
        let total: i128 = self.books.values().map(|b| b.trade_flow as i128).sum();
        total.clamp(i64::MIN as i128, i64::MAX as i128) as i64
    }

    /// Golden-comparable merged view (non-stale venues only) — the shape of
    /// `expected_anomaly_states.json` "consolidated" entries.
    pub fn consolidated_summary(&self) -> Value {
        let pairs = |v: Vec<(i64, i64)>| -> Vec<Value> {
            v.into_iter().map(|(a, b)| json!([a, b])).collect()
        };
        let opt = |v: Option<(i64, i64)>| -> Value {
            match v {
                Some((a, b)) => json!([a, b]),
                None => Value::Null,
            }
        };
        json!({
            "best_bid": opt(self.best_bid()),
            "best_ask": opt(self.best_ask()),
            "depth_bid_top5": pairs(self.depth(BID, 5)),
            "depth_ask_top5": pairs(self.depth(Side::Ask as u8, 5)),
            "is_crossed": self.is_crossed(),
            "is_locked": self.is_locked(),
            "active_venues": self.active_venues(),
            "trade_flow": self.trade_flow(),
        })
    }

    /// Serialize every venue book (sorted venue order).
    pub fn checkpoint(&self) -> ConsolidatedCheckpoint {
        ConsolidatedCheckpoint {
            instrument_id: self.instrument_id,
            reorder_window: self.reorder_window as u64,
            venues: self
                .books
                .iter()
                .map(|(&vid, book)| (vid, book.checkpoint()))
                .collect(),
        }
    }

    /// Rebuild from `checkpoint()` output.
    pub fn restore(cp: &ConsolidatedCheckpoint) -> Result<ConsolidatedBook, IapError> {
        if cp.reorder_window > MAX_REORDER_WINDOW as u64 {
            return Err(IapError::Checkpoint(
                "checkpoint reorder_window out of range".to_string(),
            ));
        }
        let mut cons = ConsolidatedBook::with_reorder_window(cp.instrument_id, cp.reorder_window as usize)?;
        for (&vid, bcp) in &cp.venues {
            cons.books.insert(vid, OrderBook::restore(bcp)?);
        }
        Ok(cons)
    }
}
