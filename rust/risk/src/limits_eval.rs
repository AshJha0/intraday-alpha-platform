//! The pinned pre-trade check sequence (rules 0-22, PLATFORM_CONVENTIONS.md
//! §11.1) and its small helpers, split out of `engine.rs` verbatim. This is
//! an `impl RiskEngine` continued from `engine.rs`: [`RiskEngine::check_order`]
//! (the public entry point) still lives there and calls
//! [`RiskEngine::evaluate`] here.
//!
//! Every f64 limit comparison is written `!(x <= limit)` (never
//! `x > limit`): a NaN on either side compares false both ways, and the
//! negated form makes it REJECT instead of passing the check. For finite
//! values the two forms are identical.

use venue::{order_validation_error, OrderRequest};

use crate::engine::{pos_add, pos_sub, Bucket, RiskDecision, RiskEngine, NS_PER_SEC};
use crate::event::{fmt_fixed, rules, Decision, Scope, Severity};
use crate::limits::RiskLimits;

impl RiskEngine {
    pub(crate) fn decision_scope(&self, order: &OrderRequest, rule_id: &str) -> (Scope, String) {
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

    /// The latest order event time the engine knows: this order's timestamp
    /// or the newest throttle-bucket time, whichever is later. A market
    /// state stamped beyond this clock by more than the stale timeout is
    /// future-stamped (see check 10). Using the engine's clock rather than
    /// the order's own timestamp keeps an order whose clock merely
    /// REGRESSED (pinned: it still reaches the throttle) apart from market
    /// data that is genuinely ahead of everything seen.
    fn event_clock(&self, order_ts: i64) -> i64 {
        self.buckets
            .values()
            .filter(|b| b.primed)
            .map(|b| b.last_ts)
            .fold(order_ts, i64::max)
    }

    /// Pre-trade conversion rate: present and fresh (age within the stale
    /// timeout, and not stamped beyond the engine's event clock by more
    /// than the timeout — a future-stamped rate is as untrusted as an old
    /// one), else the FX_RATE_MISSING reason.
    fn pretrade_rate(
        &self,
        limits: &RiskLimits,
        ccy: &str,
        ts: i64,
        clock: i64,
    ) -> Result<f64, String> {
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
                    if mark_ts > clock.saturating_add(limits.stale_feed_timeout_ns) {
                        return Err(format!(
                            "conversion rate {ccy} -> {} timestamp {mark_ts} is more than {}ns ahead of the latest order event time {clock}",
                            limits.reporting_ccy, limits.stale_feed_timeout_ns
                        ));
                    }
                }
                Ok(rate)
            }
        }
    }

    pub(crate) fn evaluate(&mut self, order: &OrderRequest) -> RiskDecision {
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
        // venue 0 = "route via SOR": the destination is not known here, so
        // ANY engaged venue kill rejects (lowest killed venue id named) —
        // the router must not be a way around a venue halt.
        if order.venue_id == 0 {
            if let Some((vid, _)) = self.kill_venues.iter().find(|(_, on)| **on) {
                return Self::reject(
                    rules::KILL_VENUE,
                    Breach,
                    format!("venue 0 (SOR) order rejected: venue {vid} kill switch engaged"),
                );
            }
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
        let clock = self.event_clock(order.timestamp);
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
                // FAIL-OPEN defect: a market state stamped AFTER the order
                // has a negative age, which never exceeded the timeout, so a
                // future-stamped (corrupt / mis-clocked) mark was trusted
                // for as long as it stayed ahead — and every genuine update
                // behind it was dropped as a regression. Stamped beyond the
                // engine's event clock by more than the same window, it is
                // exactly as untrusted as a stale one.
                if limits.stale_book_reject
                    && md.ts > clock.saturating_add(limits.stale_feed_timeout_ns)
                {
                    return Self::reject(
                        rules::STALE_PRICE,
                        Warn,
                        format!(
                            "reference price timestamp {} is more than {}ns ahead of the latest order event time {clock}",
                            md.ts, limits.stale_feed_timeout_ns
                        ),
                    );
                }
                let Some(sum_ticks) = md.bid_ticks.checked_add(md.ask_ticks) else {
                    return Self::reject(
                        rules::STALE_PRICE,
                        Warn,
                        format!("no reference price for instrument {}", order.instrument_id),
                    );
                };
                sum_ticks as f64 * tick / 2.0
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
        let fx = match self.pretrade_rate(&limits, &ins.quote_ccy, order.timestamp, clock) {
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
        if !(order_notional <= limits.max_order_notional) {
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
            if !(dev_bps <= limits.price_band_bps) {
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
            if !(bucket.tokens >= 1.0) {
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
        // Checked (symmetric i64 domain): a projection that overflows is
        // not a number the limit can be compared with — reject, never wrap.
        let open_same = self
            .open
            .values()
            .filter(|r| r.instrument_id == order.instrument_id && r.side == order.side)
            .try_fold(0i64, |acc, r| pos_add(acc, r.qty));
        let projected = open_same.and_then(|open_same| {
            if order.side == 0 {
                pos_add(pos, open_same).and_then(|v| pos_add(v, order.qty))
            } else {
                pos_sub(pos, open_same).and_then(|v| pos_sub(v, order.qty))
            }
        });
        let Some(projected) = projected else {
            return Self::reject(
                rules::MALFORMED_ORDER,
                Warn,
                "projected position overflows i64 (fail-closed)".into(),
            );
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
        if !(projected_notional <= limits.max_instrument_notional) {
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
        for (oid, r) in &self.open {
            let Some(oins) = self.instruments.get(&r.instrument_id) else {
                continue;
            };
            let price = if r.price_ticks > 0 {
                r.price_ticks as f64 * oins.tick_size
            } else {
                match self.mark_price(r.instrument_id) {
                    Some(m) => m,
                    // FAIL-OPEN defect: skipping an unvaluable OPEN ORDER
                    // (MARKET / MID / unpriced IOC-FOK on an instrument whose
                    // book went one-sided) dropped its whole notional from
                    // gross AND net, so live working exposure vanished from
                    // the aggregate and a correct GROSS_NOTIONAL reject became
                    // an ALLOW. An unvaluable open order is exactly as
                    // undeterminable as an unvaluable position: reject.
                    None => {
                        return Self::reject(
                            rules::GROSS_NOTIONAL,
                            Warn,
                            format!(
                                "open order {oid} in instrument {} has no mark price (fail-closed)",
                                r.instrument_id
                            ),
                        )
                    }
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
        if !(gross <= limits.max_gross_notional) {
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
        if !(net.abs() <= limits.max_net_notional) {
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
        if !(global_pnl > -global_limit) {
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
        if !(strat_pnl > -strat_limit) {
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
}
