//! Simulated venue endpoint (accept / ack / fill against the order book).
//!
//! The venue owns one [`OrderBook`] per instrument, built from the market
//! data stream it is fed. Pinned simulation semantics (documented, venue
//! crate normative):
//!
//! - Market-data gating (spec §16 "sequence-gap and stale-market-data
//!   protection", conventions §4): the venue keys its books by
//!   (instrument, `cfg.venue_id`) and IGNORES events of other venues
//!   (`venue_market_events_ignored_total`) so a multi-venue feed never
//!   corrupts its sequence counters. `submit()` REJECTS (reason in
//!   [`RejectReason`], counted per reason) when the instrument has no book,
//!   the book is `stale` (sequence gap not yet recovered), the session
//!   status is not TRADING (HALT / AUCTION / CLOSE), or the book is empty
//!   on both sides. Resting orders never fill while the book is stale or
//!   the status is not TRADING (they are held; fills resume after a
//!   complete SNAPSHOT burst / STATUS TRADING).
//! - Marketable fills are computed against the current merged depth
//!   *without consuming it* (impact-free simulation, like the research
//!   backtester): the market-data stream remains the sole owner of book
//!   state. v1.17 (B9): the displayed size a walk or a crossing fill
//!   already used is remembered in a per-(instrument, side, price) overlay
//!   (execution simulator rule 3b) and is not available again until the
//!   display changes (the overlay entry is capped at the new displayed
//!   size); walks see the top [`DEPTH_LEVELS`] levels (rule 3).
//! - MARKET: fills walk the opposite depth best-first; any unfilled
//!   remainder is CANCELED.
//! - LIMIT: the marketable part fills the same way; the remainder rests
//!   with a deterministic queue position (rule 4 below).
//! - IOC: marketable part fills, remainder CANCELED. Unpriced IOC behaves
//!   like MARKET.
//! - FOK: fills completely (within the limit price when priced) or is
//!   CANCELED with no fills.
//! - PEG: rests at the current same-side touch price (REJECTED when that
//!   side is empty), queued like a LIMIT at that price.
//! - MID: fills at `floor((best_bid + best_ask) / 2)` ticks up to the
//!   opposite L1 size; the remainder is CANCELED (REJECTED when the book
//!   is one-sided). Rust-only order type: no overlay, no queue.
//! - **Passive queue position (v1.17 B9; execution simulator rule 4 of
//!   `API_TRADING.md` §2 / PLATFORM_CONVENTIONS.md §11.2, ported from the
//!   Python reference `iap.execution.simulator` and pinned by
//!   `tests/golden/expected_venue_queue.json`).** A resting order at `P`
//!   starts with `ahead_qty` = displayed qty at `(side, P)` plus the
//!   `remaining` of our own orders already resting at that exact
//!   `(side, P)`. Only events the book reports APPLIED are tracked. One
//!   applied EXECUTE trades `min(event qty, the book order's remaining)` at
//!   the BOOK order's side and price: that one pool is budgeted once over
//!   our resting orders in queue (submission) order — a trade AT our limit
//!   or STRICTLY WORSE (through us) pays down `ahead_qty` first, then fills
//!   `min(budget, remaining)` at OUR limit. A marketable ADD is expanded
//!   into the per-level volumes it consumes over the pre-event displayed
//!   opposite depth. An applied CANCEL depletes `ahead_qty` by the displayed
//!   size it removed from our level, only when the cancelled order is KNOWN
//!   to be ahead of us (a real, non-synthetic id that did not join the level
//!   after we rested: an ADD at our level, or a MODIFY that grew an order
//!   and so moved it to the tail, after our rest is remembered as behind
//!   us); a MODIFY that shrinks an order known to be ahead credits us the
//!   same way; synthetic QUOTE / SNAPSHOT ids never deplete it. After the
//!   event is applied, an opposite best crossing `P` fills us at `P`,
//!   bounded by the displayed size of that crossing level net of the
//!   overlay (one pool per side, `ahead_qty` consumed first, the overlay
//!   debited) — EXEMPT while the display still shows the liquidity our own
//!   aggressive leg consumed, until the display first shows an uncrossed
//!   opposite best. On the first event after which the venue is open again
//!   (rule 8) crossed resting orders fill at the TOUCH price instead.
//! - Submission and cancel are immediate (the Rust venue has no in-flight
//!   leg): an order submitted after event `k` is in the state the Python
//!   simulator reaches by activating it with zero latency before event
//!   `k + 1`, which is how the golden vectors are built.
//! - Every report stamps `exchange_ts = event time + latency_ns` and
//!   `receive_ts = exchange_ts + latency_ns`; execution ids are monotone
//!   per venue. Taker fills pay `taker_fee_per_share * qty`; maker
//!   (resting) fills earn `-maker_rebate_per_share * qty`.
//! - v1.15 opt-in (X-P1-1, [`SimulatedVenue::with_regulatory_fees`], off by
//!   default): SELL fills also pay the DATED SEC Section 31 + FINRA TAF fee
//!   ([`crate::regfees`]) of the report's `exchange_ts`, on notional
//!   `qty * price_ticks * tick_size` (same rule as the Python and C++
//!   execution simulators).

use std::collections::{BTreeMap, BTreeSet};

use marketdata::{EventType, IapError, MarketEvent, SessionStatus};
use orderbook::{ApplyStatus, OrderBook, DEPTH_LEVELS, SYNTHETIC_ID_BASE};
use telemetry::Registry;

use crate::messages::{validate_order, ExecStatus, ExecutionReport, OrderRequest, OrderType};
use crate::regfees::RegulatoryFeeSchedule;

/// Why the simulated venue rejected the last order (pinned gating rules).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RejectReason {
    /// Order routed to another venue id.
    WrongVenue,
    /// Contract-level validation failed or the order id is already resting.
    InvalidOrder,
    /// No market data has been seen for the instrument.
    UnknownInstrument,
    /// The book is stale (sequence gap not recovered by a SNAPSHOT burst).
    StaleBook,
    /// Session status is HALT / AUCTION / CLOSE.
    NotTrading,
    /// The book is empty on both sides.
    EmptyBook,
    /// PEG/MID reference price missing (one-sided book).
    NoReferencePrice,
}

impl RejectReason {
    /// Metric counter name for this reason.
    pub const fn counter(self) -> &'static str {
        match self {
            RejectReason::WrongVenue => "venue_orders_rejected_wrong_venue_total",
            RejectReason::InvalidOrder => "venue_orders_rejected_invalid_total",
            RejectReason::UnknownInstrument => "venue_orders_rejected_unknown_instrument_total",
            RejectReason::StaleBook => "venue_orders_rejected_stale_total",
            RejectReason::NotTrading => "venue_orders_rejected_not_trading_total",
            RejectReason::EmptyBook => "venue_orders_rejected_empty_book_total",
            RejectReason::NoReferencePrice => "venue_orders_rejected_no_reference_total",
        }
    }
}

/// Simulated venue configuration.
#[derive(Debug, Clone, Copy)]
pub struct SimVenueConfig {
    /// This venue's id (order routing must match; 0 accepts any).
    pub venue_id: u16,
    /// One-way latency in ns applied to acks/fills.
    pub latency_ns: i64,
    /// Taker fee per share (currency).
    pub taker_fee_per_share: f64,
    /// Maker rebate per share (currency, charged as negative fees).
    pub maker_rebate_per_share: f64,
}

/// The queue state of one resting order (rule 4), read back through
/// [`SimulatedVenue::queue_position`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct QueuePosition {
    /// Open quantity still resting.
    pub remaining: i64,
    /// Displayed quantity believed ahead of the order at its level.
    pub ahead_qty: i64,
}

#[derive(Debug, Clone)]
struct RestingOrder {
    instrument_id: u32,
    side: u8,
    price_ticks: i64,
    remaining: i64,
    /// Rule 4 queue position.
    ahead_qty: i64,
    /// Rule 4 crossing exemption: the display still shows the liquidity our
    /// own aggressive leg consumed.
    cross_exempt: bool,
    /// Market order ids known to have joined our level after we did.
    behind: BTreeSet<u64>,
}

/// One fill produced by the passive tracking of an event, before it is
/// turned into a report.
struct PassiveFill {
    order_id: u64,
    side: u8,
    qty: i64,
    price_ticks: i64,
    done: bool,
}

/// Overlay key (rule 3b): `(instrument_id, side, price_ticks)` of a level
/// whose displayed size was partly consumed by our own fills.
type OverlayKey = (u32, u8, i64);

/// Simulated venue: accept/ack/fill via the order book.
pub struct SimulatedVenue {
    cfg: SimVenueConfig,
    books: BTreeMap<u32, OrderBook>,
    resting: BTreeMap<u64, RestingOrder>,
    /// Resting order ids in submission order: the rule-4 queue order.
    queue: Vec<u64>,
    /// Rule 3b overlay: displayed size already consumed per level.
    consumed: BTreeMap<OverlayKey, i64>,
    next_exec_id: u64,
    last_reject: Option<RejectReason>,
    /// v1.15 X-P1-1: dated regulatory fees and the tick size pricing them.
    regfees: Option<(RegulatoryFeeSchedule, f64)>,
    /// Venue metrics (orders/fills/latency histogram).
    pub metrics: Registry,
}

/// Displayed total quantity at `(side, price)`; 0 when no level rests there.
fn level_qty(book: &OrderBook, side: u8, price: i64) -> i64 {
    book.side_levels(side)
        .into_iter()
        .find(|(p, _, _)| *p == price)
        .map_or(0, |(_, q, _)| q)
}

/// The book's own record `(side, price_ticks, qty)` of a resting market
/// order, or `None`.
fn book_order(book: &OrderBook, order_id: u64) -> Option<(u8, i64, i64)> {
    book.resting_orders(None)
        .into_iter()
        .find(|(id, _, _, _)| *id == order_id)
        .map(|(_, side, price, qty)| (side, price, qty))
}

impl SimulatedVenue {
    /// New simulated venue.
    pub fn new(cfg: SimVenueConfig) -> Result<SimulatedVenue, IapError> {
        if cfg.latency_ns < 0 {
            return Err(IapError::InvalidArgument(
                "latency_ns must be >= 0".to_string(),
            ));
        }
        Ok(SimulatedVenue {
            cfg,
            books: BTreeMap::new(),
            resting: BTreeMap::new(),
            queue: Vec::new(),
            consumed: BTreeMap::new(),
            next_exec_id: 0,
            last_reject: None,
            regfees: None,
            metrics: Registry::new(),
        })
    }

    /// v1.15 X-P1-1: charge the dated SEC Section 31 + FINRA TAF fee on
    /// SELL fills, pricing notional at `tick_size` per tick.
    pub fn with_regulatory_fees(
        mut self,
        schedule: RegulatoryFeeSchedule,
        tick_size: f64,
    ) -> Result<SimulatedVenue, IapError> {
        if tick_size.is_nan() || tick_size <= 0.0 {
            return Err(IapError::InvalidArgument("tick_size must be > 0".to_string()));
        }
        self.regfees = Some((schedule, tick_size));
        Ok(self)
    }

    /// Regulatory fee of one fill reported at `ts` (0.0 when off or a buy).
    fn regulatory_fee(
        &self,
        side: u8,
        qty: i64,
        price_ticks: i64,
        ts: i64,
    ) -> Result<f64, IapError> {
        match &self.regfees {
            Some((sched, tick)) if side == 1 => {
                let notional = qty as f64 * price_ticks as f64 * tick;
                sched.fee(side, qty as f64, notional, ts + self.cfg.latency_ns)
            }
            _ => Ok(0.0),
        }
    }

    /// Reason of the most recent rejection (`None` after an accepted order).
    pub fn last_reject_reason(&self) -> Option<RejectReason> {
        self.last_reject
    }

    /// The venue's book for an instrument (read-only), if any.
    pub fn book(&self, instrument_id: u32) -> Option<&OrderBook> {
        self.books.get(&instrument_id)
    }

    /// Rule-4 queue state of a resting order (`None` once it is no longer
    /// resting: filled, cancelled or never accepted).
    pub fn queue_position(&self, order_id: u64) -> Option<QueuePosition> {
        self.resting.get(&order_id).map(|r| QueuePosition {
            remaining: r.remaining,
            ahead_qty: r.ahead_qty,
        })
    }

    fn reject(&mut self, order: &OrderRequest, reason: RejectReason) -> Vec<ExecutionReport> {
        self.last_reject = Some(reason);
        self.metrics.counter("venue_orders_rejected_total").inc();
        self.metrics.counter(reason.counter()).inc();
        vec![self.report(order.order_id, ExecStatus::Rejected, 0, 0, order.timestamp, 0.0)]
    }

    /// True iff the instrument's book may be traded against: not stale and
    /// in continuous trading.
    fn tradable(book: &OrderBook) -> bool {
        !book.stale && book.status == SessionStatus::Trading as i64
    }

    fn report(
        &mut self,
        order_id: u64,
        status: ExecStatus,
        filled_qty: i64,
        fill_price_ticks: i64,
        ts: i64,
        fees: f64,
    ) -> ExecutionReport {
        self.next_exec_id += 1;
        let exchange_ts = ts + self.cfg.latency_ns;
        ExecutionReport {
            order_id,
            execution_id: self.next_exec_id,
            status: status.as_u8(),
            filled_qty,
            fill_price_ticks,
            venue_id: self.cfg.venue_id,
            exchange_ts,
            receive_ts: exchange_ts + self.cfg.latency_ns,
            fees,
        }
    }

    fn consumed_at(&self, instrument_id: u32, side: u8, price: i64) -> i64 {
        self.consumed
            .get(&(instrument_id, side, price))
            .copied()
            .unwrap_or(0)
    }

    /// Remove a fully filled order from the queue and the resting map.
    fn retire(&mut self, order_id: u64) {
        self.resting.remove(&order_id);
        self.queue.retain(|id| *id != order_id);
    }

    /// Rule 4: one observed trade of `qty` at `(side, price_ticks)` of
    /// `instrument_id`, budgeted once over our resting orders in queue
    /// order. A trade reaches an order when it prints AT its limit or
    /// STRICTLY WORSE; both fill at OUR limit, capped by the observed volume.
    fn track_consumption(
        &mut self,
        instrument_id: u32,
        side: u8,
        price_ticks: i64,
        qty: i64,
        fills: &mut Vec<PassiveFill>,
    ) {
        let mut budget = qty;
        for id in self.queue.clone() {
            if budget <= 0 {
                break;
            }
            let Some(r) = self.resting.get_mut(&id) else { continue };
            if r.instrument_id != instrument_id || r.side != side {
                continue;
            }
            let through = if r.side == 0 {
                price_ticks < r.price_ticks
            } else {
                price_ticks > r.price_ticks
            };
            if price_ticks != r.price_ticks && !through {
                continue;
            }
            let dec = r.ahead_qty.min(budget);
            r.ahead_qty -= dec;
            budget -= dec;
            if budget > 0 {
                let fill = budget.min(r.remaining);
                r.remaining -= fill;
                budget -= fill;
                let done = r.remaining == 0;
                fills.push(PassiveFill {
                    order_id: id,
                    side: r.side,
                    qty: fill,
                    price_ticks: r.price_ticks,
                    done,
                });
                if done {
                    self.retire(id);
                }
            }
        }
    }

    /// Rule 4 last bullet / rule 8 reopen, against the post-event book: the
    /// pool is the displayed size of the crossing opposite best net of the
    /// overlay, one pool per side, shared in queue order.
    fn crossing_check(
        &mut self,
        instrument_id: u32,
        reopened: bool,
        fills: &mut Vec<PassiveFill>,
    ) {
        let book = &self.books[&instrument_id];
        let best_ask = book.best_ask();
        let best_bid = book.best_bid();
        // indexed by OUR side: a buy crosses against the ask, a sell the bid
        let pool_start = [
            best_ask.map_or(0, |(p, q)| (q - self.consumed_at(instrument_id, 1, p)).max(0)),
            best_bid.map_or(0, |(p, q)| (q - self.consumed_at(instrument_id, 0, p)).max(0)),
        ];
        let mut budget = pool_start;
        for id in self.queue.clone() {
            let Some(r) = self.resting.get_mut(&id) else { continue };
            if r.instrument_id != instrument_id {
                continue;
            }
            let opp = if r.side == 0 { best_ask } else { best_bid };
            let crossed = opp.is_some_and(|(p, _)| {
                if r.side == 0 {
                    p <= r.price_ticks
                } else {
                    p >= r.price_ticks
                }
            });
            if !crossed {
                r.cross_exempt = false; // display uncrossed: exemption ends
                continue;
            }
            if !reopened && r.cross_exempt {
                continue;
            }
            let s = r.side as usize;
            let dec = r.ahead_qty.min(budget[s]);
            r.ahead_qty -= dec;
            budget[s] -= dec;
            if budget[s] > 0 {
                let fill = budget[s].min(r.remaining);
                // rule 8 uncrosses AT THE TOUCH; the rule-4 crossing fills at
                // our own limit (no price improvement)
                let price = if reopened {
                    opp.expect("crossed").0
                } else {
                    r.price_ticks
                };
                r.remaining -= fill;
                budget[s] -= fill;
                let done = r.remaining == 0;
                fills.push(PassiveFill {
                    order_id: id,
                    side: r.side,
                    qty: fill,
                    price_ticks: price,
                    done,
                });
                if done {
                    self.retire(id);
                }
            }
        }
        // debit the overlay so the same display is not consumed again
        for (our_side, opp) in [(0u8, best_ask), (1u8, best_bid)] {
            let used = pool_start[our_side as usize] - budget[our_side as usize];
            if let (true, Some((p, _))) = (used > 0, opp) {
                *self.consumed.entry((instrument_id, 1 - our_side, p)).or_insert(0) += used;
            }
        }
    }

    /// Feed one market event; returns fills of resting orders (rule 4
    /// tracking of an APPLIED event, then the post-apply crossing check).
    /// Events of other venues are ignored (counted); no resting order fills
    /// while the book is stale or the status is not TRADING.
    pub fn on_market_event(&mut self, ev: &MarketEvent) -> Result<Vec<ExecutionReport>, IapError> {
        if self.cfg.venue_id != 0 && ev.venue_id != self.cfg.venue_id {
            self.metrics.counter("venue_market_events_ignored_total").inc();
            return Ok(Vec::new());
        }
        let iid = ev.instrument_id;
        let venue_id = self.cfg.venue_id;
        let et = EventType::from_u8(ev.event_type);
        let tracking = self.queue.iter().any(|id| self.resting[id].instrument_id == iid);
        // 1. capture the pre-event state queue tracking needs
        let pre = self.books.get(&iid);
        let pre_open = pre.is_some_and(Self::tradable);
        let mut add_depth: Vec<(i64, i64)> = Vec::new();
        let mut exec_order: Option<(u8, i64, i64)> = None;
        let mut level_before: Vec<(u64, i64)> = Vec::new();
        if let Some(pre) = pre {
            if tracking && pre_open && et == Some(EventType::Add) {
                add_depth = pre.depth(1 - ev.side.min(1), DEPTH_LEVELS);
            }
            if tracking && pre_open && et == Some(EventType::Execute) {
                exec_order = book_order(pre, ev.order_id);
            }
        }
        if tracking && matches!(et, Some(EventType::Cancel) | Some(EventType::Modify)) {
            for id in &self.queue {
                let r = &self.resting[id];
                if r.instrument_id == iid {
                    let before = pre.map_or(0, |b| level_qty(b, r.side, r.price_ticks));
                    level_before.push((*id, before));
                }
            }
        }
        let watched: Vec<(OverlayKey, i64)> = self
            .consumed
            .keys()
            .filter(|k| k.0 == iid)
            .map(|k| (*k, pre.map_or(0, |b| level_qty(b, k.1, k.2))))
            .collect();
        // 2. apply the event
        let book = self
            .books
            .entry(iid)
            .or_insert_with(|| OrderBook::new(iid, venue_id));
        let applied = book.apply(ev)? == ApplyStatus::Applied;
        self.metrics.counter("venue_market_events_total").inc();
        let open_now = Self::tradable(&self.books[&iid]);
        if !open_now {
            self.metrics.counter("venue_market_events_untradable_total").inc();
        }
        // 3. passive queue tracking (rule 4) of an APPLIED event; fills only
        //    from a book that was open before the event (rule 8)
        let mut fills: Vec<PassiveFill> = Vec::new();
        if applied && tracking {
            let real_id = ev.order_id < SYNTHETIC_ID_BASE;
            match et {
                Some(EventType::Execute) => {
                    if let Some((side, price, qty)) = exec_order {
                        self.track_consumption(iid, side, price, ev.qty.min(qty), &mut fills);
                    }
                }
                Some(EventType::Cancel) => {
                    for (id, before) in &level_before {
                        let after = {
                            let r = &self.resting[id];
                            level_qty(&self.books[&iid], r.side, r.price_ticks)
                        };
                        let removed = before - after;
                        let r = self.resting.get_mut(id).expect("resting");
                        if r.behind.contains(&ev.order_id) {
                            r.behind.remove(&ev.order_id);
                        } else if removed > 0 && real_id && pre_open {
                            // synthetic (non-MBO) ids are assumed behind us
                            r.ahead_qty -= r.ahead_qty.min(removed);
                        }
                    }
                }
                Some(EventType::Modify) => {
                    for (id, before) in &level_before {
                        let after = {
                            let r = &self.resting[id];
                            level_qty(&self.books[&iid], r.side, r.price_ticks)
                        };
                        let r = self.resting.get_mut(id).expect("resting");
                        if real_id && after > *before {
                            // a size increase moves the order to the level's tail
                            r.behind.insert(ev.order_id);
                        } else if after < *before
                            && real_id
                            && pre_open
                            && !r.behind.contains(&ev.order_id)
                        {
                            // X-P1-1: a size decrease keeps priority; an order
                            // ahead of us credits our queue like a CANCEL
                            r.ahead_qty -= r.ahead_qty.min(before - after);
                        }
                    }
                }
                Some(EventType::Add) => {
                    if real_id {
                        for id in self.queue.clone() {
                            let r = self.resting.get_mut(&id).expect("resting");
                            if r.instrument_id == iid
                                && r.side == ev.side
                                && r.price_ticks == ev.price_ticks
                            {
                                r.behind.insert(ev.order_id);
                            }
                        }
                    }
                    // marketable-ADD expansion over the pre-event displayed
                    // opposite depth, each level's volume its own pool
                    let consumed_side = 1 - ev.side.min(1);
                    let mut incoming = ev.qty;
                    for (p, q) in add_depth {
                        if incoming <= 0 {
                            break;
                        }
                        let crosses = if ev.side == 0 {
                            p <= ev.price_ticks
                        } else {
                            p >= ev.price_ticks
                        };
                        if !crosses {
                            break;
                        }
                        let consumed = incoming.min(q);
                        self.track_consumption(iid, consumed_side, p, consumed, &mut fills);
                        incoming -= consumed;
                    }
                }
                _ => {}
            }
        }
        // cap the overlay entries whose display changed (rule 3b)
        for (key, before) in watched {
            let after = level_qty(&self.books[&iid], key.1, key.2);
            if after != before {
                if let Some(prior) = self.consumed.get(&key).copied() {
                    let capped = prior.min(after);
                    if capped <= 0 {
                        self.consumed.remove(&key);
                    } else {
                        self.consumed.insert(key, capped);
                    }
                }
            }
        }
        // 4. post-apply crossing check (rule 4 last bullet / rule 8 reopen)
        if open_now {
            self.crossing_check(iid, !pre_open, &mut fills);
        }
        let mut out = Vec::with_capacity(fills.len());
        for f in fills {
            let fees = -self.cfg.maker_rebate_per_share * f.qty as f64
                + self.regulatory_fee(f.side, f.qty, f.price_ticks, ev.exchange_ts)?;
            let status = if f.done {
                ExecStatus::Filled
            } else {
                ExecStatus::Partial
            };
            out.push(self.report(f.order_id, status, f.qty, f.price_ticks, ev.exchange_ts, fees));
            self.metrics.counter("venue_fills_total").inc();
        }
        Ok(out)
    }

    /// Marketable fills against the current opposite depth net of the
    /// overlay (rules 3 / 3b, non-mutating): `(fills, remaining)`;
    /// `limit = None` walks the whole displayed top-N book.
    fn walk_depth(
        &self,
        instrument_id: u32,
        side: u8,
        qty: i64,
        limit: Option<i64>,
    ) -> (Vec<(i64, i64)>, i64) {
        let book = &self.books[&instrument_id];
        let opposite = 1 - side;
        let mut remaining = qty;
        let mut fills = Vec::new();
        for (price, size) in book.depth(opposite, DEPTH_LEVELS) {
            if remaining <= 0 {
                break;
            }
            let crosses = match limit {
                None => true,
                Some(lim) => {
                    if side == 0 {
                        price <= lim
                    } else {
                        price >= lim
                    }
                }
            };
            if !crosses {
                break;
            }
            let avail = (size - self.consumed_at(instrument_id, opposite, price)).max(0);
            if avail <= 0 {
                continue;
            }
            let fill = remaining.min(avail);
            fills.push((price, fill));
            remaining -= fill;
        }
        (fills, remaining)
    }

    /// Submit one order; returns the acknowledgement and any immediate
    /// fills / cancels (rejects come back as a single REJECTED report).
    pub fn submit(&mut self, order: &OrderRequest) -> Result<Vec<ExecutionReport>, IapError> {
        self.metrics.counter("venue_orders_total").inc();
        self.last_reject = None;
        let ts = order.timestamp;
        if self.cfg.venue_id != 0 && order.venue_id != self.cfg.venue_id {
            return Ok(self.reject(order, RejectReason::WrongVenue));
        }
        if validate_order(order).is_err() || self.resting.contains_key(&order.order_id) {
            return Ok(self.reject(order, RejectReason::InvalidOrder));
        }
        let Some(book) = self.books.get(&order.instrument_id) else {
            return Ok(self.reject(order, RejectReason::UnknownInstrument));
        };
        if book.stale {
            return Ok(self.reject(order, RejectReason::StaleBook));
        }
        if book.status != SessionStatus::Trading as i64 {
            return Ok(self.reject(order, RejectReason::NotTrading));
        }
        if book.best_bid().is_none() && book.best_ask().is_none() {
            return Ok(self.reject(order, RejectReason::EmptyBook));
        }
        let ot = OrderType::from_u8(order.order_type).expect("validated");
        let (same_touch, opp_touch) = if order.side == 0 {
            (book.best_bid(), book.best_ask())
        } else {
            (book.best_ask(), book.best_bid())
        };
        // PEG/MID need a reference price to exist
        if (ot == OrderType::Peg && same_touch.is_none())
            || (ot == OrderType::Mid && (book.best_bid().is_none() || book.best_ask().is_none()))
        {
            return Ok(self.reject(order, RejectReason::NoReferencePrice));
        }
        let mut out = vec![self.report(order.order_id, ExecStatus::New, 0, 0, ts, 0.0)];
        self.metrics.counter("venue_orders_accepted_total").inc();
        self.metrics
            .histogram("venue_ack_latency_ns")
            .record(self.cfg.latency_ns as u64);

        let iid = order.instrument_id;
        let (fills, remaining, rest_price) = match ot {
            OrderType::Market => {
                let (fills, remaining) = self.walk_depth(iid, order.side, order.qty, None);
                (fills, remaining, None)
            }
            OrderType::Limit => {
                let (fills, remaining) =
                    self.walk_depth(iid, order.side, order.qty, Some(order.price_ticks));
                (fills, remaining, Some(order.price_ticks))
            }
            OrderType::Ioc => {
                let limit = (order.price_ticks > 0).then_some(order.price_ticks);
                let (fills, remaining) = self.walk_depth(iid, order.side, order.qty, limit);
                (fills, remaining, None)
            }
            OrderType::Fok => {
                let limit = (order.price_ticks > 0).then_some(order.price_ticks);
                let (fills, remaining) = self.walk_depth(iid, order.side, order.qty, limit);
                if remaining > 0 {
                    (Vec::new(), order.qty, None) // all-or-nothing miss
                } else {
                    (fills, 0, None)
                }
            }
            OrderType::Peg => {
                let (price, _) = same_touch.expect("checked above");
                (Vec::new(), order.qty, Some(price))
            }
            OrderType::Mid => {
                let (bid, _) = book.best_bid().expect("checked");
                let (ask, _) = book.best_ask().expect("checked");
                let mid = (bid + ask).div_euclid(2);
                let avail = opp_touch.map_or(0, |(_, size)| size);
                let fill = order.qty.min(avail);
                if fill > 0 {
                    (vec![(mid, fill)], order.qty - fill, None)
                } else {
                    (Vec::new(), order.qty, None)
                }
            }
        };

        let mut done = 0;
        let opposite = 1 - order.side;
        for (price, qty) in fills {
            done += qty;
            let status = if done == order.qty {
                ExecStatus::Filled
            } else {
                ExecStatus::Partial
            };
            let fees = self.cfg.taker_fee_per_share * qty as f64
                + self.regulatory_fee(order.side, qty, price, ts)?;
            out.push(self.report(order.order_id, status, qty, price, ts, fees));
            self.metrics.counter("venue_fills_total").inc();
            if ot != OrderType::Mid {
                // rule 3b: the walk consumed this displayed size
                *self.consumed.entry((iid, opposite, price)).or_insert(0) += qty;
            }
        }
        if remaining > 0 {
            match rest_price {
                Some(price) => {
                    // rule 4: queue position = displayed qty at our level plus
                    // our own orders already resting there (FIFO)
                    let book = &self.books[&iid];
                    let mut ahead = level_qty(book, order.side, price);
                    for id in &self.queue {
                        let r = &self.resting[id];
                        if r.instrument_id == iid && r.side == order.side && r.price_ticks == price
                        {
                            ahead += r.remaining;
                        }
                    }
                    // crossing exemption: the display may still show the
                    // liquidity our aggressive leg just consumed
                    let cross_exempt = opp_touch.is_some_and(|(p, _)| {
                        if order.side == 0 {
                            p <= price
                        } else {
                            p >= price
                        }
                    });
                    self.resting.insert(
                        order.order_id,
                        RestingOrder {
                            instrument_id: iid,
                            side: order.side,
                            price_ticks: price,
                            remaining,
                            ahead_qty: ahead,
                            cross_exempt,
                            behind: BTreeSet::new(),
                        },
                    );
                    self.queue.push(order.order_id);
                }
                None => {
                    // MARKET / IOC / FOK / MID remainder cancels
                    out.push(self.report(order.order_id, ExecStatus::Canceled, 0, 0, ts, 0.0));
                }
            }
        }
        Ok(out)
    }

    /// Cancel a resting order (CANCELED, or REJECTED when unknown).
    pub fn cancel(&mut self, order_id: u64, ts: i64) -> ExecutionReport {
        if self.resting.contains_key(&order_id) {
            self.retire(order_id);
            self.report(order_id, ExecStatus::Canceled, 0, 0, ts, 0.0)
        } else {
            self.report(order_id, ExecStatus::Rejected, 0, 0, ts, 0.0)
        }
    }

    /// Number of orders currently resting on the venue.
    pub fn resting_count(&self) -> usize {
        self.resting.len()
    }
}
