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
//!   |notional|, net adds it signed. An open order that cannot be valued
//!   (unpriced AND unmarked) rejects with `GROSS_NOTIONAL`, exactly like an
//!   unmarked position — never skipped (fail-closed).
//! - **Loss limits** act on daily P&L = realized (average-cost accounting
//!   per (strategy, instrument) lot, pinned in `on_fill`) + unrealized
//!   (`pos * (mark - avg_price) * qty_unit`, mark = last consolidated
//!   mid, converted to the reporting currency at the last rate). They are
//!   evaluated after every fill AND after every market update touching a
//!   held instrument: a breach engages the STRATEGY / GLOBAL kill switch
//!   (strategy first, each at most once per latch) with no fill required,
//!   and every later order is rejected by the kill checks. An unmarked
//!   held lot makes daily P&L UNDETERMINABLE (`None`), exactly like a
//!   missing FX rate: no latch, and orders reject with `FX_RATE_MISSING`.
//! - **Re-arm precedence** (pinned): `clear_kill` clears only the switch;
//!   while daily P&L is still at/below the effective limit, checks 21/22
//!   reject and the next fill/mark re-latches. `override_loss_limit`
//!   raises the effective limit (audited with the approver) but never
//!   clears a latch. `roll_session` zeroes realized P&L, re-bases marked
//!   lots to their mark, clears overrides and never touches kill switches.
//! - **Malformed fills** (qty <= 0, side > 1, price_ticks <= 0, unknown
//!   instrument) are NOT applied: `MALFORMED_FILL` audit record +
//!   `risk_malformed_fills_total`, `on_fill` returns `false`.
//! - **Malformed kills**: a kill command whose `scope_id` does not name an
//!   addressable scope (INSTRUMENT id not a u32, VENUE id not a u16) is
//!   NEVER recorded as a success. `engage_kill` latches the GLOBAL kill
//!   (the safe wider reading of "stop trading"), `clear_kill` clears
//!   nothing, and both emit `MALFORMED_KILL` + `risk_malformed_kills_total`
//!   and return `Err`.
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
use serde_json::Value;
use telemetry::Registry;
use venue::{OrderRequest, OrderType};

use crate::event::{fmt_fixed, rules, Decision, RiskEvent, Scope, Severity};
use crate::limits::RiskLimits;

pub(crate) const NS_PER_SEC: f64 = 1e9;
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
    /// Reference data for an instrument, unvalidated (the fields are public
    /// anyway). [`RiskEngine::new`] re-validates every entry and lands
    /// fail-closed on an invalid one; prefer [`InstrumentRef::try_new`].
    pub fn new(tick_size: f64, qty_unit: f64, quote_ccy: &str) -> InstrumentRef {
        InstrumentRef {
            tick_size,
            qty_unit,
            quote_ccy: quote_ccy.to_string(),
        }
    }

    /// Validated reference data (the Java record / Python dataclass
    /// constructor semantics): `tick_size` and `qty_unit` finite and > 0,
    /// `quote_ccy` non-empty.
    pub fn try_new(
        tick_size: f64,
        qty_unit: f64,
        quote_ccy: &str,
    ) -> Result<InstrumentRef, IapError> {
        let r = InstrumentRef::new(tick_size, qty_unit, quote_ccy);
        match r.validation_error() {
            Some(why) => Err(IapError::InvalidArgument(why.to_string())),
            None => Ok(r),
        }
    }

    /// `Some(reason)` when this entry is not usable reference data. A NaN
    /// tick size or qty unit would turn every notional into NaN, and NaN
    /// compares false against every limit: such an entry must never reach
    /// the checks.
    pub fn validation_error(&self) -> Option<&'static str> {
        if !(self.tick_size.is_finite() && self.tick_size > 0.0)
            || !(self.qty_unit.is_finite() && self.qty_unit > 0.0)
        {
            return Some("tick_size and qty_unit must be finite and > 0");
        }
        if self.quote_ccy.is_empty() {
            return Some("quote_ccy must be non-empty");
        }
        None
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
pub(crate) struct MarketState {
    pub(crate) bid_ticks: i64,
    pub(crate) ask_ticks: i64,
    pub(crate) ts: i64,
    pub(crate) gaps: u64,
    pub(crate) gated: bool,
}

#[derive(Debug, Clone)]
pub(crate) struct OpenOrder {
    pub(crate) instrument_id: u32,
    pub(crate) side: u8,
    /// Limit / pegged price in ticks; 0 = unpriced (MARKET, IOC/FOK
    /// without a price, MID).
    pub(crate) price_ticks: i64,
    pub(crate) qty: i64,
}

#[derive(Debug, Clone, Copy, Default)]
pub(crate) struct Bucket {
    pub(crate) tokens: f64,
    pub(crate) last_ts: i64,
    pub(crate) primed: bool,
}

#[derive(Debug, Clone, Copy, Default)]
pub(crate) struct Lot {
    pub(crate) pos: i64,
    /// Real price per base unit (quote ccy).
    pub(crate) avg_price: f64,
}

/// `a + b` inside the SYMMETRIC i64 domain `[-i64::MAX, i64::MAX]`: `None`
/// on overflow and on `i64::MIN`, so a later negation / `abs` of the result
/// can never overflow either.
pub(crate) fn pos_add(a: i64, b: i64) -> Option<i64> {
    a.checked_add(b).filter(|v| *v != i64::MIN)
}

/// `a - b` inside the symmetric i64 domain (see [`pos_add`]).
pub(crate) fn pos_sub(a: i64, b: i64) -> Option<i64> {
    a.checked_sub(b).filter(|v| *v != i64::MIN)
}

/// Fail-closed hard risk engine.
pub struct RiskEngine {
    pub(crate) limits: Option<RiskLimits>,
    pub(crate) config_error: String,
    pub(crate) instruments: BTreeMap<u32, InstrumentRef>,
    pub(crate) bootstrapped: bool,
    pub(crate) kill_global: bool,
    pub(crate) kill_strategies: BTreeMap<String, bool>,
    pub(crate) kill_instruments: BTreeMap<u32, bool>,
    pub(crate) kill_venues: BTreeMap<u16, bool>,
    pub(crate) venues_down: BTreeMap<u16, bool>,
    pub(crate) market: BTreeMap<u32, MarketState>,
    pub(crate) seen_orders: BTreeMap<u64, i64>,
    pub(crate) buckets: BTreeMap<String, Bucket>,
    pub(crate) open: BTreeMap<u64, OpenOrder>,
    pub(crate) positions: BTreeMap<u32, i64>,
    pub(crate) lots: BTreeMap<(String, u32), Lot>,
    /// Realized P&L in the instrument's quote currency per (strategy, ccy).
    pub(crate) realized: BTreeMap<(String, String), f64>,
    pub(crate) loss_override_global: Option<f64>,
    pub(crate) loss_override_strategy: BTreeMap<String, f64>,
    pub(crate) audit: Vec<RiskEvent>,
    /// Engine metrics (decision counters, PnL gauges).
    pub metrics: Registry,
}

impl RiskEngine {
    /// New engine from parsed limits + per-instrument reference data. An
    /// invalid reference entry (see [`InstrumentRef::validation_error`])
    /// lands the engine FAIL-CLOSED (`CONFIG_MISSING` on every order), the
    /// counterpart of the Java / Python constructors refusing to build it.
    pub fn new(limits: RiskLimits, instruments: BTreeMap<u32, InstrumentRef>) -> RiskEngine {
        let bad_ref = instruments.iter().find_map(|(iid, r)| {
            r.validation_error()
                .map(|why| format!("invalid reference data for instrument {iid}: {why}"))
        });
        let mut eng = RiskEngine::build(limits, instruments);
        if let Some(why) = bad_ref {
            eng.limits = None;
            eng.config_error = why;
        }
        eng
    }

    fn build(limits: RiskLimits, instruments: BTreeMap<u32, InstrumentRef>) -> RiskEngine {
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
        let mut eng = RiskEngine::build(
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

    /// Build from a `configs/risk/risk.json` document: a parse failure lands
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

    // Venue connect/disconnect, kill-switch engage/clear/override, session
    // roll and the scope-id parsing they share live in `killswitch.rs`
    // (`impl RiskEngine` continued there).

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
        // Checked position accounting, BEFORE anything is applied: a fill
        // that would take the strategy lot or the aggregate position out of
        // the symmetric i64 domain cannot be booked, and an engine that
        // cannot book a fill no longer knows its exposure. Latch the GLOBAL
        // kill (release builds used to wrap silently here).
        let signed = if fill.side == 0 { fill.qty } else { -fill.qty };
        let lot_pos = self
            .lots
            .get(&(fill.strategy_id.clone(), fill.instrument_id))
            .map_or(0, |l| l.pos);
        if lot_pos == i64::MIN
            || pos_add(lot_pos, signed).is_none()
            || pos_add(self.position(fill.instrument_id), signed).is_none()
        {
            let _ = self.engage_kill(
                Scope::Global,
                "",
                fill.ts,
                &format!(
                    "fill for order {} overflows i64 position accounting (fail-closed)",
                    fill.order_id
                ),
            );
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
        *self.positions.entry(fill.instrument_id).or_insert(0) += signed;
        *self
            .realized
            .entry((fill.strategy_id.clone(), ccy))
            .or_insert(0.0) += realized;
        // open order reduction
        if fill.order_id != 0 {
            let remove = match self.open.get_mut(&fill.order_id) {
                Some(r) => match pos_sub(r.qty, fill.qty) {
                    Some(left) if left > 0 => {
                        r.qty = left;
                        false
                    }
                    _ => true,
                },
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
    pub(crate) fn fx_rate(&self, ccy: &str) -> Option<(f64, i64)> {
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
        let mid = md.bid_ticks.checked_add(md.ask_ticks)? as f64 * tick / 2.0;
        if mid <= 0.0 {
            return None;
        }
        Some((if conv.invert { 1.0 / mid } else { mid }, md.ts))
    }

    /// Last consolidated mid as a real price (quote ccy), if two-sided
    /// (`None` too when `bid + ask` leaves i64: no usable mark).
    pub(crate) fn mark_price(&self, instrument_id: u32) -> Option<f64> {
        let md = self.market.get(&instrument_id)?;
        if md.bid_ticks <= 0 || md.ask_ticks <= 0 {
            return None;
        }
        let tick = self.instruments.get(&instrument_id)?.tick_size;
        Some(md.bid_ticks.checked_add(md.ask_ticks)? as f64 * tick / 2.0)
    }

    /// Daily P&L of one strategy in the reporting currency: realized +
    /// unrealized of every held lot. `None` when a needed conversion rate
    /// is missing OR a held lot has no mark (undeterminable).
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
            // FAIL-OPEN defect: skipping an unmarked held lot let its
            // unrealized P&L count as zero, so a loss limit could fail to
            // trip on a book that is only partly valuable. An unmarked lot
            // makes the daily total undeterminable, exactly like the missing
            // FX rate above — `None` rejects, it does not guess.
            let mark = self.mark_price(*iid)?;
            let ins = &self.instruments[iid];
            total += lot.pos as f64 * (mark - lot.avg_price) * ins.qty_unit
                * self.fx_rate(&ins.quote_ccy)?.0;
        }
        Some(total)
    }

    /// Firm-wide daily P&L in the reporting currency (`None` when a
    /// conversion rate is missing or a held lot has no mark).
    pub fn global_daily_pnl(&self) -> Option<f64> {
        let mut total = 0.0;
        for ((_, ccy), pnl) in &self.realized {
            total += pnl * self.fx_rate(ccy)?.0;
        }
        for ((_, iid), lot) in &self.lots {
            if lot.pos == 0 {
                continue;
            }
            // FAIL-OPEN defect: see `strategy_daily_pnl` — an unmarked held
            // lot makes the daily total undeterminable, never zero.
            let mark = self.mark_price(*iid)?;
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

    pub(crate) fn effective_strategy_loss(&self, limits: &RiskLimits, sid: &str) -> f64 {
        self.loss_override_strategy
            .get(sid)
            .copied()
            .unwrap_or(limits.strategy_max_daily_loss)
    }

    pub(crate) fn effective_global_loss(&self, limits: &RiskLimits) -> f64 {
        self.loss_override_global.unwrap_or(limits.max_daily_loss)
    }

    pub(crate) fn refresh_pnl_gauges(&mut self) {
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
                    let _ = self.set_kill(Scope::Global, "", true);
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

    pub(crate) fn strategy_killed(&self, sid: &str) -> bool {
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

    // `audit_jsonl`, `emit`, `snapshot` and `restore` live in `audit.rs`
    // (`impl RiskEngine` continued there).

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
    pub(crate) fn tracked_price(&self, order: &OrderRequest) -> i64 {
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

    // `decision_scope`, `reject`, `pretrade_rate` and the pinned check-order
    // sequence `evaluate` (rules 0-22) live in `limits_eval.rs` (`impl
    // RiskEngine` continued there).

    // `snapshot` and `restore` (schema x-version 1, and the audit-log
    // helpers `audit_jsonl` / `emit`) live in `audit.rs` (`impl RiskEngine`
    // continued there).
}

fn equity_refs(ticks: BTreeMap<u32, f64>) -> BTreeMap<u32, InstrumentRef> {
    ticks
        .into_iter()
        .map(|(iid, t)| (iid, InstrumentRef::equity(t)))
        .collect()
}
