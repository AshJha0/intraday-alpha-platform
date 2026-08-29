package com.iap.execution;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.SortedMap;
import java.util.TreeMap;

import com.iap.core.EventType;
import com.iap.core.MarketEvent;
import com.iap.core.SplitMix64;
import com.iap.core.Side;
import com.iap.orderbook.ConsolidatedBook;
import com.iap.orderbook.OrderBook;

/**
 * Production-grade event-driven execution simulator (spec sections 17-18),
 * matching the C++ reference (cpp/include/iap/execution/execution.hpp)
 * EXACTLY — that port generated tests/golden/expected_replay_fills.json.
 *
 * <p>PINNED RULES:
 * <ol>
 *   <li><b>Latency</b>: a child decided at decision_ts arrives at
 *       {@code decision_ts + decision_ns + risk_ns + wire_ns +
 *       venue.latency_mean_ns + jitter}, jitter =
 *       {@code SplitMix64(seed).below(venue.latency_jitter_ns + 1)} — one
 *       draw per submitted order, in submission order (no draw when the
 *       venue jitter is 0).</li>
 *   <li><b>Activation</b>: a pending order becomes active while processing
 *       the first market event with {@code exchange_ts >= arrival_ts},
 *       BEFORE that event is applied to the books; orders activate in
 *       (arrival_ts, order_id) order. Aggressive fills are stamped with
 *       arrival_ts.</li>
 *   <li><b>Aggressive execution</b> (MARKET, and the marketable part of
 *       LIMIT/IOC/FOK) walks the DISPLAYED top-10 depth of the target
 *       venue's opposite side, best price first, up to the limit price. One
 *       fill per price level. Simulated orders never mutate the replayed
 *       book. Unfilled MARKET/IOC remainders are cancelled; FOK fills fully
 *       or not at all (checked against displayed depth within the limit
 *       before any fill).</li>
 *   <li><b>Passive queue position</b> (pinned deterministic rule): when a
 *       LIMIT remainder rests at price P, ahead_qty := displayed qty at
 *       (side, P) on that venue at rest time. Then, on that venue: an
 *       observed EXECUTE at (side, P) reduces ahead_qty by its full qty and
 *       any leftover fills our order at P (partials supported); an observed
 *       CANCEL at (side, P) reduces ahead_qty by its full qty, floored at
 *       0; an EXECUTE on our side at a price WORSE than P fills us in full
 *       at P (trade-through); a MARKETABLE incoming ADD is expanded into
 *       the per-level volumes it consumes over the pre-event displayed
 *       depth and the two EXECUTE rules apply level by level; after the
 *       event is applied, a crossed displayed opposite best fills us in
 *       full at P — EXEMPTION: a remainder that rests while the display
 *       already crosses its price (the aggressive leg just consumed that
 *       display) is crossing-exempt until the display first shows an
 *       uncrossed opposite best; MODIFY events never change ahead_qty.
 *       Passive fills are stamped with the triggering event's
 *       exchange_ts.</li>
 *   <li><b>Fees</b>: equity venues charge taker_fee_per_share * qty on
 *       aggressive fills and rebate maker_rebate_per_share * qty on passive
 *       fills (fee &lt; 0 = rebate). FX venues charge
 *       commission_per_million * notional / 1e6 on every fill, notional =
 *       qty * lot_size * price_ticks * tick_size.</li>
 *   <li><b>Linear impact</b> (aggressive fills only): impact_bps =
 *       impact_coeff_bps_per_pct_adv * (child_qty / adv * 100); each taker
 *       fill is charged impact_bps * 1e-4 * its own notional.</li>
 * </ol>
 *
 * <p>Deterministic: same config + seed = identical fills, bit for bit
 * (SplitMix64 only, no wall clock).
 */
public final class ExecutionSimulator {
    private final ExecConfig config;
    private final SplitMix64 rng;
    private final TreeMap<Long, ConsolidatedBook> books = new TreeMap<>();
    private final TreeMap<Long, ChildOrder> orders = new TreeMap<>();
    private final ArrayList<Long> pending = new ArrayList<>(); // (arrival, id)
    private final ArrayList<Long> resting = new ArrayList<>(); // ACTIVE ids
    private final ArrayList<Fill> fills = new ArrayList<>(256);
    private long nextOrderId = 1;
    private long nextFillId = 1;

    public ExecutionSimulator(ExecConfig config) {
        this.config = config;
        this.rng = new SplitMix64(config.seed);
    }

    private ExecutionSimulator(ExecutionSimulator o) {
        this.config = o.config;
        this.rng = new SplitMix64(o.rng.state());
        for (Map.Entry<Long, ConsolidatedBook> e : o.books.entrySet()) {
            this.books.put(e.getKey(),
                    ConsolidatedBook.restore(e.getValue().checkpoint()));
        }
        for (Map.Entry<Long, ChildOrder> e : o.orders.entrySet()) {
            this.orders.put(e.getKey(), e.getValue().copy());
        }
        this.pending.addAll(o.pending);
        this.resting.addAll(o.resting);
        this.fills.addAll(o.fills);
        this.nextOrderId = o.nextOrderId;
        this.nextFillId = o.nextFillId;
    }

    /** Deep-copy snapshot of the full simulator state (checkpoint support). */
    public ExecutionSimulator snapshot() {
        return new ExecutionSimulator(this);
    }

    public ExecConfig config() {
        return config;
    }

    /** All fills so far, in emission order. */
    public List<Fill> fills() {
        return java.util.Collections.unmodifiableList(fills);
    }

    /** All orders ever submitted, keyed by order_id (ascending). */
    public SortedMap<Long, ChildOrder> orders() {
        return java.util.Collections.unmodifiableSortedMap(orders);
    }

    /** Order ids currently in flight (sorted by arrival_ts, order_id). */
    public List<Long> pendingIds() {
        return java.util.Collections.unmodifiableList(pending);
    }

    /** Order ids currently resting (ACTIVE). */
    public List<Long> restingIds() {
        return java.util.Collections.unmodifiableList(resting);
    }

    /** Consolidated book for an instrument (lazily created). */
    public ConsolidatedBook instrumentBook(long instrumentId) {
        ConsolidatedBook b = books.get(instrumentId);
        if (b == null) {
            b = new ConsolidatedBook(instrumentId);
            books.put(instrumentId, b);
        }
        return b;
    }

    /** Venue book for (instrument, venue); null before any event touched it. */
    public OrderBook venueBook(long instrumentId, int venueId) {
        ConsolidatedBook b = books.get(instrumentId);
        return b == null ? null : b.venues().get(venueId);
    }

    /**
     * Submit a child order (decision-time semantics per pinned rule 1).
     * Returns the assigned order_id.
     */
    public long submit(ChildOrder child) {
        if (child.qty <= 0) {
            throw new IllegalArgumentException("child qty must be > 0");
        }
        if (child.side != 0 && child.side != 1) {
            throw new IllegalArgumentException("child side must be 0/1");
        }
        if (child.type != OrderType.MARKET && child.limitTicks <= 0) {
            throw new IllegalArgumentException("non-MARKET child needs a limit price");
        }
        ChildOrder o = child.copy();
        o.orderId = nextOrderId++;
        VenueSpec v = config.venue(o.venueId);
        // Pinned rule 1: one jitter draw per order, in submission order.
        long jitter = v.latencyJitterNs() > 0 ? rng.below(v.latencyJitterNs() + 1) : 0;
        o.arrivalTs = o.decisionTs + config.latency.decisionNs()
                + config.latency.riskNs() + config.latency.wireNs()
                + v.latencyMeanNs() + jitter;
        o.state = OrderState.PENDING;
        o.remaining = o.qty;
        long id = o.orderId;
        orders.put(id, o);
        // Keep pending sorted by (arrival_ts, order_id).
        int pos = pending.size();
        for (int i = 0; i < pending.size(); i++) {
            ChildOrder other = orders.get(pending.get(i));
            if (other.arrivalTs > o.arrivalTs
                    || (other.arrivalTs == o.arrivalTs && other.orderId > id)) {
                pos = i;
                break;
            }
        }
        pending.add(pos, id);
        return id;
    }

    /** Cancel an order (pending or resting); no-op for terminal states. */
    public void cancel(long orderId) {
        ChildOrder o = orders.get(orderId);
        if (o == null) {
            throw new IllegalArgumentException("unknown order_id " + orderId);
        }
        if (o.state == OrderState.FILLED || o.state == OrderState.CANCELLED) {
            return;
        }
        o.state = OrderState.CANCELLED;
        o.resting = false;
        pending.remove(Long.valueOf(orderId));
        resting.remove(Long.valueOf(orderId));
    }

    /** Cancel every non-terminal order (end of session). */
    public void cancelAll() {
        while (!pending.isEmpty()) {
            cancel(pending.get(0));
        }
        while (!resting.isEmpty()) {
            cancel(resting.get(0));
        }
    }

    private double fillFee(ChildOrder o, long priceTicks, long qty, Liquidity liq) {
        VenueSpec v = config.venue(o.venueId);
        if (v.isFx()) {
            InstrumentSpec ins = config.instrument(o.instrumentId);
            double notional = (double) qty * ins.lotSize()
                    * (double) priceTicks * ins.tickSize();
            return v.commissionPerMillion() * notional / 1e6;
        }
        if (liq == Liquidity.TAKER) {
            return v.takerFeePerShare() * (double) qty;
        }
        return -v.makerRebatePerShare() * (double) qty;
    }

    private void emitFill(ChildOrder o, long priceTicks, long qty, long ts,
            Liquidity liq) {
        double impact = 0.0;
        if (liq == Liquidity.TAKER) {
            // Pinned rule 6: linear impact from the child's total size.
            InstrumentSpec ins = config.instrument(o.instrumentId);
            double impactBps = config.impactCoeffBpsPerPctAdv
                    * ((double) o.qty / ins.adv() * 100.0);
            double notional = (double) qty * ins.lotSize()
                    * (double) priceTicks * ins.tickSize();
            impact = impactBps * 1e-4 * notional;
        }
        fills.add(new Fill(nextFillId++, o.orderId, o.parentId, o.instrumentId,
                o.venueId, o.side, priceTicks, qty, ts, liq,
                fillFee(o, priceTicks, qty, liq), impact));
        o.remaining -= qty;
        if (o.remaining == 0) {
            o.state = OrderState.FILLED;
            o.resting = false;
        }
    }

    private void aggressiveFill(ChildOrder o, OrderBook book) {
        int opp = o.side == 0 ? Side.ASK : Side.BID;
        // Displayed opposite depth, best first (pinned rule 3).
        long[][] depth = book.depth(opp, OrderBook.DEPTH_LEVELS);
        if (o.type == OrderType.FOK) {
            long avail = 0;
            for (long[] lvl : depth) {
                if (withinLimit(o, lvl[0])) {
                    avail += lvl[1];
                }
            }
            if (avail < o.remaining) {
                return; // all-or-none: no fills at all
            }
        }
        for (long[] lvl : depth) {
            if (o.remaining == 0) {
                break;
            }
            if (!withinLimit(o, lvl[0])) {
                break; // levels are sorted best-first
            }
            long take = Math.min(o.remaining, lvl[1]);
            emitFill(o, lvl[0], take, o.arrivalTs, Liquidity.TAKER);
        }
    }

    private static boolean withinLimit(ChildOrder o, long price) {
        if (o.type == OrderType.MARKET) {
            return true;
        }
        return o.side == 0 ? price <= o.limitTicks : price >= o.limitTicks;
    }

    /** Displayed qty at (side, price) on a venue book, 0 when absent. */
    private static long levelQty(OrderBook book, int side, long price) {
        for (long[] lvl : book.sideLevels(side)) {
            if (lvl[0] == price) {
                return lvl[1];
            }
        }
        return 0;
    }

    private void activate(ChildOrder o) {
        OrderBook book = venueBook(o.instrumentId, o.venueId);
        if (book != null) {
            aggressiveFill(o, book);
        }
        if (o.remaining == 0) {
            return; // fully filled aggressively
        }
        switch (o.type) {
            case LIMIT -> {
                // Rest passively: queue position = displayed qty at our level.
                o.state = OrderState.ACTIVE;
                o.resting = true;
                int side = o.side == 0 ? Side.BID : Side.ASK;
                o.aheadQty = book != null ? levelQty(book, side, o.limitTicks) : 0;
                // Crossing exemption: the display may still show the
                // liquidity our aggressive leg just consumed (rule 4).
                if (book != null) {
                    long[] opp = o.side == 0 ? book.bestAsk() : book.bestBid();
                    o.crossExempt = opp != null
                            && (o.side == 0 ? opp[0] <= o.limitTicks
                                            : opp[0] >= o.limitTicks);
                }
                resting.add(o.orderId);
            }
            case MARKET, IOC, FOK ->
                // Unfilled remainder is cancelled (pinned rule 3).
                o.state = OrderState.CANCELLED;
        }
    }

    /**
     * Queue tracking for observed consumption of displayed liquidity at one
     * price level (EXECUTE events and marketable-ADD expansion).
     */
    private void trackConsumption(long instrumentId, int venueId, int side,
            long priceTicks, long qty, long ts) {
        for (int i = 0; i < resting.size();) {
            ChildOrder o = orders.get(resting.get(i));
            if (o.instrumentId == instrumentId && o.venueId == venueId
                    && o.side == side && o.state == OrderState.ACTIVE) {
                if (priceTicks == o.limitTicks) {
                    long dec = Math.min(o.aheadQty, qty);
                    o.aheadQty -= dec;
                    long leftover = qty - dec;
                    if (leftover > 0) {
                        emitFill(o, o.limitTicks, Math.min(leftover, o.remaining),
                                ts, Liquidity.MAKER);
                    }
                } else {
                    // Consumption strictly worse than our price: the market
                    // traded through our level — full fill at our limit.
                    boolean through = o.side == 0 ? priceTicks < o.limitTicks
                                                  : priceTicks > o.limitTicks;
                    if (through) {
                        emitFill(o, o.limitTicks, o.remaining, ts, Liquidity.MAKER);
                    }
                }
            }
            if (o.state == OrderState.FILLED) {
                resting.remove(i);
            } else {
                i++;
            }
        }
    }

    /**
     * Process one market event (activation, queue tracking, book application,
     * crossing check — in the pinned order).
     */
    public void onEvent(MarketEvent ev) {
        long t = ev.exchangeTs;

        // 1. Activate due orders against the pre-event book state (rule 2).
        while (!pending.isEmpty()) {
            long id = pending.get(0);
            ChildOrder o = orders.get(id);
            if (o.arrivalTs > t) {
                break;
            }
            pending.remove(0);
            activate(o);
        }

        // 2. Passive queue tracking on the raw event (rule 4), before the
        //    book is mutated.
        int et = ev.eventType;
        if (!resting.isEmpty()) {
            if (et == EventType.EXECUTE) {
                trackConsumption(ev.instrumentId, ev.venueId, ev.side,
                        ev.priceTicks, ev.qty, t);
            } else if (et == EventType.CANCEL) {
                for (long id : resting) {
                    ChildOrder o = orders.get(id);
                    if (o.instrumentId == ev.instrumentId
                            && o.venueId == ev.venueId && o.side == ev.side
                            && ev.priceTicks == o.limitTicks
                            && o.state == OrderState.ACTIVE) {
                        o.aheadQty -= Math.min(o.aheadQty, ev.qty);
                    }
                }
            } else if (et == EventType.ADD) {
                // Marketable-ADD expansion (rule 4): the replayed book
                // matches a crossing ADD internally without EXECUTE events;
                // walk the pre-event displayed opposite depth and track the
                // consumption.
                OrderBook vb = venueBook(ev.instrumentId, ev.venueId);
                if (vb != null) {
                    int consumedSide = ev.side == 0 ? 1 : 0;
                    long[][] depth = vb.depth(
                            consumedSide == 0 ? Side.BID : Side.ASK,
                            OrderBook.DEPTH_LEVELS);
                    long incoming = ev.qty;
                    for (long[] lvl : depth) {
                        if (incoming <= 0) {
                            break;
                        }
                        boolean crosses = ev.side == 0 ? lvl[0] <= ev.priceTicks
                                                       : lvl[0] >= ev.priceTicks;
                        if (!crosses) {
                            break;
                        }
                        long consumed = Math.min(incoming, lvl[1]);
                        trackConsumption(ev.instrumentId, ev.venueId,
                                consumedSide, lvl[0], consumed, t);
                        incoming -= consumed;
                    }
                }
            }
        }

        // 3. Apply the event to the replayed books.
        instrumentBook(ev.instrumentId).apply(ev);

        // 4. Post-apply crossing check (rule 4, last bullet).
        OrderBook book = venueBook(ev.instrumentId, ev.venueId);
        if (book != null) {
            for (int i = 0; i < resting.size();) {
                ChildOrder o = orders.get(resting.get(i));
                boolean filled = false;
                if (o.instrumentId == ev.instrumentId && o.venueId == ev.venueId
                        && o.state == OrderState.ACTIVE) {
                    long[] opp = o.side == 0 ? book.bestAsk() : book.bestBid();
                    boolean crossed = opp != null
                            && (o.side == 0 ? opp[0] <= o.limitTicks
                                            : opp[0] >= o.limitTicks);
                    if (!crossed) {
                        o.crossExempt = false; // display uncrossed: exemption ends
                    } else if (!o.crossExempt) {
                        emitFill(o, o.limitTicks, o.remaining, t, Liquidity.MAKER);
                        filled = true;
                    }
                }
                if (filled) {
                    resting.remove(i);
                } else {
                    i++;
                }
            }
        }
    }
}
