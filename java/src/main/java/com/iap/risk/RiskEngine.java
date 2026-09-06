package com.iap.risk;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

import com.iap.config.Json;
import com.iap.monitoring.MetricsRegistry;

/**
 * The FAIL-CLOSED hard risk engine (spec §16, PLATFORM_CONVENTIONS.md §11)
 * — production Java port of the normative Rust reference
 * ({@code rust/risk/src/engine.rs}); it must reproduce
 * {@code tests/golden/expected_risk_decisions.json} exactly, the audit
 * golden {@code expected_risk_audit.jsonl} byte for byte, and restore
 * {@code expected_risk_snapshot.json}.
 *
 * <p>Pre-trade checks run in a PINNED order — the first failing rule
 * decides and is emitted as the decision's {@code rule_id}:
 * <ol start="0">
 *   <li>CONFIG_MISSING / NOT_BOOTSTRAPPED</li>
 *   <li>KILL_GLOBAL</li> <li>KILL_STRATEGY</li> <li>KILL_INSTRUMENT</li>
 *   <li>KILL_VENUE</li> <li>MALFORMED_ORDER</li> <li>UNKNOWN_INSTRUMENT</li>
 *   <li>DUPLICATE_ORDER_ID</li> <li>VENUE_DISCONNECTED</li>
 *   <li>SEQUENCE_GAP</li> <li>STALE_PRICE</li> <li>FAT_FINGER_QTY</li>
 *   <li>FX_RATE_MISSING</li> <li>FAT_FINGER_NOTIONAL</li> <li>PRICE_BAND</li>
 *   <li>RATE_THROTTLE</li> <li>SELF_MATCH</li> <li>POSITION_LIMIT</li>
 *   <li>INSTRUMENT_NOTIONAL</li> <li>GROSS_NOTIONAL</li> <li>NET_NOTIONAL</li>
 *   <li>DAILY_LOSS</li> <li>STRATEGY_LOSS</li>
 * </ol>
 * — else ALLOW.
 *
 * <p>Pinned semantics (the Rust module docs are normative; replicated):
 * fail-closed on any configuration error; every notional and P&amp;L
 * figure in the reporting currency ({@code notional = qty * qty_unit *
 * price * fx_rate}, rate = last mid of the conversion pair, fresh
 * pre-trade else FX_RATE_MISSING); reference price = last consolidated
 * mid stamped with the market-data event time (older updates dropped +
 * counted); per-strategy event-time token bucket whose clock never moves
 * backwards; EVERY allowed order tracked as open until
 * {@link #onOrderDone} or a full fill (PEG at its pegged touch, MARKET /
 * unpriced IOC/FOK / MID unpriced) — the OMS MUST call
 * {@link #onOrderDone} for every terminal execution report; firm-wide
 * (any strategy, any venue) self-match prevention, unpriced orders
 * conservative; worst-case position projection and gross/net including
 * every open order; loss limits on daily P&amp;L = realized (average
 * cost) + unrealized (mark-to-market at the last mid) evaluated on fills
 * AND on mark updates of held instruments (a mark move latches a kill
 * with no fill); re-arm precedence {@code clear_kill} (switch only),
 * {@link #overrideLossLimit} (limit only, audited), {@link #rollSession}
 * (P&amp;L re-based, switches untouched); malformed fills audited and
 * dropped; {@link #snapshot} / {@link #restore} of the full mutable
 * state; money in audit reasons formatted by {@link #fmtFixed} (integer
 * scaled, never floating-point formatting) so logs are byte-identical to
 * the Rust engine.
 */
public final class RiskEngine {
    private static final double NS_PER_SEC = 1e9;
    /** Snapshot schema version. */
    public static final long SNAPSHOT_VERSION = 1;

    private static final class MarketState {
        long bidTicks;
        long askTicks;
        long ts;
        long gaps;
        boolean gated;
    }

    private static final class OpenOrder {
        long instrumentId;
        int side;
        long priceTicks; // 0 = unpriced
        long qty;
    }

    private static final class Bucket {
        double tokens;
        long lastTs;
        boolean primed;
    }

    private static final class Lot {
        long pos;
        double avgPrice; // real price per base unit (quote ccy)
    }

    private RiskLimits limits; // null = fail-closed
    private String configError = "";
    private final TreeMap<Long, InstrumentRef> instruments;
    private boolean bootstrapped = true;
    private boolean killGlobal;
    private final TreeMap<String, Boolean> killStrategies = new TreeMap<>();
    private final TreeMap<Long, Boolean> killInstruments = new TreeMap<>();
    private final TreeMap<Integer, Boolean> killVenues = new TreeMap<>();
    private final TreeMap<Integer, Boolean> venuesDown = new TreeMap<>();
    private final TreeMap<Long, MarketState> market = new TreeMap<>();
    // Order ids are u64: unsigned ordering so iteration (and therefore the
    // named order in SELF_MATCH reasons) matches the Rust BTreeMap<u64>.
    private final TreeMap<Long, Long> seenOrders =
            new TreeMap<>(Long::compareUnsigned);
    private final TreeMap<String, Bucket> buckets = new TreeMap<>();
    private final TreeMap<Long, OpenOrder> open =
            new TreeMap<>(Long::compareUnsigned);
    private final TreeMap<Long, Long> positions = new TreeMap<>();
    /** (strategy, instrument) lots — key order = Rust (String, u32) tuple order. */
    private final TreeMap<String, TreeMap<Long, Lot>> lots = new TreeMap<>();
    /** Realized P&amp;L per (strategy, quote ccy) in the quote currency. */
    private final TreeMap<String, TreeMap<String, Double>> realized = new TreeMap<>();
    private Double lossOverrideGlobal;
    private final TreeMap<String, Double> lossOverrideStrategy = new TreeMap<>();
    private final List<RiskEvent> audit = new ArrayList<>();
    /** Engine metrics (decision counters, PnL + kill-switch gauges). */
    public final MetricsRegistry metrics;

    /** New engine from parsed limits + per-instrument reference data. */
    public RiskEngine(RiskLimits limits, Map<Long, InstrumentRef> instruments) {
        this(limits, instruments, new MetricsRegistry());
    }

    /** New engine publishing its metrics into a shared registry. */
    public RiskEngine(RiskLimits limits, Map<Long, InstrumentRef> instruments,
            MetricsRegistry metrics) {
        this.limits = limits;
        this.instruments = new TreeMap<>(instruments);
        this.killGlobal = limits.killSwitchEngaged();
        this.metrics = metrics;
        metrics.gauge("risk_kill_switch_engaged").set(killGlobal ? 1.0 : 0.0);
    }

    /** Tick-size map to reference data (every instrument a USD equity). */
    public static TreeMap<Long, InstrumentRef> equityRefs(Map<Long, Double> ticks) {
        TreeMap<Long, InstrumentRef> out = new TreeMap<>();
        for (Map.Entry<Long, Double> e : ticks.entrySet()) {
            out.put(e.getKey(), InstrumentRef.equity(e.getValue()));
        }
        return out;
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
                1.0, 1.0, 1, 1.0, 1.0, true, 0, 1, 1.0, 1.0, 0, 1, "USD",
                new TreeMap<>()), new TreeMap<>(), metrics);
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
            Map<Long, InstrumentRef> instruments) {
        return fromConfig(doc, instruments, new MetricsRegistry());
    }

    /** {@link #fromConfig(Map, Map)} with a shared metrics registry. */
    public static RiskEngine fromConfig(Map<String, Object> doc,
            Map<Long, InstrumentRef> instruments, MetricsRegistry metrics) {
        try {
            return new RiskEngine(RiskLimits.fromJson(doc), instruments, metrics);
        } catch (RuntimeException e) {
            return failClosed(e.getMessage() == null
                    ? e.getClass().getSimpleName() : e.getMessage(), metrics);
        }
    }

    /** {@link #fromConfig(Map, Map)} with tick sizes only (USD equities). */
    public static RiskEngine fromConfigTicks(Map<String, Object> doc,
            Map<Long, Double> ticks) {
        return fromConfig(doc, equityRefs(ticks), new MetricsRegistry());
    }

    /**
     * Enter the awaiting-bootstrap state: every order rejects with
     * {@code NOT_BOOTSTRAPPED} until {@link #bootstrapPositions} or
     * {@link #restore} supplies the real positions.
     */
    public void requireBootstrap() {
        bootstrapped = false;
    }

    /** True once positions are trusted (default for a fresh engine). */
    public boolean isBootstrapped() {
        return bootstrapped;
    }

    /**
     * Apply drop-copy fills through the normal fill path (P&amp;L accounted,
     * loss limits evaluated), then mark the engine bootstrapped. Returns
     * the number of fills rejected as malformed.
     */
    public int bootstrapPositions(List<RiskFill> fills, long ts) {
        int bad = 0;
        for (RiskFill f : fills) {
            if (!onFill(f)) {
                bad++;
            }
        }
        bootstrapped = true;
        emit(new RiskEvent(ts, Scope.GLOBAL, "", Rules.BOOTSTRAP_COMPLETE,
                Severity.INFO.code(), Decision.ALLOW.code(),
                "bootstrapped from " + fills.size() + " drop-copy fills ("
                        + bad + " rejected)"));
        return bad;
    }

    // -------------------------------------------------------- formatting

    /**
     * Fixed-decimal formatting for audit reasons, PINNED for byte parity
     * with the Rust engine ({@code risk::fmt_fixed}): scale by
     * {@code 10^decimals}, round half away from zero to an integer (the
     * identical IEEE-754 product in both languages), print
     * {@code [-]int.frac}; a value rounding to zero has no sign.
     */
    public static String fmtFixed(double v, int decimals) {
        long scaleI = 1;
        for (int i = 0; i < decimals; i++) {
            scaleI *= 10;
        }
        double scale = (double) scaleI;
        long units = Math.round(Math.abs(v) * scale);
        String sign = v < 0.0 && units > 0 ? "-" : "";
        if (decimals == 0) {
            return sign + units;
        }
        String frac = Long.toString(units % scaleI);
        StringBuilder sb = new StringBuilder(24);
        sb.append(sign).append(units / scaleI).append('.');
        for (int i = frac.length(); i < decimals; i++) {
            sb.append('0');
        }
        return sb.append(frac).toString();
    }

    // -------------------------------------------------------------- state in

    /**
     * Consolidated market update (best bid/ask ticks at the market-data
     * event time). Older updates are dropped and counted. Re-evaluates the
     * loss limits of every strategy holding the instrument.
     */
    public void onMarket(long instrumentId, long bidTicks, long askTicks, long ts) {
        MarketState st = market.get(instrumentId);
        if (st == null) {
            st = new MarketState();
            st.ts = ts;
            market.put(instrumentId, st);
        }
        if (ts < st.ts) {
            metrics.counter("risk_market_regressions_dropped_total").inc();
            return;
        }
        st.bidTicks = bidTicks;
        st.askTicks = askTicks;
        st.ts = ts;
        List<String> holders = new ArrayList<>();
        for (Map.Entry<String, TreeMap<Long, Lot>> e : lots.entrySet()) {
            Lot lot = e.getValue().get(instrumentId);
            if (lot != null && lot.pos != 0) {
                holders.add(e.getKey());
            }
        }
        evaluateLossLimits(ts, holders);
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

    /** Clear a kill switch (the switch only — see the re-arm precedence). */
    public void clearKill(Scope scope, String scopeId, long ts, String reason) {
        setKill(scope, scopeId, false);
        emit(new RiskEvent(ts, scope, scopeId, Rules.KILL_SWITCH_CLEARED,
                Severity.INFO.code(), Decision.ALLOW.code(), reason));
    }

    /**
     * Raise (or lower) the effective daily loss limit of the GLOBAL or a
     * STRATEGY scope with written approval. Audited; never clears a latched
     * kill switch. Throws on a non-positive/non-finite limit or an
     * unsupported scope (nothing changes).
     */
    public void overrideLossLimit(Scope scope, String scopeId, double newLimit,
            long ts, String approver) {
        if (!(Double.isFinite(newLimit) && newLimit > 0.0)) {
            throw new IllegalArgumentException(
                    "loss limit override must be finite and > 0, got " + newLimit);
        }
        if (limits == null) {
            throw new IllegalStateException("engine is fail-closed (no limits)");
        }
        double old;
        switch (scope) {
            case GLOBAL -> {
                old = lossOverrideGlobal == null
                        ? limits.maxDailyLoss() : lossOverrideGlobal;
                lossOverrideGlobal = newLimit;
            }
            case STRATEGY -> {
                Double prev = lossOverrideStrategy.get(scopeId);
                old = prev == null ? limits.strategyMaxDailyLoss() : prev;
                lossOverrideStrategy.put(scopeId, newLimit);
            }
            default -> throw new IllegalArgumentException(
                    "loss limits exist at GLOBAL and STRATEGY scope only");
        }
        emit(new RiskEvent(ts, scope, scopeId, Rules.LOSS_LIMIT_OVERRIDE,
                Severity.WARN.code(), Decision.ALLOW.code(),
                "daily loss limit " + fmtFixed(old, 2) + " -> "
                        + fmtFixed(newLimit, 2) + " approved by " + approver));
    }

    /**
     * Session roll: realized P&amp;L zeroed, every marked lot re-based to
     * its mark, loss-limit overrides cleared. Kill switches, positions,
     * open orders and seen order ids are untouched. Audited.
     */
    public void rollSession(long ts, String reason) {
        realized.clear();
        for (TreeMap<Long, Lot> byIns : lots.values()) {
            for (Map.Entry<Long, Lot> e : byIns.entrySet()) {
                Double mark = markPrice(e.getKey());
                if (mark != null) {
                    e.getValue().avgPrice = mark;
                }
            }
        }
        lossOverrideGlobal = null;
        lossOverrideStrategy.clear();
        refreshPnlGauges();
        emit(new RiskEvent(ts, Scope.GLOBAL, "", Rules.SESSION_ROLLED,
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
                    int vid = Integer.parseInt(scopeId);
                    if (vid >= 0 && vid <= 0xFFFF) {
                        killVenues.put(vid, engaged);
                    }
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
     * A terminal order state (cancel / full fill / reject / expiry
     * downstream): stop tracking it as open. The OMS MUST call this for
     * every terminal execution report.
     */
    public void onOrderDone(long orderId) {
        open.remove(orderId);
    }

    /**
     * Apply one fill: positions, realized PnL (average-cost, pinned), open
     * order reduction, then loss-limit evaluation (strategy first, then
     * global). Returns {@code false} (and audits {@code MALFORMED_FILL})
     * when the fill is invalid or unpriceable — nothing is applied.
     */
    public boolean onFill(RiskFill fill) {
        String invalid = null;
        if (fill.qty() <= 0) {
            invalid = "qty must be > 0: " + fill.qty();
        } else if (fill.side() < 0 || fill.side() > 1) {
            invalid = "side must be 0 or 1: " + fill.side();
        } else if (fill.priceTicks() <= 0) {
            invalid = "price_ticks must be > 0: " + fill.priceTicks();
        } else if (!instruments.containsKey(fill.instrumentId())) {
            invalid = "no reference data for instrument " + fill.instrumentId();
        }
        if (invalid != null) {
            metrics.counter("risk_malformed_fills_total").inc();
            emit(new RiskEvent(fill.ts(), Scope.STRATEGY, fill.strategyId(),
                    Rules.MALFORMED_FILL, Severity.WARN.code(),
                    Decision.REJECT.code(), "fill for order "
                            + Long.toUnsignedString(fill.orderId())
                            + " rejected: " + invalid));
            return false;
        }
        InstrumentRef ins = instruments.get(fill.instrumentId());
        double unit = ins.qtyUnit();
        double price = (double) fill.priceTicks() * ins.tickSize();
        Lot lot = lots.computeIfAbsent(fill.strategyId(), k -> new TreeMap<>())
                .computeIfAbsent(fill.instrumentId(), k -> new Lot());
        double realizedPnl = 0.0;
        if (fill.side() == 0) { // buy
            if (lot.pos >= 0) {
                long newPos = lot.pos + fill.qty();
                lot.avgPrice = (lot.avgPrice * (double) lot.pos
                        + price * (double) fill.qty()) / (double) newPos;
                lot.pos = newPos;
            } else {
                long closed = Math.min(fill.qty(), -lot.pos);
                realizedPnl += (lot.avgPrice - price) * (double) closed;
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
                realizedPnl += (price - lot.avgPrice) * (double) closed;
                lot.pos -= fill.qty();
                if (lot.pos < 0) {
                    lot.avgPrice = price;
                }
            }
        }
        realizedPnl *= unit;
        long signed = fill.side() == 0 ? fill.qty() : -fill.qty();
        positions.merge(fill.instrumentId(), signed, Long::sum);
        realized.computeIfAbsent(fill.strategyId(), k -> new TreeMap<>())
                .merge(ins.quoteCcy(), realizedPnl, Double::sum);
        // open order reduction
        if (fill.orderId() != 0) {
            OpenOrder r = open.get(fill.orderId());
            if (r != null) {
                r.qty -= fill.qty();
                if (r.qty <= 0) {
                    open.remove(fill.orderId());
                }
            }
        }
        evaluateLossLimits(fill.ts(), List.of(fill.strategyId()));
        return true;
    }

    // ------------------------------------------------------------- money

    /** {rate, markTs}: quote ccy to reporting ccy; null when missing. */
    private double[] fxRate(String ccy) {
        if (limits == null) {
            return null;
        }
        if (ccy.equals(limits.reportingCcy())) {
            return new double[] {1.0, Double.NaN};
        }
        RiskLimits.FxConversion conv = limits.fxConversion().get(ccy);
        if (conv == null) {
            return null;
        }
        MarketState md = market.get(conv.instrumentId());
        if (md == null || md.bidTicks <= 0 || md.askTicks <= 0) {
            return null;
        }
        InstrumentRef pair = instruments.get(conv.instrumentId());
        if (pair == null) {
            return null;
        }
        double mid = (double) (md.bidTicks + md.askTicks) * pair.tickSize() / 2.0;
        if (mid <= 0.0) {
            return null;
        }
        return new double[] {conv.invert() ? 1.0 / mid : mid, (double) md.ts};
    }

    /** Whether a rate carries a mark time (non-reporting currency). */
    private static boolean hasMarkTs(double[] rate) {
        return !Double.isNaN(rate[1]);
    }

    private long fxMarkTs(String ccy) {
        // exact mark timestamp (the double slot only signals presence)
        RiskLimits.FxConversion conv = limits.fxConversion().get(ccy);
        return market.get(conv.instrumentId()).ts;
    }

    /** Last consolidated mid as a real price (quote ccy), if two-sided. */
    private Double markPrice(long instrumentId) {
        MarketState md = market.get(instrumentId);
        if (md == null || md.bidTicks <= 0 || md.askTicks <= 0) {
            return null;
        }
        InstrumentRef ins = instruments.get(instrumentId);
        if (ins == null) {
            return null;
        }
        return (double) (md.bidTicks + md.askTicks) * ins.tickSize() / 2.0;
    }

    /** Sum of realized (converted) + unrealized of marked lots for the given
     *  strategy (null = all). Returns null when a rate is missing. */
    private Double dailyPnl(String sid) {
        double total = 0.0;
        for (Map.Entry<String, TreeMap<String, Double>> e : realized.entrySet()) {
            if (sid != null && !e.getKey().equals(sid)) {
                continue;
            }
            for (Map.Entry<String, Double> c : e.getValue().entrySet()) {
                double[] rate = fxRate(c.getKey());
                if (rate == null) {
                    return null;
                }
                total += c.getValue() * rate[0];
            }
        }
        for (Map.Entry<String, TreeMap<Long, Lot>> e : lots.entrySet()) {
            if (sid != null && !e.getKey().equals(sid)) {
                continue;
            }
            for (Map.Entry<Long, Lot> l : e.getValue().entrySet()) {
                Lot lot = l.getValue();
                if (lot.pos == 0) {
                    continue;
                }
                Double mark = markPrice(l.getKey());
                if (mark == null) {
                    continue; // unmarked: undeterminable, contributes nothing
                }
                InstrumentRef ins = instruments.get(l.getKey());
                double[] rate = fxRate(ins.quoteCcy());
                if (rate == null) {
                    return null;
                }
                total += (double) lot.pos * (mark - lot.avgPrice) * ins.qtyUnit()
                        * rate[0];
            }
        }
        return total;
    }

    /** Daily P&amp;L of one strategy in the reporting currency (null when
     *  a conversion rate is missing). */
    public Double strategyDailyPnl(String sid) {
        return dailyPnl(sid);
    }

    /** Firm-wide daily P&amp;L in the reporting currency (null when a
     *  conversion rate is missing). */
    public Double globalDailyPnl() {
        return dailyPnl(null);
    }

    private double realizedConverted(String sid) {
        double total = 0.0;
        for (Map.Entry<String, TreeMap<String, Double>> e : realized.entrySet()) {
            if (sid != null && !e.getKey().equals(sid)) {
                continue;
            }
            for (Map.Entry<String, Double> c : e.getValue().entrySet()) {
                double[] rate = fxRate(c.getKey());
                total += c.getValue() * (rate == null ? 0.0 : rate[0]);
            }
        }
        return total;
    }

    /** Firm-wide realized P&amp;L in the reporting currency (gauge). */
    public double realizedPnl() {
        return realizedConverted(null);
    }

    /** One strategy's realized P&amp;L in the reporting currency. */
    public double strategyPnl(String sid) {
        return realizedConverted(sid);
    }

    /** Firm-wide unrealized P&amp;L in the reporting currency (gauge). */
    public double unrealizedPnl() {
        Double daily = globalDailyPnl();
        return (daily == null ? 0.0 : daily) - realizedPnl();
    }

    private double effectiveStrategyLoss(String sid) {
        Double o = lossOverrideStrategy.get(sid);
        return o == null ? limits.strategyMaxDailyLoss() : o;
    }

    private double effectiveGlobalLoss() {
        return lossOverrideGlobal == null ? limits.maxDailyLoss() : lossOverrideGlobal;
    }

    private void refreshPnlGauges() {
        double realizedNow = realizedPnl();
        Double dailyBox = globalDailyPnl();
        double daily = dailyBox == null ? realizedNow : dailyBox;
        metrics.gauge("risk_realized_pnl").set(realizedNow);
        metrics.gauge("risk_unrealized_pnl").set(daily - realizedNow);
        metrics.gauge("risk_daily_pnl").set(daily);
    }

    private void evaluateLossLimits(long ts, List<String> strategies) {
        refreshPnlGauges();
        if (limits == null) {
            return;
        }
        for (String sid : strategies) {
            if (strategyKilled(sid)) {
                continue;
            }
            Double pnl = strategyDailyPnl(sid);
            if (pnl == null) {
                continue;
            }
            double limit = effectiveStrategyLoss(sid);
            if (pnl <= -limit) {
                killStrategies.put(sid, true);
                emit(new RiskEvent(ts, Scope.STRATEGY, sid, Rules.STRATEGY_LOSS,
                        Severity.BREACH.code(), Decision.KILL.code(),
                        "strategy daily pnl " + fmtFixed(pnl, 2)
                                + " breaches loss limit " + fmtFixed(limit, 2)));
            }
        }
        if (!killGlobal) {
            Double pnl = globalDailyPnl();
            if (pnl != null) {
                double limit = effectiveGlobalLoss();
                if (pnl <= -limit) {
                    setKillGlobal(true);
                    emit(new RiskEvent(ts, Scope.GLOBAL, "", Rules.DAILY_LOSS,
                            Severity.BREACH.code(), Decision.KILL.code(),
                            "global daily pnl " + fmtFixed(pnl, 2)
                                    + " breaches daily loss limit "
                                    + fmtFixed(limit, 2)));
                }
            }
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

    /** Number of open (allowed, not yet terminal) orders tracked. */
    public int openOrderCount() {
        return open.size();
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
        Scope scope = decisionScope(outcome.ruleId());
        String scopeId = decisionScopeId(order, scope);
        metrics.counter("risk_decisions_total").inc();
        if (outcome.allowed()) {
            metrics.counter("risk_allowed_total").inc();
        } else {
            metrics.counter("risk_rejected_total").inc();
        }
        emit(new RiskEvent(order.timestamp(), scope, scopeId, outcome.ruleId(),
                outcome.severity().code(), outcome.decision().code(),
                outcome.reason()));
        // track every allowed order as open (self-match / projections)
        if (outcome.allowed()) {
            OpenOrder r = new OpenOrder();
            r.instrumentId = order.instrumentId();
            r.side = order.side();
            r.priceTicks = trackedPrice(order);
            r.qty = order.qty();
            open.put(order.orderId(), r);
        }
        return outcome;
    }

    /** Limit price, the pegged same-side touch for PEG, else 0 (unpriced). */
    private long trackedPrice(OrderRequest order) {
        if (order.priceTicks() > 0) {
            return order.priceTicks();
        }
        if (order.orderType() == OrderRequest.PEG) {
            MarketState md = market.get(order.instrumentId());
            if (md != null) {
                return order.side() == 0 ? md.bidTicks : md.askTicks;
            }
        }
        return 0;
    }

    private static Scope decisionScope(String ruleId) {
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

    private static String decisionScopeId(OrderRequest order, Scope scope) {
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

    /** Pre-trade conversion rate (fresh) or the FX_RATE_MISSING reason. */
    private Object pretradeRate(String ccy, long ts) {
        double[] rate = fxRate(ccy);
        if (rate == null) {
            return "no conversion rate for " + ccy + " -> " + limits.reportingCcy();
        }
        if (hasMarkTs(rate) && limits.staleBookReject()) {
            long age = ts - fxMarkTs(ccy);
            if (age > limits.staleFeedTimeoutNs()) {
                return "conversion rate " + ccy + " -> " + limits.reportingCcy()
                        + " age " + age + "ns exceeds "
                        + limits.staleFeedTimeoutNs() + "ns";
            }
        }
        return rate[0];
    }

    private RiskDecision evaluate(OrderRequest order) {
        // 0. fail-closed configuration / bootstrap
        if (limits == null) {
            return reject(Rules.CONFIG_MISSING, Severity.BREACH,
                    "fail-closed: " + configError);
        }
        if (!bootstrapped) {
            return reject(Rules.NOT_BOOTSTRAPPED, Severity.BREACH,
                    "positions not bootstrapped (fail-closed)");
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
        InstrumentRef ins = instruments.get(order.instrumentId());
        if (ins == null) {
            return reject(Rules.UNKNOWN_INSTRUMENT, Severity.WARN,
                    "no reference data for instrument " + order.instrumentId());
        }
        double tick = ins.tickSize();
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
        if (limits.duplicateOrderWindowNs() > 0) {
            long cutoff = order.timestamp() - limits.duplicateOrderWindowNs();
            seenOrders.values().removeIf(ts -> ts < cutoff);
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
        // 12. conversion rate to the reporting currency
        Object rateOrReason = pretradeRate(ins.quoteCcy(), order.timestamp());
        if (rateOrReason instanceof String why) {
            return reject(Rules.FX_RATE_MISSING, Severity.WARN, why);
        }
        double fx = (Double) rateOrReason;
        // 13. fat-finger notional (priced orders use the limit price,
        // unpriced the mid); notional in the reporting currency
        double refPrice = order.priceTicks() > 0
                ? (double) order.priceTicks() * tick : mid;
        double orderNotional = (double) order.qty() * ins.qtyUnit() * refPrice * fx;
        if (orderNotional > limits.maxOrderNotional()) {
            return reject(Rules.FAT_FINGER_NOTIONAL, Severity.WARN,
                    "notional " + fmtFixed(orderNotional, 2) + " "
                            + limits.reportingCcy() + " exceeds max_order_notional "
                            + fmtFixed(limits.maxOrderNotional(), 2));
        }
        // 14. price band (priced orders only)
        if (order.priceTicks() > 0) {
            double devBps = Math.abs((double) order.priceTicks() * tick - mid)
                    / mid * 1e4;
            if (devBps > limits.priceBandBps()) {
                return reject(Rules.PRICE_BAND, Severity.WARN,
                        "price deviates " + fmtFixed(devBps, 1)
                                + "bps from mid, band "
                                + fmtFixed(limits.priceBandBps(), 1) + "bps");
            }
        }
        // 15. order-rate throttle (event-time token bucket per strategy)
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
            bucket.lastTs = Math.max(bucket.lastTs, order.timestamp());
            if (bucket.tokens < 1.0) {
                return reject(Rules.RATE_THROTTLE, Severity.WARN,
                        "strategy " + order.strategyId() + " exceeded "
                                + fmtFixed(limits.maxOrderRatePerSec(), 2)
                                + " orders/s (burst "
                                + fmtFixed(limits.orderRateBurst(), 2) + ")");
            }
            bucket.tokens -= 1.0;
        }
        // 16. self-match prevention (any venue; PEG at its pegged touch)
        long myPrice = trackedPrice(order);
        for (Map.Entry<Long, OpenOrder> e : open.entrySet()) {
            OpenOrder r = e.getValue();
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
                                + Long.toUnsignedString(e.getKey())
                                + " at " + r.priceTicks);
            }
        }
        // 17. position limit (worst-case projection incl. open orders)
        long pos = position(order.instrumentId());
        long openSame = 0;
        for (OpenOrder r : open.values()) {
            if (r.instrumentId == order.instrumentId() && r.side == order.side()) {
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
        // 18. per-instrument notional (projection marked at the mid)
        double projectedNotional = (double) Math.abs(projected) * ins.qtyUnit()
                * mid * fx;
        if (projectedNotional > limits.maxInstrumentNotional()) {
            return reject(Rules.INSTRUMENT_NOTIONAL, Severity.WARN,
                    "projected notional " + fmtFixed(projectedNotional, 2)
                            + " exceeds max_instrument_notional "
                            + fmtFixed(limits.maxInstrumentNotional(), 2));
        }
        // 19-20. gross / net notional (filled positions + every open order
        // + this order; fail-closed on unmarked or unconvertible positions)
        double gross = 0.0;
        double net = 0.0;
        for (Map.Entry<Long, Long> e : positions.entrySet()) {
            long p = e.getValue();
            if (p == 0) {
                continue;
            }
            Double mark = markPrice(e.getKey());
            if (mark == null) {
                return reject(Rules.GROSS_NOTIONAL, Severity.WARN,
                        "position in instrument " + e.getKey()
                                + " has no mark price (fail-closed)");
            }
            InstrumentRef pins = instruments.get(e.getKey());
            double[] rate = fxRate(pins.quoteCcy());
            if (rate == null) {
                return reject(Rules.GROSS_NOTIONAL, Severity.WARN,
                        "position in instrument " + e.getKey() + " has no "
                                + pins.quoteCcy()
                                + " conversion rate (fail-closed)");
            }
            double v = (double) p * pins.qtyUnit() * mark * rate[0];
            gross += Math.abs(v);
            net += v;
        }
        for (OpenOrder r : open.values()) {
            InstrumentRef oins = instruments.get(r.instrumentId);
            if (oins == null) {
                continue;
            }
            double price;
            if (r.priceTicks > 0) {
                price = (double) r.priceTicks * oins.tickSize();
            } else {
                Double m = markPrice(r.instrumentId);
                if (m == null) {
                    continue; // unpriced and unmarked: cannot value
                }
                price = m;
            }
            double[] rate = fxRate(oins.quoteCcy());
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
        if (gross > limits.maxGrossNotional()) {
            return reject(Rules.GROSS_NOTIONAL, Severity.WARN,
                    "projected gross notional " + fmtFixed(gross, 2)
                            + " exceeds max_gross_notional "
                            + fmtFixed(limits.maxGrossNotional(), 2));
        }
        net += order.side() == 0 ? orderNotional : -orderNotional;
        if (Math.abs(net) > limits.maxNetNotional()) {
            return reject(Rules.NET_NOTIONAL, Severity.WARN,
                    "projected net notional " + fmtFixed(net, 2)
                            + " exceeds max_net_notional "
                            + fmtFixed(limits.maxNetNotional(), 2));
        }
        // 21-22. loss limits on daily P&L (belt-and-braces after a cleared
        // latch; undeterminable P&L rejects fail-closed)
        Double globalPnl = globalDailyPnl();
        if (globalPnl == null) {
            return reject(Rules.FX_RATE_MISSING, Severity.WARN,
                    "global daily pnl undeterminable: conversion rate missing");
        }
        double globalLimit = effectiveGlobalLoss();
        if (globalPnl <= -globalLimit) {
            return reject(Rules.DAILY_LOSS, Severity.BREACH,
                    "global daily pnl " + fmtFixed(globalPnl, 2)
                            + " at daily loss limit " + fmtFixed(globalLimit, 2));
        }
        Double stratPnl = strategyDailyPnl(order.strategyId());
        if (stratPnl == null) {
            return reject(Rules.FX_RATE_MISSING, Severity.WARN,
                    "strategy daily pnl undeterminable: conversion rate missing");
        }
        double stratLimit = effectiveStrategyLoss(order.strategyId());
        if (stratPnl <= -stratLimit) {
            return reject(Rules.STRATEGY_LOSS, Severity.BREACH,
                    "strategy daily pnl " + fmtFixed(stratPnl, 2)
                            + " at loss limit " + fmtFixed(stratLimit, 2));
        }
        return new RiskDecision(Decision.ALLOW, Rules.ALLOW, Severity.INFO, "");
    }

    // ---------------------------------------------------- snapshot/restore

    private static void jsonStr(StringBuilder sb, String s) {
        sb.append('"').append(RiskEvent.esc(s)).append('"');
    }

    private static void jsonDouble(StringBuilder sb, double v) {
        if (!Double.isFinite(v)) {
            throw new IllegalStateException("non-finite value in snapshot");
        }
        sb.append(Double.toString(v));
    }

    /**
     * Serialize the full mutable state as a schema-versioned JSON document
     * (same field set and semantics as the Rust {@code snapshot()}; keys
     * sorted; doubles via {@code Double.toString}, exact on round trip).
     * The audit log and metrics are not part of the snapshot.
     */
    public String snapshot() {
        StringBuilder sb = new StringBuilder(1024);
        sb.append("{\"bootstrapped\":").append(bootstrapped);
        sb.append(",\"buckets\":{");
        boolean first = true;
        for (Map.Entry<String, Bucket> e : buckets.entrySet()) {
            sb.append(first ? "" : ",");
            first = false;
            jsonStr(sb, e.getKey());
            sb.append(":{\"last_ts\":").append(e.getValue().lastTs)
                    .append(",\"primed\":").append(e.getValue().primed)
                    .append(",\"tokens\":");
            jsonDouble(sb, e.getValue().tokens);
            sb.append('}');
        }
        sb.append("},\"kill_global\":").append(killGlobal);
        sb.append(",\"kill_instruments\":{");
        first = true;
        for (Map.Entry<Long, Boolean> e : killInstruments.entrySet()) {
            sb.append(first ? "" : ",").append('"').append(e.getKey())
                    .append("\":").append(e.getValue());
            first = false;
        }
        sb.append("},\"kill_strategies\":{");
        first = true;
        for (Map.Entry<String, Boolean> e : killStrategies.entrySet()) {
            sb.append(first ? "" : ",");
            first = false;
            jsonStr(sb, e.getKey());
            sb.append(':').append(e.getValue());
        }
        sb.append("},\"kill_venues\":{");
        first = true;
        for (Map.Entry<Integer, Boolean> e : killVenues.entrySet()) {
            sb.append(first ? "" : ",").append('"').append(e.getKey())
                    .append("\":").append(e.getValue());
            first = false;
        }
        sb.append("},\"loss_override_global\":");
        if (lossOverrideGlobal == null) {
            sb.append("null");
        } else {
            jsonDouble(sb, lossOverrideGlobal);
        }
        sb.append(",\"loss_override_strategy\":{");
        first = true;
        for (Map.Entry<String, Double> e : lossOverrideStrategy.entrySet()) {
            sb.append(first ? "" : ",");
            first = false;
            jsonStr(sb, e.getKey());
            sb.append(':');
            jsonDouble(sb, e.getValue());
        }
        sb.append("},\"lots\":[");
        first = true;
        for (Map.Entry<String, TreeMap<Long, Lot>> s : lots.entrySet()) {
            for (Map.Entry<Long, Lot> l : s.getValue().entrySet()) {
                sb.append(first ? "" : ",");
                first = false;
                sb.append("{\"avg_price\":");
                jsonDouble(sb, l.getValue().avgPrice);
                sb.append(",\"instrument_id\":").append(l.getKey())
                        .append(",\"pos\":").append(l.getValue().pos)
                        .append(",\"strategy_id\":");
                jsonStr(sb, s.getKey());
                sb.append('}');
            }
        }
        sb.append("],\"market\":{");
        first = true;
        for (Map.Entry<Long, MarketState> e : market.entrySet()) {
            MarketState m = e.getValue();
            sb.append(first ? "" : ",").append('"').append(e.getKey())
                    .append("\":{\"ask_ticks\":").append(m.askTicks)
                    .append(",\"bid_ticks\":").append(m.bidTicks)
                    .append(",\"gaps\":").append(m.gaps)
                    .append(",\"gated\":").append(m.gated)
                    .append(",\"ts\":").append(m.ts).append('}');
            first = false;
        }
        sb.append("},\"open\":{");
        first = true;
        for (Map.Entry<Long, OpenOrder> e : open.entrySet()) {
            OpenOrder o = e.getValue();
            sb.append(first ? "" : ",").append('"')
                    .append(Long.toUnsignedString(e.getKey()))
                    .append("\":{\"instrument_id\":").append(o.instrumentId)
                    .append(",\"price_ticks\":").append(o.priceTicks)
                    .append(",\"qty\":").append(o.qty)
                    .append(",\"side\":").append(o.side).append('}');
            first = false;
        }
        sb.append("},\"positions\":{");
        first = true;
        for (Map.Entry<Long, Long> e : positions.entrySet()) {
            sb.append(first ? "" : ",").append('"').append(e.getKey())
                    .append("\":").append(e.getValue());
            first = false;
        }
        sb.append("},\"realized\":[");
        first = true;
        for (Map.Entry<String, TreeMap<String, Double>> s : realized.entrySet()) {
            for (Map.Entry<String, Double> c : s.getValue().entrySet()) {
                sb.append(first ? "" : ",");
                first = false;
                sb.append("{\"ccy\":");
                jsonStr(sb, c.getKey());
                sb.append(",\"pnl\":");
                jsonDouble(sb, c.getValue());
                sb.append(",\"strategy_id\":");
                jsonStr(sb, s.getKey());
                sb.append('}');
            }
        }
        sb.append("],\"seen_orders\":[");
        first = true;
        for (Map.Entry<Long, Long> e : seenOrders.entrySet()) {
            sb.append(first ? "" : ",").append('[')
                    .append(Long.toUnsignedString(e.getKey())).append(',')
                    .append(e.getValue()).append(']');
            first = false;
        }
        sb.append("],\"venues_down\":{");
        first = true;
        for (Map.Entry<Integer, Boolean> e : venuesDown.entrySet()) {
            sb.append(first ? "" : ",").append('"').append(e.getKey())
                    .append("\":").append(e.getValue());
            first = false;
        }
        sb.append("},\"x-version\":").append(SNAPSHOT_VERSION).append('}');
        return sb.toString();
    }

    private static IllegalArgumentException bad(String what) {
        return new IllegalArgumentException("risk snapshot: bad " + what);
    }

    private static long snapLong(Object v, String what) {
        if (!(v instanceof Long)) {
            throw bad(what);
        }
        return (Long) v;
    }

    private static boolean snapBool(Object v, String what) {
        if (!(v instanceof Boolean)) {
            throw bad(what);
        }
        return (Boolean) v;
    }

    private static double snapDouble(Object v, String what) {
        if (!(v instanceof Long) && !(v instanceof Double)) {
            throw bad(what);
        }
        double d = Json.asDouble(v);
        if (!Double.isFinite(d)) {
            throw bad(what);
        }
        return d;
    }

    private static String snapStr(Object v, String what) {
        if (!(v instanceof String)) {
            throw bad(what);
        }
        return (String) v;
    }

    private static Map<String, Object> snapObj(Object v, String what) {
        if (!(v instanceof Map)) {
            throw bad(what);
        }
        return Json.object(v);
    }

    private static List<Object> snapArr(Object v, String what) {
        if (!(v instanceof List)) {
            throw bad(what);
        }
        return Json.array(v);
    }

    /**
     * Rebuild an engine from limits, reference data and a parsed
     * {@link #snapshot} document (strict: unknown version or a malformed
     * field throws, nothing is restored). Emits {@code STATE_RESTORED}.
     */
    public static RiskEngine restore(RiskLimits limits,
            Map<Long, InstrumentRef> instruments, Map<String, Object> snap,
            long ts, MetricsRegistry metrics) {
        if (snapLong(snap.get("x-version"), "x-version") != SNAPSHOT_VERSION) {
            throw bad("x-version");
        }
        RiskEngine eng = new RiskEngine(limits, instruments, metrics);
        eng.bootstrapped = snapBool(snap.get("bootstrapped"), "bootstrapped");
        eng.setKillGlobal(snapBool(snap.get("kill_global"), "kill_global"));
        for (Map.Entry<String, Object> e
                : snapObj(snap.get("kill_strategies"), "kill_strategies").entrySet()) {
            eng.killStrategies.put(e.getKey(), snapBool(e.getValue(), "kill_strategies"));
        }
        for (Map.Entry<String, Object> e
                : snapObj(snap.get("kill_instruments"), "kill_instruments").entrySet()) {
            eng.killInstruments.put(parseU32(e.getKey(), "kill_instruments"),
                    snapBool(e.getValue(), "kill_instruments"));
        }
        for (Map.Entry<String, Object> e
                : snapObj(snap.get("kill_venues"), "kill_venues").entrySet()) {
            eng.killVenues.put(parseU16(e.getKey(), "kill_venues"),
                    snapBool(e.getValue(), "kill_venues"));
        }
        for (Map.Entry<String, Object> e
                : snapObj(snap.get("venues_down"), "venues_down").entrySet()) {
            eng.venuesDown.put(parseU16(e.getKey(), "venues_down"),
                    snapBool(e.getValue(), "venues_down"));
        }
        for (Map.Entry<String, Object> e
                : snapObj(snap.get("market"), "market").entrySet()) {
            Map<String, Object> m = snapObj(e.getValue(), "market");
            MarketState st = new MarketState();
            st.bidTicks = snapLong(m.get("bid_ticks"), "market.bid_ticks");
            st.askTicks = snapLong(m.get("ask_ticks"), "market.ask_ticks");
            st.ts = snapLong(m.get("ts"), "market.ts");
            st.gaps = snapLong(m.get("gaps"), "market.gaps");
            st.gated = snapBool(m.get("gated"), "market.gated");
            eng.market.put(parseU32(e.getKey(), "market"), st);
        }
        for (Object pair : snapArr(snap.get("seen_orders"), "seen_orders")) {
            List<Object> p = snapArr(pair, "seen_orders");
            if (p.size() != 2) {
                throw bad("seen_orders");
            }
            eng.seenOrders.put(snapLong(p.get(0), "seen_orders"),
                    snapLong(p.get(1), "seen_orders"));
        }
        for (Map.Entry<String, Object> e
                : snapObj(snap.get("buckets"), "buckets").entrySet()) {
            Map<String, Object> b = snapObj(e.getValue(), "buckets");
            Bucket bk = new Bucket();
            bk.tokens = snapDouble(b.get("tokens"), "buckets.tokens");
            bk.lastTs = snapLong(b.get("last_ts"), "buckets.last_ts");
            bk.primed = snapBool(b.get("primed"), "buckets.primed");
            eng.buckets.put(e.getKey(), bk);
        }
        for (Map.Entry<String, Object> e : snapObj(snap.get("open"), "open").entrySet()) {
            Map<String, Object> o = snapObj(e.getValue(), "open");
            OpenOrder r = new OpenOrder();
            r.instrumentId = parseU32Value(o.get("instrument_id"), "open.instrument_id");
            long side = snapLong(o.get("side"), "open.side");
            if (side < 0 || side > 1) {
                throw bad("open.side");
            }
            r.side = (int) side;
            r.priceTicks = snapLong(o.get("price_ticks"), "open.price_ticks");
            r.qty = snapLong(o.get("qty"), "open.qty");
            long id;
            try {
                id = Long.parseUnsignedLong(e.getKey());
            } catch (NumberFormatException ex) {
                throw bad("open");
            }
            eng.open.put(id, r);
        }
        for (Map.Entry<String, Object> e
                : snapObj(snap.get("positions"), "positions").entrySet()) {
            eng.positions.put(parseU32(e.getKey(), "positions"),
                    snapLong(e.getValue(), "positions"));
        }
        for (Object lv : snapArr(snap.get("lots"), "lots")) {
            Map<String, Object> l = snapObj(lv, "lots");
            Lot lot = new Lot();
            lot.pos = snapLong(l.get("pos"), "lots.pos");
            lot.avgPrice = snapDouble(l.get("avg_price"), "lots.avg_price");
            eng.lots.computeIfAbsent(snapStr(l.get("strategy_id"), "lots.strategy_id"),
                    k -> new TreeMap<>())
                    .put(parseU32Value(l.get("instrument_id"), "lots.instrument_id"), lot);
        }
        for (Object rv : snapArr(snap.get("realized"), "realized")) {
            Map<String, Object> r = snapObj(rv, "realized");
            eng.realized.computeIfAbsent(snapStr(r.get("strategy_id"),
                    "realized.strategy_id"), k -> new TreeMap<>())
                    .put(snapStr(r.get("ccy"), "realized.ccy"),
                            snapDouble(r.get("pnl"), "realized.pnl"));
        }
        Object og = snap.get("loss_override_global");
        eng.lossOverrideGlobal = og == null ? null
                : snapDouble(og, "loss_override_global");
        for (Map.Entry<String, Object> e : snapObj(snap.get("loss_override_strategy"),
                "loss_override_strategy").entrySet()) {
            eng.lossOverrideStrategy.put(e.getKey(),
                    snapDouble(e.getValue(), "loss_override_strategy"));
        }
        eng.refreshPnlGauges();
        int nPos = 0;
        for (long p : eng.positions.values()) {
            if (p != 0) {
                nPos++;
            }
        }
        eng.emit(new RiskEvent(ts, Scope.GLOBAL, "", Rules.STATE_RESTORED,
                Severity.INFO.code(), Decision.ALLOW.code(),
                "restored snapshot v" + SNAPSHOT_VERSION + ": " + nPos
                        + " positions, " + eng.open.size() + " open orders"));
        return eng;
    }

    /** {@link #restore(RiskLimits, Map, Map, long, MetricsRegistry)} with a fresh registry. */
    public static RiskEngine restore(RiskLimits limits,
            Map<Long, InstrumentRef> instruments, Map<String, Object> snap, long ts) {
        return restore(limits, instruments, snap, ts, new MetricsRegistry());
    }

    private static long parseU32(String s, String what) {
        try {
            return Integer.toUnsignedLong(Integer.parseUnsignedInt(s));
        } catch (NumberFormatException e) {
            throw bad(what);
        }
    }

    private static long parseU32Value(Object v, String what) {
        long x = snapLong(v, what);
        if (x < 0 || x > 0xFFFFFFFFL) {
            throw bad(what);
        }
        return x;
    }

    private static int parseU16(String s, String what) {
        try {
            int v = Integer.parseInt(s);
            if (v < 0 || v > 0xFFFF) {
                throw bad(what);
            }
            return v;
        } catch (NumberFormatException e) {
            throw bad(what);
        }
    }
}
