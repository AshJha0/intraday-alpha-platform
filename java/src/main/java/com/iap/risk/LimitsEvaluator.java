package com.iap.risk;

import java.util.Map;

/**
 * The pinned pre-trade check sequence (rules 0-22, PLATFORM_CONVENTIONS.md
 * §11.1) and its small helpers, split out of {@code RiskEngine} verbatim.
 * Every method takes the {@link RiskEngine} instance it operates on and
 * reads/writes its package-private state directly — the Java analogue of
 * the Rust port's {@code impl RiskEngine} continued in a separate file
 * ({@code rust/risk/src/limits_eval.rs}). {@link RiskEngine#checkOrder} (the
 * public entry point) still lives on {@code RiskEngine} and calls
 * {@link #evaluate}.
 *
 * <p>Every double limit comparison is written {@code !(x <= limit)} (never
 * {@code x > limit}): a NaN on either side compares false both ways, and the
 * negated form makes it REJECT instead of passing the check. For finite
 * values the two forms are identical.
 */
final class LimitsEvaluator {
    private LimitsEvaluator() {
    }

    static Scope decisionScope(String ruleId) {
        return switch (ruleId) {
            case Rules.KILL_GLOBAL, Rules.GROSS_NOTIONAL, Rules.NET_NOTIONAL,
                    Rules.DAILY_LOSS, Rules.CONFIG_MISSING,
                    Rules.NOT_BOOTSTRAPPED -> Scope.GLOBAL;
            case Rules.KILL_STRATEGY, Rules.MALFORMED_ORDER,
                    Rules.DUPLICATE_ORDER_ID, Rules.RATE_THROTTLE,
                    Rules.STRATEGY_LOSS, Rules.ALLOW -> Scope.STRATEGY;
            case Rules.KILL_VENUE, Rules.VENUE_DISCONNECTED -> Scope.VENUE;
            default -> Scope.INSTRUMENT;
        };
    }

    static String decisionScopeId(OrderRequest order, Scope scope) {
        return switch (scope) {
            case GLOBAL -> "";
            case STRATEGY -> order.strategyId();
            case VENUE -> Integer.toString(order.venueId());
            case INSTRUMENT -> Long.toString(order.instrumentId());
        };
    }

    /**
     * Reason of the {@code MALFORMED_ORDER} reject when a timestamp
     * difference (order age, duplicate window, throttle elapsed) leaves i64.
     */
    static final String TS_OVERFLOW =
            "timestamp arithmetic overflows i64 (fail-closed)";

    /**
     * The reject for a timestamp difference that leaves i64: such an order
     * (or the state it is compared with) carries a time no check can reason
     * about — never wrapped.
     */
    private static RiskDecision tsOverflow() {
        return reject(Rules.MALFORMED_ORDER, Severity.WARN, TS_OVERFLOW);
    }

    private static RiskDecision reject(String ruleId, Severity severity,
            String reason) {
        return new RiskDecision(Decision.REJECT, ruleId, severity, reason);
    }

    /**
     * The latest order event time the engine knows: this order's timestamp
     * or the newest throttle-bucket time, whichever is later. A market
     * state stamped beyond this clock by more than the stale timeout is
     * future-stamped (see check 10). Using the engine's clock rather than
     * the order's own timestamp keeps an order whose clock merely REGRESSED
     * (pinned: it still reaches the throttle) apart from market data that
     * is genuinely ahead of everything seen.
     */
    private static long eventClock(RiskEngine e, long orderTs) {
        long clock = orderTs;
        for (RiskEngine.Bucket b : e.buckets.values()) {
            if (b.primed && b.lastTs > clock) {
                clock = b.lastTs;
            }
        }
        return clock;
    }

    /**
     * Pre-trade conversion rate (fresh, and not stamped beyond the engine's
     * event clock by more than the stale timeout) or the FX_RATE_MISSING
     * reason.
     */
    private static Object pretradeRate(RiskEngine e, String ccy, long ts,
            long clock) {
        double[] rate = e.fxRate(ccy);
        if (rate == null) {
            return "no conversion rate for " + ccy + " -> " + e.limits.reportingCcy();
        }
        if (RiskEngine.hasMarkTs(rate) && e.limits.staleBookReject()) {
            long markTs = e.fxMarkTs(ccy);
            Long checkedAge = RiskEngine.tsSub(ts, markTs);
            if (checkedAge == null) {
                return TS_OVERFLOW;
            }
            long age = checkedAge;
            if (age > e.limits.staleFeedTimeoutNs()) {
                return "conversion rate " + ccy + " -> " + e.limits.reportingCcy()
                        + " age " + age + "ns exceeds "
                        + e.limits.staleFeedTimeoutNs() + "ns";
            }
            if (markTs > RiskEngine.satAdd(clock, e.limits.staleFeedTimeoutNs())) {
                return "conversion rate " + ccy + " -> " + e.limits.reportingCcy()
                        + " timestamp " + markTs + " is more than "
                        + e.limits.staleFeedTimeoutNs()
                        + "ns ahead of the latest order event time " + clock;
            }
        }
        return rate[0];
    }

    static RiskDecision evaluate(RiskEngine e, OrderRequest order) {
        // 0. fail-closed configuration / bootstrap
        if (e.limits == null) {
            return reject(Rules.CONFIG_MISSING, Severity.BREACH,
                    "fail-closed: " + e.configError);
        }
        if (!e.bootstrapped) {
            return reject(Rules.NOT_BOOTSTRAPPED, Severity.BREACH,
                    "positions not bootstrapped (fail-closed)");
        }
        // 1-4. kill switches, global > strategy > instrument > venue
        if (e.killGlobal) {
            return reject(Rules.KILL_GLOBAL, Severity.BREACH,
                    "global kill switch engaged");
        }
        if (e.strategyKilled(order.strategyId())) {
            return reject(Rules.KILL_STRATEGY, Severity.BREACH,
                    "strategy " + order.strategyId() + " kill switch engaged");
        }
        if (e.killInstruments.getOrDefault(order.instrumentId(), false)) {
            return reject(Rules.KILL_INSTRUMENT, Severity.BREACH,
                    "instrument " + order.instrumentId() + " kill switch engaged");
        }
        if (order.venueId() != 0
                && e.killVenues.getOrDefault(order.venueId(), false)) {
            return reject(Rules.KILL_VENUE, Severity.BREACH,
                    "venue " + order.venueId() + " kill switch engaged");
        }
        // venue 0 = "route via SOR": the destination is not known here, so
        // ANY engaged venue kill rejects (lowest killed venue id named) —
        // the router must not be a way around a venue halt.
        if (order.venueId() == 0) {
            for (Map.Entry<Integer, Boolean> kv : e.killVenues.entrySet()) {
                if (kv.getValue()) {
                    return reject(Rules.KILL_VENUE, Severity.BREACH,
                            "venue 0 (SOR) order rejected: venue " + kv.getKey()
                                    + " kill switch engaged");
                }
            }
        }
        // 5. schema-level validation
        String invalid = order.validationError();
        if (invalid != null) {
            return reject(Rules.MALFORMED_ORDER, Severity.WARN, invalid);
        }
        // 6. reference data
        InstrumentRef ins = e.instruments.get(order.instrumentId());
        if (ins == null) {
            return reject(Rules.UNKNOWN_INSTRUMENT, Severity.WARN,
                    "no reference data for instrument " + order.instrumentId());
        }
        double tick = ins.tickSize();
        // 7. duplicate order id
        Long prevTs = e.seenOrders.get(order.orderId());
        if (prevTs != null) {
            long window = e.limits.duplicateOrderWindowNs();
            boolean within = true;
            if (window != 0) {
                Long since = RiskEngine.tsSub(order.timestamp(), prevTs);
                if (since == null) {
                    return tsOverflow();
                }
                within = since <= window;
            }
            if (within) {
                return reject(Rules.DUPLICATE_ORDER_ID, Severity.WARN,
                        "order_id " + Long.toUnsignedString(order.orderId())
                                + " already used at ts " + prevTs);
            }
        }
        if (e.limits.duplicateOrderWindowNs() > 0) {
            Long checkedCutoff = RiskEngine.tsSub(order.timestamp(),
                    e.limits.duplicateOrderWindowNs());
            if (checkedCutoff == null) {
                return tsOverflow();
            }
            long cutoff = checkedCutoff;
            e.seenOrders.values().removeIf(ts -> ts < cutoff);
        }
        e.seenOrders.put(order.orderId(), order.timestamp());
        // 8. venue connectivity
        if (order.venueId() != 0
                && e.venuesDown.getOrDefault(order.venueId(), false)) {
            return reject(Rules.VENUE_DISCONNECTED, Severity.WARN,
                    "venue " + order.venueId() + " is disconnected");
        }
        // venue 0 = "route via SOR": while at least one known venue is up
        // the router has somewhere to go, but when EVERY known venue is
        // disconnected no venue could take the order.
        if (order.venueId() == 0 && !e.venuesDown.isEmpty()
                && !e.venuesDown.containsValue(false)) {
            return reject(Rules.VENUE_DISCONNECTED, Severity.WARN,
                    "venue 0 (SOR) order rejected: every known venue is disconnected");
        }
        // 9-10. market-data gate
        RiskEngine.MarketState md = e.market.get(order.instrumentId());
        if (md != null && md.gated) {
            return reject(Rules.SEQUENCE_GAP, Severity.WARN,
                    "instrument " + order.instrumentId()
                            + " feed has an unrecovered gap");
        }
        long clock = eventClock(e, order.timestamp());
        double mid;
        if (md != null && md.bidTicks > 0 && md.askTicks > 0) {
            Long checkedAge = RiskEngine.tsSub(order.timestamp(), md.ts);
            if (checkedAge == null) {
                return tsOverflow();
            }
            long age = checkedAge;
            if (e.limits.staleBookReject() && age > e.limits.staleFeedTimeoutNs()) {
                return reject(Rules.STALE_PRICE, Severity.WARN,
                        "reference price age " + age + "ns exceeds "
                                + e.limits.staleFeedTimeoutNs() + "ns");
            }
            // FAIL-OPEN defect: a market state stamped AFTER the order has a
            // negative age, which never exceeded the timeout, so a
            // future-stamped (corrupt / mis-clocked) mark was trusted for as
            // long as it stayed ahead — and every genuine update behind it
            // was dropped as a regression. Stamped beyond the engine's event
            // clock by more than the same window, it is exactly as untrusted
            // as a stale one.
            if (e.limits.staleBookReject()
                    && md.ts > RiskEngine.satAdd(clock, e.limits.staleFeedTimeoutNs())) {
                return reject(Rules.STALE_PRICE, Severity.WARN,
                        "reference price timestamp " + md.ts + " is more than "
                                + e.limits.staleFeedTimeoutNs()
                                + "ns ahead of the latest order event time " + clock);
            }
            Long sumTicks = RiskEngine.sumTicks(md.bidTicks, md.askTicks);
            if (sumTicks == null) {
                return reject(Rules.STALE_PRICE, Severity.WARN,
                        "no reference price for instrument " + order.instrumentId());
            }
            mid = (double) sumTicks.longValue() * tick / 2.0;
        } else {
            return reject(Rules.STALE_PRICE, Severity.WARN,
                    "no reference price for instrument " + order.instrumentId());
        }
        // 11. fat-finger quantity
        if (order.qty() > e.limits.maxOrderQty()) {
            return reject(Rules.FAT_FINGER_QTY, Severity.WARN,
                    "qty " + order.qty() + " exceeds max_order_qty "
                            + e.limits.maxOrderQty());
        }
        // 12. conversion rate to the reporting currency
        Object rateOrReason = pretradeRate(e, ins.quoteCcy(), order.timestamp(),
                clock);
        if (rateOrReason instanceof String why) {
            return reject(TS_OVERFLOW.equals(why)
                    ? Rules.MALFORMED_ORDER : Rules.FX_RATE_MISSING,
                    Severity.WARN, why);
        }
        double fx = (Double) rateOrReason;
        // 13. fat-finger notional (priced orders use the limit price,
        // unpriced the mid); notional in the reporting currency
        double refPrice = order.priceTicks() > 0
                ? (double) order.priceTicks() * tick : mid;
        double orderNotional = (double) order.qty() * ins.qtyUnit() * refPrice * fx;
        if (!(orderNotional <= e.limits.maxOrderNotional())) {
            return reject(Rules.FAT_FINGER_NOTIONAL, Severity.WARN,
                    "notional " + RiskEngine.fmtFixed(orderNotional, 2) + " "
                            + e.limits.reportingCcy() + " exceeds max_order_notional "
                            + RiskEngine.fmtFixed(e.limits.maxOrderNotional(), 2));
        }
        // 14. price band (priced orders only)
        if (order.priceTicks() > 0) {
            double devBps = Math.abs((double) order.priceTicks() * tick - mid)
                    / mid * 1e4;
            if (!(devBps <= e.limits.priceBandBps())) {
                return reject(Rules.PRICE_BAND, Severity.WARN,
                        "price deviates " + RiskEngine.fmtFixed(devBps, 1)
                                + "bps from mid, band "
                                + RiskEngine.fmtFixed(e.limits.priceBandBps(), 1) + "bps");
            }
        }
        // 15. order-rate throttle (event-time token bucket per strategy)
        {
            RiskEngine.Bucket bucket = e.buckets.computeIfAbsent(order.strategyId(), k -> {
                RiskEngine.Bucket b = new RiskEngine.Bucket();
                b.tokens = e.limits.orderRateBurst();
                b.lastTs = order.timestamp();
                b.primed = true;
                return b;
            });
            if (!bucket.primed) {
                bucket.tokens = e.limits.orderRateBurst();
                bucket.primed = true;
                bucket.lastTs = order.timestamp();
            }
            Long gap = RiskEngine.tsSub(order.timestamp(), bucket.lastTs);
            if (gap == null) {
                return tsOverflow();
            }
            long elapsed = Math.max(gap.longValue(), 0L);
            bucket.tokens = Math.min(bucket.tokens
                    + (double) elapsed * e.limits.maxOrderRatePerSec() / RiskEngine.NS_PER_SEC,
                    e.limits.orderRateBurst());
            bucket.lastTs = Math.max(bucket.lastTs, order.timestamp());
            if (!(bucket.tokens >= 1.0)) {
                return reject(Rules.RATE_THROTTLE, Severity.WARN,
                        "strategy " + order.strategyId() + " exceeded "
                                + RiskEngine.fmtFixed(e.limits.maxOrderRatePerSec(), 2)
                                + " orders/s (burst "
                                + RiskEngine.fmtFixed(e.limits.orderRateBurst(), 2) + ")");
            }
            bucket.tokens -= 1.0;
        }
        // 16. self-match prevention (any venue; PEG at its pegged touch)
        long myPrice = e.trackedPrice(order);
        for (Map.Entry<Long, RiskEngine.OpenOrder> en : e.open.entrySet()) {
            RiskEngine.OpenOrder r = en.getValue();
            if (r.instrumentId != order.instrumentId() || r.side == order.side()) {
                continue;
            }
            boolean crosses;
            if (myPrice > 0 && r.priceTicks > 0) {
                crosses = order.side() == 0
                        ? myPrice >= r.priceTicks : myPrice <= r.priceTicks;
            } else {
                crosses = true; // unpriced on either side: conservative
            }
            if (crosses) {
                return reject(Rules.SELF_MATCH, Severity.WARN,
                        "would cross own open order "
                                + Long.toUnsignedString(en.getKey())
                                + " at " + r.priceTicks);
            }
        }
        // 17. position limit (worst-case projection incl. open orders)
        long pos = e.position(order.instrumentId());
        // Checked (symmetric i64 domain): a projection that overflows is
        // not a number the limit can be compared with — reject, never wrap.
        Long openSame = 0L;
        for (RiskEngine.OpenOrder r : e.open.values()) {
            if (r.instrumentId == order.instrumentId() && r.side == order.side()) {
                openSame = RiskEngine.posAdd(openSame, r.qty);
                if (openSame == null) {
                    break;
                }
            }
        }
        Long checked = null;
        if (openSame != null) {
            checked = order.side() == 0
                    ? RiskEngine.posAdd(pos, openSame)
                    : RiskEngine.posSub(pos, openSame);
        }
        if (checked != null) {
            checked = order.side() == 0
                    ? RiskEngine.posAdd(checked, order.qty())
                    : RiskEngine.posSub(checked, order.qty());
        }
        if (checked == null) {
            return reject(Rules.MALFORMED_ORDER, Severity.WARN,
                    "projected position overflows i64 (fail-closed)");
        }
        long projected = checked;
        if (Math.abs(projected) > e.limits.maxPositionQty()) {
            return reject(Rules.POSITION_LIMIT, Severity.WARN,
                    "projected position " + projected
                            + " exceeds max_position_qty "
                            + e.limits.maxPositionQty());
        }
        // 18. per-instrument notional (projection marked at the mid)
        double projectedNotional = (double) Math.abs(projected) * ins.qtyUnit()
                * mid * fx;
        if (!(projectedNotional <= e.limits.maxInstrumentNotional())) {
            return reject(Rules.INSTRUMENT_NOTIONAL, Severity.WARN,
                    "projected notional " + RiskEngine.fmtFixed(projectedNotional, 2)
                            + " exceeds max_instrument_notional "
                            + RiskEngine.fmtFixed(e.limits.maxInstrumentNotional(), 2));
        }
        // 19-20. gross / net notional (filled positions + every open order
        // + this order; fail-closed on unmarked or unconvertible positions)
        double gross = 0.0;
        double net = 0.0;
        for (Map.Entry<Long, Long> pe : e.positions.entrySet()) {
            long p = pe.getValue();
            if (p == 0) {
                continue;
            }
            Double mark = e.markPrice(pe.getKey());
            if (mark == null) {
                return reject(Rules.GROSS_NOTIONAL, Severity.WARN,
                        "position in instrument " + pe.getKey()
                                + " has no mark price (fail-closed)");
            }
            InstrumentRef pins = e.instruments.get(pe.getKey());
            double[] rate = e.fxRate(pins.quoteCcy());
            if (rate == null) {
                return reject(Rules.GROSS_NOTIONAL, Severity.WARN,
                        "position in instrument " + pe.getKey() + " has no "
                                + pins.quoteCcy()
                                + " conversion rate (fail-closed)");
            }
            double v = (double) p * pins.qtyUnit() * mark * rate[0];
            gross += Math.abs(v);
            net += v;
        }
        for (Map.Entry<Long, RiskEngine.OpenOrder> oe : e.open.entrySet()) {
            RiskEngine.OpenOrder r = oe.getValue();
            InstrumentRef oins = e.instruments.get(r.instrumentId);
            if (oins == null) {
                continue;
            }
            double price;
            if (r.priceTicks > 0) {
                price = (double) r.priceTicks * oins.tickSize();
            } else {
                Double m = e.markPrice(r.instrumentId);
                if (m == null) {
                    // FAIL-OPEN defect: skipping an unvaluable OPEN ORDER
                    // (MARKET / MID / unpriced IOC-FOK on an instrument whose
                    // book went one-sided) dropped its whole notional from
                    // gross AND net, so live working exposure vanished from
                    // the aggregate and a correct GROSS_NOTIONAL reject became
                    // an ALLOW. An unvaluable open order is exactly as
                    // undeterminable as an unvaluable position: reject.
                    return reject(Rules.GROSS_NOTIONAL, Severity.WARN,
                            "open order " + Long.toUnsignedString(oe.getKey())
                                    + " in instrument " + r.instrumentId
                                    + " has no mark price (fail-closed)");
                }
                price = m;
            }
            double[] rate = e.fxRate(oins.quoteCcy());
            if (rate == null) {
                return reject(Rules.GROSS_NOTIONAL, Severity.WARN,
                        "open order in instrument " + r.instrumentId + " has no "
                                + oins.quoteCcy()
                                + " conversion rate (fail-closed)");
            }
            double v = (double) r.qty * oins.qtyUnit() * price * rate[0];
            gross += v;
            net += r.side == 0 ? v : -v;
        }
        gross += orderNotional;
        if (!(gross <= e.limits.maxGrossNotional())) {
            return reject(Rules.GROSS_NOTIONAL, Severity.WARN,
                    "projected gross notional " + RiskEngine.fmtFixed(gross, 2)
                            + " exceeds max_gross_notional "
                            + RiskEngine.fmtFixed(e.limits.maxGrossNotional(), 2));
        }
        net += order.side() == 0 ? orderNotional : -orderNotional;
        if (!(Math.abs(net) <= e.limits.maxNetNotional())) {
            return reject(Rules.NET_NOTIONAL, Severity.WARN,
                    "projected net notional " + RiskEngine.fmtFixed(net, 2)
                            + " exceeds max_net_notional "
                            + RiskEngine.fmtFixed(e.limits.maxNetNotional(), 2));
        }
        // 21-22. loss limits on daily P&L (belt-and-braces after a cleared
        // latch; undeterminable P&L rejects fail-closed)
        Double globalPnl = e.globalDailyPnl();
        if (globalPnl == null) {
            return reject(Rules.FX_RATE_MISSING, Severity.WARN,
                    "global daily pnl undeterminable: conversion rate missing");
        }
        double globalLimit = e.effectiveGlobalLoss();
        if (!(globalPnl > -globalLimit)) {
            return reject(Rules.DAILY_LOSS, Severity.BREACH,
                    "global daily pnl " + RiskEngine.fmtFixed(globalPnl, 2)
                            + " at daily loss limit " + RiskEngine.fmtFixed(globalLimit, 2));
        }
        Double stratPnl = e.strategyDailyPnl(order.strategyId());
        if (stratPnl == null) {
            return reject(Rules.FX_RATE_MISSING, Severity.WARN,
                    "strategy daily pnl undeterminable: conversion rate missing");
        }
        double stratLimit = e.effectiveStrategyLoss(order.strategyId());
        if (!(stratPnl > -stratLimit)) {
            return reject(Rules.STRATEGY_LOSS, Severity.BREACH,
                    "strategy daily pnl " + RiskEngine.fmtFixed(stratPnl, 2)
                            + " at loss limit " + RiskEngine.fmtFixed(stratLimit, 2));
        }
        return new RiskDecision(Decision.ALLOW, Rules.ALLOW, Severity.INFO, "");
    }
}
