package com.iap.execution;

import java.util.ArrayList;
import java.util.List;
import java.util.TreeMap;

import com.iap.core.EventType;
import com.iap.core.MarketEvent;
import com.iap.orderbook.ConsolidatedBook;
import com.iap.orderbook.OrderBook;
import com.iap.sor.SmartOrderRouter;
import com.iap.sor.SorOptions;

/**
 * Execution replay — the event-driven parent-order driver (spec section 18),
 * mirroring the C++ reference ({@code cpp/src/replay/exec_replay.cpp}).
 * Consumes the normalized event stream in file order (event time), drives
 * the {@link ExecutionSimulator}'s books and order lifecycle, and works a
 * set of parent orders through their VWAP/TWAP/POV/IS schedules
 * ({@link Algos}). Same events + config + seed = identical fills, bit for
 * bit.
 *
 * <p>Per event, in pinned order: (1) the simulator processes the event
 * (expiries, child activation, passive queue tracking, book application,
 * crossing checks); (2) new fills are booked per parent; (3) the scheduler
 * evaluates every parent against the post-event state — due TWAP/VWAP/IS
 * slices are issued (decision_ts = the event's exchange_ts, limit prices
 * read from the just-updated book, split into children of at most
 * max_child_qty) and POV targets are re-evaluated after TRADE events of the
 * parent's instrument inside its window against filled + in-flight qty.
 * Children expire at their parent's end_ts; after the last event every
 * still-unfinished child is cancelled (unfilled residual = opportunity
 * cost, reported per parent). Every fill attributed to a parent lies inside
 * its window by construction (verified — a violation throws).
 *
 * <p>Child order styles (pinned): TWAP and VWAP children are passive LIMIT
 * orders joining the same-side best price at decision time (falling back to
 * MARKET when that side is empty); POV and IS children are MARKET orders.
 * Venue: parent.venue_id, or SOR-routed when venue_id == 0; with no
 * eligible venue the child is NOT submitted and counted in
 * {@link Result#sorNoRoute}.
 *
 * <p>Parent accounting identity (tested):
 * {@code total_cost = fees - rebates + impact} with fees/rebates/impact
 * exact sums over the parent's fills.
 */
public final class ExecutionReplay {
    /** Per-parent execution report. */
    public static final class ParentReport {
        public long parentId;
        public long filledQty;
        public long unfilledQty;
        public long children;
        public double notional;  // sum of fill qty * price * tick * qty_unit
        public double avgPrice;  // notional / (filled qty * qty_unit); 0 if unfilled
        public double fees;      // taker fees (>= 0)
        public double rebates;   // maker rebates (>= 0)
        public double impact;    // linear impact charges (>= 0)
        public double totalCost; // fees - rebates + impact
    }

    /** Result of one replay run. */
    public static final class Result {
        public final List<Fill> fills;
        public final TreeMap<Long, ParentReport> parents;
        public final long eventsProcessed;
        /** Children not submitted because the SOR found no eligible venue. */
        public final long sorNoRoute;

        Result(List<Fill> fills, TreeMap<Long, ParentReport> parents,
                long eventsProcessed, long sorNoRoute) {
            this.fills = fills;
            this.parents = parents;
            this.eventsProcessed = eventsProcessed;
            this.sorNoRoute = sorNoRoute;
        }
    }

    private static final class ParentState {
        ParentOrder order;
        long[] sliceQty = new long[0];  // TWAP/VWAP/IS
        long[] sliceDue = new long[0];  // TWAP/VWAP/IS
        int nextSlice;
        long filledQty;                 // fills booked so far
        long povVolume;                 // window TRADE volume (POV)
        final List<Long> childIds = new ArrayList<>();
    }

    private final ExecConfig config;
    private final ExecutionSimulator sim;
    private final SmartOrderRouter sor;
    private final List<Integer> sorCandidates = new ArrayList<>();
    private final List<ParentState> parents = new ArrayList<>();
    private int fillsBooked;
    private long sorNoRoute;
    private boolean ran;

    public ExecutionReplay(ExecConfig config, List<ParentOrder> parentOrders) {
        this(config, parentOrders, SorOptions.DEFAULT);
    }

    public ExecutionReplay(ExecConfig config, List<ParentOrder> parentOrders,
            SorOptions sorOptions) {
        this.config = config;
        this.sim = new ExecutionSimulator(config);
        this.sor = new SmartOrderRouter(config.venues, sorOptions);
        for (Integer vid : config.venues.keySet()) {
            sorCandidates.add(vid);
        }
        for (ParentOrder p : parentOrders) {
            if (p.qty <= 0) {
                throw new IllegalArgumentException("parent qty must be > 0");
            }
            if (p.maxChildQty <= 0) {
                throw new IllegalArgumentException("max_child_qty must be > 0");
            }
            if (p.endTs <= p.startTs) {
                throw new IllegalArgumentException(
                        "parent window must have end_ts > start_ts");
            }
            ParentState ps = new ParentState();
            ps.order = p;
            if (p.algo != AlgoType.POV) {
                ps.sliceQty = Algos.sliceQuantities(p);
                ps.sliceDue = Algos.sliceTimes(p);
            }
            parents.add(ps);
        }
    }

    public ExecutionSimulator simulator() {
        return sim;
    }

    /** Issue one child of at most max_child_qty; false when unroutable. */
    private boolean issueChild(ParentState ps, long childQty, long decisionTs,
            boolean passive) {
        if (childQty <= 0) {
            return true;
        }
        ParentOrder p = ps.order;
        ChildOrder c = new ChildOrder();
        c.parentId = p.parentId;
        c.instrumentId = p.instrumentId;
        c.side = p.side;
        c.qty = childQty;
        c.decisionTs = decisionTs;
        c.expireTs = p.endTs; // pinned: no child outlives the window
        ConsolidatedBook book = sim.instrumentBook(p.instrumentId);
        if (p.venueId != 0) {
            c.venueId = p.venueId;
        } else if (passive) {
            c.venueId = sor.routePassive(book, p.side, sorCandidates);
        } else {
            c.venueId = sor.routeAggressive(book, p.side, sorCandidates);
        }
        if (c.venueId == SmartOrderRouter.NO_ROUTE) {
            sorNoRoute++; // no eligible venue: do not submit (pinned)
            return false;
        }
        if (passive) {
            // Join the same-side best on the routed venue; MARKET fallback.
            OrderBook vb = sim.venueBook(p.instrumentId, c.venueId);
            long[] best = null;
            if (vb != null) {
                best = p.side == 0 ? vb.bestBid() : vb.bestAsk();
            }
            if (best != null) {
                c.type = OrderType.LIMIT;
                c.limitTicks = best[0];
            } else {
                c.type = OrderType.MARKET;
            }
        } else {
            c.type = OrderType.MARKET;
        }
        ps.childIds.add(sim.submit(c));
        return true;
    }

    /** Split a slice into children of at most max_child_qty (pinned). */
    private void issueSlice(ParentState ps, long sliceQty, long decisionTs,
            boolean passive) {
        long cap = ps.order.maxChildQty;
        long left = sliceQty;
        while (left > 0) {
            long q = Math.min(left, cap);
            issueChild(ps, q, decisionTs, passive);
            left -= q;
        }
    }

    /** Filled + still open/in-flight qty of the parent's children. */
    private long committedQty(ParentState ps) {
        long open = 0;
        for (long id : ps.childIds) {
            ChildOrder o = sim.orders().get(id);
            if (o.state == OrderState.PENDING || o.state == OrderState.ACTIVE) {
                open += o.remaining;
            }
        }
        return ps.filledQty + open;
    }

    private void bookNewFills() {
        List<Fill> fills = sim.fills();
        while (fillsBooked < fills.size()) {
            Fill f = fills.get(fillsBooked++);
            for (ParentState ps : parents) {
                if (ps.order.parentId == f.parentId()) {
                    ps.filledQty += f.qty();
                }
            }
        }
    }

    private void schedule(ParentState ps, MarketEvent ev) {
        ParentOrder p = ps.order;
        long t = ev.exchangeTs;
        if (p.algo == AlgoType.POV) {
            if (ev.instrumentId != p.instrumentId
                    || ev.eventType != EventType.TRADE
                    || t < p.startTs || t >= p.endTs) {
                return;
            }
            ps.povVolume += ev.qty;
            long target = (long) Math.floor(p.participation * (double) ps.povVolume);
            // Deficit against FILLED + in-flight qty, never sent qty (pinned).
            long deficit = Math.min(target, p.qty) - committedQty(ps);
            if (deficit > 0) {
                issueChild(ps, Math.min(deficit, p.maxChildQty), t, false);
            }
            return;
        }
        // TWAP / VWAP / IS: issue every slice that has come due (inside the
        // window only — a slice due at/after end_ts would expire on arrival).
        while (ps.nextSlice < ps.sliceDue.length && t >= ps.sliceDue[ps.nextSlice]) {
            long q = ps.sliceQty[ps.nextSlice];
            ps.nextSlice++;
            if (t >= p.endTs) {
                continue;
            }
            boolean passive = p.algo == AlgoType.TWAP || p.algo == AlgoType.VWAP;
            issueSlice(ps, q, t, passive);
        }
    }

    /** Replay the stream, working every parent. Callable once. */
    public Result run(Iterable<MarketEvent> events) {
        if (ran) {
            throw new IllegalStateException("ExecutionReplay.run is one-shot");
        }
        ran = true;
        long processed = 0;
        for (MarketEvent ev : events) {
            sim.onEvent(ev);
            bookNewFills();
            for (ParentState ps : parents) {
                schedule(ps, ev);
            }
            processed++;
        }
        sim.cancelAll();
        bookNewFills();

        List<Fill> fills = new ArrayList<>(sim.fills());
        TreeMap<Long, ParentReport> reports = new TreeMap<>();
        for (ParentState ps : parents) {
            ParentReport r = new ParentReport();
            r.parentId = ps.order.parentId;
            r.children = ps.childIds.size();
            InstrumentSpec ins = config.instruments.get(ps.order.instrumentId);
            double lot = ins == null ? 1.0 : ins.qtyUnit();
            double tick = ins == null ? 1.0 : ins.tickSize();
            for (Fill f : fills) {
                if (f.parentId() != ps.order.parentId) {
                    continue;
                }
                if (f.ts() < ps.order.startTs || f.ts() > ps.order.endTs) {
                    throw new IllegalStateException(
                            "fill outside the parent window (time-in-force broken)");
                }
                r.filledQty += f.qty();
                r.notional += (double) f.qty() * lot * (double) f.priceTicks() * tick;
                if (f.fee() >= 0.0) {
                    r.fees += f.fee();
                } else {
                    r.rebates += -f.fee();
                }
                r.impact += f.impactCost();
            }
            r.unfilledQty = ps.order.qty - r.filledQty;
            r.avgPrice = r.filledQty > 0
                    ? r.notional / ((double) r.filledQty * lot)
                    : 0.0;
            r.totalCost = r.fees - r.rebates + r.impact;
            reports.put(r.parentId, r);
        }
        return new Result(fills, reports, processed, sorNoRoute);
    }
}
