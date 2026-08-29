package com.iap.execution;

import java.util.ArrayList;
import java.util.List;
import java.util.TreeMap;

import com.iap.core.EventType;
import com.iap.core.MarketEvent;
import com.iap.orderbook.ConsolidatedBook;
import com.iap.orderbook.OrderBook;
import com.iap.sor.SmartOrderRouter;

/**
 * Execution replay — the event-driven parent-order driver (spec section 18).
 * Consumes the normalized event stream in file order (event time), drives
 * the {@link ExecutionSimulator}'s books and order lifecycle, and works a
 * set of parent orders through their VWAP/TWAP/POV/IS schedules
 * ({@link Algos}). Same events + config + seed = identical fills, bit for
 * bit.
 *
 * <p>Per event, in pinned order: (1) the simulator processes the event
 * (child activation, passive queue tracking, book application, crossing
 * checks); (2) the scheduler evaluates every parent against the post-event
 * state — due TWAP/VWAP/IS slices are issued (decision_ts = the event's
 * exchange_ts, limit prices read from the just-updated book) and POV
 * targets are re-evaluated after TRADE events of the parent's instrument
 * inside its window. After the last event every unfinished child is
 * cancelled (unfilled residual = opportunity cost, reported per parent).
 *
 * <p>Child order styles (pinned): TWAP and VWAP children are passive LIMIT
 * orders joining the same-side best price at decision time (falling back to
 * MARKET when that side is empty); POV and IS children are MARKET orders.
 * Venue: parent.venue_id, or SOR-routed when venue_id == 0.
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
        public double notional;  // sum of fill qty * price * tick * lot
        public double avgPrice;  // notional / (filled qty * lot); 0 if unfilled
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

        Result(List<Fill> fills, TreeMap<Long, ParentReport> parents,
                long eventsProcessed) {
            this.fills = fills;
            this.parents = parents;
            this.eventsProcessed = eventsProcessed;
        }
    }

    private static final class ParentState {
        ParentOrder order;
        long[] sliceQty = new long[0];  // TWAP/VWAP/IS
        long[] sliceDue = new long[0];  // TWAP/VWAP/IS
        int nextSlice;
        long sentQty;                   // qty submitted so far
        long povVolume;                 // window TRADE volume (POV)
        final List<Long> childIds = new ArrayList<>();
    }

    private final ExecConfig config;
    private final ExecutionSimulator sim;
    private final SmartOrderRouter sor;
    private final List<Integer> sorCandidates = new ArrayList<>();
    private final List<ParentState> parents = new ArrayList<>();
    private boolean ran;

    public ExecutionReplay(ExecConfig config, List<ParentOrder> parentOrders) {
        this.config = config;
        this.sim = new ExecutionSimulator(config);
        this.sor = new SmartOrderRouter(config.venues);
        for (Integer vid : config.venues.keySet()) {
            sorCandidates.add(vid);
        }
        for (ParentOrder p : parentOrders) {
            if (p.qty <= 0) {
                throw new IllegalArgumentException("parent qty must be > 0");
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

    private void issueChild(ParentState ps, long childQty, long decisionTs,
            boolean passive) {
        if (childQty <= 0) {
            return;
        }
        ParentOrder p = ps.order;
        ChildOrder c = new ChildOrder();
        c.parentId = p.parentId;
        c.instrumentId = p.instrumentId;
        c.side = p.side;
        c.qty = childQty;
        c.decisionTs = decisionTs;
        ConsolidatedBook book = sim.instrumentBook(p.instrumentId);
        if (p.venueId != 0) {
            c.venueId = p.venueId;
        } else if (passive) {
            c.venueId = sor.routePassive(book, p.side, sorCandidates);
        } else {
            c.venueId = sor.routeAggressive(book, p.side, sorCandidates);
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
        ps.sentQty += childQty;
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
            long deficit = Math.min(target, p.qty) - ps.sentQty; // cap at parent
            if (deficit > 0) {
                issueChild(ps, Math.min(deficit, p.maxChildQty), t, false);
            }
            return;
        }
        // TWAP / VWAP / IS: issue every slice that has come due.
        while (ps.nextSlice < ps.sliceDue.length && t >= ps.sliceDue[ps.nextSlice]) {
            long q = ps.sliceQty[ps.nextSlice];
            ps.nextSlice++;
            boolean passive = p.algo == AlgoType.TWAP || p.algo == AlgoType.VWAP;
            issueChild(ps, Math.min(q, p.maxChildQty), t, passive);
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
            for (ParentState ps : parents) {
                schedule(ps, ev);
            }
            processed++;
        }
        sim.cancelAll();

        List<Fill> fills = new ArrayList<>(sim.fills());
        TreeMap<Long, ParentReport> reports = new TreeMap<>();
        for (ParentState ps : parents) {
            ParentReport r = new ParentReport();
            r.parentId = ps.order.parentId;
            r.children = ps.childIds.size();
            InstrumentSpec ins = config.instruments.get(ps.order.instrumentId);
            double lot = ins == null ? 1.0 : ins.lotSize();
            double tick = ins == null ? 1.0 : ins.tickSize();
            for (Fill f : fills) {
                if (f.parentId() != ps.order.parentId) {
                    continue;
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
        return new Result(fills, reports, processed);
    }
}
