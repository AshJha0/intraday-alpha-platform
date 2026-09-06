package com.iap.execution;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.SortedMap;
import java.util.TreeMap;

import com.iap.core.EventType;
import com.iap.core.MarketEvent;
import com.iap.core.SessionStatus;
import com.iap.core.Side;
import com.iap.core.SplitMix64;
import com.iap.orderbook.ConsolidatedBook;
import com.iap.orderbook.OrderBook;

/**
 * Production-grade event-driven execution simulator (spec sections 17-18),
 * matching the C++ reference (cpp/include/iap/execution/execution.hpp)
 * EXACTLY — that port generated tests/golden/expected_replay_fills.json
 * (v2). PLATFORM_CONVENTIONS.md §11 is the contract.
 *
 * <p>PINNED RULES:
 * <ol>
 *   <li><b>Latency</b>: a child decided at decision_ts arrives at
 *       {@code decision_ts + decision_ns + risk_ns + wire_ns +
 *       venue.latency_mean_ns + jitter}, jitter =
 *       {@code SplitMix64(seed).below(venue.latency_jitter_ns + 1)} — one
 *       draw per submitted order OR cancel, in submission order (no draw
 *       when the venue jitter is 0).</li>
 *   <li><b>Activation</b>: a pending order becomes active while processing
 *       the first market event with {@code exchange_ts >= arrival_ts},
 *       BEFORE that event is applied to the books; orders activate in
 *       (arrival_ts, order_id) order. Aggressive fills are stamped with
 *       arrival_ts.</li>
 *   <li><b>Aggressive execution</b> (MARKET, and the marketable part of
 *       LIMIT/IOC/FOK) walks the DISPLAYED top-10 depth of the target
 *       venue's opposite side, best price first, up to the limit price. One
 *       fill per price level. Simulated orders never mutate the replayed
 *       book. Unfilled MARKET/IOC remainders are cancelled
 *       (UNFILLED_REMAINDER); FOK fills fully or not at all.
 *       <b>3b — displayed-liquidity consumption</b>: a per-(instrument,
 *       venue, side, price) overlay of what OUR aggressive fills already
 *       consumed; walks see {@code displayed - consumed} and debit it; when
 *       an applied event changes a level's displayed size the entry becomes
 *       {@code min(consumed, new displayed)} (0 removes it). Two children
 *       on the same display share one copy of the liquidity.</li>
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
 *       qty * qty_unit * price_ticks * tick_size.</li>
 *   <li><b>Linear impact</b> (aggressive fills only, identical to the
 *       research cost model): impact_bps = impact_coeff_bps_per_pct_adv *
 *       (child_qty * qty_unit / adv * 100); each taker fill is charged
 *       impact_bps * 1e-4 * its own notional.</li>
 *   <li><b>Cancels and time-in-force</b>: {@link #cancel(long, long)}
 *       travels the same latency path (one jitter draw) and takes effect at
 *       max(cancel arrival, order arrival) — never overtaking its order —
 *       merged with activations in time order (activation first on ties);
 *       an order that fills before the cancel arrives is filled.
 *       {@link #cancelAll()} is the immediate end-of-stream sweep. An order
 *       with {@code expireTs != 0} is expired (EXPIRED), pending or
 *       resting, at the start of the first event with
 *       {@code exchange_ts >= expireTs}, before any activation.</li>
 *   <li><b>Venue trading-state gate</b>: while the venue book is missing,
 *       stale or not TRADING, nothing fills on that venue: aggressive
 *       arrivals do not execute (MARKET/IOC/FOK cancelled
 *       VENUE_NOT_TRADING, LIMIT rests), resting orders ignore observed
 *       consumption and the crossing check is skipped. On the first event
 *       after which the venue is open again, crossed resting orders fill in
 *       full at the TOUCH (uncross) price.</li>
 *   <li><b>Event order</b>: expiries; activations + cancel arrivals; passive
 *       queue tracking; book update; overlay cap; crossing check.</li>
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
    private final ArrayList<Long> cancels = new ArrayList<>(); // (effective, id)
    /** Rule 3b overlay: "instrument|venue|side|price" -> consumed qty. */
    private final TreeMap<String, Long> consumed = new TreeMap<>();
    private final ExecCounters counters;
    private final ArrayList<Fill> fills = new ArrayList<>(256);
    private long nextOrderId = 1;
    private long nextFillId = 1;

    public ExecutionSimulator(ExecConfig config) {
        this.config = config;
        this.rng = new SplitMix64(config.seed);
        this.counters = new ExecCounters();
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
        this.cancels.addAll(o.cancels);
        this.consumed.putAll(o.consumed);
        this.counters = o.counters.copy();
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

    /** Named counters (live view). */
    public ExecCounters counters() {
        return counters;
    }

    /** The order id the next {@link #submit} will assign (deterministic). */
    public long nextOrderId() {
        return nextOrderId;
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

    /** True when the venue book exists, is not stale and is TRADING (rule 8). */
    public static boolean venueOpen(OrderBook book) {
        return book != null && !book.isStale()
                && book.status() == SessionStatus.TRADING;
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
        if (child.expireTs < 0) {
            throw new IllegalArgumentException("expire_ts must be >= 0");
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
        o.cancelReason = CancelReason.NONE;
        o.cancelArrivalTs = 0;
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

    private void terminate(ChildOrder o, CancelReason reason) {
        o.state = OrderState.CANCELLED;
        o.cancelReason = reason;
        o.resting = false;
        Long boxed = o.orderId;
        pending.remove(boxed);
        resting.remove(boxed);
        cancels.remove(boxed);
    }

    /**
     * Request a cancel at decision time {@code cancelTs} (rule 7: latency
     * path; no-op for terminal states or when a cancel is already in flight;
     * throws on an unknown id).
     */
    public void cancel(long orderId, long cancelTs) {
        ChildOrder o = orders.get(orderId);
        if (o == null) {
            throw new IllegalArgumentException("unknown order_id " + orderId);
        }
        if (o.state == OrderState.FILLED || o.state == OrderState.CANCELLED
                || o.cancelArrivalTs != 0) {
            return;
        }
        VenueSpec v = config.venue(o.venueId);
        long jitter = v.latencyJitterNs() > 0 ? rng.below(v.latencyJitterNs() + 1) : 0;
        long arrival = cancelTs + config.latency.decisionNs()
                + config.latency.riskNs() + config.latency.wireNs()
                + v.latencyMeanNs() + jitter;
        o.cancelArrivalTs = Math.max(arrival, o.arrivalTs);
        int pos = cancels.size();
        for (int i = 0; i < cancels.size(); i++) {
            ChildOrder other = orders.get(cancels.get(i));
            if (other.cancelArrivalTs > o.cancelArrivalTs
                    || (other.cancelArrivalTs == o.cancelArrivalTs
                            && other.orderId > orderId)) {
                pos = i;
                break;
            }
        }
        cancels.add(pos, orderId);
    }

    /** Cancel every non-terminal order (end of stream, immediate). */
    public void cancelAll() {
        while (!pending.isEmpty()) {
            terminate(orders.get(pending.get(0)), CancelReason.END_OF_STREAM);
        }
        while (!resting.isEmpty()) {
            terminate(orders.get(resting.get(0)), CancelReason.END_OF_STREAM);
        }
        cancels.clear();
    }

    private double fillFee(ChildOrder o, long priceTicks, long qty, Liquidity liq) {
        VenueSpec v = config.venue(o.venueId);
        if (v.isFx()) {
            InstrumentSpec ins = config.instrument(o.instrumentId);
            double notional = (double) qty * ins.qtyUnit()
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
            // Pinned rule 6: linear impact from the child's total size in
            // base units (qty * qty_unit), identical to the research model.
            InstrumentSpec ins = config.instrument(o.instrumentId);
            double impactBps = config.impactCoeffBpsPerPctAdv
                    * ((double) o.qty * ins.qtyUnit() / ins.adv() * 100.0);
            double notional = (double) qty * ins.qtyUnit()
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
            cancels.remove(Long.valueOf(o.orderId));
        }
    }

    private static String overlayKey(long instrumentId, int venueId, int side,
            long price) {
        return instrumentId + "|" + venueId + "|" + side + "|" + price;
    }

    private long consumedAt(long instrumentId, int venueId, int side, long price) {
        Long c = consumed.get(overlayKey(instrumentId, venueId, side, price));
        return c == null ? 0 : c;
    }

    private void aggressiveFill(ChildOrder o, OrderBook book) {
        int opp = o.side == 0 ? Side.ASK : Side.BID;
        // Displayed opposite depth, best first (rule 3), net of what our own
        // earlier fills already consumed (rule 3b).
        long[][] depth = book.depth(opp, OrderBook.DEPTH_LEVELS);
        if (o.type == OrderType.FOK) {
            long avail = 0;
            for (long[] lvl : depth) {
                if (withinLimit(o, lvl[0])) {
                    avail += available(o, opp, lvl[0], lvl[1]);
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
            long avail = available(o, opp, lvl[0], lvl[1]);
            if (avail < lvl[1]) {
                counters.overlayThinnedFills++;
            }
            if (avail <= 0) {
                continue;
            }
            long take = Math.min(o.remaining, avail);
            consumed.merge(overlayKey(o.instrumentId, o.venueId, opp, lvl[0]),
                    take, Long::sum);
            emitFill(o, lvl[0], take, o.arrivalTs, Liquidity.TAKER);
        }
    }

    private long available(ChildOrder o, int oppSide, long price, long displayed) {
        return Math.max(displayed - consumedAt(o.instrumentId, o.venueId,
                oppSide, price), 0);
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
        boolean open = venueOpen(book);
        if (open) {
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
                // liquidity our aggressive leg just consumed (rule 4). While
                // gated (rule 8) nothing was consumed: no exemption.
                o.crossExempt = false;
                if (open) {
                    long[] opp = o.side == 0 ? book.bestAsk() : book.bestBid();
                    o.crossExempt = opp != null
                            && (o.side == 0 ? opp[0] <= o.limitTicks
                                            : opp[0] >= o.limitTicks);
                }
                resting.add(o.orderId);
            }
            case MARKET, IOC, FOK -> {
                // Unfilled remainder is cancelled (rule 3 / rule 8).
                if (open) {
                    terminate(o, CancelReason.UNFILLED_REMAINDER);
                } else {
                    counters.venueNotTradingCancels++;
                    terminate(o, CancelReason.VENUE_NOT_TRADING);
                }
            }
        }
    }

    private void expireDue(long t) {
        ArrayList<Long> due = new ArrayList<>();
        for (long id : pending) {
            ChildOrder o = orders.get(id);
            if (o.expireTs != 0 && o.expireTs <= t) {
                due.add(id);
            }
        }
        for (long id : resting) {
            ChildOrder o = orders.get(id);
            if (o.expireTs != 0 && o.expireTs <= t) {
                due.add(id);
            }
        }
        java.util.Collections.sort(due);
        for (long id : due) {
            counters.expiredOrders++;
            terminate(orders.get(id), CancelReason.EXPIRED);
        }
    }

    private void activateAndCancelDue(long t) {
        while (true) {
            boolean haveAct = !pending.isEmpty()
                    && orders.get(pending.get(0)).arrivalTs <= t;
            boolean haveCxl = !cancels.isEmpty()
                    && orders.get(cancels.get(0)).cancelArrivalTs <= t;
            if (!haveAct && !haveCxl) {
                break;
            }
            boolean doAct = haveAct;
            if (haveAct && haveCxl) {
                long ta = orders.get(pending.get(0)).arrivalTs;
                long tc = orders.get(cancels.get(0)).cancelArrivalTs;
                doAct = ta <= tc;
            }
            if (doAct) {
                long id = pending.remove(0);
                activate(orders.get(id));
            } else {
                long id = cancels.remove(0);
                ChildOrder o = orders.get(id);
                if (o.state != OrderState.FILLED && o.state != OrderState.CANCELLED) {
                    counters.userCancels++;
                    terminate(o, CancelReason.USER);
                }
            }
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

    private void crossingCheck(MarketEvent ev, OrderBook book, boolean reopened) {
        long t = ev.exchangeTs;
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
                } else if (reopened) {
                    // Rule 8: uncross at the touch, not at the limit.
                    counters.reopenTouchFills++;
                    emitFill(o, opp[0], o.remaining, t, Liquidity.MAKER);
                    filled = true;
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

    /** Process one market event (rule 9 order). */
    public void onEvent(MarketEvent ev) {
        long t = ev.exchangeTs;

        // 1. Expiries, then activations + cancel arrivals (rules 7, 2).
        expireDue(t);
        activateAndCancelDue(t);

        // 2. Passive queue tracking on the raw event (rule 4), before the
        //    book is mutated — only while the venue is open (rule 8).
        int et = ev.eventType;
        OrderBook pre = venueBook(ev.instrumentId, ev.venueId);
        boolean preOpen = venueOpen(pre);
        if (!resting.isEmpty() && preOpen) {
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
                // Marketable-ADD expansion (rule 4).
                int consumedSide = ev.side == 0 ? 1 : 0;
                long[][] depth = pre.depth(
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
                    long c = Math.min(incoming, lvl[1]);
                    trackConsumption(ev.instrumentId, ev.venueId,
                            consumedSide, lvl[0], c, t);
                    incoming -= c;
                }
            }
        }

        // 3. Snapshot the displayed sizes behind this venue's overlay
        //    entries, apply the event, cap the entries whose display
        //    changed (rule 3b).
        String prefix = ev.instrumentId + "|" + ev.venueId + "|";
        ArrayList<String> watched = new ArrayList<>();
        ArrayList<Long> before = new ArrayList<>();
        for (Map.Entry<String, Long> e : consumed.tailMap(prefix).entrySet()) {
            if (!e.getKey().startsWith(prefix)) {
                break;
            }
            watched.add(e.getKey());
            before.add(pre == null ? 0L : overlayLevelQty(pre, e.getKey()));
        }
        instrumentBook(ev.instrumentId).apply(ev);
        OrderBook book = venueBook(ev.instrumentId, ev.venueId);
        for (int i = 0; i < watched.size(); i++) {
            long after = book == null ? 0L : overlayLevelQty(book, watched.get(i));
            if (after != before.get(i)) {
                long c = Math.min(consumed.get(watched.get(i)), after);
                if (c <= 0) {
                    consumed.remove(watched.get(i));
                } else {
                    consumed.put(watched.get(i), c);
                }
            }
        }

        // 4. Post-apply crossing check (rule 4 last bullet / rule 8 reopen).
        if (venueOpen(book)) {
            crossingCheck(ev, book, !preOpen);
        }
    }

    private static long overlayLevelQty(OrderBook book, String key) {
        String[] parts = key.split("\\|");
        int side = Integer.parseInt(parts[2]);
        long price = Long.parseLong(parts[3]);
        return levelQty(book, side, price);
    }
}
