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
//!   state.
//! - MARKET: fills walk the opposite depth best-first; any unfilled
//!   remainder is CANCELED.
//! - LIMIT: the marketable part fills the same way; the remainder rests
//!   and fills when a later market update crosses its price (at the limit
//!   price, up to the crossing level's size).
//! - IOC: marketable part fills, remainder CANCELED. Unpriced IOC behaves
//!   like MARKET.
//! - FOK: fills completely (within the limit price when priced) or is
//!   CANCELED with no fills.
//! - PEG: rests at the current same-side touch price (REJECTED when that
//!   side is empty).
//! - MID: fills at `floor((best_bid + best_ask) / 2)` ticks up to the
//!   opposite L1 size; the remainder is CANCELED (REJECTED when the book
//!   is one-sided).
//! - Every report stamps `exchange_ts = event time + latency_ns` and
//!   `receive_ts = exchange_ts + latency_ns`; execution ids are monotone
//!   per venue. Taker fills pay `taker_fee_per_share * qty`; maker
//!   (resting) fills earn `-maker_rebate_per_share * qty`.

use std::collections::BTreeMap;

use marketdata::{IapError, MarketEvent, SessionStatus};
use orderbook::OrderBook;
use telemetry::Registry;

use crate::messages::{validate_order, ExecStatus, ExecutionReport, OrderRequest, OrderType};

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

#[derive(Debug, Clone)]
struct RestingOrder {
    instrument_id: u32,
    side: u8,
    price_ticks: i64,
    remaining: i64,
}

/// Simulated venue: accept/ack/fill via the order book.
pub struct SimulatedVenue {
    cfg: SimVenueConfig,
    books: BTreeMap<u32, OrderBook>,
    resting: BTreeMap<u64, RestingOrder>,
    next_exec_id: u64,
    last_reject: Option<RejectReason>,
    /// Venue metrics (orders/fills/latency histogram).
    pub metrics: Registry,
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
            next_exec_id: 0,
            last_reject: None,
            metrics: Registry::new(),
        })
    }

    /// Reason of the most recent rejection (`None` after an accepted order).
    pub fn last_reject_reason(&self) -> Option<RejectReason> {
        self.last_reject
    }

    /// The venue's book for an instrument (read-only), if any.
    pub fn book(&self, instrument_id: u32) -> Option<&OrderBook> {
        self.books.get(&instrument_id)
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

    /// Feed one market event; returns fills of resting orders the update
    /// crossed. Events of other venues are ignored (counted); no resting
    /// order fills while the book is stale or the status is not TRADING.
    pub fn on_market_event(&mut self, ev: &MarketEvent) -> Result<Vec<ExecutionReport>, IapError> {
        if self.cfg.venue_id != 0 && ev.venue_id != self.cfg.venue_id {
            self.metrics.counter("venue_market_events_ignored_total").inc();
            return Ok(Vec::new());
        }
        let venue_id = self.cfg.venue_id;
        let book = self
            .books
            .entry(ev.instrument_id)
            .or_insert_with(|| OrderBook::new(ev.instrument_id, venue_id));
        book.apply(ev)?;
        self.metrics.counter("venue_market_events_total").inc();
        if !Self::tradable(book) {
            self.metrics.counter("venue_market_events_untradable_total").inc();
            return Ok(Vec::new());
        }
        // resting orders crossed by the new touch fill at their limit price
        let best_bid = self.books[&ev.instrument_id].best_bid();
        let best_ask = self.books[&ev.instrument_id].best_ask();
        let mut out = Vec::new();
        let ids: Vec<u64> = self
            .resting
            .iter()
            .filter(|(_, r)| r.instrument_id == ev.instrument_id)
            .map(|(&id, _)| id)
            .collect();
        for id in ids {
            let r = self.resting.get(&id).expect("known id").clone();
            let crossing = if r.side == 0 {
                // resting buy fills when the market ask reaches its price
                best_ask.filter(|&(p, _)| p <= r.price_ticks)
            } else {
                best_bid.filter(|&(p, _)| p >= r.price_ticks)
            };
            let Some((_, avail)) = crossing else { continue };
            let fill = r.remaining.min(avail);
            if fill <= 0 {
                continue;
            }
            let fees = -self.cfg.maker_rebate_per_share * fill as f64;
            let status = if fill == r.remaining {
                ExecStatus::Filled
            } else {
                ExecStatus::Partial
            };
            out.push(self.report(id, status, fill, r.price_ticks, ev.exchange_ts, fees));
            self.metrics.counter("venue_fills_total").inc();
            if fill == r.remaining {
                self.resting.remove(&id);
            } else if let Some(rr) = self.resting.get_mut(&id) {
                rr.remaining -= fill;
            }
        }
        Ok(out)
    }

    /// Marketable fills against the current opposite depth (non-mutating):
    /// `(fills, remaining)`; `limit = None` walks the whole book.
    fn walk_depth(
        book: &OrderBook,
        side: u8,
        qty: i64,
        limit: Option<i64>,
    ) -> (Vec<(i64, i64)>, i64) {
        let opposite = 1 - side;
        let mut remaining = qty;
        let mut fills = Vec::new();
        for (price, size) in book.depth(opposite, usize::MAX) {
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
            let fill = remaining.min(size);
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

        let book = &self.books[&order.instrument_id];
        let (fills, remaining, rest_price) = match ot {
            OrderType::Market => {
                let (fills, remaining) = Self::walk_depth(book, order.side, order.qty, None);
                (fills, remaining, None)
            }
            OrderType::Limit => {
                let (fills, remaining) =
                    Self::walk_depth(book, order.side, order.qty, Some(order.price_ticks));
                (fills, remaining, Some(order.price_ticks))
            }
            OrderType::Ioc => {
                let limit = (order.price_ticks > 0).then_some(order.price_ticks);
                let (fills, remaining) = Self::walk_depth(book, order.side, order.qty, limit);
                (fills, remaining, None)
            }
            OrderType::Fok => {
                let limit = (order.price_ticks > 0).then_some(order.price_ticks);
                let (fills, remaining) = Self::walk_depth(book, order.side, order.qty, limit);
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
        for (price, qty) in fills {
            done += qty;
            let status = if done == order.qty {
                ExecStatus::Filled
            } else {
                ExecStatus::Partial
            };
            let fees = self.cfg.taker_fee_per_share * qty as f64;
            out.push(self.report(order.order_id, status, qty, price, ts, fees));
            self.metrics.counter("venue_fills_total").inc();
        }
        if remaining > 0 {
            match rest_price {
                Some(price) => {
                    self.resting.insert(
                        order.order_id,
                        RestingOrder {
                            instrument_id: order.instrument_id,
                            side: order.side,
                            price_ticks: price,
                            remaining,
                        },
                    );
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
        if self.resting.remove(&order_id).is_some() {
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
