package com.iap.risk;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

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
    static final double NS_PER_SEC = 1e9;
    /** Snapshot schema version. */
    public static final long SNAPSHOT_VERSION = 1;

    // Package-private (not private): LimitsEvaluator, KillSwitch and
    // RiskAudit in this same package continue RiskEngine's implementation
    // and read/write this state directly — the Java analogue of the Rust
    // port's `pub(crate)` split (rust/risk/src/{limits_eval,killswitch,audit}.rs).
    static final class MarketState {
        long bidTicks;
        long askTicks;
        long ts;
        long gaps;
        boolean gated;
    }

    static final class OpenOrder {
        long instrumentId;
        int side;
        long priceTicks; // 0 = unpriced
        long qty;
    }

    static final class Bucket {
        double tokens;
        long lastTs;
        boolean primed;
    }

    static final class Lot {
        long pos;
        double avgPrice; // real price per base unit (quote ccy)
    }

    RiskLimits limits; // null = fail-closed
    String configError = "";
    final TreeMap<Long, InstrumentRef> instruments;
    boolean bootstrapped = true;
    boolean killGlobal;
    final TreeMap<String, Boolean> killStrategies = new TreeMap<>();
    final TreeMap<Long, Boolean> killInstruments = new TreeMap<>();
    final TreeMap<Integer, Boolean> killVenues = new TreeMap<>();
    final TreeMap<Integer, Boolean> venuesDown = new TreeMap<>();
    final TreeMap<Long, MarketState> market = new TreeMap<>();
    // Order ids are u64: unsigned ordering so iteration (and therefore the
    // named order in SELF_MATCH reasons) matches the Rust BTreeMap<u64>.
    final TreeMap<Long, Long> seenOrders =
            new TreeMap<>(Long::compareUnsigned);
    final TreeMap<String, Bucket> buckets = new TreeMap<>();
    final TreeMap<Long, OpenOrder> open =
            new TreeMap<>(Long::compareUnsigned);
    final TreeMap<Long, Long> positions = new TreeMap<>();
    /** (strategy, instrument) lots — key order = Rust (String, u32) tuple order. */
    final TreeMap<String, TreeMap<Long, Lot>> lots = new TreeMap<>();
    /** Realized P&amp;L per (strategy, quote ccy) in the quote currency. */
    final TreeMap<String, TreeMap<String, Double>> realized = new TreeMap<>();
    Double lossOverrideGlobal;
    final TreeMap<String, Double> lossOverrideStrategy = new TreeMap<>();
    final List<RiskEvent> audit = new ArrayList<>();
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
     * Build from a parsed {@code configs/risk/risk.json} document: a parse
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

    // ------------------------------------------------- checked arithmetic

    /**
     * {@code a + b} inside the SYMMETRIC i64 domain
     * {@code [-Long.MAX_VALUE, Long.MAX_VALUE]}: {@code null} on overflow
     * and on {@code Long.MIN_VALUE}, so a later negation / {@code abs} of
     * the result can never overflow either ({@code risk::engine::pos_add}).
     */
    static Long posAdd(long a, long b) {
        try {
            long r = Math.addExact(a, b);
            return r == Long.MIN_VALUE ? null : r;
        } catch (ArithmeticException ex) {
            return null;
        }
    }

    /** {@code a - b} inside the symmetric i64 domain (see {@link #posAdd}). */
    static Long posSub(long a, long b) {
        try {
            long r = Math.subtractExact(a, b);
            return r == Long.MIN_VALUE ? null : r;
        } catch (ArithmeticException ex) {
            return null;
        }
    }

    /** {@code bid + ask} as an i64, {@code null} on overflow. */
    static Long sumTicks(long bidTicks, long askTicks) {
        try {
            return Math.addExact(bidTicks, askTicks);
        } catch (ArithmeticException ex) {
            return null;
        }
    }

    /** Saturating i64 add (Rust {@code i64::saturating_add}). */
    static long satAdd(long a, long b) {
        try {
            return Math.addExact(a, b);
        } catch (ArithmeticException ex) {
            return b < 0 ? Long.MIN_VALUE : Long.MAX_VALUE;
        }
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
        KillSwitch.onVenueDisconnect(this, venueId, ts);
    }

    /** Venue reconnect. */
    public void onVenueReconnect(int venueId, long ts) {
        KillSwitch.onVenueReconnect(this, venueId, ts);
    }

    /**
     * Manually engage a kill switch. Throws {@link IllegalArgumentException}
     * (changing nothing in the requested scope, emitting
     * {@code MALFORMED_KILL} instead of {@code KILL_SWITCH_ENGAGED}) when
     * {@code scopeId} does not parse.
     *
     * <p>SILENT NO-OP defect: an unparseable scope id used to leave the
     * engine untouched while the audit log recorded a convincing
     * KILL_SWITCH_ENGAGED, so an operator halting an instrument by ticker
     * ("AAPL") believed the halt was in force and the next order was
     * ALLOWed. Fail-closed: the operator's intent is to STOP trading and the
     * narrow scope is undeterminable, so the engine takes the wider safe
     * interpretation and latches the GLOBAL kill, then reports the failure
     * loudly. Over-halting is recoverable; a phantom halt is not.
     */
    public void engageKill(Scope scope, String scopeId, long ts, String reason) {
        KillSwitch.engageKill(this, scope, scopeId, ts, reason);
    }

    /**
     * Clear a kill switch (the switch only — see the re-arm precedence).
     * Throws {@link IllegalArgumentException} (clearing NOTHING and emitting
     * {@code MALFORMED_KILL} instead of {@code KILL_SWITCH_CLEARED}) when
     * {@code scopeId} does not parse: clearing is the permissive direction,
     * so an unresolvable scope leaves every switch exactly as it was.
     */
    public void clearKill(Scope scope, String scopeId, long ts, String reason) {
        KillSwitch.clearKill(this, scope, scopeId, ts, reason);
    }

    /**
     * Raise (or lower) the effective daily loss limit of the GLOBAL or a
     * STRATEGY scope with written approval. Audited; never clears a latched
     * kill switch. Throws on a non-positive/non-finite limit or an
     * unsupported scope (nothing changes).
     */
    public void overrideLossLimit(Scope scope, String scopeId, double newLimit,
            long ts, String approver) {
        KillSwitch.overrideLossLimit(this, scope, scopeId, newLimit, ts, approver);
    }

    /**
     * Session roll: realized P&amp;L zeroed, every marked lot re-based to
     * its mark, loss-limit overrides cleared. Kill switches, positions,
     * open orders and seen order ids are untouched. Audited.
     */
    public void rollSession(long ts, String reason) {
        KillSwitch.rollSession(this, ts, reason);
    }

    /**
     * Apply a kill-switch change. Returns {@code false} (changing NOTHING)
     * when {@code scopeId} does not name a scope this engine can address —
     * an INSTRUMENT id that is not a u32 or a VENUE id that is not a u16.
     * Callers MUST act on {@code false}: a silently dropped kill is the
     * defect this return value exists to prevent. Package-private: shared
     * with {@link KillSwitch} and the loss-limit latch in
     * {@link #evaluateLossLimits}.
     */
    boolean setKill(Scope scope, String scopeId, boolean engaged) {
        return KillSwitch.setKill(this, scope, scopeId, engaged);
    }

    void setKillGlobal(boolean engaged) {
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
        // Checked position accounting, BEFORE anything is applied: a fill
        // that would take the strategy lot or the aggregate position out of
        // the symmetric i64 domain cannot be booked, and an engine that
        // cannot book a fill no longer knows its exposure. Latch the GLOBAL
        // kill (long arithmetic used to wrap silently here).
        long signed = fill.side() == 0 ? fill.qty() : -fill.qty();
        TreeMap<Long, Lot> heldLots = lots.get(fill.strategyId());
        Lot held = heldLots == null ? null : heldLots.get(fill.instrumentId());
        long lotPos = held == null ? 0 : held.pos;
        if (lotPos == Long.MIN_VALUE
                || posAdd(lotPos, signed) == null
                || posAdd(position(fill.instrumentId()), signed) == null) {
            engageKill(Scope.GLOBAL, "", fill.ts(), "fill for order "
                    + Long.toUnsignedString(fill.orderId())
                    + " overflows i64 position accounting (fail-closed)");
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
        positions.merge(fill.instrumentId(), signed, Long::sum);
        realized.computeIfAbsent(fill.strategyId(), k -> new TreeMap<>())
                .merge(ins.quoteCcy(), realizedPnl, Double::sum);
        // open order reduction
        if (fill.orderId() != 0) {
            OpenOrder r = open.get(fill.orderId());
            if (r != null) {
                Long left = posSub(r.qty, fill.qty());
                if (left != null && left > 0) {
                    r.qty = left;
                } else {
                    open.remove(fill.orderId());
                }
            }
        }
        evaluateLossLimits(fill.ts(), List.of(fill.strategyId()));
        return true;
    }

    // ------------------------------------------------------------- money

    /** {rate, markTs}: quote ccy to reporting ccy; null when missing. */
    double[] fxRate(String ccy) {
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
        Long sumTicks = sumTicks(md.bidTicks, md.askTicks);
        if (sumTicks == null) {
            return null;
        }
        double mid = (double) sumTicks.longValue() * pair.tickSize() / 2.0;
        if (mid <= 0.0) {
            return null;
        }
        return new double[] {conv.invert() ? 1.0 / mid : mid, (double) md.ts};
    }

    /** Whether a rate carries a mark time (non-reporting currency). */
    static boolean hasMarkTs(double[] rate) {
        return !Double.isNaN(rate[1]);
    }

    long fxMarkTs(String ccy) {
        // exact mark timestamp (the double slot only signals presence)
        RiskLimits.FxConversion conv = limits.fxConversion().get(ccy);
        return market.get(conv.instrumentId()).ts;
    }

    /**
     * Last consolidated mid as a real price (quote ccy), if two-sided
     * ({@code null} too when {@code bid + ask} leaves i64: no usable mark).
     */
    Double markPrice(long instrumentId) {
        MarketState md = market.get(instrumentId);
        if (md == null || md.bidTicks <= 0 || md.askTicks <= 0) {
            return null;
        }
        InstrumentRef ins = instruments.get(instrumentId);
        if (ins == null) {
            return null;
        }
        Long sumTicks = sumTicks(md.bidTicks, md.askTicks);
        if (sumTicks == null) {
            return null;
        }
        return (double) sumTicks.longValue() * ins.tickSize() / 2.0;
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
                    // FAIL-OPEN defect: skipping an unmarked held lot let its
                    // unrealized P&L count as zero, so a loss limit could fail
                    // to trip on a book that is only partly valuable. An
                    // unmarked lot makes the daily total undeterminable,
                    // exactly like the missing FX rate below — null rejects,
                    // it does not guess.
                    return null;
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

    double effectiveStrategyLoss(String sid) {
        Double o = lossOverrideStrategy.get(sid);
        return o == null ? limits.strategyMaxDailyLoss() : o;
    }

    double effectiveGlobalLoss() {
        return lossOverrideGlobal == null ? limits.maxDailyLoss() : lossOverrideGlobal;
    }

    void refreshPnlGauges() {
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

    boolean strategyKilled(String sid) {
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
        return RiskAudit.auditJsonl(this);
    }

    void emit(RiskEvent ev) {
        RiskAudit.emit(this, ev);
    }

    // ------------------------------------------------------- pre-trade path

    /**
     * Run the pinned pre-trade check sequence for one order. Emits the
     * decision as a RiskEvent and returns it.
     */
    public RiskDecision checkOrder(OrderRequest order) {
        RiskDecision outcome = LimitsEvaluator.evaluate(this, order);
        Scope scope = LimitsEvaluator.decisionScope(outcome.ruleId());
        String scopeId = LimitsEvaluator.decisionScopeId(order, scope);
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
    long trackedPrice(OrderRequest order) {
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

    // `decisionScope`, `decisionScopeId`, `reject`, `pretradeRate` and the
    // pinned check-order sequence `evaluate` (rules 0-22) live in
    // LimitsEvaluator.java.

    // ---------------------------------------------------- snapshot/restore
    // JSON serialization helpers, `snapshot()` and `restore(...)` live in
    // RiskAudit.java.

    /**
     * Serialize the full mutable state as a schema-versioned JSON document
     * (same field set and semantics as the Rust {@code snapshot()}; keys
     * sorted; doubles via {@code Double.toString}, exact on round trip).
     * The audit log and metrics are not part of the snapshot.
     */
    public String snapshot() {
        return RiskAudit.snapshot(this);
    }

    /**
     * Rebuild an engine from limits, reference data and a parsed
     * {@link #snapshot} document (strict: unknown version or a malformed
     * field throws, nothing is restored). Emits {@code STATE_RESTORED}.
     */
    public static RiskEngine restore(RiskLimits limits,
            Map<Long, InstrumentRef> instruments, Map<String, Object> snap,
            long ts, MetricsRegistry metrics) {
        return RiskAudit.restore(limits, instruments, snap, ts, metrics);
    }

    /** {@link #restore(RiskLimits, Map, Map, long, MetricsRegistry)} with a fresh registry. */
    public static RiskEngine restore(RiskLimits limits,
            Map<Long, InstrumentRef> instruments, Map<String, Object> snap, long ts) {
        return restore(limits, instruments, snap, ts, new MetricsRegistry());
    }
}
