//! The FAIL-CLOSED hard risk engine (spec §16; PLATFORM_CONVENTIONS.md §11
//! is the pinned contract, this crate is the normative implementation).
//!
//! Pre-trade checks run in a PINNED order — the first failing rule decides
//! and is emitted as the decision's `rule_id` (deterministic, replayable):
//!
//! 0. `CONFIG_MISSING` / `NOT_BOOTSTRAPPED`
//! 1. `KILL_GLOBAL`  2. `KILL_STRATEGY`  3. `KILL_INSTRUMENT`
//! 4. `KILL_VENUE`  5. `MALFORMED_ORDER`  6. `UNKNOWN_INSTRUMENT`
//! 7. `DUPLICATE_ORDER_ID`  8. `VENUE_DISCONNECTED`  9. `SEQUENCE_GAP`
//! 10. `STALE_PRICE`  11. `FAT_FINGER_QTY`  12. `FX_RATE_MISSING`
//! 13. `FAT_FINGER_NOTIONAL`  14. `PRICE_BAND`  15. `RATE_THROTTLE`
//! 16. `SELF_MATCH`  17. `POSITION_LIMIT`  18. `INSTRUMENT_NOTIONAL`
//! 19. `GROSS_NOTIONAL`  20. `NET_NOTIONAL`  21. `DAILY_LOSS`
//! 22. `STRATEGY_LOSS` — else `ALLOW`.
//!
//! Pinned semantics:
//!
//! - **Fail-closed**: an engine built from a missing/invalid config rejects
//!   everything with `CONFIG_MISSING` (severity BREACH); an instrument with
//!   no reference data, an unmarked nonzero position or a position/P&L in a
//!   currency without a conversion rate rejects rather than guesses.
//! - **Reference data**: [`InstrumentRef`] `{tick_size, qty_unit,
//!   quote_ccy}`; `qty_unit` is the real base units per qty unit
//!   (`lot_size` for FX, 1 for EQUITY/ETF — conventions §1).
//! - **Money**: every notional and P&L figure is in the reporting currency
//!   (`currency.reporting_ccy`). `notional = qty * qty_unit * price *
//!   fx_rate(quote_ccy)`. The rate of a non-reporting quote currency is the
//!   last consolidated mid of the pair named in `currency.conversion`
//!   (inverted when the pair is REPORTING/CCY). Pre-trade the rate must be
//!   present and not older than `stale_feed_timeout_ns` (else
//!   `FX_RATE_MISSING`); kill evaluation on fills/marks uses the last rate
//!   regardless of age (a stale rate is still the best estimate; the stale
//!   gate blocks new orders separately). A P&L bucket whose rate is missing
//!   makes the loss checks undeterminable: orders reject with
//!   `FX_RATE_MISSING`, no kill is latched (a latch needs a determinate
//!   breach).
//! - **Reference price**: the last consolidated mid `(bid + ask) / 2 *
//!   tick`, stamped with the market-data event time (callers pass the
//!   book's exchange_ts, never a decision clock). Updates with a timestamp
//!   older than the stored one are dropped and counted
//!   (`risk_market_regressions_dropped_total`). Priced orders use their
//!   limit price for notionals; unpriced orders use the mid. No mid ever
//!   seen => the STALE_PRICE gate rejects.
//! - **Throttle**: per-strategy event-time token bucket (capacity
//!   `order_rate_burst`, refill `max_order_rate_per_sec`/s). A token is
//!   consumed by every order reaching check 15; event time never refills
//!   backwards: `last_ts = max(last_ts, order.timestamp)`.
//! - **Open orders**: EVERY allowed order is tracked as open (remaining
//!   qty) regardless of type until `on_order_done(id)` or a full fill;
//!   fills with the order id reduce it. The OMS MUST call `on_order_done`
//!   for every terminal execution report (FILLED / CANCELED / REJECTED /
//!   EXPIRED); an order whose terminal report never arrives stays counted
//!   (fail-closed). PEG orders are tracked at their pegged touch price
//!   (same-side best at check time); MARKET / unpriced IOC/FOK / MID are
//!   tracked unpriced (price 0).
//! - **Self-match prevention** (wash-trade control, any venue): an order
//!   that would cross an own open order on the same instrument is
//!   rejected — a priced buy (sell) crosses when its price >= (<=) an own
//!   open priced ask (bid); an unpriced order, or any order against an own
//!   unpriced open opposite order, crosses unconditionally (conservative,
//!   fail-closed).
//! - **Projections** (worst case): buys check `pos + open_buy_qty + qty`,
//!   sells `pos - open_sell_qty - qty` (all order types); gross/net include
//!   every open order at its limit price (unpriced: the mid): gross adds
//!   |notional|, net adds it signed.
//! - **Loss limits** act on daily P&L = realized (average-cost accounting
//!   per (strategy, instrument) lot, pinned in `on_fill`) + unrealized
//!   (`pos * (mark - avg_price) * qty_unit`, mark = last consolidated
//!   mid, converted to the reporting currency at the last rate). They are
//!   evaluated after every fill AND after every market update touching a
//!   held instrument: a breach engages the STRATEGY / GLOBAL kill switch
//!   (strategy first, each at most once per latch) with no fill required,
//!   and every later order is rejected by the kill checks. Unmarked lots
//!   contribute nothing to a latch (undeterminable) but fail the gross
//!   check pre-trade.
//! - **Re-arm precedence** (pinned): `clear_kill` clears only the switch;
//!   while daily P&L is still at/below the effective limit, checks 21/22
//!   reject and the next fill/mark re-latches. `override_loss_limit`
//!   raises the effective limit (audited with the approver) but never
//!   clears a latch. `roll_session` zeroes realized P&L, re-bases marked
//!   lots to their mark, clears overrides and never touches kill switches.
//! - **Malformed fills** (qty <= 0, side > 1, price_ticks <= 0, unknown
//!   instrument) are NOT applied: `MALFORMED_FILL` audit record +
//!   `risk_malformed_fills_total`, `on_fill` returns `false`.
//! - **Snapshot / restore**: [`RiskEngine::snapshot`] serializes the full
//!   mutable state (schema x-version 1); [`RiskEngine::restore`] resumes it
//!   with bit-identical subsequent decisions and audit lines. An engine
//!   created with `require_bootstrap` rejects everything with
//!   `NOT_BOOTSTRAPPED` until `bootstrap_positions` or `restore`.
//! - Every decision and state transition appends a `RiskEvent` to the
//!   audit log; identical input sequences produce byte-identical logs in
//!   every language (money in reasons is formatted with
//!   [`crate::event::fmt_fixed`], never floating-point formatting).
//! - Bounds: `seen_orders` grows with the session's order count when
//!   `duplicate_order_window_ns` is 0 and is pruned to the window
//!   otherwise; `open` is bounded by the OMS honoring `on_order_done`.

use std::collections::BTreeMap;

use marketdata::IapError;
use serde_json::{json, Value};
use telemetry::Registry;
use venue::{order_validation_error, OrderRequest, OrderType};

use crate::event::{fmt_fixed, rules, Decision, RiskEvent, Scope, Severity};
use crate::limits::RiskLimits;

const NS_PER_SEC: f64 = 1e9;
/// Snapshot schema version.
pub const SNAPSHOT_VERSION: u64 = 1;

/// Per-instrument reference data the engine needs.
#[derive(Debug, Clone, PartialEq)]
pub struct InstrumentRef {
    /// Real price per tick.
    pub tick_size: f64,
    /// Real base units per qty unit (lot_size for FX, 1 for EQUITY/ETF).
    pub qty_unit: f64,
    /// Currency of prices / P&L of this instrument.
    pub quote_ccy: String,
}

impl InstrumentRef {
    /// Reference data for an instrument.
    pub fn new(tick_size: f64, qty_unit: f64, quote_ccy: &str) -> InstrumentRef {
        InstrumentRef {
            tick_size,
            qty_unit,
            quote_ccy: quote_ccy.to_string(),
        }
    }

    /// A USD equity: qty in shares, prices in USD.
    pub fn equity(tick_size: f64) -> InstrumentRef {
        InstrumentRef::new(tick_size, 1.0, "USD")
    }
}

/// A fill notification (from the venue layer / drop copy).
#[derive(Debug, Clone, PartialEq)]
pub struct Fill {
    /// Event time (ns).
    pub ts: i64,
    /// Strategy the fill belongs to.
    pub strategy_id: String,
    /// Filled instrument.
    pub instrument_id: u32,
    /// Originating order id (0 = external adjustment, no open order).
    pub order_id: u64,
    /// BID=0 buy, ASK=1 sell.
    pub side: u8,
    /// Filled quantity (> 0).
    pub qty: i64,
    /// Fill price in ticks (> 0).
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
struct OpenOrder {
    instrument_id: u32,
    side: u8,
    /// Limit / pegged price in ticks; 0 = unpriced (MARKET, IOC/FOK
    /// without a price, MID).
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
    /// Real price per base unit (quote ccy).
    avg_price: f64,
}

/// Fail-closed hard risk engine.
pub struct RiskEngine {
    limits: Option<RiskLimits>,
    config_error: String,
    instruments: BTreeMap<u32, InstrumentRef>,
    bootstrapped: bool,
    kill_global: bool,
    kill_strategies: BTreeMap<String, bool>,
    kill_instruments: BTreeMap<u32, bool>,
    kill_venues: BTreeMap<u16, bool>,
    venues_down: BTreeMap<u16, bool>,
    market: BTreeMap<u32, MarketState>,
    seen_orders: BTreeMap<u64, i64>,
    buckets: BTreeMap<String, Bucket>,
    open: BTreeMap<u64, OpenOrder>,
    positions: BTreeMap<u32, i64>,
    lots: BTreeMap<(String, u32), Lot>,
    /// Realized P&L in the instrument's quote currency per (strategy, ccy).
    realized: BTreeMap<(String, String), f64>,
    loss_override_global: Option<f64>,
    loss_override_strategy: BTreeMap<String, f64>,
    audit: Vec<RiskEvent>,
    /// Engine metrics (decision counters, PnL gauges).
    pub metrics: Registry,
}

impl RiskEngine {
    /// New engine from parsed limits + per-instrument reference data.
    pub fn new(limits: RiskLimits, instruments: BTreeMap<u32, InstrumentRef>) -> RiskEngine {
        let kill_global = limits.kill_switch_engaged;
        let mut eng = RiskEngine {
            limits: Some(limits),
            config_error: String::new(),
            instruments,
            bootstrapped: true,
            kill_global,
            kill_strategies: BTreeMap::new(),
            kill_instruments: BTreeMap::new(),
            kill_venues: BTreeMap::new(),
            venues_down: BTreeMap::new(),
            market: BTreeMap::new(),
            seen_orders: BTreeMap::new(),
            buckets: BTreeMap::new(),
            open: BTreeMap::new(),
            positions: BTreeMap::new(),
            lots: BTreeMap::new(),
            realized: BTreeMap::new(),
            loss_override_global: None,
            loss_override_strategy: BTreeMap::new(),
            audit: Vec::new(),
            metrics: Registry::new(),
        };
        eng.metrics
            .gauge("risk_kill_switch_engaged")
            .set(if kill_global { 1.0 } else { 0.0 });
        eng
    }

    /// New engine from parsed limits + tick sizes only (every instrument a
    /// USD equity: qty_unit 1). Convenience for tests and single-currency
    /// deployments.
    pub fn with_ticks(limits: RiskLimits, ticks: BTreeMap<u32, f64>) -> RiskEngine {
        RiskEngine::new(limits, equity_refs(ticks))
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
                reporting_ccy: "USD".to_string(),
                fx_conversion: BTreeMap::new(),
            },
            BTreeMap::new(),
        );
        eng.limits = None;
        eng.config_error = reason.to_string();
        eng
    }

    /// Build from a `configs/risk.json` document: a parse failure lands
    /// fail-closed instead of erroring (hard risk never runs open).
    pub fn from_config(doc: &Value, instruments: BTreeMap<u32, InstrumentRef>) -> RiskEngine {
        match RiskLimits::from_json(doc) {
            Ok(limits) => RiskEngine::new(limits, instruments),
            Err(e) => RiskEngine::fail_closed(&e.to_string()),
        }
    }

    /// [`RiskEngine::from_config`] with tick sizes only (USD equities).
    pub fn from_config_ticks(doc: &Value, ticks: BTreeMap<u32, f64>) -> RiskEngine {
        RiskEngine::from_config(doc, equity_refs(ticks))
    }

    /// Enter the awaiting-bootstrap state: every order rejects with
    /// `NOT_BOOTSTRAPPED` until [`RiskEngine::bootstrap_positions`] or
    /// [`RiskEngine::restore`] supplies the real positions (a restart is
    /// never fail-open on exposure).
    pub fn require_bootstrap(&mut self) {
        self.bootstrapped = false;
    }

    /// True once positions are trusted (default for a fresh engine).
    pub fn is_bootstrapped(&self) -> bool {
        self.bootstrapped
    }

    /// Apply drop-copy fills through the normal fill path (P&L accounted,
    /// loss limits evaluated — fail-closed), then mark the engine
    /// bootstrapped. Returns the number of fills rejected as malformed.
    pub fn bootstrap_positions(&mut self, fills: &[Fill], ts: i64) -> usize {
        let mut bad = 0;
        for f in fills {
            if !self.on_fill(f) {
                bad += 1;
            }
        }
        self.bootstrapped = true;
        self.emit(RiskEvent {
            timestamp: ts,
            scope: Scope::Global,
            scope_id: String::new(),
            rule_id: rules::BOOTSTRAP_COMPLETE.to_string(),
            severity: Severity::Info as u8,
            decision: Decision::Allow as u8,
            reason: format!(
                "bootstrapped from {} drop-copy fills ({bad} rejected)",
                fills.len()
            ),
        });
        bad
    }

    // ------------------------------------------------------------ state in

    /// Consolidated market update (best bid/ask ticks at the market-data
    /// event time). Updates older than the stored state are dropped and
    /// counted. Re-evaluates the loss limits of every strategy holding the
    /// instrument (a mark move can latch a kill with no fill).
    pub fn on_market(&mut self, instrument_id: u32, bid_ticks: i64, ask_ticks: i64, ts: i64) {
        let st = self.market.entry(instrument_id).or_insert(MarketState {
            bid_ticks,
            ask_ticks,
            ts,
            gaps: 0,
            gated: false,
        });
        if ts < st.ts {
            self.metrics
                .counter("risk_market_regressions_dropped_total")
                .inc();
            return;
        }
        st.bid_ticks = bid_ticks;
        st.ask_ticks = ask_ticks;
        st.ts = ts;
        let holders: Vec<String> = self
            .lots
            .iter()
            .filter(|((_, iid), lot)| *iid == instrument_id && lot.pos != 0)
            .map(|((sid, _), _)| sid.clone())
            .collect();
        // A conversion pair's mid moves every bucket in that currency: the
        // global check below covers it; strategies are re-checked only
        // when they hold the instrument (bounded work per update).
        self.evaluate_loss_limits(ts, &holders);
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

    /// Clear a kill switch (the switch only — see the re-arm precedence).
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

    /// Raise (or lower) the effective daily loss limit of the GLOBAL or a
    /// STRATEGY scope with written approval. Audited; never clears a
    /// latched kill switch. Errors on a non-positive/non-finite limit or an
    /// unsupported scope (nothing changes).
    pub fn override_loss_limit(
        &mut self,
        scope: Scope,
        scope_id: &str,
        new_limit: f64,
        ts: i64,
        approver: &str,
    ) -> Result<(), IapError> {
        if !(new_limit.is_finite() && new_limit > 0.0) {
            return Err(IapError::InvalidArgument(format!(
                "loss limit override must be finite and > 0, got {new_limit}"
            )));
        }
        let Some(limits) = self.limits.as_ref() else {
            return Err(IapError::InvalidArgument(
                "engine is fail-closed (no limits)".to_string(),
            ));
        };
        let old = match scope {
            Scope::Global => {
                let old = self.loss_override_global.unwrap_or(limits.max_daily_loss);
                self.loss_override_global = Some(new_limit);
                old
            }
            Scope::Strategy => {
                let old = self
                    .loss_override_strategy
                    .get(scope_id)
                    .copied()
                    .unwrap_or(limits.strategy_max_daily_loss);
                self.loss_override_strategy
                    .insert(scope_id.to_string(), new_limit);
                old
            }
            _ => {
                return Err(IapError::InvalidArgument(
                    "loss limits exist at GLOBAL and STRATEGY scope only".to_string(),
                ))
            }
        };
        self.emit(RiskEvent {
            timestamp: ts,
            scope,
            scope_id: scope_id.to_string(),
            rule_id: rules::LOSS_LIMIT_OVERRIDE.to_string(),
            severity: Severity::Warn as u8,
            decision: Decision::Allow as u8,
            reason: format!(
                "daily loss limit {} -> {} approved by {approver}",
                fmt_fixed(old, 2),
                fmt_fixed(new_limit, 2)
            ),
        });
        Ok(())
    }

    /// Session roll: realized P&L zeroed, every marked lot re-based to its
    /// mark (unrealized restarts at 0; unmarked lots keep their cost),
    /// loss-limit overrides cleared. Kill switches, positions, open orders
    /// and seen order ids are untouched. Audited.
    pub fn roll_session(&mut self, ts: i64, reason: &str) {
        self.realized.clear();
        let marks: Vec<(u32, f64)> = self
            .lots
            .keys()
            .map(|(_, iid)| *iid)
            .filter_map(|iid| self.mark_price(iid).map(|m| (iid, m)))
            .collect();
        for ((_, iid), lot) in self.lots.iter_mut() {
            if let Some((_, m)) = marks.iter().find(|(i, _)| i == iid) {
                lot.avg_price = *m;
            }
        }
        self.loss_override_global = None;
        self.loss_override_strategy.clear();
        self.refresh_pnl_gauges();
        self.emit(RiskEvent {
            timestamp: ts,
            scope: Scope::Global,
            scope_id: String::new(),
            rule_id: rules::SESSION_ROLLED.to_string(),
            severity: Severity::Info as u8,
            decision: Decision::Allow as u8,
            reason: reason.to_string(),
        });
    }

    fn set_kill(&mut self, scope: Scope, scope_id: &str, engaged: bool) {
        match scope {
            Scope::Global => {
                self.kill_global = engaged;
                self.metrics
                    .gauge("risk_kill_switch_engaged")
                    .set(if engaged { 1.0 } else { 0.0 });
            }
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

    /// A terminal order state (cancel / full fill / reject / expiry
    /// downstream): stop tracking it as open. The OMS MUST call this for
    /// every terminal execution report.
    pub fn on_order_done(&mut self, order_id: u64) {
        self.open.remove(&order_id);
    }

    /// Apply one fill: positions, realized PnL (average-cost, pinned), open
    /// order reduction, then loss-limit evaluation (strategy first, then
    /// global; each engages its kill switch at most once per latch).
    /// Returns `false` (and audits `MALFORMED_FILL`) when the fill is
    /// invalid or unpriceable — nothing is applied.
    pub fn on_fill(&mut self, fill: &Fill) -> bool {
        let invalid = if fill.qty <= 0 {
            Some(format!("qty must be > 0: {}", fill.qty))
        } else if fill.side > 1 {
            Some(format!("side must be 0 or 1: {}", fill.side))
        } else if fill.price_ticks <= 0 {
            Some(format!("price_ticks must be > 0: {}", fill.price_ticks))
        } else if !self.instruments.contains_key(&fill.instrument_id) {
            Some(format!(
                "no reference data for instrument {}",
                fill.instrument_id
            ))
        } else {
            None
        };
        if let Some(why) = invalid {
            self.metrics.counter("risk_malformed_fills_total").inc();
            self.emit(RiskEvent {
                timestamp: fill.ts,
                scope: Scope::Strategy,
                scope_id: fill.strategy_id.clone(),
                rule_id: rules::MALFORMED_FILL.to_string(),
                severity: Severity::Warn as u8,
                decision: Decision::Reject as u8,
                reason: format!("fill for order {} rejected: {why}", fill.order_id),
            });
            return false;
        }
        let ins = &self.instruments[&fill.instrument_id];
        let ccy = ins.quote_ccy.clone();
        let unit = ins.qty_unit;
        let price = fill.price_ticks as f64 * ins.tick_size;
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
        realized *= unit;
        let signed = if fill.side == 0 { fill.qty } else { -fill.qty };
        *self.positions.entry(fill.instrument_id).or_insert(0) += signed;
        *self
            .realized
            .entry((fill.strategy_id.clone(), ccy))
            .or_insert(0.0) += realized;
        // open order reduction
        if fill.order_id != 0 {
            let remove = match self.open.get_mut(&fill.order_id) {
                Some(r) => {
                    r.qty -= fill.qty;
                    r.qty <= 0
                }
                None => false,
            };
            if remove {
                self.open.remove(&fill.order_id);
            }
        }
        self.evaluate_loss_limits(fill.ts, std::slice::from_ref(&fill.strategy_id));
        true
    }

    // ------------------------------------------------------------- money

    /// Quote-currency -> reporting-currency rate and its mark time.
    fn fx_rate(&self, ccy: &str) -> Option<(f64, i64)> {
        let limits = self.limits.as_ref()?;
        if ccy == limits.reporting_ccy {
            return Some((1.0, i64::MAX));
        }
        let conv = limits.fx_conversion.get(ccy)?;
        let md = self.market.get(&conv.instrument_id)?;
        if md.bid_ticks <= 0 || md.ask_ticks <= 0 {
            return None;
        }
        let tick = self.instruments.get(&conv.instrument_id)?.tick_size;
        let mid = (md.bid_ticks + md.ask_ticks) as f64 * tick / 2.0;
        if mid <= 0.0 {
            return None;
        }
        Some((if conv.invert { 1.0 / mid } else { mid }, md.ts))
    }

    /// Last consolidated mid as a real price (quote ccy), if two-sided.
    fn mark_price(&self, instrument_id: u32) -> Option<f64> {
        let md = self.market.get(&instrument_id)?;
        if md.bid_ticks <= 0 || md.ask_ticks <= 0 {
            return None;
        }
        let tick = self.instruments.get(&instrument_id)?.tick_size;
        Some((md.bid_ticks + md.ask_ticks) as f64 * tick / 2.0)
    }

    /// Daily P&L of one strategy in the reporting currency: realized +
    /// unrealized of every marked lot. `None` when a needed conversion
    /// rate is missing (undeterminable).
    pub fn strategy_daily_pnl(&self, sid: &str) -> Option<f64> {
        let mut total = 0.0;
        for ((s, ccy), pnl) in &self.realized {
            if s != sid {
                continue;
            }
            total += pnl * self.fx_rate(ccy)?.0;
        }
        for ((s, iid), lot) in &self.lots {
            if s != sid || lot.pos == 0 {
                continue;
            }
            let Some(mark) = self.mark_price(*iid) else {
                continue; // unmarked: undeterminable, contributes nothing
            };
            let ins = &self.instruments[iid];
            total += lot.pos as f64 * (mark - lot.avg_price) * ins.qty_unit
                * self.fx_rate(&ins.quote_ccy)?.0;
        }
        Some(total)
    }

    /// Firm-wide daily P&L in the reporting currency (`None` when a
    /// conversion rate is missing).
    pub fn global_daily_pnl(&self) -> Option<f64> {
        let mut total = 0.0;
        for ((_, ccy), pnl) in &self.realized {
            total += pnl * self.fx_rate(ccy)?.0;
        }
        for ((_, iid), lot) in &self.lots {
            if lot.pos == 0 {
                continue;
            }
            let Some(mark) = self.mark_price(*iid) else {
                continue;
            };
            let ins = &self.instruments[iid];
            total += lot.pos as f64 * (mark - lot.avg_price) * ins.qty_unit
                * self.fx_rate(&ins.quote_ccy)?.0;
        }
        Some(total)
    }

    /// Firm-wide realized P&L in the reporting currency (missing rates
    /// contribute 0 — a gauge, not a control).
    pub fn realized_pnl(&self) -> f64 {
        self.realized
            .iter()
            .map(|((_, ccy), pnl)| pnl * self.fx_rate(ccy).map_or(0.0, |r| r.0))
            .sum()
    }

    /// One strategy's realized P&L in the reporting currency (missing
    /// rates contribute 0).
    pub fn strategy_pnl(&self, sid: &str) -> f64 {
        self.realized
            .iter()
            .filter(|((s, _), _)| s == sid)
            .map(|((_, ccy), pnl)| pnl * self.fx_rate(ccy).map_or(0.0, |r| r.0))
            .sum()
    }

    /// Firm-wide unrealized P&L in the reporting currency (gauge).
    pub fn unrealized_pnl(&self) -> f64 {
        self.global_daily_pnl().unwrap_or(0.0) - self.realized_pnl()
    }

    fn effective_strategy_loss(&self, limits: &RiskLimits, sid: &str) -> f64 {
        self.loss_override_strategy
            .get(sid)
            .copied()
            .unwrap_or(limits.strategy_max_daily_loss)
    }

    fn effective_global_loss(&self, limits: &RiskLimits) -> f64 {
        self.loss_override_global.unwrap_or(limits.max_daily_loss)
    }

    fn refresh_pnl_gauges(&mut self) {
        let realized = self.realized_pnl();
        let daily = self.global_daily_pnl().unwrap_or(realized);
        self.metrics.gauge("risk_realized_pnl").set(realized);
        self.metrics.gauge("risk_unrealized_pnl").set(daily - realized);
        self.metrics.gauge("risk_daily_pnl").set(daily);
    }

    /// Loss-limit evaluation (strategies given first, in order, then
    /// global). A determinate breach latches the kill switch once.
    fn evaluate_loss_limits(&mut self, ts: i64, strategies: &[String]) {
        self.refresh_pnl_gauges();
        let Some(limits) = self.limits.clone() else {
            return;
        };
        for sid in strategies {
            if self.strategy_killed(sid) {
                continue;
            }
            let Some(pnl) = self.strategy_daily_pnl(sid) else {
                continue;
            };
            let limit = self.effective_strategy_loss(&limits, sid);
            if pnl <= -limit {
                self.kill_strategies.insert(sid.clone(), true);
                self.emit(RiskEvent {
                    timestamp: ts,
                    scope: Scope::Strategy,
                    scope_id: sid.clone(),
                    rule_id: rules::STRATEGY_LOSS.to_string(),
                    severity: Severity::Breach as u8,
                    decision: Decision::Kill as u8,
                    reason: format!(
                        "strategy daily pnl {} breaches loss limit {}",
                        fmt_fixed(pnl, 2),
                        fmt_fixed(limit, 2)
                    ),
                });
            }
        }
        if !self.kill_global {
            if let Some(pnl) = self.global_daily_pnl() {
                let limit = self.effective_global_loss(&limits);
                if pnl <= -limit {
                    self.set_kill(Scope::Global, "", true);
                    self.emit(RiskEvent {
                        timestamp: ts,
                        scope: Scope::Global,
                        scope_id: String::new(),
                        rule_id: rules::DAILY_LOSS.to_string(),
                        severity: Severity::Breach as u8,
                        decision: Decision::Kill as u8,
                        reason: format!(
                            "global daily pnl {} breaches daily loss limit {}",
                            fmt_fixed(pnl, 2),
                            fmt_fixed(limit, 2)
                        ),
                    });
                }
            }
        }
    }

    // --------------------------------------------------------- state reads

    fn strategy_killed(&self, sid: &str) -> bool {
        self.kill_strategies.get(sid).copied().unwrap_or(false)
    }

    /// True when the global kill switch is engaged.
    pub fn kill_switch_engaged(&self) -> bool {
        self.kill_global
    }

    /// Aggregate position of an instrument.
    pub fn position(&self, instrument_id: u32) -> i64 {
        self.positions.get(&instrument_id).copied().unwrap_or(0)
    }

    /// Number of open (allowed, not yet terminal) orders tracked.
    pub fn open_order_count(&self) -> usize {
        self.open.len()
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
        // track every allowed order as open (self-match / projections)
        if outcome.allowed() {
            let price_ticks = self.tracked_price(order);
            self.open.insert(
                order.order_id,
                OpenOrder {
                    instrument_id: order.instrument_id,
                    side: order.side,
                    price_ticks,
                    qty: order.qty,
                },
            );
        }
        outcome
    }

    /// Price an open order is tracked at: its limit price, the pegged
    /// same-side touch for PEG, 0 (unpriced) otherwise.
    fn tracked_price(&self, order: &OrderRequest) -> i64 {
        if order.price_ticks > 0 {
            return order.price_ticks;
        }
        if order.order_type == OrderType::Peg.as_u8() {
            if let Some(md) = self.market.get(&order.instrument_id) {
                return if order.side == 0 {
                    md.bid_ticks
                } else {
                    md.ask_ticks
                };
            }
        }
        0
    }

    fn decision_scope(&self, order: &OrderRequest, rule_id: &str) -> (Scope, String) {
        match rule_id {
            rules::KILL_GLOBAL
            | rules::GROSS_NOTIONAL
            | rules::NET_NOTIONAL
            | rules::DAILY_LOSS
            | rules::CONFIG_MISSING
            | rules::NOT_BOOTSTRAPPED => (Scope::Global, String::new()),
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

    /// Pre-trade conversion rate: present and fresh (age within the stale
    /// timeout), else the FX_RATE_MISSING reason.
    fn pretrade_rate(&self, limits: &RiskLimits, ccy: &str, ts: i64) -> Result<f64, String> {
        match self.fx_rate(ccy) {
            None => Err(format!("no conversion rate for {ccy} -> {}", limits.reporting_ccy)),
            Some((rate, mark_ts)) => {
                if mark_ts != i64::MAX && limits.stale_book_reject {
                    let age = ts - mark_ts;
                    if age > limits.stale_feed_timeout_ns {
                        return Err(format!(
                            "conversion rate {ccy} -> {} age {age}ns exceeds {}ns",
                            limits.reporting_ccy, limits.stale_feed_timeout_ns
                        ));
                    }
                }
                Ok(rate)
            }
        }
    }

    fn evaluate(&mut self, order: &OrderRequest) -> RiskDecision {
        use Severity::{Breach, Warn};
        // 0. fail-closed configuration / bootstrap
        let Some(limits) = self.limits.clone() else {
            return Self::reject(
                rules::CONFIG_MISSING,
                Breach,
                format!("fail-closed: {}", self.config_error),
            );
        };
        if !self.bootstrapped {
            return Self::reject(
                rules::NOT_BOOTSTRAPPED,
                Breach,
                "positions not bootstrapped (fail-closed)".into(),
            );
        }
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
        let Some(ins) = self.instruments.get(&order.instrument_id).cloned() else {
            return Self::reject(
                rules::UNKNOWN_INSTRUMENT,
                Warn,
                format!("no reference data for instrument {}", order.instrument_id),
            );
        };
        let tick = ins.tick_size;
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
        if limits.duplicate_order_window_ns > 0 {
            // prune ids that fell out of the window (bounded growth)
            let cutoff = order.timestamp - limits.duplicate_order_window_ns;
            self.seen_orders.retain(|_, &mut ts| ts >= cutoff);
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
        // 12. conversion rate to the reporting currency
        let fx = match self.pretrade_rate(&limits, &ins.quote_ccy, order.timestamp) {
            Ok(r) => r,
            Err(why) => return Self::reject(rules::FX_RATE_MISSING, Warn, why),
        };
        // 13. fat-finger notional (priced orders use the limit price,
        // unpriced the mid); notional in the reporting currency
        let ref_price = if order.price_ticks > 0 {
            order.price_ticks as f64 * tick
        } else {
            mid
        };
        let order_notional = order.qty as f64 * ins.qty_unit * ref_price * fx;
        if order_notional > limits.max_order_notional {
            return Self::reject(
                rules::FAT_FINGER_NOTIONAL,
                Warn,
                format!(
                    "notional {} {} exceeds max_order_notional {}",
                    fmt_fixed(order_notional, 2),
                    limits.reporting_ccy,
                    fmt_fixed(limits.max_order_notional, 2)
                ),
            );
        }
        // 14. price band (priced orders only)
        if order.price_ticks > 0 {
            let dev_bps = ((order.price_ticks as f64 * tick) - mid).abs() / mid * 1e4;
            if dev_bps > limits.price_band_bps {
                return Self::reject(
                    rules::PRICE_BAND,
                    Warn,
                    format!(
                        "price deviates {}bps from mid, band {}bps",
                        fmt_fixed(dev_bps, 1),
                        fmt_fixed(limits.price_band_bps, 1)
                    ),
                );
            }
        }
        // 15. order-rate throttle (event-time token bucket per strategy)
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
            bucket.last_ts = bucket.last_ts.max(order.timestamp);
            if bucket.tokens < 1.0 {
                return Self::reject(
                    rules::RATE_THROTTLE,
                    Warn,
                    format!(
                        "strategy {} exceeded {} orders/s (burst {})",
                        order.strategy_id,
                        fmt_fixed(limits.max_order_rate_per_sec, 2),
                        fmt_fixed(limits.order_rate_burst, 2)
                    ),
                );
            }
            bucket.tokens -= 1.0;
        }
        // 16. self-match prevention (any venue; PEG at its pegged touch)
        let my_price = self.tracked_price(order);
        for (oid, r) in &self.open {
            if r.instrument_id != order.instrument_id || r.side == order.side {
                continue;
            }
            let crosses = if my_price > 0 && r.price_ticks > 0 {
                if order.side == 0 {
                    my_price >= r.price_ticks
                } else {
                    my_price <= r.price_ticks
                }
            } else {
                true // unpriced on either side: conservative
            };
            if crosses {
                return Self::reject(
                    rules::SELF_MATCH,
                    Warn,
                    format!("would cross own open order {oid} at {}", r.price_ticks),
                );
            }
        }
        // 17. position limit (worst-case projection incl. open orders)
        let pos = self.position(order.instrument_id);
        let open_same: i64 = self
            .open
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
        // 18. per-instrument notional (projection marked at the mid)
        let projected_notional = projected.abs() as f64 * ins.qty_unit * mid * fx;
        if projected_notional > limits.max_instrument_notional {
            return Self::reject(
                rules::INSTRUMENT_NOTIONAL,
                Warn,
                format!(
                    "projected notional {} exceeds max_instrument_notional {}",
                    fmt_fixed(projected_notional, 2),
                    fmt_fixed(limits.max_instrument_notional, 2)
                ),
            );
        }
        // 19-20. gross / net notional (filled positions + every open order
        // + this order; fail-closed on unmarked or unconvertible positions)
        let mut gross = 0.0f64;
        let mut net = 0.0f64;
        for (&iid, &p) in &self.positions {
            if p == 0 {
                continue;
            }
            let Some(mark) = self.mark_price(iid) else {
                return Self::reject(
                    rules::GROSS_NOTIONAL,
                    Warn,
                    format!("position in instrument {iid} has no mark price (fail-closed)"),
                );
            };
            let pins = &self.instruments[&iid];
            let Some((rate, _)) = self.fx_rate(&pins.quote_ccy) else {
                return Self::reject(
                    rules::GROSS_NOTIONAL,
                    Warn,
                    format!(
                        "position in instrument {iid} has no {} conversion rate (fail-closed)",
                        pins.quote_ccy
                    ),
                );
            };
            let v = p as f64 * pins.qty_unit * mark * rate;
            gross += v.abs();
            net += v;
        }
        for r in self.open.values() {
            let Some(oins) = self.instruments.get(&r.instrument_id) else {
                continue;
            };
            let price = if r.price_ticks > 0 {
                r.price_ticks as f64 * oins.tick_size
            } else {
                match self.mark_price(r.instrument_id) {
                    Some(m) => m,
                    None => continue, // unpriced and unmarked: cannot value
                }
            };
            let Some((rate, _)) = self.fx_rate(&oins.quote_ccy) else {
                return Self::reject(
                    rules::GROSS_NOTIONAL,
                    Warn,
                    format!(
                        "open order in instrument {} has no {} conversion rate (fail-closed)",
                        r.instrument_id, oins.quote_ccy
                    ),
                );
            };
            let v = r.qty as f64 * oins.qty_unit * price * rate;
            gross += v;
            net += if r.side == 0 { v } else { -v };
        }
        gross += order_notional;
        if gross > limits.max_gross_notional {
            return Self::reject(
                rules::GROSS_NOTIONAL,
                Warn,
                format!(
                    "projected gross notional {} exceeds max_gross_notional {}",
                    fmt_fixed(gross, 2),
                    fmt_fixed(limits.max_gross_notional, 2)
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
                    "projected net notional {} exceeds max_net_notional {}",
                    fmt_fixed(net, 2),
                    fmt_fixed(limits.max_net_notional, 2)
                ),
            );
        }
        // 21-22. loss limits on daily P&L (belt-and-braces after a cleared
        // latch; undeterminable P&L rejects fail-closed)
        let Some(global_pnl) = self.global_daily_pnl() else {
            return Self::reject(
                rules::FX_RATE_MISSING,
                Warn,
                "global daily pnl undeterminable: conversion rate missing".into(),
            );
        };
        let global_limit = self.effective_global_loss(&limits);
        if global_pnl <= -global_limit {
            return Self::reject(
                rules::DAILY_LOSS,
                Breach,
                format!(
                    "global daily pnl {} at daily loss limit {}",
                    fmt_fixed(global_pnl, 2),
                    fmt_fixed(global_limit, 2)
                ),
            );
        }
        let Some(strat_pnl) = self.strategy_daily_pnl(&order.strategy_id) else {
            return Self::reject(
                rules::FX_RATE_MISSING,
                Warn,
                "strategy daily pnl undeterminable: conversion rate missing".into(),
            );
        };
        let strat_limit = self.effective_strategy_loss(&limits, &order.strategy_id);
        if strat_pnl <= -strat_limit {
            return Self::reject(
                rules::STRATEGY_LOSS,
                Breach,
                format!(
                    "strategy daily pnl {} at loss limit {}",
                    fmt_fixed(strat_pnl, 2),
                    fmt_fixed(strat_limit, 2)
                ),
            );
        }
        RiskDecision {
            decision: Decision::Allow,
            rule_id: rules::ALLOW.to_string(),
            severity: Severity::Info,
            reason: String::new(),
        }
    }

    // ---------------------------------------------------- snapshot/restore

    /// Serialize the full mutable state (positions, lots, realized P&L,
    /// kill/latch state, marks, open orders, throttle buckets, seen order
    /// ids, overrides, bootstrap flag) as a schema-versioned JSON document.
    /// The audit log and metrics are NOT part of the snapshot (the audit
    /// log is the external JSONL file; metrics restart).
    pub fn snapshot(&self) -> Value {
        let bool_map = |m: &BTreeMap<String, bool>| -> Value {
            Value::Object(m.iter().map(|(k, v)| (k.clone(), json!(v))).collect())
        };
        json!({
            "x-version": SNAPSHOT_VERSION,
            "bootstrapped": self.bootstrapped,
            "kill_global": self.kill_global,
            "kill_strategies": bool_map(&self.kill_strategies),
            "kill_instruments": Value::Object(self.kill_instruments.iter()
                .map(|(k, v)| (k.to_string(), json!(v))).collect()),
            "kill_venues": Value::Object(self.kill_venues.iter()
                .map(|(k, v)| (k.to_string(), json!(v))).collect()),
            "venues_down": Value::Object(self.venues_down.iter()
                .map(|(k, v)| (k.to_string(), json!(v))).collect()),
            "market": Value::Object(self.market.iter().map(|(k, m)| (k.to_string(), json!({
                "bid_ticks": m.bid_ticks, "ask_ticks": m.ask_ticks, "ts": m.ts,
                "gaps": m.gaps, "gated": m.gated }))).collect()),
            "seen_orders": self.seen_orders.iter()
                .map(|(id, ts)| json!([id, ts])).collect::<Vec<_>>(),
            "buckets": Value::Object(self.buckets.iter().map(|(k, b)| (k.clone(), json!({
                "tokens": b.tokens, "last_ts": b.last_ts, "primed": b.primed }))).collect()),
            "open": Value::Object(self.open.iter().map(|(k, o)| (k.to_string(), json!({
                "instrument_id": o.instrument_id, "side": o.side,
                "price_ticks": o.price_ticks, "qty": o.qty }))).collect()),
            "positions": Value::Object(self.positions.iter()
                .map(|(k, v)| (k.to_string(), json!(v))).collect()),
            "lots": self.lots.iter().map(|((sid, iid), lot)| json!({
                "strategy_id": sid, "instrument_id": iid,
                "pos": lot.pos, "avg_price": lot.avg_price })).collect::<Vec<_>>(),
            "realized": self.realized.iter().map(|((sid, ccy), pnl)| json!({
                "strategy_id": sid, "ccy": ccy, "pnl": pnl })).collect::<Vec<_>>(),
            "loss_override_global": self.loss_override_global,
            "loss_override_strategy": Value::Object(self.loss_override_strategy.iter()
                .map(|(k, v)| (k.clone(), json!(v))).collect()),
        })
    }

    /// Rebuild an engine from `limits`, `instruments` and a
    /// [`RiskEngine::snapshot`] document (strict: unknown version or a
    /// malformed field is an error, nothing is restored). Emits a
    /// `STATE_RESTORED` audit record stamped `ts`.
    pub fn restore(
        limits: RiskLimits,
        instruments: BTreeMap<u32, InstrumentRef>,
        snap: &Value,
        ts: i64,
    ) -> Result<RiskEngine, IapError> {
        let bad = |what: &str| IapError::InvalidArgument(format!("risk snapshot: bad {what}"));
        if snap["x-version"].as_u64() != Some(SNAPSHOT_VERSION) {
            return Err(bad("x-version"));
        }
        let mut eng = RiskEngine::new(limits, instruments);
        eng.bootstrapped = snap["bootstrapped"].as_bool().ok_or_else(|| bad("bootstrapped"))?;
        eng.kill_global = snap["kill_global"].as_bool().ok_or_else(|| bad("kill_global"))?;
        eng.metrics
            .gauge("risk_kill_switch_engaged")
            .set(if eng.kill_global { 1.0 } else { 0.0 });
        for (k, v) in snap["kill_strategies"].as_object().ok_or_else(|| bad("kill_strategies"))? {
            eng.kill_strategies
                .insert(k.clone(), v.as_bool().ok_or_else(|| bad("kill_strategies"))?);
        }
        for (k, v) in snap["kill_instruments"].as_object().ok_or_else(|| bad("kill_instruments"))? {
            eng.kill_instruments.insert(
                k.parse::<u32>().map_err(|_| bad("kill_instruments"))?,
                v.as_bool().ok_or_else(|| bad("kill_instruments"))?,
            );
        }
        for (k, v) in snap["kill_venues"].as_object().ok_or_else(|| bad("kill_venues"))? {
            eng.kill_venues.insert(
                k.parse::<u16>().map_err(|_| bad("kill_venues"))?,
                v.as_bool().ok_or_else(|| bad("kill_venues"))?,
            );
        }
        for (k, v) in snap["venues_down"].as_object().ok_or_else(|| bad("venues_down"))? {
            eng.venues_down.insert(
                k.parse::<u16>().map_err(|_| bad("venues_down"))?,
                v.as_bool().ok_or_else(|| bad("venues_down"))?,
            );
        }
        for (k, m) in snap["market"].as_object().ok_or_else(|| bad("market"))? {
            eng.market.insert(
                k.parse::<u32>().map_err(|_| bad("market"))?,
                MarketState {
                    bid_ticks: m["bid_ticks"].as_i64().ok_or_else(|| bad("market.bid_ticks"))?,
                    ask_ticks: m["ask_ticks"].as_i64().ok_or_else(|| bad("market.ask_ticks"))?,
                    ts: m["ts"].as_i64().ok_or_else(|| bad("market.ts"))?,
                    gaps: m["gaps"].as_u64().ok_or_else(|| bad("market.gaps"))?,
                    gated: m["gated"].as_bool().ok_or_else(|| bad("market.gated"))?,
                },
            );
        }
        for pair in snap["seen_orders"].as_array().ok_or_else(|| bad("seen_orders"))? {
            let id = pair[0].as_u64().ok_or_else(|| bad("seen_orders"))?;
            let t = pair[1].as_i64().ok_or_else(|| bad("seen_orders"))?;
            eng.seen_orders.insert(id, t);
        }
        for (k, b) in snap["buckets"].as_object().ok_or_else(|| bad("buckets"))? {
            eng.buckets.insert(
                k.clone(),
                Bucket {
                    tokens: b["tokens"].as_f64().ok_or_else(|| bad("buckets.tokens"))?,
                    last_ts: b["last_ts"].as_i64().ok_or_else(|| bad("buckets.last_ts"))?,
                    primed: b["primed"].as_bool().ok_or_else(|| bad("buckets.primed"))?,
                },
            );
        }
        for (k, o) in snap["open"].as_object().ok_or_else(|| bad("open"))? {
            let side = o["side"].as_u64().ok_or_else(|| bad("open.side"))?;
            if side > 1 {
                return Err(bad("open.side"));
            }
            eng.open.insert(
                k.parse::<u64>().map_err(|_| bad("open"))?,
                OpenOrder {
                    instrument_id: o["instrument_id"]
                        .as_u64()
                        .and_then(|v| u32::try_from(v).ok())
                        .ok_or_else(|| bad("open.instrument_id"))?,
                    side: side as u8,
                    price_ticks: o["price_ticks"].as_i64().ok_or_else(|| bad("open.price_ticks"))?,
                    qty: o["qty"].as_i64().ok_or_else(|| bad("open.qty"))?,
                },
            );
        }
        for (k, v) in snap["positions"].as_object().ok_or_else(|| bad("positions"))? {
            eng.positions.insert(
                k.parse::<u32>().map_err(|_| bad("positions"))?,
                v.as_i64().ok_or_else(|| bad("positions"))?,
            );
        }
        for l in snap["lots"].as_array().ok_or_else(|| bad("lots"))? {
            let sid = l["strategy_id"].as_str().ok_or_else(|| bad("lots.strategy_id"))?;
            let iid = l["instrument_id"]
                .as_u64()
                .and_then(|v| u32::try_from(v).ok())
                .ok_or_else(|| bad("lots.instrument_id"))?;
            let avg = l["avg_price"].as_f64().ok_or_else(|| bad("lots.avg_price"))?;
            if !avg.is_finite() {
                return Err(bad("lots.avg_price"));
            }
            eng.lots.insert(
                (sid.to_string(), iid),
                Lot {
                    pos: l["pos"].as_i64().ok_or_else(|| bad("lots.pos"))?,
                    avg_price: avg,
                },
            );
        }
        for r in snap["realized"].as_array().ok_or_else(|| bad("realized"))? {
            let sid = r["strategy_id"].as_str().ok_or_else(|| bad("realized.strategy_id"))?;
            let ccy = r["ccy"].as_str().ok_or_else(|| bad("realized.ccy"))?;
            let pnl = r["pnl"].as_f64().ok_or_else(|| bad("realized.pnl"))?;
            if !pnl.is_finite() {
                return Err(bad("realized.pnl"));
            }
            eng.realized.insert((sid.to_string(), ccy.to_string()), pnl);
        }
        eng.loss_override_global = match &snap["loss_override_global"] {
            Value::Null => None,
            v => Some(v.as_f64().ok_or_else(|| bad("loss_override_global"))?),
        };
        for (k, v) in snap["loss_override_strategy"]
            .as_object()
            .ok_or_else(|| bad("loss_override_strategy"))?
        {
            eng.loss_override_strategy
                .insert(k.clone(), v.as_f64().ok_or_else(|| bad("loss_override_strategy"))?);
        }
        eng.refresh_pnl_gauges();
        eng.emit(RiskEvent {
            timestamp: ts,
            scope: Scope::Global,
            scope_id: String::new(),
            rule_id: rules::STATE_RESTORED.to_string(),
            severity: Severity::Info as u8,
            decision: Decision::Allow as u8,
            reason: format!(
                "restored snapshot v{SNAPSHOT_VERSION}: {} positions, {} open orders",
                eng.positions.values().filter(|p| **p != 0).count(),
                eng.open.len()
            ),
        });
        Ok(eng)
    }
}

fn equity_refs(ticks: BTreeMap<u32, f64>) -> BTreeMap<u32, InstrumentRef> {
    ticks
        .into_iter()
        .map(|(iid, t)| (iid, InstrumentRef::equity(t)))
        .collect()
}
