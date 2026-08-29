package com.iap.backtest;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

import com.iap.core.MarketEvent;
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
import com.iap.sor.SmartOrderRouter;

/**
 * Production event-driven backtest engine (spec section 18). Replays
 * normalized events (or the golden vectors) and, per event, in pinned
 * order:
 * <ol>
 *   <li>the {@link ExecutionSimulator} processes the event (child
 *       activation, passive queue tracking, book application, crossing
 *       checks — the pinned execution rules);</li>
 *   <li>new fills are booked into the deterministic P&amp;L accounts;</li>
 *   <li>the {@link FeatureEngine} updates and emits a feature vector
 *       (cadence 0), the {@link Strategy} maps it to a target position, the
 *       {@link RiskHook} runs its pre-trade check, and any resulting child
 *       order (MARKET, capped at max_child_qty; SOR-routed when no venue is
 *       pinned) is submitted with decision_ts = the event's
 *       exchange_ts.</li>
 * </ol>
 *
 * <p><b>Accounting identity</b> (tested): equity changes only through mark
 * moves and fills, so per instrument
 * {@code total_pnl = gross_pnl - spread_cost - (fees - rebates) - impact}
 * where gross is mark-to-market price-move P&amp;L, spread_cost is the
 * fill-vs-prevailing-mid slippage, and fees/rebates/impact are exact sums
 * over the fills.
 *
 * <p><b>Checkpoint/restart</b>: {@link #checkpoint()} deep-copies the full
 * engine state (feature engine, simulator incl. RNG stream position and
 * in-flight orders, accounts); {@link #restore} continues from it with
 * bit-identical subsequent behavior. Strategies and risk hooks are
 * stateless by contract and are re-supplied at restore.
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
     * Pre-trade risk check (simple pluggable hook; the full risk service
     * arrives in a later wave). Returns the permitted signed order delta
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

    /** Risk hook that approves everything. */
    public static final RiskHook PASSTHROUGH_RISK =
            (iid, pos, inflight, delta, ts) -> delta;

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

    /** Per-instrument deterministic P&amp;L account. */
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
        public final double totalPnl;
        public final double grossPnl;
        public final double feesNet;   // fees - rebates
        public final double impact;
        public final double spreadCost;
        public final TreeMap<Long, Account> accounts;

        Summary(long eventsProcessed, long fillCount, double totalPnl,
                double grossPnl, double feesNet, double impact,
                double spreadCost, TreeMap<Long, Account> accounts) {
            this.eventsProcessed = eventsProcessed;
            this.fillCount = fillCount;
            this.totalPnl = totalPnl;
            this.grossPnl = grossPnl;
            this.feesNet = feesNet;
            this.impact = impact;
            this.spreadCost = spreadCost;
            this.accounts = accounts;
        }
    }

    /** Deep frozen engine state; {@link #restore} continues from it. */
    public static final class Checkpoint {
        final ExecConfig config;
        final long maxChildQty;
        final int fixedVenueId;
        final long eventsProcessed;
        final FeatureEngine features;
        final ExecutionSimulator sim;
        final TreeMap<Long, Account> accounts;
        final int fillsBooked;

        public final long eventsProcessed() {
            return eventsProcessed;
        }

        Checkpoint(ExecConfig config, long maxChildQty, int fixedVenueId,
                long eventsProcessed, FeatureEngine features,
                ExecutionSimulator sim, TreeMap<Long, Account> accounts,
                int fillsBooked) {
            this.config = config;
            this.maxChildQty = maxChildQty;
            this.fixedVenueId = fixedVenueId;
            this.eventsProcessed = eventsProcessed;
            this.features = features;
            this.sim = sim;
            this.accounts = accounts;
            this.fillsBooked = fillsBooked;
        }
    }

    private final ExecConfig config;
    private final Strategy strategy;
    private final RiskHook risk;
    private final long maxChildQty;
    private final int fixedVenueId; // 0 = SOR-routed
    private final SmartOrderRouter sor;
    private final List<Integer> sorCandidates = new ArrayList<>();
    private FeatureEngine features;
    private ExecutionSimulator sim;
    private final TreeMap<Long, Account> accounts = new TreeMap<>();
    private final FeatureVector scratch = new FeatureVector();
    private long eventsProcessed;
    private int fillsBooked;
    private boolean finished;

    /**
     * @param fixedVenueId venue for child orders; 0 routes each child
     *     through the SOR over all configured venues
     */
    public BacktestEngine(ExecConfig config, Strategy strategy, RiskHook risk,
            long maxChildQty, int fixedVenueId) {
        if (maxChildQty <= 0) {
            throw new IllegalArgumentException("max_child_qty must be > 0");
        }
        this.config = config;
        this.strategy = strategy;
        this.risk = risk;
        this.maxChildQty = maxChildQty;
        this.fixedVenueId = fixedVenueId;
        this.sor = new SmartOrderRouter(config.venues);
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
        BacktestEngine e = new BacktestEngine(cp.config, strategy, risk,
                cp.maxChildQty, cp.fixedVenueId);
        e.features = cp.features.snapshot();
        e.sim = cp.sim.snapshot();
        e.accounts.clear();
        for (Map.Entry<Long, Account> a : cp.accounts.entrySet()) {
            e.accounts.put(a.getKey(), a.getValue().copy());
        }
        e.eventsProcessed = cp.eventsProcessed;
        e.fillsBooked = cp.fillsBooked;
        return e;
    }

    /** Deep-copy checkpoint of the full engine state. */
    public Checkpoint checkpoint() {
        TreeMap<Long, Account> accs = new TreeMap<>();
        for (Map.Entry<Long, Account> a : accounts.entrySet()) {
            accs.put(a.getKey(), a.getValue().copy());
        }
        return new Checkpoint(config, maxChildQty, fixedVenueId,
                eventsProcessed, features.snapshot(), sim.snapshot(), accs,
                fillsBooked);
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
        return ins == null ? 1.0 : ins.lotSize();
    }

    private void bookNewFills() {
        List<Fill> fills = sim.fills();
        while (fillsBooked < fills.size()) {
            Fill f = fills.get(fillsBooked++);
            Account a = account(f.instrumentId());
            InstrumentSpec ins = config.instrument(f.instrumentId());
            double u = ins.lotSize();
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
            a.spreadCost += sgn * (double) f.qty() * u * (price - a.mark);
            a.position += sgn * f.qty();
            a.fillCount++;
            a.exposure.add(new long[] {f.ts(), a.position});
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

    /** Process one event (pinned order; see class doc). */
    public void onEvent(MarketEvent ev) {
        if (finished) {
            throw new IllegalStateException("engine already finished");
        }
        sim.onEvent(ev);
        bookNewFills();
        if (features.apply(ev, scratch)) {
            Account a = account(ev.instrumentId);
            // Mark-to-market BEFORE the trading decision.
            if (scratch.valid[Features.MID_PRICE]) {
                double mid = scratch.values[Features.MID_PRICE];
                if (a.markValid) {
                    a.grossPnl += (double) a.position * unit(ev.instrumentId)
                            * (mid - a.mark);
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
            // The hook sees the worst-case in-flight for this delta: only
            // orders on the delta's own side (opposite-side in-flight may
            // never fill and must not offset the projection).
            long worstInflight = delta == 0 ? 0
                    : inFlight(ev.instrumentId, delta > 0 ? 0 : 1);
            delta = risk.approve(ev.instrumentId, a.position, worstInflight,
                    delta, ev.exchangeTs);
            if (delta != 0) {
                ChildOrder c = new ChildOrder();
                c.parentId = 0;
                c.instrumentId = ev.instrumentId;
                c.side = delta > 0 ? 0 : 1;
                c.type = OrderType.MARKET;
                c.qty = Math.abs(delta);
                c.decisionTs = ev.exchangeTs;
                c.venueId = fixedVenueId != 0 ? fixedVenueId
                        : sor.routeAggressive(sim.instrumentBook(ev.instrumentId),
                                c.side, sorCandidates);
                sim.submit(c);
                a.ordersSubmitted++;
            }
        }
        eventsProcessed++;
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
            finished = true;
        }
        long fillCount = 0;
        double totalPnl = 0.0;
        double gross = 0.0;
        double feesNet = 0.0;
        double impact = 0.0;
        double spread = 0.0;
        TreeMap<Long, Account> accs = new TreeMap<>();
        for (Map.Entry<Long, Account> e : accounts.entrySet()) {
            Account a = e.getValue();
            fillCount += a.fillCount;
            totalPnl += a.equity(unit(e.getKey()));
            gross += a.grossPnl;
            feesNet += a.fees - a.rebates;
            impact += a.impact;
            spread += a.spreadCost;
            accs.put(e.getKey(), a.copy());
        }
        return new Summary(eventsProcessed, fillCount, totalPnl, gross,
                feesNet, impact, spread, accs);
    }
}
