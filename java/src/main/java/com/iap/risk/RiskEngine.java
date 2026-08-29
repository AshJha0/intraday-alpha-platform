package com.iap.risk;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.TreeMap;

import com.iap.monitoring.MetricsRegistry;

/**
 * The FAIL-CLOSED hard risk engine (spec §16) — production Java port of the
 * normative Rust reference ({@code rust/risk/src/engine.rs}); it must
 * reproduce {@code tests/golden/expected_risk_decisions.json} exactly.
 *
 * <p>Pre-trade checks run in a PINNED order — the first failing rule
 * decides and is emitted as the decision's {@code rule_id}:
 * <ol>
 *   <li>KILL_GLOBAL</li> <li>KILL_STRATEGY</li> <li>KILL_INSTRUMENT</li>
 *   <li>KILL_VENUE</li> <li>MALFORMED_ORDER</li> <li>UNKNOWN_INSTRUMENT</li>
 *   <li>DUPLICATE_ORDER_ID</li> <li>VENUE_DISCONNECTED</li>
 *   <li>SEQUENCE_GAP</li> <li>STALE_PRICE</li> <li>FAT_FINGER_QTY</li>
 *   <li>FAT_FINGER_NOTIONAL</li> <li>PRICE_BAND</li> <li>RATE_THROTTLE</li>
 *   <li>SELF_MATCH</li> <li>POSITION_LIMIT</li> <li>INSTRUMENT_NOTIONAL</li>
 *   <li>GROSS_NOTIONAL</li> <li>NET_NOTIONAL</li> <li>DAILY_LOSS</li>
 *   <li>STRATEGY_LOSS</li>
 * </ol>
 * — else ALLOW.
 *
 * <p>Pinned semantics (see the Rust module docs, replicated here):
 * fail-closed on any configuration error ({@code CONFIG_MISSING}, BREACH);
 * reference price = last consolidated mid, priced orders use the limit
 * price for notionals; per-strategy event-time token bucket throttle;
 * conservative self-match prevention (allowed LIMIT orders tracked as
 * resting); worst-case position projection including open orders; loss
 * limits on realized PnL (average-cost accounting) engaging the
 * STRATEGY/GLOBAL kill switches at fill time (strategy first). Every
 * decision and state transition appends a {@link RiskEvent} to the audit
 * log; identical input sequences produce byte-identical logs.
 */
public final class RiskEngine {
    private static final double NS_PER_SEC = 1e9;

    private static final class MarketState {
        long bidTicks;
        long askTicks;
        long ts;
        long gaps;
        boolean gated;
    }

    private static final class RestingOrder {
        long instrumentId;
        int side;
        long priceTicks;
        long qty;
    }

    private static final class Bucket {
        double tokens;
        long lastTs;
        boolean primed;
    }

    private static final class Lot {
        long pos;
        double avgPrice;
    }

    private RiskLimits limits; // null = fail-closed
    private String configError = "";
    private final TreeMap<Long, Double> ticks;
    private boolean killGlobal;
    private final TreeMap<String, Boolean> killStrategies = new TreeMap<>();
    private final TreeMap<Long, Boolean> killInstruments = new TreeMap<>();
    private final TreeMap<Integer, Boolean> killVenues = new TreeMap<>();
    private final TreeMap<Integer, Boolean> venuesDown = new TreeMap<>();
    private final TreeMap<Long, MarketState> market = new TreeMap<>();
    // Order ids are u64: unsigned ordering so iteration (and therefore the
    // named order in SELF_MATCH reasons) matches the Rust BTreeMap<u64> for
    // ids >= 2^63.
    private final TreeMap<Long, Long> seenOrders =
            new TreeMap<>(Long::compareUnsigned);
    private final TreeMap<String, Bucket> buckets = new TreeMap<>();
    private final TreeMap<Long, RestingOrder> resting =
            new TreeMap<>(Long::compareUnsigned);
    private final TreeMap<Long, Long> positions = new TreeMap<>();
    private final TreeMap<String, Lot> lots = new TreeMap<>();
    private final TreeMap<String, Double> realizedByStrategy = new TreeMap<>();
    private double realizedGlobal;
    private final List<RiskEvent> audit = new ArrayList<>();
    /** Engine metrics (decision counters, PnL + kill-switch gauges). */
    public final MetricsRegistry metrics;

    /** New engine from parsed limits + per-instrument tick sizes. */
    public RiskEngine(RiskLimits limits, Map<Long, Double> ticks) {
        this(limits, ticks, new MetricsRegistry());
    }

    /** New engine publishing its metrics into a shared registry. */
    public RiskEngine(RiskLimits limits, Map<Long, Double> ticks,
            MetricsRegistry metrics) {
        this.limits = limits;
        this.ticks = new TreeMap<>(ticks);
        this.killGlobal = limits.killSwitchEngaged();
        this.metrics = metrics;
        metrics.gauge("risk_kill_switch_engaged").set(killGlobal ? 1.0 : 0.0);
    }

    /**
     * New engine in FAIL-CLOSED mode: every order is rejected with
     * {@code CONFIG_MISSING} carrying {@code reason}. This is the mandatory
     * landing state for any configuration error.
     */
    public static RiskEngine failClosed(String reason) {
        return failClosed(reason, new MetricsRegistry());
    }

    /** {@link #failClosed(String)} with a shared metrics registry. */
    public static RiskEngine failClosed(String reason, MetricsRegistry metrics) {
        RiskEngine eng = new RiskEngine(new RiskLimits(false, 1.0, 1.0, 1.0,
                1.0, 1.0, 1, 1.0, 1.0, true, 0, 1, 1.0, 1.0, 0, 1),
                new TreeMap<>(), metrics);
        eng.limits = null;
        eng.configError = reason;
        return eng;
    }

    /**
     * Build from a parsed {@code configs/risk.json} document: a parse
     * failure lands fail-closed instead of throwing (hard risk never runs
     * open).
     */
    public static RiskEngine fromConfig(Map<String, Object> doc,
            Map<Long, Double> ticks) {
        return fromConfig(doc, ticks, new MetricsRegistry());
    }

    /** {@link #fromConfig(Map, Map)} with a shared metrics registry. */
    public static RiskEngine fromConfig(Map<String, Object> doc,
            Map<Long, Double> ticks, MetricsRegistry metrics) {
        try {
            return new RiskEngine(RiskLimits.fromJson(doc), ticks, metrics);
        } catch (RuntimeException e) {
            return failClosed(e.getMessage() == null
                    ? e.getClass().getSimpleName() : e.getMessage(), metrics);
        }
    }

    // -------------------------------------------------------------- state in

    /** Consolidated market update (best bid/ask ticks at event time). */
    public void onMarket(long instrumentId, long bidTicks, long askTicks, long ts) {
        MarketState st = market.computeIfAbsent(instrumentId, k -> new MarketState());
        st.bidTicks = bidTicks;
        st.askTicks = askTicks;
        st.ts = ts;
    }

    /**
     * A sequence gap on the instrument's feed; the gate closes after
     * {@code max_sequence_gap_before_halt} gaps and stays closed until
     * {@link #onFeedRecovered}.
     */
    public void onSequenceGap(long instrumentId, long ts) {
        long threshold = limits == null ? 0 : limits.maxSequenceGapBeforeHalt();
        MarketState st = market.computeIfAbsent(instrumentId, k -> {
            MarketState m = new MarketState();
            m.ts = ts;
            return m;
        });
        st.gaps++;
        if (st.gaps >= threshold) {
            st.gated = true;
        }
    }

    /** The feed recovered (snapshot complete): the gap gate reopens. */
    public void onFeedRecovered(long instrumentId, long ts) {
        MarketState st = market.get(instrumentId);
        if (st != null) {
            st.gated = false;
            st.gaps = 0;
        }
    }

    /** Venue disconnect: orders to the venue reject until reconnect. */
    public void onVenueDisconnect(int venueId, long ts) {
        venuesDown.put(venueId, true);
        emit(new RiskEvent(ts, Scope.VENUE, Integer.toString(venueId),
                Rules.VENUE_DISCONNECT, Severity.WARN.code(),
                Decision.KILL.code(), "venue " + venueId + " disconnected"));
    }

    /** Venue reconnect. */
    public void onVenueReconnect(int venueId, long ts) {
        venuesDown.put(venueId, false);
        emit(new RiskEvent(ts, Scope.VENUE, Integer.toString(venueId),
                Rules.VENUE_RECONNECT, Severity.INFO.code(),
                Decision.ALLOW.code(), "venue " + venueId + " reconnected"));
    }

    /** Manually engage a kill switch. */
    public void engageKill(Scope scope, String scopeId, long ts, String reason) {
        setKill(scope, scopeId, true);
        emit(new RiskEvent(ts, scope, scopeId, Rules.KILL_SWITCH_ENGAGED,
                Severity.BREACH.code(), Decision.KILL.code(), reason));
    }

    /** Clear a kill switch. */
    public void clearKill(Scope scope, String scopeId, long ts, String reason) {
        setKill(scope, scopeId, false);
        emit(new RiskEvent(ts, scope, scopeId, Rules.KILL_SWITCH_CLEARED,
                Severity.INFO.code(), Decision.ALLOW.code(), reason));
    }

    private void setKill(Scope scope, String scopeId, boolean engaged) {
        switch (scope) {
            case GLOBAL -> setKillGlobal(engaged);
            case STRATEGY -> killStrategies.put(scopeId, engaged);
            case INSTRUMENT -> {
                try {
                    // Instrument ids are u32 (the Rust reference parses the
                    // scope id with parse::<u32>()): out-of-range or
                    // unparseable ids are a no-op.
                    killInstruments.put(
                            Integer.toUnsignedLong(
                                    Integer.parseUnsignedInt(scopeId)),
                            engaged);
                } catch (NumberFormatException ignored) {
                    // unparseable id: no-op, mirrors the reference
                }
            }
            case VENUE -> {
                try {
                    killVenues.put(Integer.parseInt(scopeId), engaged);
                } catch (NumberFormatException ignored) {
                    // unparseable id: no-op, mirrors the reference
                }
            }
        }
    }

    private void setKillGlobal(boolean engaged) {
        killGlobal = engaged;
        metrics.gauge("risk_kill_switch_engaged").set(engaged ? 1.0 : 0.0);
    }

    /**
     * A terminal order state (cancel / full fill / reject downstream):
     * stop tracking it as resting.
     */
    public void onOrderDone(long orderId) {
        resting.remove(orderId);
    }

    private static String lotKey(String strategyId, long instrumentId) {
        return strategyId + " " + instrumentId;
    }

    /**
     * Apply one fill: positions, realized PnL (average-cost, pinned),
     * resting reduction, then loss-limit evaluation (strategy first, then
     * global; each engages its kill switch at most once).
     */
    public void onFill(RiskFill fill) {
        double tick = ticks.getOrDefault(fill.instrumentId(), 0.0);
        double price = (double) fill.priceTicks() * tick;
        Lot lot = lots.computeIfAbsent(
                lotKey(fill.strategyId(), fill.instrumentId()), k -> new Lot());
        double realized = 0.0;
        if (fill.side() == 0) { // buy
            if (lot.pos >= 0) {
                long newPos = lot.pos + fill.qty();
                lot.avgPrice = (lot.avgPrice * (double) lot.pos
                        + price * (double) fill.qty()) / (double) newPos;
                lot.pos = newPos;
            } else {
                long closed = Math.min(fill.qty(), -lot.pos);
                realized += (lot.avgPrice - price) * (double) closed;
                lot.pos += fill.qty();
                if (lot.pos > 0) {
                    lot.avgPrice = price;
                }
            }
        } else { // sell
            if (lot.pos <= 0) {
                long newShort = -lot.pos + fill.qty();
                lot.avgPrice = (lot.avgPrice * (double) (-lot.pos)
                        + price * (double) fill.qty()) / (double) newShort;
                lot.pos -= fill.qty();
            } else {
                long closed = Math.min(fill.qty(), lot.pos);
                realized += (price - lot.avgPrice) * (double) closed;
                lot.pos -= fill.qty();
                if (lot.pos < 0) {
                    lot.avgPrice = price;
                }
            }
        }
        long signed = fill.side() == 0 ? fill.qty() : -fill.qty();
        positions.merge(fill.instrumentId(), signed, Long::sum);
        realizedByStrategy.merge(fill.strategyId(), realized, Double::sum);
        realizedGlobal += realized;
        metrics.gauge("risk_realized_pnl").set(realizedGlobal);
        // resting order reduction
        if (fill.orderId() != 0) {
            RestingOrder r = resting.get(fill.orderId());
            if (r != null) {
                r.qty -= fill.qty();
                if (r.qty <= 0) {
                    resting.remove(fill.orderId());
                }
            }
        }
        // loss limits (strategy first, then global) — latch via kill state
        if (limits == null) {
            return;
        }
        double stratLoss = limits.strategyMaxDailyLoss();
        double dailyLoss = limits.maxDailyLoss();
        double stratPnl = realizedByStrategy.get(fill.strategyId());
        if (stratPnl <= -stratLoss && !strategyKilled(fill.strategyId())) {
            killStrategies.put(fill.strategyId(), true);
            emit(new RiskEvent(fill.ts(), Scope.STRATEGY, fill.strategyId(),
                    Rules.STRATEGY_LOSS, Severity.BREACH.code(),
                    Decision.KILL.code(),
                    String.format(Locale.ROOT,
                            "strategy realized pnl %.2f breaches loss limit %.2f",
                            stratPnl, stratLoss)));
        }
        if (realizedGlobal <= -dailyLoss && !killGlobal) {
            setKillGlobal(true);
            emit(new RiskEvent(fill.ts(), Scope.GLOBAL, "", Rules.DAILY_LOSS,
                    Severity.BREACH.code(), Decision.KILL.code(),
                    String.format(Locale.ROOT,
                            "global realized pnl %.2f breaches daily loss limit %.2f",
                            realizedGlobal, dailyLoss)));
        }
    }

    // ------------------------------------------------------------ state reads

    private boolean strategyKilled(String sid) {
        return killStrategies.getOrDefault(sid, false);
    }

    /** Aggregate position of an instrument. */
    public long position(long instrumentId) {
        return positions.getOrDefault(instrumentId, 0L);
    }

    /** Firm-wide realized PnL. */
    public double realizedPnl() {
        return realizedGlobal;
    }

    /** One strategy's realized PnL. */
    public double strategyPnl(String sid) {
        return realizedByStrategy.getOrDefault(sid, 0.0);
    }

    /** True when the global kill switch is engaged. */
    public boolean killSwitchEngaged() {
        return killGlobal;
    }

    /** The audit log so far (immutable view). */
    public List<RiskEvent> audit() {
        return List.copyOf(audit);
    }

    /** Full audit log as JSONL (one RiskEvent per line, trailing newline). */
    public String auditJsonl() {
        StringBuilder sb = new StringBuilder(audit.size() * 96);
        for (RiskEvent ev : audit) {
            sb.append(ev.toJsonLine()).append('\n');
        }
        return sb.toString();
    }

    private void emit(RiskEvent ev) {
        metrics.counter("risk_events_total").inc();
        audit.add(ev);
    }

    // ------------------------------------------------------- pre-trade path

    /**
     * Run the pinned pre-trade check sequence for one order. Emits the
     * decision as a RiskEvent and returns it.
     */
    public RiskDecision checkOrder(OrderRequest order) {
        RiskDecision outcome = evaluate(order);
        Scope scope = decisionScope(order, outcome.ruleId());
        String scopeId = decisionScopeId(order, outcome.ruleId(), scope);
        metrics.counter("risk_decisions_total").inc();
        if (outcome.allowed()) {
            metrics.counter("risk_allowed_total").inc();
        } else {
            metrics.counter("risk_rejected_total").inc();
        }
        emit(new RiskEvent(order.timestamp(), scope, scopeId, outcome.ruleId(),
                outcome.severity().code(), outcome.decision().code(),
                outcome.reason()));
        // track allowed LIMIT orders as resting (self-match / projections)
        if (outcome.allowed() && order.orderType() == OrderRequest.LIMIT) {
            RestingOrder r = new RestingOrder();
            r.instrumentId = order.instrumentId();
            r.side = order.side();
            r.priceTicks = order.priceTicks();
            r.qty = order.qty();
            resting.put(order.orderId(), r);
        }
        return outcome;
    }

    private Scope decisionScope(OrderRequest order, String ruleId) {
        return switch (ruleId) {
            case Rules.KILL_GLOBAL, Rules.GROSS_NOTIONAL, Rules.NET_NOTIONAL,
                    Rules.DAILY_LOSS, Rules.CONFIG_MISSING -> Scope.GLOBAL;
            case Rules.KILL_STRATEGY, Rules.MALFORMED_ORDER,
                    Rules.DUPLICATE_ORDER_ID, Rules.RATE_THROTTLE,
                    Rules.STRATEGY_LOSS, Rules.ALLOW -> Scope.STRATEGY;
            case Rules.KILL_VENUE, Rules.VENUE_DISCONNECTED -> Scope.VENUE;
            default -> Scope.INSTRUMENT;
        };
    }

    private String decisionScopeId(OrderRequest order, String ruleId, Scope scope) {
        return switch (scope) {
            case GLOBAL -> "";
            case STRATEGY -> order.strategyId();
            case VENUE -> Integer.toString(order.venueId());
            case INSTRUMENT -> Long.toString(order.instrumentId());
        };
    }

    private static RiskDecision reject(String ruleId, Severity severity,
            String reason) {
        return new RiskDecision(Decision.REJECT, ruleId, severity, reason);
    }

    private RiskDecision evaluate(OrderRequest order) {
        // 0. fail-closed configuration
        if (limits == null) {
            return reject(Rules.CONFIG_MISSING, Severity.BREACH,
                    "fail-closed: " + configError);
        }
        // 1-4. kill switches, global > strategy > instrument > venue
        if (killGlobal) {
            return reject(Rules.KILL_GLOBAL, Severity.BREACH,
                    "global kill switch engaged");
        }
        if (strategyKilled(order.strategyId())) {
            return reject(Rules.KILL_STRATEGY, Severity.BREACH,
                    "strategy " + order.strategyId() + " kill switch engaged");
        }
        if (killInstruments.getOrDefault(order.instrumentId(), false)) {
            return reject(Rules.KILL_INSTRUMENT, Severity.BREACH,
                    "instrument " + order.instrumentId() + " kill switch engaged");
        }
        if (order.venueId() != 0
                && killVenues.getOrDefault(order.venueId(), false)) {
            return reject(Rules.KILL_VENUE, Severity.BREACH,
                    "venue " + order.venueId() + " kill switch engaged");
        }
        // 5. schema-level validation
        String invalid = order.validationError();
        if (invalid != null) {
            return reject(Rules.MALFORMED_ORDER, Severity.WARN, invalid);
        }
        // 6. reference data
        Double tickBox = ticks.get(order.instrumentId());
        if (tickBox == null) {
            return reject(Rules.UNKNOWN_INSTRUMENT, Severity.WARN,
                    "no tick_size for instrument " + order.instrumentId());
        }
        double tick = tickBox;
        // 7. duplicate order id
        Long prevTs = seenOrders.get(order.orderId());
        if (prevTs != null) {
            long window = limits.duplicateOrderWindowNs();
            if (window == 0 || order.timestamp() - prevTs <= window) {
                return reject(Rules.DUPLICATE_ORDER_ID, Severity.WARN,
                        "order_id " + Long.toUnsignedString(order.orderId())
                                + " already used at ts " + prevTs);
            }
        }
        seenOrders.put(order.orderId(), order.timestamp());
        // 8. venue connectivity
        if (order.venueId() != 0
                && venuesDown.getOrDefault(order.venueId(), false)) {
            return reject(Rules.VENUE_DISCONNECTED, Severity.WARN,
                    "venue " + order.venueId() + " is disconnected");
        }
        // 9-10. market-data gate
        MarketState md = market.get(order.instrumentId());
        if (md != null && md.gated) {
            return reject(Rules.SEQUENCE_GAP, Severity.WARN,
                    "instrument " + order.instrumentId()
                            + " feed has an unrecovered gap");
        }
        double mid;
        if (md != null && md.bidTicks > 0 && md.askTicks > 0) {
            long age = order.timestamp() - md.ts;
            if (limits.staleBookReject() && age > limits.staleFeedTimeoutNs()) {
                return reject(Rules.STALE_PRICE, Severity.WARN,
                        "reference price age " + age + "ns exceeds "
                                + limits.staleFeedTimeoutNs() + "ns");
            }
            mid = (double) (md.bidTicks + md.askTicks) * tick / 2.0;
        } else {
            return reject(Rules.STALE_PRICE, Severity.WARN,
                    "no reference price for instrument " + order.instrumentId());
        }
        // 11. fat-finger quantity
        if (order.qty() > limits.maxOrderQty()) {
            return reject(Rules.FAT_FINGER_QTY, Severity.WARN,
                    "qty " + order.qty() + " exceeds max_order_qty "
                            + limits.maxOrderQty());
        }
        // 12. fat-finger notional (priced orders use the limit price,
        // unpriced the mid)
        double refPrice = order.priceTicks() > 0
                ? (double) order.priceTicks() * tick : mid;
        double orderNotional = (double) order.qty() * refPrice;
        if (orderNotional > limits.maxOrderNotional()) {
            return reject(Rules.FAT_FINGER_NOTIONAL, Severity.WARN,
                    String.format(Locale.ROOT,
                            "notional %.2f exceeds max_order_notional %.2f",
                            orderNotional, limits.maxOrderNotional()));
        }
        // 13. price band (priced orders only)
        if (order.priceTicks() > 0) {
            double devBps = Math.abs((double) order.priceTicks() * tick - mid)
                    / mid * 1e4;
            if (devBps > limits.priceBandBps()) {
                return reject(Rules.PRICE_BAND, Severity.WARN,
                        String.format(Locale.ROOT,
                                "price deviates %.1fbps from mid, band %.1fbps",
                                devBps, limits.priceBandBps()));
            }
        }
        // 14. order-rate throttle (event-time token bucket per strategy)
        {
            Bucket bucket = buckets.computeIfAbsent(order.strategyId(), k -> {
                Bucket b = new Bucket();
                b.tokens = limits.orderRateBurst();
                b.lastTs = order.timestamp();
                b.primed = true;
                return b;
            });
            if (!bucket.primed) {
                bucket.tokens = limits.orderRateBurst();
                bucket.primed = true;
                bucket.lastTs = order.timestamp();
            }
            long elapsed = Math.max(order.timestamp() - bucket.lastTs, 0);
            bucket.tokens = Math.min(bucket.tokens
                    + (double) elapsed * limits.maxOrderRatePerSec() / NS_PER_SEC,
                    limits.orderRateBurst());
            bucket.lastTs = order.timestamp();
            if (bucket.tokens < 1.0) {
                return reject(Rules.RATE_THROTTLE, Severity.WARN,
                        "strategy " + order.strategyId() + " exceeded "
                                + MetricsRegistry.num(limits.maxOrderRatePerSec())
                                + " orders/s (burst "
                                + MetricsRegistry.num(limits.orderRateBurst())
                                + ")");
            }
            bucket.tokens -= 1.0;
        }
        // 15. self-match prevention
        boolean priced = order.priceTicks() > 0;
        boolean isPeg = order.orderType() == OrderRequest.PEG;
        if (!isPeg) {
            for (Map.Entry<Long, RestingOrder> e : resting.entrySet()) {
                RestingOrder r = e.getValue();
                if (r.instrumentId != order.instrumentId()
                        || r.side == order.side()) {
                    continue;
                }
                boolean crosses;
                if (priced) {
                    crosses = order.side() == 0
                            ? order.priceTicks() >= r.priceTicks
                            : order.priceTicks() <= r.priceTicks;
                } else {
                    crosses = true; // unpriced marketable vs any own opposite
                }
                if (crosses) {
                    return reject(Rules.SELF_MATCH, Severity.WARN,
                            "would cross own resting order "
                                    + Long.toUnsignedString(e.getKey())
                                    + " at " + r.priceTicks);
                }
            }
        }
        // 16. position limit (worst-case projection incl. open orders)
        long pos = position(order.instrumentId());
        long openSame = 0;
        for (RestingOrder r : resting.values()) {
            if (r.instrumentId == order.instrumentId()
                    && r.side == order.side()) {
                openSame += r.qty;
            }
        }
        long projected = order.side() == 0
                ? pos + openSame + order.qty()
                : pos - openSame - order.qty();
        if (Math.abs(projected) > limits.maxPositionQty()) {
            return reject(Rules.POSITION_LIMIT, Severity.WARN,
                    "projected position " + projected
                            + " exceeds max_position_qty "
                            + limits.maxPositionQty());
        }
        // 17. per-instrument notional (projection marked at the mid)
        double projectedNotional = (double) Math.abs(projected) * mid;
        if (projectedNotional > limits.maxInstrumentNotional()) {
            return reject(Rules.INSTRUMENT_NOTIONAL, Severity.WARN,
                    String.format(Locale.ROOT,
                            "projected notional %.2f exceeds max_instrument_notional %.2f",
                            projectedNotional, limits.maxInstrumentNotional()));
        }
        // 18-19. gross / net notional (filled positions + this order;
        // fail-closed on unmarked positions)
        double gross = 0.0;
        double net = 0.0;
        for (Map.Entry<Long, Long> e : positions.entrySet()) {
            long p = e.getValue();
            if (p == 0) {
                continue;
            }
            Double mark = markMid(e.getKey());
            if (mark == null) {
                return reject(Rules.GROSS_NOTIONAL, Severity.WARN,
                        "position in instrument " + e.getKey()
                                + " has no mark price (fail-closed)");
            }
            gross += Math.abs((double) p * mark);
            net += (double) p * mark;
        }
        gross += orderNotional;
        if (gross > limits.maxGrossNotional()) {
            return reject(Rules.GROSS_NOTIONAL, Severity.WARN,
                    String.format(Locale.ROOT,
                            "projected gross notional %.2f exceeds max_gross_notional %.2f",
                            gross, limits.maxGrossNotional()));
        }
        net += order.side() == 0 ? orderNotional : -orderNotional;
        if (Math.abs(net) > limits.maxNetNotional()) {
            return reject(Rules.NET_NOTIONAL, Severity.WARN,
                    String.format(Locale.ROOT,
                            "projected net notional %.2f exceeds max_net_notional %.2f",
                            net, limits.maxNetNotional()));
        }
        // 20-21. loss limits (belt-and-braces: the kill normally engaged at
        // fill time already rejected at check 1/2)
        if (realizedGlobal <= -limits.maxDailyLoss()) {
            return reject(Rules.DAILY_LOSS, Severity.BREACH,
                    String.format(Locale.ROOT,
                            "global realized pnl %.2f at daily loss limit",
                            realizedGlobal));
        }
        double stratPnl = strategyPnl(order.strategyId());
        if (stratPnl <= -limits.strategyMaxDailyLoss()) {
            return reject(Rules.STRATEGY_LOSS, Severity.BREACH,
                    String.format(Locale.ROOT,
                            "strategy realized pnl %.2f at loss limit", stratPnl));
        }
        return new RiskDecision(Decision.ALLOW, Rules.ALLOW, Severity.INFO, "");
    }

    private Double markMid(long instrumentId) {
        MarketState md = market.get(instrumentId);
        if (md == null || md.bidTicks <= 0 || md.askTicks <= 0) {
            return null;
        }
        Double tick = ticks.get(instrumentId);
        if (tick == null) {
            return null;
        }
        return (double) (md.bidTicks + md.askTicks) * tick / 2.0;
    }
}
