package com.iap.backtest;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

import com.iap.core.EventType;
import com.iap.core.MarketEvent;
import com.iap.core.Side;
import com.iap.execution.ChildOrder;
import com.iap.execution.ExecConfig;
import com.iap.execution.ExecutionSimulator;
import com.iap.execution.Fill;
import com.iap.execution.InstrumentSpec;
import com.iap.execution.OrderState;
import com.iap.execution.OrderType;
import com.iap.features.FeatureEngine;
import com.iap.features.FeatureVector;
import com.iap.features.Features;
import com.iap.orderbook.ConsolidatedBook;
import com.iap.orderbook.OrderBook;
import com.iap.sor.SmartOrderRouter;
import com.iap.sor.SorOptions;

/**
 * Production event-driven backtest engine (spec section 18). Replays
 * normalized events (or the golden vectors) and, per event, in pinned
 * order:
 * <ol>
 *   <li>the {@link ExecutionSimulator} processes the event (expiries,
 *       child activation, passive queue tracking, book application,
 *       crossing checks — the pinned execution rules);</li>
 *   <li>new fills are booked into the deterministic P&amp;L accounts and
 *       handed to the {@link ExecutionListener} (risk sees every fill
 *       BEFORE the next decision), then terminal children are reported
 *       ({@code onOrderTerminal});</li>
 *   <li>the listener receives the market event with the instrument's
 *       consolidated book ({@code onMarket}: reference prices are stamped
 *       with the market-data event time, never a decision clock);</li>
 *   <li>the {@link FeatureEngine} updates and emits a feature vector
 *       (cadence 0), the {@link Strategy} maps it to a target position, the
 *       execution controls ({@link ExecutionLimits}: participation caps,
 *       minimum slice interval, latency budget) and the {@link RiskHook}
 *       clamp the delta, and any resulting child order (MARKET; SOR-routed
 *       when no venue is pinned — no eligible venue = no order, counted) is
 *       submitted with decision_ts = the event's exchange_ts and reported
 *       to the listener ({@code onOrderSubmitted}) so a risk engine can map
 *       its own order id to the child and track it as open until the
 *       terminal report.</li>
 * </ol>
 *
 * <p><b>Accounting identity</b> (tested): equity changes only through mark
 * moves and fills, so per instrument
 * {@code total_pnl = gross_pnl - spread_cost - (fees - rebates) - impact}
 * where gross is mark-to-market price-move P&amp;L, spread_cost is the
 * fill-vs-prevailing-mid slippage, and fees/rebates/impact are exact sums
 * over the fills — all in the instrument's quote currency. The run
 * summary converts every equity increment into the reporting currency at
 * the prevailing rate of the {@link FxConverter} ({@code totalPnl}; no FX
 * translation P&amp;L on inventory — pinned, API_PORTFOLIO_TCA.md §4).
 *
 * <p><b>Checkpoint/restart</b>: {@link #checkpoint()} deep-copies the full
 * engine state (feature engine, simulator incl. RNG stream position and
 * in-flight orders, accounts); {@link #restore} continues from it with
 * bit-identical subsequent behavior. Strategies, risk hooks and listeners
 * are stateless by contract and are re-supplied at restore.
 *
 * <p>Deterministic: same events + config + seed = identical fills and
 * P&amp;L. No wall clock anywhere.
 */
public final class BacktestEngine {
    /** Maps an emitted feature vector to a desired signed target position. */
    @FunctionalInterface
    public interface Strategy {
        long targetPosition(FeatureVector vec);
    }

    /**
     * Pre-trade risk check. Returns the permitted signed order delta
     * (possibly clamped, 0 = reject). {@code inFlight} is the WORST-CASE
     * in-flight quantity for the proposed delta: the signed remaining qty of
     * open orders on the delta's own side (opposite-side orders may never
     * fill, so netting them would let the realized position overshoot —
     * same worst-case projection the hard risk engine pins).
     */
    @FunctionalInterface
    public interface RiskHook {
        long approve(long instrumentId, long currentPos, long inFlight,
                long proposedDelta, long decisionTs);
    }

    /**
     * Execution-path callbacks (all optional; the risk engine wiring in
     * PaperTrading implements them). Every method is invoked on the
     * deterministic path in the pinned order of {@link #onEvent}.
     */
    public interface ExecutionListener {
        /** A fill was booked (before the next decision). */
        default void onFill(Fill fill) {
        }

        /** A child order was submitted (right after the risk hook approved it). */
        default void onOrderSubmitted(ChildOrder order) {
        }

        /** A child order reached FILLED or CANCELLED. */
        default void onOrderTerminal(ChildOrder order) {
        }

        /** The event was applied; {@code book} is the instrument's book. */
        default void onMarket(MarketEvent ev, ConsolidatedBook book) {
        }
    }

    /** Converts a quote-currency amount at {@code ts} into the reporting currency. */
    @FunctionalInterface
    public interface FxConverter {
        double rate(String quoteCcy, long ts);
    }

    /** Reporting currency USD; any other quote currency fails closed. */
    public static final FxConverter USD_ONLY = (ccy, ts) -> {
        if (!"USD".equals(ccy)) {
            throw new IllegalStateException(
                    "no conversion rate for " + ccy + " (USD-only converter)");
        }
        return 1.0;
    };

    /**
     * Execution controls from configs/execution.json defaults: a child is
     * capped at {@code maxParticipation} of the displayed contra depth
     * (top-10 levels of the routed venue) AND of the instrument's session
     * volume (EXECUTE + TRADE qty seen so far) — a cap of 0 blocks the
     * order; children of one instrument are at least
     * {@code minSliceIntervalNs} apart; a child whose modelled
     * decision-to-venue latency (internal legs + venue mean) exceeds
     * {@code latencyBudgetNs} is not sent. Every block is counted.
     */
    public record ExecutionLimits(double maxParticipation, long minSliceIntervalNs,
            long latencyBudgetNs) {
        public ExecutionLimits {
            if (!(maxParticipation > 0.0 && maxParticipation <= 1.0)) {
                throw new IllegalArgumentException(
                        "max_participation must be in (0, 1]");
            }
            if (minSliceIntervalNs < 0 || latencyBudgetNs <= 0) {
                throw new IllegalArgumentException(
                        "min_slice_interval_ns >= 0 and latency_budget_ns > 0");
            }
        }

        /** No participation cap, no interval, no latency budget. */
        public static final ExecutionLimits NONE =
                new ExecutionLimits(1.0, 0, Long.MAX_VALUE);
    }

    /** Execution-control counters (conventions §8). */
    public static final class Counters {
        public long participationCapped;   // delta reduced to the cap
        public long participationBlocked;  // cap was 0: no order
        public long sliceIntervalBlocked;  // too soon after the last child
        public long latencyBudgetBlocked;  // venue path slower than the budget
        public long sorNoRoute;            // no eligible venue

        Counters copy() {
            Counters c = new Counters();
            c.participationCapped = participationCapped;
            c.participationBlocked = participationBlocked;
            c.sliceIntervalBlocked = sliceIntervalBlocked;
            c.latencyBudgetBlocked = latencyBudgetBlocked;
            c.sorNoRoute = sorNoRoute;
            return c;
        }
    }

    /** Risk hook that approves everything. */
    public static final RiskHook PASSTHROUGH_RISK =
            (iid, pos, inflight, delta, ts) -> delta;

    /** Listener that ignores everything. */
    public static final ExecutionListener NO_LISTENER = new ExecutionListener() {
    };

    /**
     * Risk hook capping the worst-case projected position
     * |position + same-side in-flight + delta| at a hard limit.
     */
    public static RiskHook maxPositionRisk(long limit) {
        if (limit <= 0) {
            throw new IllegalArgumentException("risk limit must be > 0");
        }
        return (iid, pos, inflight, delta, ts) -> {
            long resulting = pos + inflight + delta;
            if (resulting > limit) {
                delta -= resulting - limit;
            } else if (resulting < -limit) {
                delta += -limit - resulting;
            }
            return delta;
        };
    }

    /** Per-instrument deterministic P&amp;L account (quote currency). */
    public static final class Account {
        public long position;
        public double cash;
        public double fees;      // taker fees paid (>= 0)
        public double rebates;   // maker rebates received (>= 0)
        public double impact;    // linear impact charges (>= 0)
        public double spreadCost; // fill-vs-mark slippage (signed sum)
        public double grossPnl;  // mark-to-market price-move P&L
        public double mark;      // last valid mid (real price)
        public boolean markValid;
        public long fillCount;
        public long ordersSubmitted;
        /** Equity increments converted to the reporting currency. */
        public double pnlReporting;
        /** Session volume seen (EXECUTE + TRADE qty, all venues). */
        public long sessionVolume;
        /** Our filled qty (participation accounting). */
        public long filledQty;
        public long lastChildDecisionTs = Long.MIN_VALUE;
        /** Exposure timeline: (exchange_ts, position) after every fill. */
        public final List<long[]> exposure = new ArrayList<>();

        Account copy() {
            Account a = new Account();
            a.position = position;
            a.cash = cash;
            a.fees = fees;
            a.rebates = rebates;
            a.impact = impact;
            a.spreadCost = spreadCost;
            a.grossPnl = grossPnl;
            a.mark = mark;
            a.markValid = markValid;
            a.fillCount = fillCount;
            a.ordersSubmitted = ordersSubmitted;
            a.pnlReporting = pnlReporting;
            a.sessionVolume = sessionVolume;
            a.filledQty = filledQty;
            a.lastChildDecisionTs = lastChildDecisionTs;
            for (long[] e : exposure) {
                a.exposure.add(e.clone());
            }
            return a;
        }

        /** cash + position * unit * mark (0 mark before any valid mid). */
        public double equity(double unit) {
            return cash + (double) position * unit * (markValid ? mark : 0.0);
        }
    }

    /** Run summary (per-instrument accounts + totals). */
    public static final class Summary {
        public final long eventsProcessed;
        public final long fillCount;
        /** Total P&amp;L in the REPORTING currency (converted increments). */
        public final double totalPnl;
        /** Sum of native (quote-currency) equities — informational only. */
        public final double totalPnlNative;
        public final double grossPnl;
        public final double feesNet;   // fees - rebates
        public final double impact;
        public final double spreadCost;
        public final TreeMap<Long, Account> accounts;
        public final Counters counters;

        Summary(long eventsProcessed, long fillCount, double totalPnl,
                double totalPnlNative, double grossPnl, double feesNet,
                double impact, double spreadCost, TreeMap<Long, Account> accounts,
                Counters counters) {
            this.eventsProcessed = eventsProcessed;
            this.fillCount = fillCount;
            this.totalPnl = totalPnl;
            this.totalPnlNative = totalPnlNative;
            this.grossPnl = grossPnl;
            this.feesNet = feesNet;
            this.impact = impact;
            this.spreadCost = spreadCost;
            this.accounts = accounts;
            this.counters = counters;
        }
    }

    /** Deep frozen engine state; {@link #restore} continues from it. */
    public static final class Checkpoint {
        final ExecConfig config;
        final long maxChildQty;
        final int fixedVenueId;
        final ExecutionLimits limits;
        final SorOptions sorOptions;
        final long eventsProcessed;
        final FeatureEngine features;
        final ExecutionSimulator sim;
        final TreeMap<Long, Account> accounts;
        final int fillsBooked;
        final List<Long> liveChildren;
        final Counters counters;

        public final long eventsProcessed() {
            return eventsProcessed;
        }

        Checkpoint(ExecConfig config, long maxChildQty, int fixedVenueId,
                ExecutionLimits limits, SorOptions sorOptions,
                long eventsProcessed, FeatureEngine features,
                ExecutionSimulator sim, TreeMap<Long, Account> accounts,
                int fillsBooked, List<Long> liveChildren, Counters counters) {
            this.config = config;
            this.maxChildQty = maxChildQty;
            this.fixedVenueId = fixedVenueId;
            this.limits = limits;
            this.sorOptions = sorOptions;
            this.eventsProcessed = eventsProcessed;
            this.features = features;
            this.sim = sim;
            this.accounts = accounts;
            this.fillsBooked = fillsBooked;
            this.liveChildren = liveChildren;
            this.counters = counters;
        }
    }

    private final ExecConfig config;
    private final Strategy strategy;
    private final RiskHook risk;
    private final ExecutionListener listener;
    private final FxConverter fx;
    private final long maxChildQty;
    private final int fixedVenueId; // 0 = SOR-routed
    private final ExecutionLimits limits;
    private final SorOptions sorOptions;
    private final SmartOrderRouter sor;
    private final List<Integer> sorCandidates = new ArrayList<>();
    private FeatureEngine features;
    private ExecutionSimulator sim;
    private final TreeMap<Long, Account> accounts = new TreeMap<>();
    private final FeatureVector scratch = new FeatureVector();
    private final List<Long> liveChildren = new ArrayList<>();
    private Counters counters = new Counters();
    private long eventsProcessed;
    private int fillsBooked;
    private boolean finished;

    /**
     * @param fixedVenueId venue for child orders; 0 routes each child
     *     through the SOR over all configured venues
     */
    public BacktestEngine(ExecConfig config, Strategy strategy, RiskHook risk,
            long maxChildQty, int fixedVenueId) {
        this(config, strategy, risk, NO_LISTENER, USD_ONLY, maxChildQty,
                fixedVenueId, ExecutionLimits.NONE, SorOptions.DEFAULT);
    }

    /** Full constructor (listener, FX conversion, execution controls, SOR options). */
    public BacktestEngine(ExecConfig config, Strategy strategy, RiskHook risk,
            ExecutionListener listener, FxConverter fx, long maxChildQty,
            int fixedVenueId, ExecutionLimits limits, SorOptions sorOptions) {
        if (maxChildQty <= 0) {
            throw new IllegalArgumentException("max_child_qty must be > 0");
        }
        this.config = config;
        this.strategy = strategy;
        this.risk = risk;
        this.listener = listener;
        this.fx = fx;
        this.maxChildQty = maxChildQty;
        this.fixedVenueId = fixedVenueId;
        this.limits = limits;
        this.sorOptions = sorOptions;
        this.sor = new SmartOrderRouter(config.venues, sorOptions);
        this.sorCandidates.addAll(config.venues.keySet());
        TreeMap<Long, Double> ticks = new TreeMap<>();
        for (Map.Entry<Long, InstrumentSpec> e : config.instruments.entrySet()) {
            ticks.put(e.getKey(), e.getValue().tickSize());
        }
        this.features = new FeatureEngine(ticks, 0);
        this.sim = new ExecutionSimulator(config);
    }

    /** Continue from a {@link #checkpoint()} (stateless strategy/risk). */
    public static BacktestEngine restore(Checkpoint cp, Strategy strategy,
            RiskHook risk) {
        return restore(cp, strategy, risk, NO_LISTENER, USD_ONLY);
    }

    /** Continue from a checkpoint with a listener and FX converter. */
    public static BacktestEngine restore(Checkpoint cp, Strategy strategy,
            RiskHook risk, ExecutionListener listener, FxConverter fx) {
        BacktestEngine e = new BacktestEngine(cp.config, strategy, risk, listener,
                fx, cp.maxChildQty, cp.fixedVenueId, cp.limits, cp.sorOptions);
        e.features = cp.features.snapshot();
        e.sim = cp.sim.snapshot();
        e.accounts.clear();
        for (Map.Entry<Long, Account> a : cp.accounts.entrySet()) {
            e.accounts.put(a.getKey(), a.getValue().copy());
        }
        e.eventsProcessed = cp.eventsProcessed;
        e.fillsBooked = cp.fillsBooked;
        e.liveChildren.addAll(cp.liveChildren);
        e.counters = cp.counters.copy();
        return e;
    }

    /** Deep-copy checkpoint of the full engine state. */
    public Checkpoint checkpoint() {
        TreeMap<Long, Account> accs = new TreeMap<>();
        for (Map.Entry<Long, Account> a : accounts.entrySet()) {
            accs.put(a.getKey(), a.getValue().copy());
        }
        return new Checkpoint(config, maxChildQty, fixedVenueId, limits,
                sorOptions, eventsProcessed, features.snapshot(), sim.snapshot(),
                accs, fillsBooked, new ArrayList<>(liveChildren), counters.copy());
    }

    public ExecutionSimulator simulator() {
        return sim;
    }

    public TreeMap<Long, Account> accounts() {
        return accounts;
    }

    public long eventsProcessed() {
        return eventsProcessed;
    }

    /** Execution-control counters (live view). */
    public Counters counters() {
        return counters;
    }

    private Account account(long instrumentId) {
        Account a = accounts.get(instrumentId);
        if (a == null) {
            a = new Account();
            accounts.put(instrumentId, a);
        }
        return a;
    }

    private double unit(long instrumentId) {
        InstrumentSpec ins = config.instruments.get(instrumentId);
        return ins == null ? 1.0 : ins.qtyUnit();
    }

    private String quoteCcy(long instrumentId) {
        InstrumentSpec ins = config.instruments.get(instrumentId);
        return ins == null ? "USD" : ins.quoteCcy();
    }

    private void bookNewFills() {
        List<Fill> fills = sim.fills();
        while (fillsBooked < fills.size()) {
            Fill f = fills.get(fillsBooked++);
            Account a = account(f.instrumentId());
            InstrumentSpec ins = config.instrument(f.instrumentId());
            double u = ins.qtyUnit();
            double price = (double) f.priceTicks() * ins.tickSize();
            long sgn = f.side() == 0 ? 1 : -1;
            a.cash -= sgn * (double) f.qty() * u * price;
            a.cash -= f.fee();
            a.cash -= f.impactCost();
            if (f.fee() >= 0.0) {
                a.fees += f.fee();
            } else {
                a.rebates += -f.fee();
            }
            a.impact += f.impactCost();
            if (!a.markValid) {
                // First mark = first fill price (position is still flat, so
                // no equity jump); keeps the accounting identity exact.
                a.mark = price;
                a.markValid = true;
            }
            double spread = sgn * (double) f.qty() * u * (price - a.mark);
            a.spreadCost += spread;
            // Equity increment of this fill (quote ccy), converted at the
            // prevailing rate: -spread - fee - impact.
            a.pnlReporting += (-spread - f.fee() - f.impactCost())
                    * fx.rate(ins.quoteCcy(), f.ts());
            a.position += sgn * f.qty();
            a.filledQty += f.qty();
            a.fillCount++;
            a.exposure.add(new long[] {f.ts(), a.position});
            listener.onFill(f);
        }
    }

    private void reportTerminalChildren() {
        for (int i = 0; i < liveChildren.size();) {
            ChildOrder o = sim.orders().get(liveChildren.get(i));
            if (o.state == OrderState.FILLED || o.state == OrderState.CANCELLED) {
                liveChildren.remove(i);
                listener.onOrderTerminal(o);
            } else {
                i++;
            }
        }
    }

    /**
     * Signed remaining qty of in-flight (pending/resting) orders.
     * {@code side < 0}: net over both sides (target-delta basis);
     * {@code side} 0/1: that side only (worst-case risk projection basis).
     */
    private long inFlight(long instrumentId, int side) {
        long signed = 0;
        for (long id : sim.pendingIds()) {
            ChildOrder o = sim.orders().get(id);
            if (o.instrumentId == instrumentId
                    && (side < 0 || o.side == side)) {
                signed += (o.side == 0 ? 1 : -1) * o.remaining;
            }
        }
        for (long id : sim.restingIds()) {
            ChildOrder o = sim.orders().get(id);
            if (o.instrumentId == instrumentId
                    && (side < 0 || o.side == side)
                    && o.state == OrderState.ACTIVE) {
                signed += (o.side == 0 ? 1 : -1) * o.remaining;
            }
        }
        return signed;
    }

    /** Displayed contra depth (top-10 levels) on a venue book, 0 if none. */
    private static long contraDepth(OrderBook book, int side) {
        if (book == null) {
            return 0;
        }
        long total = 0;
        for (long[] lvl : book.depth(side == 0 ? Side.ASK : Side.BID,
                OrderBook.DEPTH_LEVELS)) {
            total += lvl[1];
        }
        return total;
    }

    /** Process one event (pinned order; see class doc). */
    public void onEvent(MarketEvent ev) {
        if (finished) {
            throw new IllegalStateException("engine already finished");
        }
        sim.onEvent(ev);
        bookNewFills();
        reportTerminalChildren();
        if (ev.eventType == EventType.EXECUTE || ev.eventType == EventType.TRADE) {
            account(ev.instrumentId).sessionVolume += ev.qty;
        }
        listener.onMarket(ev, sim.instrumentBook(ev.instrumentId));
        if (features.apply(ev, scratch)) {
            decide(ev);
        }
        eventsProcessed++;
    }

    private void decide(MarketEvent ev) {
        Account a = account(ev.instrumentId);
        // Mark-to-market BEFORE the trading decision.
        if (scratch.valid[Features.MID_PRICE]) {
            double mid = scratch.values[Features.MID_PRICE];
            if (a.markValid) {
                double d = (double) a.position * unit(ev.instrumentId) * (mid - a.mark);
                a.grossPnl += d;
                a.pnlReporting += d * fx.rate(quoteCcy(ev.instrumentId), ev.exchangeTs);
            }
            a.mark = mid;
            a.markValid = true;
        }
        long target = strategy.targetPosition(scratch);
        long inflight = inFlight(ev.instrumentId, -1);
        long delta = target - (a.position + inflight);
        if (delta > maxChildQty) {
            delta = maxChildQty;
        } else if (delta < -maxChildQty) {
            delta = -maxChildQty;
        }
        if (delta == 0) {
            return;
        }
        int side = delta > 0 ? 0 : 1;
        // Route first: participation is measured on the routed venue.
        int venueId = fixedVenueId != 0 ? fixedVenueId
                : sor.routeAggressive(sim.instrumentBook(ev.instrumentId), side,
                        sorCandidates);
        if (venueId == SmartOrderRouter.NO_ROUTE) {
            counters.sorNoRoute++;
            return;
        }
        // Execution controls (configs/execution.json defaults).
        if (limits.minSliceIntervalNs() > 0 && a.lastChildDecisionTs != Long.MIN_VALUE
                && ev.exchangeTs - a.lastChildDecisionTs < limits.minSliceIntervalNs()) {
            counters.sliceIntervalBlocked++;
            return;
        }
        long venueLatency = config.latency.decisionNs() + config.latency.riskNs()
                + config.latency.wireNs() + config.venue(venueId).latencyMeanNs();
        if (venueLatency > limits.latencyBudgetNs()) {
            counters.latencyBudgetBlocked++;
            return;
        }
        if (limits.maxParticipation() < 1.0) {
            long depth = contraDepth(sim.venueBook(ev.instrumentId, venueId), side);
            long capDepth = (long) Math.floor(limits.maxParticipation() * depth);
            long capVolume = (long) Math.floor(
                    limits.maxParticipation() * a.sessionVolume) - a.filledQty;
            long cap = Math.max(Math.min(capDepth, capVolume), 0);
            if (cap == 0) {
                counters.participationBlocked++;
                return;
            }
            if (Math.abs(delta) > cap) {
                counters.participationCapped++;
                delta = delta > 0 ? cap : -cap;
            }
        }
        // The hook sees the worst-case in-flight for this delta: only orders
        // on the delta's own side (opposite-side in-flight may never fill and
        // must not offset the projection).
        long worstInflight = inFlight(ev.instrumentId, side);
        delta = risk.approve(ev.instrumentId, a.position, worstInflight, delta,
                ev.exchangeTs);
        if (delta != 0) {
            ChildOrder c = new ChildOrder();
            c.parentId = 0;
            c.instrumentId = ev.instrumentId;
            c.side = delta > 0 ? 0 : 1;
            c.type = OrderType.MARKET;
            c.qty = Math.abs(delta);
            c.decisionTs = ev.exchangeTs;
            c.venueId = venueId;
            long id = sim.submit(c);
            liveChildren.add(id);
            a.ordersSubmitted++;
            a.lastChildDecisionTs = ev.exchangeTs;
            listener.onOrderSubmitted(sim.orders().get(id));
        }
    }

    /** Run a whole stream and finish (cancel residuals, final accounting). */
    public Summary run(Iterable<MarketEvent> events) {
        for (MarketEvent ev : events) {
            onEvent(ev);
        }
        return finish();
    }

    /** Cancel every unfinished child and build the run summary. */
    public Summary finish() {
        if (!finished) {
            sim.cancelAll();
            bookNewFills();
            reportTerminalChildren();
            finished = true;
        }
        long fillCount = 0;
        double totalPnl = 0.0;
        double totalNative = 0.0;
        double gross = 0.0;
        double feesNet = 0.0;
        double impact = 0.0;
        double spread = 0.0;
        TreeMap<Long, Account> accs = new TreeMap<>();
        for (Map.Entry<Long, Account> e : accounts.entrySet()) {
            Account a = e.getValue();
            fillCount += a.fillCount;
            totalPnl += a.pnlReporting;
            totalNative += a.equity(unit(e.getKey()));
            gross += a.grossPnl;
            feesNet += a.fees - a.rebates;
            impact += a.impact;
            spread += a.spreadCost;
            accs.put(e.getKey(), a.copy());
        }
        return new Summary(eventsProcessed, fillCount, totalPnl, totalNative, gross,
                feesNet, impact, spread, accs, counters.copy());
    }
}
