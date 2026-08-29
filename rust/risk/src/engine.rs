//! The FAIL-CLOSED hard risk engine (spec §16).
//!
//! Pre-trade checks run in a PINNED order — the first failing rule decides
//! and is emitted as the decision's `rule_id` (deterministic, replayable):
//!
//! 1. `KILL_GLOBAL`  2. `KILL_STRATEGY`  3. `KILL_INSTRUMENT`
//! 4. `KILL_VENUE`  5. `MALFORMED_ORDER`  6. `UNKNOWN_INSTRUMENT`
//! 7. `DUPLICATE_ORDER_ID`  8. `VENUE_DISCONNECTED`  9. `SEQUENCE_GAP`
//! 10. `STALE_PRICE`  11. `FAT_FINGER_QTY`  12. `FAT_FINGER_NOTIONAL`
//! 13. `PRICE_BAND`  14. `RATE_THROTTLE`  15. `SELF_MATCH`
//! 16. `POSITION_LIMIT`  17. `INSTRUMENT_NOTIONAL`  18. `GROSS_NOTIONAL`
//! 19. `NET_NOTIONAL`  20. `DAILY_LOSS`  21. `STRATEGY_LOSS` — else `ALLOW`.
//!
//! Pinned semantics (this crate is normative for the platform):
//!
//! - **Fail-closed**: an engine built from a missing/invalid config rejects
//!   everything with `CONFIG_MISSING` (severity BREACH); an instrument with
//!   no tick size, or an unmarked nonzero position during a notional check,
//!   rejects rather than guesses.
//! - **Reference price**: the last observed consolidated mid
//!   (`(bid + ask) / 2 * tick`). Priced orders use their limit price for
//!   notionals; unpriced orders use the mid. No mid ever seen => the
//!   STALE_PRICE gate rejects.
//! - **Throttle**: per-strategy event-time token bucket (capacity
//!   `order_rate_burst`, refill `max_order_rate_per_sec`/s). A token is
//!   consumed by every order reaching check 14; event time never refills
//!   backwards.
//! - **Self-match prevention**: an order that would cross an own resting
//!   order on the same instrument is rejected — a priced buy (sell)
//!   crosses when its price >= (<=) an own resting ask (bid); an unpriced
//!   marketable order (MARKET, unpriced IOC/FOK, MID) crosses when ANY own
//!   opposite resting order exists (conservative, fail-closed). PEG orders
//!   are passive by construction and skip the check. Allowed LIMIT orders
//!   are tracked as resting until `on_order_done` / full fill.
//! - **Position projection** (worst case): buys check
//!   `pos + open_buy_qty + qty`, sells check `pos - open_sell_qty - qty`.
//! - **Loss limits** act on realized PnL (average-cost accounting, pinned
//!   in `on_fill`): a breach engages the STRATEGY / GLOBAL kill switch at
//!   fill time (strategy evaluated first) and every later order is
//!   rejected by the kill checks.
//! - Every decision and state transition appends a `RiskEvent` to the
//!   audit log; identical input sequences produce byte-identical logs.

use std::collections::BTreeMap;

use telemetry::Registry;
use venue::{order_validation_error, OrderRequest, OrderType};

use crate::event::{rules, Decision, RiskEvent, Scope, Severity};
use crate::limits::RiskLimits;

const NS_PER_SEC: f64 = 1e9;

/// A fill notification (from the venue layer / drop copy).
#[derive(Debug, Clone, PartialEq)]
pub struct Fill {
    /// Event time (ns).
    pub ts: i64,
    /// Strategy the fill belongs to.
    pub strategy_id: String,
    /// Filled instrument.
    pub instrument_id: u32,
    /// Originating order id (0 = external adjustment, no resting order).
    pub order_id: u64,
    /// BID=0 buy, ASK=1 sell.
    pub side: u8,
    /// Filled quantity (> 0).
    pub qty: i64,
    /// Fill price in ticks.
    pub price_ticks: i64,
}

/// The outcome of one pre-trade check.
#[derive(Debug, Clone, PartialEq)]
pub struct RiskDecision {
    /// ALLOW / REJECT (KILL never decides an order directly).
    pub decision: Decision,
    /// The deciding rule.
    pub rule_id: String,
    /// INFO/WARN/BREACH.
    pub severity: Severity,
    /// Human-readable reason.
    pub reason: String,
}

impl RiskDecision {
    /// True when the order may proceed.
    pub fn allowed(&self) -> bool {
        self.decision == Decision::Allow
    }
}

#[derive(Debug, Clone, Copy)]
struct MarketState {
    bid_ticks: i64,
    ask_ticks: i64,
    ts: i64,
    gaps: u64,
    gated: bool,
}

#[derive(Debug, Clone)]
struct RestingOrder {
    instrument_id: u32,
    side: u8,
    price_ticks: i64,
    qty: i64,
}

#[derive(Debug, Clone, Copy, Default)]
struct Bucket {
    tokens: f64,
    last_ts: i64,
    primed: bool,
}

#[derive(Debug, Clone, Copy, Default)]
struct Lot {
    pos: i64,
    avg_price: f64,
}

/// Fail-closed hard risk engine.
pub struct RiskEngine {
    limits: Option<RiskLimits>,
    config_error: String,
    ticks: BTreeMap<u32, f64>,
    kill_global: bool,
    kill_strategies: BTreeMap<String, bool>,
    kill_instruments: BTreeMap<u32, bool>,
    kill_venues: BTreeMap<u16, bool>,
    venues_down: BTreeMap<u16, bool>,
    market: BTreeMap<u32, MarketState>,
    seen_orders: BTreeMap<u64, i64>,
    buckets: BTreeMap<String, Bucket>,
    resting: BTreeMap<u64, RestingOrder>,
    positions: BTreeMap<u32, i64>,
    lots: BTreeMap<(String, u32), Lot>,
    realized_by_strategy: BTreeMap<String, f64>,
    realized_global: f64,
    audit: Vec<RiskEvent>,
    /// Engine metrics (decision counters, PnL gauge).
    pub metrics: Registry,
}

impl RiskEngine {
    /// New engine from parsed limits + per-instrument tick sizes.
    pub fn new(limits: RiskLimits, ticks: BTreeMap<u32, f64>) -> RiskEngine {
        let kill_global = limits.kill_switch_engaged;
        RiskEngine {
            limits: Some(limits),
            config_error: String::new(),
            ticks,
            kill_global,
            kill_strategies: BTreeMap::new(),
            kill_instruments: BTreeMap::new(),
            kill_venues: BTreeMap::new(),
            venues_down: BTreeMap::new(),
            market: BTreeMap::new(),
            seen_orders: BTreeMap::new(),
            buckets: BTreeMap::new(),
            resting: BTreeMap::new(),
            positions: BTreeMap::new(),
            lots: BTreeMap::new(),
            realized_by_strategy: BTreeMap::new(),
            realized_global: 0.0,
            audit: Vec::new(),
            metrics: Registry::new(),
        }
    }

    /// New engine in FAIL-CLOSED mode: every order is rejected with
    /// `CONFIG_MISSING` carrying `reason`. This is the mandatory landing
    /// state for any configuration error.
    pub fn fail_closed(reason: &str) -> RiskEngine {
        let mut eng = RiskEngine::new(
            // limits are absent; the placeholder is never read
            RiskLimits {
                kill_switch_engaged: false,
                max_gross_notional: 1.0,
                max_net_notional: 1.0,
                max_daily_loss: 1.0,
                max_order_rate_per_sec: 1.0,
                order_rate_burst: 1.0,
                max_order_qty: 1,
                max_order_notional: 1.0,
                price_band_bps: 1.0,
                stale_book_reject: true,
                duplicate_order_window_ns: 0,
                max_position_qty: 1,
                max_instrument_notional: 1.0,
                strategy_max_daily_loss: 1.0,
                max_sequence_gap_before_halt: 0,
                stale_feed_timeout_ns: 1,
            },
            BTreeMap::new(),
        );
        eng.limits = None;
        eng.config_error = reason.to_string();
        eng
    }

    /// Build from a `configs/risk.json` document: a parse failure lands
    /// fail-closed instead of erroring (hard risk never runs open).
    pub fn from_config(doc: &serde_json::Value, ticks: BTreeMap<u32, f64>) -> RiskEngine {
        match RiskLimits::from_json(doc) {
            Ok(limits) => RiskEngine::new(limits, ticks),
            Err(e) => RiskEngine::fail_closed(&e.to_string()),
        }
    }

    // ------------------------------------------------------------ state in

    /// Consolidated market update (best bid/ask ticks at event time).
    pub fn on_market(&mut self, instrument_id: u32, bid_ticks: i64, ask_ticks: i64, ts: i64) {
        let st = self.market.entry(instrument_id).or_insert(MarketState {
            bid_ticks,
            ask_ticks,
            ts,
            gaps: 0,
            gated: false,
        });
        st.bid_ticks = bid_ticks;
        st.ask_ticks = ask_ticks;
        st.ts = ts;
    }

    /// A sequence gap on the instrument's feed; the gate closes after
    /// `max_sequence_gap_before_halt` gaps and stays closed until
    /// [`RiskEngine::on_feed_recovered`].
    pub fn on_sequence_gap(&mut self, instrument_id: u32, ts: i64) {
        let threshold = self
            .limits
            .as_ref()
            .map_or(0, |l| l.max_sequence_gap_before_halt);
        let st = self.market.entry(instrument_id).or_insert(MarketState {
            bid_ticks: 0,
            ask_ticks: 0,
            ts,
            gaps: 0,
            gated: false,
        });
        st.gaps += 1;
        if st.gaps >= threshold {
            st.gated = true;
        }
    }

    /// The feed recovered (snapshot complete): the gap gate reopens.
    pub fn on_feed_recovered(&mut self, instrument_id: u32, _ts: i64) {
        if let Some(st) = self.market.get_mut(&instrument_id) {
            st.gated = false;
            st.gaps = 0;
        }
    }

    /// Venue disconnect: orders to the venue reject until reconnect.
    pub fn on_venue_disconnect(&mut self, venue_id: u16, ts: i64) {
        self.venues_down.insert(venue_id, true);
        self.emit(RiskEvent {
            timestamp: ts,
            scope: Scope::Venue,
            scope_id: venue_id.to_string(),
            rule_id: rules::VENUE_DISCONNECT.to_string(),
            severity: Severity::Warn as u8,
            decision: Decision::Kill as u8,
            reason: format!("venue {venue_id} disconnected"),
        });
    }

    /// Venue reconnect.
    pub fn on_venue_reconnect(&mut self, venue_id: u16, ts: i64) {
        self.venues_down.insert(venue_id, false);
        self.emit(RiskEvent {
            timestamp: ts,
            scope: Scope::Venue,
            scope_id: venue_id.to_string(),
            rule_id: rules::VENUE_RECONNECT.to_string(),
            severity: Severity::Info as u8,
            decision: Decision::Allow as u8,
            reason: format!("venue {venue_id} reconnected"),
        });
    }

    /// Manually engage a kill switch.
    pub fn engage_kill(&mut self, scope: Scope, scope_id: &str, ts: i64, reason: &str) {
        self.set_kill(scope, scope_id, true);
        self.emit(RiskEvent {
            timestamp: ts,
            scope,
            scope_id: scope_id.to_string(),
            rule_id: rules::KILL_SWITCH_ENGAGED.to_string(),
            severity: Severity::Breach as u8,
            decision: Decision::Kill as u8,
            reason: reason.to_string(),
        });
    }

    /// Clear a kill switch.
    pub fn clear_kill(&mut self, scope: Scope, scope_id: &str, ts: i64, reason: &str) {
        self.set_kill(scope, scope_id, false);
        self.emit(RiskEvent {
            timestamp: ts,
            scope,
            scope_id: scope_id.to_string(),
            rule_id: rules::KILL_SWITCH_CLEARED.to_string(),
            severity: Severity::Info as u8,
            decision: Decision::Allow as u8,
            reason: reason.to_string(),
        });
    }

    fn set_kill(&mut self, scope: Scope, scope_id: &str, engaged: bool) {
        match scope {
            Scope::Global => self.kill_global = engaged,
            Scope::Strategy => {
                self.kill_strategies.insert(scope_id.to_string(), engaged);
            }
            Scope::Instrument => {
                if let Ok(iid) = scope_id.parse::<u32>() {
                    self.kill_instruments.insert(iid, engaged);
                }
            }
            Scope::Venue => {
                if let Ok(vid) = scope_id.parse::<u16>() {
                    self.kill_venues.insert(vid, engaged);
                }
            }
        }
    }

    /// A terminal order state (cancel / full fill / reject downstream):
    /// stop tracking it as resting.
    pub fn on_order_done(&mut self, order_id: u64) {
        self.resting.remove(&order_id);
    }

    /// Apply one fill: positions, realized PnL (average-cost, pinned),
    /// resting reduction, then loss-limit evaluation (strategy first, then
    /// global; each engages its kill switch at most once).
    pub fn on_fill(&mut self, fill: &Fill) {
        debug_assert!(fill.qty > 0);
        let tick = self.ticks.get(&fill.instrument_id).copied().unwrap_or(0.0);
        let price = fill.price_ticks as f64 * tick;
        let lot = self
            .lots
            .entry((fill.strategy_id.clone(), fill.instrument_id))
            .or_default();
        let mut realized = 0.0f64;
        if fill.side == 0 {
            // buy
            if lot.pos >= 0 {
                let new_pos = lot.pos + fill.qty;
                lot.avg_price =
                    (lot.avg_price * lot.pos as f64 + price * fill.qty as f64) / new_pos as f64;
                lot.pos = new_pos;
            } else {
                let closed = fill.qty.min(-lot.pos);
                realized += (lot.avg_price - price) * closed as f64;
                lot.pos += fill.qty;
                if lot.pos > 0 {
                    lot.avg_price = price;
                }
            }
        } else {
            // sell
            if lot.pos <= 0 {
                let new_short = -lot.pos + fill.qty;
                lot.avg_price = (lot.avg_price * (-lot.pos) as f64 + price * fill.qty as f64)
                    / new_short as f64;
                lot.pos -= fill.qty;
            } else {
                let closed = fill.qty.min(lot.pos);
                realized += (price - lot.avg_price) * closed as f64;
                lot.pos -= fill.qty;
                if lot.pos < 0 {
                    lot.avg_price = price;
                }
            }
        }
        let signed = if fill.side == 0 { fill.qty } else { -fill.qty };
        *self.positions.entry(fill.instrument_id).or_insert(0) += signed;
        *self
            .realized_by_strategy
            .entry(fill.strategy_id.clone())
            .or_insert(0.0) += realized;
        self.realized_global += realized;
        self.metrics.gauge("risk_realized_pnl").set(self.realized_global);
        // resting order reduction
        if fill.order_id != 0 {
            let remove = match self.resting.get_mut(&fill.order_id) {
                Some(r) => {
                    r.qty -= fill.qty;
                    r.qty <= 0
                }
                None => false,
            };
            if remove {
                self.resting.remove(&fill.order_id);
            }
        }
        // loss limits (strategy first, then global) — latch via kill state
        let Some(limits) = self.limits.as_ref() else {
            return;
        };
        let strat_loss = limits.strategy_max_daily_loss;
        let daily_loss = limits.max_daily_loss;
        let strat_pnl = self.realized_by_strategy[&fill.strategy_id];
        if strat_pnl <= -strat_loss && !self.strategy_killed(&fill.strategy_id) {
            self.kill_strategies.insert(fill.strategy_id.clone(), true);
            self.emit(RiskEvent {
                timestamp: fill.ts,
                scope: Scope::Strategy,
                scope_id: fill.strategy_id.clone(),
                rule_id: rules::STRATEGY_LOSS.to_string(),
                severity: Severity::Breach as u8,
                decision: Decision::Kill as u8,
                reason: format!(
                    "strategy realized pnl {strat_pnl:.2} breaches loss limit {strat_loss:.2}"
                ),
            });
        }
        if self.realized_global <= -daily_loss && !self.kill_global {
            self.kill_global = true;
            self.emit(RiskEvent {
                timestamp: fill.ts,
                scope: Scope::Global,
                scope_id: String::new(),
                rule_id: rules::DAILY_LOSS.to_string(),
                severity: Severity::Breach as u8,
                decision: Decision::Kill as u8,
                reason: format!(
                    "global realized pnl {:.2} breaches daily loss limit {daily_loss:.2}",
                    self.realized_global
                ),
            });
        }
    }

    // --------------------------------------------------------- state reads

    fn strategy_killed(&self, sid: &str) -> bool {
        self.kill_strategies.get(sid).copied().unwrap_or(false)
    }

    /// Aggregate position of an instrument.
    pub fn position(&self, instrument_id: u32) -> i64 {
        self.positions.get(&instrument_id).copied().unwrap_or(0)
    }

    /// Firm-wide realized PnL.
    pub fn realized_pnl(&self) -> f64 {
        self.realized_global
    }

    /// One strategy's realized PnL.
    pub fn strategy_pnl(&self, sid: &str) -> f64 {
        self.realized_by_strategy.get(sid).copied().unwrap_or(0.0)
    }

    /// The audit log so far.
    pub fn audit(&self) -> &[RiskEvent] {
        &self.audit
    }

    /// Full audit log as JSONL (one RiskEvent per line, trailing newline).
    pub fn audit_jsonl(&self) -> String {
        let mut out = String::new();
        for ev in &self.audit {
            out.push_str(&ev.to_json_line());
            out.push('\n');
        }
        out
    }

    fn emit(&mut self, ev: RiskEvent) {
        self.metrics.counter("risk_events_total").inc();
        self.audit.push(ev);
    }

    // ------------------------------------------------------ pre-trade path

    /// Run the pinned pre-trade check sequence for one order. Emits the
    /// decision as a RiskEvent and returns it.
    pub fn check_order(&mut self, order: &OrderRequest) -> RiskDecision {
        let outcome = self.evaluate(order);
        let (scope, scope_id) = self.decision_scope(order, &outcome.rule_id);
        self.metrics.counter("risk_decisions_total").inc();
        if outcome.allowed() {
            self.metrics.counter("risk_allowed_total").inc();
        } else {
            self.metrics.counter("risk_rejected_total").inc();
        }
        self.emit(RiskEvent {
            timestamp: order.timestamp,
            scope,
            scope_id,
            rule_id: outcome.rule_id.clone(),
            severity: outcome.severity as u8,
            decision: outcome.decision as u8,
            reason: outcome.reason.clone(),
        });
        // track allowed LIMIT orders as resting (self-match / projections)
        if outcome.allowed() && order.order_type == OrderType::Limit.as_u8() {
            self.resting.insert(
                order.order_id,
                RestingOrder {
                    instrument_id: order.instrument_id,
                    side: order.side,
                    price_ticks: order.price_ticks,
                    qty: order.qty,
                },
            );
        }
        outcome
    }

    fn decision_scope(&self, order: &OrderRequest, rule_id: &str) -> (Scope, String) {
        match rule_id {
            rules::KILL_GLOBAL
            | rules::GROSS_NOTIONAL
            | rules::NET_NOTIONAL
            | rules::DAILY_LOSS
            | rules::CONFIG_MISSING => (Scope::Global, String::new()),
            rules::KILL_STRATEGY
            | rules::MALFORMED_ORDER
            | rules::DUPLICATE_ORDER_ID
            | rules::RATE_THROTTLE
            | rules::STRATEGY_LOSS
            | rules::ALLOW => (Scope::Strategy, order.strategy_id.clone()),
            rules::KILL_VENUE | rules::VENUE_DISCONNECTED => {
                (Scope::Venue, order.venue_id.to_string())
            }
            _ => (Scope::Instrument, order.instrument_id.to_string()),
        }
    }

    fn reject(rule_id: &str, severity: Severity, reason: String) -> RiskDecision {
        RiskDecision {
            decision: Decision::Reject,
            rule_id: rule_id.to_string(),
            severity,
            reason,
        }
    }

    fn evaluate(&mut self, order: &OrderRequest) -> RiskDecision {
        use Severity::{Breach, Warn};
        // 0. fail-closed configuration
        let Some(limits) = self.limits.clone() else {
            return Self::reject(
                rules::CONFIG_MISSING,
                Breach,
                format!("fail-closed: {}", self.config_error),
            );
        };
        // 1-4. kill switches, global > strategy > instrument > venue
        if self.kill_global {
            return Self::reject(rules::KILL_GLOBAL, Breach, "global kill switch engaged".into());
        }
        if self.strategy_killed(&order.strategy_id) {
            return Self::reject(
                rules::KILL_STRATEGY,
                Breach,
                format!("strategy {} kill switch engaged", order.strategy_id),
            );
        }
        if self
            .kill_instruments
            .get(&order.instrument_id)
            .copied()
            .unwrap_or(false)
        {
            return Self::reject(
                rules::KILL_INSTRUMENT,
                Breach,
                format!("instrument {} kill switch engaged", order.instrument_id),
            );
        }
        if order.venue_id != 0
            && self.kill_venues.get(&order.venue_id).copied().unwrap_or(false)
        {
            return Self::reject(
                rules::KILL_VENUE,
                Breach,
                format!("venue {} kill switch engaged", order.venue_id),
            );
        }
        // 5. schema-level validation
        if let Some(reason) = order_validation_error(order) {
            return Self::reject(rules::MALFORMED_ORDER, Warn, reason);
        }
        // 6. reference data
        let Some(&tick) = self.ticks.get(&order.instrument_id) else {
            return Self::reject(
                rules::UNKNOWN_INSTRUMENT,
                Warn,
                format!("no tick_size for instrument {}", order.instrument_id),
            );
        };
        // 7. duplicate order id
        if let Some(&prev_ts) = self.seen_orders.get(&order.order_id) {
            let window = limits.duplicate_order_window_ns;
            if window == 0 || order.timestamp - prev_ts <= window {
                return Self::reject(
                    rules::DUPLICATE_ORDER_ID,
                    Warn,
                    format!("order_id {} already used at ts {prev_ts}", order.order_id),
                );
            }
        }
        self.seen_orders.insert(order.order_id, order.timestamp);
        // 8. venue connectivity
        if order.venue_id != 0
            && self.venues_down.get(&order.venue_id).copied().unwrap_or(false)
        {
            return Self::reject(
                rules::VENUE_DISCONNECTED,
                Warn,
                format!("venue {} is disconnected", order.venue_id),
            );
        }
        // 9-10. market-data gate
        let md = self.market.get(&order.instrument_id).copied();
        if let Some(md) = md {
            if md.gated {
                return Self::reject(
                    rules::SEQUENCE_GAP,
                    Warn,
                    format!("instrument {} feed has an unrecovered gap", order.instrument_id),
                );
            }
        }
        let mid = match md {
            Some(md) if md.bid_ticks > 0 && md.ask_ticks > 0 => {
                let age = order.timestamp - md.ts;
                if limits.stale_book_reject && age > limits.stale_feed_timeout_ns {
                    return Self::reject(
                        rules::STALE_PRICE,
                        Warn,
                        format!(
                            "reference price age {age}ns exceeds {}ns",
                            limits.stale_feed_timeout_ns
                        ),
                    );
                }
                (md.bid_ticks + md.ask_ticks) as f64 * tick / 2.0
            }
            _ => {
                return Self::reject(
                    rules::STALE_PRICE,
                    Warn,
                    format!("no reference price for instrument {}", order.instrument_id),
                );
            }
        };
        // 11. fat-finger quantity
        if order.qty > limits.max_order_qty {
            return Self::reject(
                rules::FAT_FINGER_QTY,
                Warn,
                format!("qty {} exceeds max_order_qty {}", order.qty, limits.max_order_qty),
            );
        }
        // 12. fat-finger notional (priced orders use the limit price,
        // unpriced the mid)
        let ref_price = if order.price_ticks > 0 {
            order.price_ticks as f64 * tick
        } else {
            mid
        };
        let order_notional = order.qty as f64 * ref_price;
        if order_notional > limits.max_order_notional {
            return Self::reject(
                rules::FAT_FINGER_NOTIONAL,
                Warn,
                format!(
                    "notional {order_notional:.2} exceeds max_order_notional {:.2}",
                    limits.max_order_notional
                ),
            );
        }
        // 13. price band (priced orders only)
        if order.price_ticks > 0 {
            let dev_bps = ((order.price_ticks as f64 * tick) - mid).abs() / mid * 1e4;
            if dev_bps > limits.price_band_bps {
                return Self::reject(
                    rules::PRICE_BAND,
                    Warn,
                    format!(
                        "price deviates {dev_bps:.1}bps from mid, band {:.1}bps",
                        limits.price_band_bps
                    ),
                );
            }
        }
        // 14. order-rate throttle (event-time token bucket per strategy)
        {
            let bucket = self
                .buckets
                .entry(order.strategy_id.clone())
                .or_insert(Bucket {
                    tokens: limits.order_rate_burst,
                    last_ts: order.timestamp,
                    primed: true,
                });
            if !bucket.primed {
                bucket.tokens = limits.order_rate_burst;
                bucket.primed = true;
                bucket.last_ts = order.timestamp;
            }
            let elapsed = (order.timestamp - bucket.last_ts).max(0);
            bucket.tokens = (bucket.tokens
                + elapsed as f64 * limits.max_order_rate_per_sec / NS_PER_SEC)
                .min(limits.order_rate_burst);
            bucket.last_ts = order.timestamp;
            if bucket.tokens < 1.0 {
                return Self::reject(
                    rules::RATE_THROTTLE,
                    Warn,
                    format!(
                        "strategy {} exceeded {} orders/s (burst {})",
                        order.strategy_id, limits.max_order_rate_per_sec, limits.order_rate_burst
                    ),
                );
            }
            bucket.tokens -= 1.0;
        }
        // 15. self-match prevention
        let priced = order.price_ticks > 0;
        let is_peg = order.order_type == OrderType::Peg.as_u8();
        if !is_peg {
            for (oid, r) in &self.resting {
                if r.instrument_id != order.instrument_id || r.side == order.side {
                    continue;
                }
                let crosses = if priced {
                    if order.side == 0 {
                        order.price_ticks >= r.price_ticks
                    } else {
                        order.price_ticks <= r.price_ticks
                    }
                } else {
                    true // unpriced marketable vs any own opposite resting
                };
                if crosses {
                    return Self::reject(
                        rules::SELF_MATCH,
                        Warn,
                        format!("would cross own resting order {oid} at {}", r.price_ticks),
                    );
                }
            }
        }
        // 16. position limit (worst-case projection incl. open orders)
        let pos = self.position(order.instrument_id);
        let open_same: i64 = self
            .resting
            .values()
            .filter(|r| r.instrument_id == order.instrument_id && r.side == order.side)
            .map(|r| r.qty)
            .sum();
        let projected = if order.side == 0 {
            pos + open_same + order.qty
        } else {
            pos - open_same - order.qty
        };
        if projected.abs() > limits.max_position_qty {
            return Self::reject(
                rules::POSITION_LIMIT,
                Warn,
                format!(
                    "projected position {projected} exceeds max_position_qty {}",
                    limits.max_position_qty
                ),
            );
        }
        // 17. per-instrument notional (projection marked at the mid)
        let projected_notional = projected.abs() as f64 * mid;
        if projected_notional > limits.max_instrument_notional {
            return Self::reject(
                rules::INSTRUMENT_NOTIONAL,
                Warn,
                format!(
                    "projected notional {projected_notional:.2} exceeds max_instrument_notional {:.2}",
                    limits.max_instrument_notional
                ),
            );
        }
        // 18-19. gross / net notional (filled positions + this order;
        // fail-closed on unmarked positions)
        let mut gross = 0.0f64;
        let mut net = 0.0f64;
        for (&iid, &p) in &self.positions {
            if p == 0 {
                continue;
            }
            let mark = self.mark_mid(iid);
            let Some(mark) = mark else {
                return Self::reject(
                    rules::GROSS_NOTIONAL,
                    Warn,
                    format!("position in instrument {iid} has no mark price (fail-closed)"),
                );
            };
            gross += (p as f64 * mark).abs();
            net += p as f64 * mark;
        }
        gross += order_notional;
        if gross > limits.max_gross_notional {
            return Self::reject(
                rules::GROSS_NOTIONAL,
                Warn,
                format!(
                    "projected gross notional {gross:.2} exceeds max_gross_notional {:.2}",
                    limits.max_gross_notional
                ),
            );
        }
        net += if order.side == 0 {
            order_notional
        } else {
            -order_notional
        };
        if net.abs() > limits.max_net_notional {
            return Self::reject(
                rules::NET_NOTIONAL,
                Warn,
                format!(
                    "projected net notional {net:.2} exceeds max_net_notional {:.2}",
                    limits.max_net_notional
                ),
            );
        }
        // 20-21. loss limits (belt-and-braces: the kill normally engaged
        // at fill time already rejected at check 1/2)
        if self.realized_global <= -limits.max_daily_loss {
            return Self::reject(
                rules::DAILY_LOSS,
                Breach,
                format!("global realized pnl {:.2} at daily loss limit", self.realized_global),
            );
        }
        let strat_pnl = self.strategy_pnl(&order.strategy_id);
        if strat_pnl <= -limits.strategy_max_daily_loss {
            return Self::reject(
                rules::STRATEGY_LOSS,
                Breach,
                format!("strategy realized pnl {strat_pnl:.2} at loss limit"),
            );
        }
        RiskDecision {
            decision: Decision::Allow,
            rule_id: rules::ALLOW.to_string(),
            severity: Severity::Info,
            reason: String::new(),
        }
    }

    fn mark_mid(&self, instrument_id: u32) -> Option<f64> {
        let md = self.market.get(&instrument_id)?;
        if md.bid_ticks <= 0 || md.ask_ticks <= 0 {
            return None;
        }
        let tick = self.ticks.get(&instrument_id)?;
        Some((md.bid_ticks + md.ask_ticks) as f64 * tick / 2.0)
    }
}
