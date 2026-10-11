package com.iap.orderbook;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

import com.iap.core.EventType;
import com.iap.core.MarketEvent;
import com.iap.core.SessionStatus;
import com.iap.core.Side;
import com.iap.core.Validation;

/**
 * MBO order book for one instrument on one venue (venueId=0: accept any venue).
 * Pinned semantics from PLATFORM_CONVENTIONS.md section 4 / API_CORE.md
 * section 4, mirroring the Python reference {@code iap/orderbook/book.py}:
 *
 * <ul>
 *   <li>ADD: FIFO tail of its (side, price) level; while the status is
 *       TRADING a crossing limit ADD executes against the opposite side from
 *       the best level's FIFO head (marketable), leftover posts; while
 *       HALT/AUCTION/CLOSE nothing matches (the ADD rests, the book may be
 *       crossed); duplicate order_id dropped + counted.</li>
 *   <li>MODIFY: qty change only; a non-zero price that differs from the
 *       resting price is dropped + counted ({@code modify_price_mismatch});
 *       decrease keeps queue position, increase moves to level tail, &lt;= 0
 *       removes.</li>
 *   <li>CANCEL: remove by order_id; EXECUTE: partial fill keeps position,
 *       removed at 0, never touches trade_flow.</li>
 *   <li>TRADE: trade_flow += qty (BID aggressor) / -= qty (ASK aggressor).</li>
 *   <li>QUOTE (FX): replaces the venue's whole side at L1; {@code order_id
 *       == 0} uses the synthetic id {@link #syntheticOrderId}; an explicit id
 *       resting on the other side is dropped + counted.</li>
 *   <li>SNAPSHOT: recovery burst; first record clears both sides, the record
 *       with trade_id == 0 ends the burst and clears {@code stale} — unless a
 *       sequence gap occurred INSIDE the burst, which marks it broken: a
 *       broken burst still ends at its trade_id == 0 record but leaves
 *       {@code stale} set; only a later complete gap-free burst clears it.</li>
 *   <li>Side domain: for side-indexed event types (ADD/QUOTE/SNAPSHOT/TRADE)
 *       {@code side} must be BID (0) or ASK (1); side &gt; 1 is malformed:
 *       dropped + counted ({@code invalid_side_dropped}) after its sequence
 *       number is consumed, never raised mid-stream.</li>
 *   <li>Malformed payloads (qty/price domain, order_id 0, reserved ids,
 *       bad STATUS code, i64 overflow) and unknown event types are dropped +
 *       counted ({@code invalid_payload_dropped}, {@code unknown_type_dropped})
 *       after the sequence number is consumed; SNAPSHOT countdowns are
 *       validated ({@code snapshot_restarts}); id-less SNAPSHOT records get
 *       synthetic ids.</li>
 *   <li>Sequencing: the first event of an epoch is accepted whatever its
 *       sequence; duplicates (sequence &lt;= last) dropped + counted; gaps
 *       mark the book stale + counted unless a {@code reorderWindow} holds
 *       the event back until the hole fills ({@code late_recovered}); a
 *       SNAPSHOT burst starting below the last sequence is a venue sequence
 *       reset ({@code sequence_resets}); while stale only SNAPSHOT/STATUS/
 *       TRADE/HEARTBEAT are applied, the rest dropped + counted.</li>
 * </ul>
 *
 * <p>Allocation-conscious: order nodes are pooled (free list) and looked up in
 * an open-addressing primitive-long hash map; per-level FIFO queues are
 * intrusive doubly-linked lists. Price levels live in TreeMaps (sorted, and
 * never iterated in unordered fashion on any deterministic path).
 */
public final class OrderBook {
    public static final int DEPTH_LEVELS = 10;
    /** Largest accepted reorder window (hold-back buffer, events) — pinned. */
    public static final int MAX_REORDER_WINDOW = 4096;
    /** Reserved synthetic order-id range: every id with the top 16 bits set. */
    public static final long SYNTHETIC_ID_BASE = Validation.SYNTHETIC_ID_BASE;

    /** Deterministic synthetic order id for id-less QUOTE/SNAPSHOT records. */
    public static long syntheticOrderId(int side, long ordinal) {
        return SYNTHETIC_ID_BASE | ((long) side << 40) | (ordinal & ((1L << 40) - 1));
    }

    private static boolean addOverflows(long a, long b) {
        long r = a + b;
        return ((a ^ r) & (b ^ r)) < 0;
    }

    /** One resting order: intrusive FIFO list node. */
    private static final class Order {
        long orderId;
        long qty;
        long arrival; // global arrival stamp (monotone per insertion)
        Order prev;
        Order next;
        Level level;
    }

    /** One price level: FIFO queue of orders plus cached totals. */
    private static final class Level {
        int side;
        long price;
        long totalQty;
        int orderCount;
        Order head;
        Order tail;
    }

    /** Open-addressing long -&gt; Order map (linear probing, tombstones). */
    private static final class OrderMap {
        private static final Order TOMBSTONE = new Order();
        private long[] keys = new long[64];
        private Order[] vals = new Order[64];
        private int size; // live entries with key != 0
        private int used; // live + tombstones
        private Order zeroVal;

        private static int slot(long key, int mask) {
            long h = key * 0x9E3779B97F4A7C15L;
            h ^= h >>> 32;
            return (int) h & mask;
        }

        Order get(long key) {
            if (key == 0) {
                return zeroVal;
            }
            int mask = keys.length - 1;
            int i = slot(key, mask);
            while (true) {
                Order v = vals[i];
                if (v == null) {
                    return null;
                }
                if (v != TOMBSTONE && keys[i] == key) {
                    return v;
                }
                i = (i + 1) & mask;
            }
        }

        void put(long key, Order value) {
            if (key == 0) {
                zeroVal = value;
                return;
            }
            if ((used + 1) * 10 >= keys.length * 7) {
                rehash();
            }
            int mask = keys.length - 1;
            int i = slot(key, mask);
            int firstTomb = -1;
            while (true) {
                Order v = vals[i];
                if (v == null) {
                    if (firstTomb >= 0) {
                        i = firstTomb;
                    } else {
                        used++;
                    }
                    keys[i] = key;
                    vals[i] = value;
                    size++;
                    return;
                }
                if (v == TOMBSTONE) {
                    if (firstTomb < 0) {
                        firstTomb = i;
                    }
                } else if (keys[i] == key) {
                    vals[i] = value;
                    return;
                }
                i = (i + 1) & mask;
            }
        }

        Order remove(long key) {
            if (key == 0) {
                Order v = zeroVal;
                zeroVal = null;
                return v;
            }
            int mask = keys.length - 1;
            int i = slot(key, mask);
            while (true) {
                Order v = vals[i];
                if (v == null) {
                    return null;
                }
                if (v != TOMBSTONE && keys[i] == key) {
                    vals[i] = TOMBSTONE;
                    size--;
                    return v;
                }
                i = (i + 1) & mask;
            }
        }

        int size() {
            return size + (zeroVal != null ? 1 : 0);
        }

        void clear() {
            java.util.Arrays.fill(vals, null);
            size = 0;
            used = 0;
            zeroVal = null;
        }

        private void rehash() {
            int newCap = 64;
            while ((size + 1) * 10 >= newCap * 7) {
                newCap <<= 1;
            }
            long[] oldKeys = keys;
            Order[] oldVals = vals;
            keys = new long[newCap];
            vals = new Order[newCap];
            int liveBefore = size;
            size = 0;
            used = 0;
            for (int i = 0; i < oldVals.length; i++) {
                Order v = oldVals[i];
                if (v != null && v != TOMBSTONE) {
                    put(oldKeys[i], v);
                }
            }
            used = size;
            if (size != liveBefore) {
                throw new IllegalStateException("OrderMap rehash lost entries");
            }
        }
    }

    public final long instrumentId;
    public final int venueId;
    private final int reorderWindow;

    private final TreeMap<Long, Level> bids = new TreeMap<>(Comparator.reverseOrder());
    private final TreeMap<Long, Level> asks = new TreeMap<>();
    private final OrderMap orders = new OrderMap();
    private final ArrayDeque<Order> orderPool = new ArrayDeque<>();
    private final ArrayDeque<Level> levelPool = new ArrayDeque<>();
    /** Hold-back buffer keyed by (unsigned) sequence. */
    private final TreeMap<Long, MarketEvent> pending = new TreeMap<>(Long::compareUnsigned);

    private long lastSequence;
    private boolean hasSequence;
    private long sequenceEpoch;
    private long exchangeTs;
    private long receiveTs;
    private long tradeFlow;
    private long status = SessionStatus.TRADING;
    private boolean stale;
    private boolean snapshotActive;
    private boolean snapshotBroken;
    private long snapshotCountdown;
    private final long[] snapshotSyntheticNext = new long[2];
    private long arrivalCounter;
    private long duplicatesDropped;
    private long gapsDetected;
    private long droppedWhileStale;
    private long unknownOrderEvents;
    private long invalidSideDropped;
    private long invalidPayloadDropped;
    private long unknownTypeDropped;
    private long modifyPriceMismatch;
    private long snapshotRestarts;
    private long sequenceResets;
    private long lateRecovered;
    private long eventsApplied;

    public OrderBook(long instrumentId, int venueId) {
        this(instrumentId, venueId, 0);
    }

    /**
     * @param reorderWindow hold-back buffer for late retransmissions (0 = off,
     *     at most {@link #MAX_REORDER_WINDOW})
     */
    public OrderBook(long instrumentId, int venueId, int reorderWindow) {
        if (reorderWindow < 0 || reorderWindow > MAX_REORDER_WINDOW) {
            throw new IllegalArgumentException("reorderWindow must be in [0, "
                    + MAX_REORDER_WINDOW + "]: " + reorderWindow);
        }
        this.instrumentId = instrumentId;
        this.venueId = venueId;
        this.reorderWindow = reorderWindow;
    }

    // -------------------------------------------------------------- applying

    /**
     * Apply one event (sequence-checked). Throws only on routing errors;
     * every malformed event is dropped + counted.
     *
     * @return the pinned per-event verdict: downstream consumers (the feature
     *     engine, API_FEATURES.md section 2) fold ONLY APPLIED events into
     *     rolling state.
     */
    public ApplyStatus apply(MarketEvent ev) {
        if (ev.instrumentId != instrumentId || (venueId != 0 && ev.venueId != venueId)) {
            throw new IllegalArgumentException("event routed to wrong book: event "
                    + ev.instrumentId + "@" + ev.venueId + ", book "
                    + instrumentId + "@" + venueId);
        }
        if (reorderWindow != 0 && hasSequence
                && Long.compareUnsigned(ev.sequence, lastSequence) > 0
                && Long.compareUnsigned(ev.sequence - lastSequence, 1) > 0) {
            // Out-of-sequence event ahead of a hole: hold it back until the
            // missing sequences arrive (bounded by reorderWindow).
            if (pending.containsKey(ev.sequence)) {
                duplicatesDropped++;
                return ApplyStatus.DROPPED;
            }
            if (pending.size() < reorderWindow) {
                pending.put(ev.sequence, ev);
                return ApplyStatus.HELD;
            }
            // Buffer full: give up on the hole, declare the gap and apply
            // everything held so far in sequence order.
            pending.put(ev.sequence, ev);
            return flushPending(ev.sequence, true);
        }
        ApplyStatus status = applySequenced(ev, false);
        if (!pending.isEmpty()) {
            drainPending();
        }
        return status;
    }

    private ApplyStatus flushPending() {
        return flushPending(0L, false);
    }

    /**
     * Apply every held-back event in sequence order (gap declared); returns
     * the verdict of {@code target} when the caller tracks one.
     */
    private ApplyStatus flushPending(long target, boolean hasTarget) {
        List<MarketEvent> held = new ArrayList<>(pending.values());
        pending.clear();
        ApplyStatus status = ApplyStatus.APPLIED;
        for (MarketEvent pev : held) {
            ApplyStatus st = applySequenced(pev, true);
            if (hasTarget && pev.sequence == target) {
                status = st;
            }
        }
        return status;
    }

    private void drainPending() {
        while (!pending.isEmpty()) {
            MarketEvent pev = pending.remove(lastSequence + 1);
            if (pev == null) {
                return;
            }
            applySequenced(pev, true);
        }
    }

    /**
     * Explicit venue sequence reset (session roll known out of band): held
     * events are flushed, then a new epoch starts (next event accepted
     * whatever its sequence) with the book stale until a complete burst.
     */
    public void resetSequence() {
        if (!pending.isEmpty()) {
            flushPending();
        }
        hasSequence = false;
        sequenceEpoch++;
        sequenceResets++;
        stale = true;
        snapshotActive = false;
        snapshotBroken = false;
        snapshotCountdown = 0;
    }

    private static boolean payloadOk(MarketEvent ev) {
        switch (ev.eventType) {
            case EventType.ADD:
                return ev.orderId != 0 && ev.qty > 0 && ev.priceTicks > 0
                        && Long.compareUnsigned(ev.orderId, SYNTHETIC_ID_BASE) < 0;
            case EventType.MODIFY:
            case EventType.CANCEL:
                return ev.orderId != 0;
            case EventType.EXECUTE:
                return ev.orderId != 0 && ev.qty > 0;
            case EventType.QUOTE:
            case EventType.SNAPSHOT:
                return ev.qty > 0 && ev.priceTicks > 0
                        && Long.compareUnsigned(ev.orderId, SYNTHETIC_ID_BASE) < 0;
            case EventType.TRADE:
                return ev.qty > 0 && ev.priceTicks > 0;
            case EventType.STATUS:
                return SessionStatus.isValid(ev.qty);
            default:
                return true; // HEARTBEAT
        }
    }

    private ApplyStatus applySequenced(MarketEvent ev, boolean fromBuffer) {
        int et = ev.eventType;
        if (hasSequence) {
            if (Long.compareUnsigned(ev.sequence, lastSequence) <= 0) {
                if (et == EventType.SNAPSHOT && !snapshotActive
                        && Long.compareUnsigned(ev.sequence, lastSequence) < 0) {
                    // Venue sequence reset (daily restart / fail-over): the
                    // SNAPSHOT burst starting the new epoch recovers the book.
                    if (!pending.isEmpty()) {
                        flushPending();
                    }
                    sequenceEpoch++;
                    sequenceResets++;
                    stale = true;
                } else {
                    duplicatesDropped++;
                    return ApplyStatus.DROPPED;
                }
            } else if (Long.compareUnsigned(ev.sequence - lastSequence, 1) > 0) {
                gapsDetected++;
                stale = true;
                if (snapshotActive) {
                    // Gap inside an active SNAPSHOT burst: the burst is broken —
                    // its completion record must NOT clear `stale`.
                    snapshotBroken = true;
                }
            } else if (!pending.isEmpty() && !fromBuffer) {
                lateRecovered++; // a gap filler arrived late
            }
        }
        hasSequence = true;
        lastSequence = ev.sequence;
        exchangeTs = ev.exchangeTs;
        receiveTs = ev.receiveTs;

        // Malformed-event classes: dropped + counted (never raised), after
        // the sequence number above is consumed (pinned).
        if (!EventType.isValid(et)) {
            unknownTypeDropped++;
            return ApplyStatus.DROPPED;
        }
        if (ev.side > 1 && (et == EventType.ADD || et == EventType.QUOTE
                || et == EventType.SNAPSHOT || et == EventType.TRADE)) {
            invalidSideDropped++;
            return ApplyStatus.DROPPED;
        }
        if (!payloadOk(ev)) {
            invalidPayloadDropped++;
            return ApplyStatus.DROPPED;
        }
        if (stale && et != EventType.SNAPSHOT && et != EventType.STATUS
                && et != EventType.TRADE && et != EventType.HEARTBEAT) {
            droppedWhileStale++;
            return ApplyStatus.DROPPED;
        }

        boolean applied;
        switch (et) {
            case EventType.ADD -> applied = applyAdd(ev);
            case EventType.MODIFY -> applied = applyModify(ev);
            case EventType.CANCEL -> applied = applyCancel(ev);
            case EventType.EXECUTE -> applied = applyExecute(ev);
            case EventType.TRADE -> {
                long delta = ev.side == Side.BID ? ev.qty : -ev.qty;
                if (addOverflows(tradeFlow, delta)) {
                    invalidPayloadDropped++;
                    applied = false;
                } else {
                    tradeFlow += delta;
                    applied = true;
                }
            }
            case EventType.QUOTE -> applied = applyQuote(ev);
            case EventType.SNAPSHOT -> applied = applySnapshot(ev);
            case EventType.STATUS -> {
                status = ev.qty;
                applied = true;
            }
            default -> applied = true; // HEARTBEAT: timestamps/sequence only
        }
        if (!applied) {
            return ApplyStatus.DROPPED;
        }
        eventsApplied++;
        return ApplyStatus.APPLIED;
    }

    // ------------------------------------------------------------ primitives

    private TreeMap<Long, Level> sideTree(int side) {
        return side == Side.BID ? bids : asks;
    }

    private Order allocOrder(long orderId, long qty, Level level) {
        Order o = orderPool.pollFirst();
        if (o == null) {
            o = new Order();
        }
        o.orderId = orderId;
        o.qty = qty;
        o.level = level;
        o.prev = null;
        o.next = null;
        return o;
    }

    private void freeOrder(Order o) {
        o.level = null;
        o.prev = null;
        o.next = null;
        orderPool.addLast(o);
    }

    private Level allocLevel(int side, long price) {
        Level level = levelPool.pollFirst();
        if (level == null) {
            level = new Level();
        }
        level.side = side;
        level.price = price;
        level.totalQty = 0;
        level.orderCount = 0;
        level.head = null;
        level.tail = null;
        return level;
    }

    private void freeLevel(Level level) {
        level.head = null;
        level.tail = null;
        levelPool.addLast(level);
    }

    private static void appendTail(Level level, Order o) {
        o.next = null;
        if (level.tail == null) {
            o.prev = null;
            level.head = o;
            level.tail = o;
        } else {
            o.prev = level.tail;
            level.tail.next = o;
            level.tail = o;
        }
    }

    private static void unlink(Level level, Order o) {
        if (o.prev != null) {
            o.prev.next = o.next;
        } else {
            level.head = o.next;
        }
        if (o.next != null) {
            o.next.prev = o.prev;
        } else {
            level.tail = o.prev;
        }
        o.prev = null;
        o.next = null;
    }

    private void insertOrder(int side, long price, long orderId, long qty) {
        TreeMap<Long, Level> tree = sideTree(side);
        Long key = price;
        Level level = tree.get(key);
        if (level == null) {
            level = allocLevel(side, price);
            tree.put(key, level);
        }
        Order o = allocOrder(orderId, qty, level);
        o.arrival = ++arrivalCounter;
        appendTail(level, o);
        level.totalQty += qty;
        level.orderCount++;
        orders.put(orderId, o);
    }

    private void removeOrder(Order o) {
        Level level = o.level;
        orders.remove(o.orderId);
        unlink(level, o);
        level.totalQty -= o.qty;
        level.orderCount--;
        if (level.head == null) {
            sideTree(level.side).remove(level.price);
            freeLevel(level);
        }
        freeOrder(o);
    }

    private long levelTotal(int side, long price) {
        Level level = sideTree(side).get(price);
        return level == null ? 0 : level.totalQty;
    }

    private boolean applyAdd(MarketEvent ev) {
        if (orders.get(ev.orderId) != null) {
            unknownOrderEvents++; // duplicate order id: drop, count
            return false;
        }
        if (addOverflows(levelTotal(ev.side, ev.priceTicks), ev.qty)) {
            invalidPayloadDropped++;
            return false;
        }
        long remaining = status == SessionStatus.TRADING
                ? matchMarketable(ev.side, ev.priceTicks, ev.qty)
                : ev.qty;
        if (remaining > 0) {
            insertOrder(ev.side, ev.priceTicks, ev.orderId, remaining);
        }
        return true;
    }

    /** Execute a crossing limit against the opposite side; return leftover. */
    private long matchMarketable(int side, long price, long qty) {
        TreeMap<Long, Level> opp = side == Side.BID ? asks : bids;
        while (qty > 0) {
            Map.Entry<Long, Level> e = opp.firstEntry();
            if (e == null) {
                break;
            }
            Level best = e.getValue();
            boolean crosses = side == Side.BID ? price >= best.price : price <= best.price;
            if (!crosses) {
                break;
            }
            // Fill from FIFO head of the best opposite level.
            Order head = best.head;
            long fill = Math.min(qty, head.qty);
            qty -= fill;
            if (fill == head.qty) {
                removeOrder(head);
            } else {
                head.qty -= fill;
                best.totalQty -= fill;
            }
        }
        return qty;
    }

    private boolean applyModify(MarketEvent ev) {
        Order o = orders.get(ev.orderId);
        if (o == null) {
            unknownOrderEvents++;
            return false;
        }
        Level level = o.level;
        if (ev.priceTicks != 0 && ev.priceTicks != level.price) {
            modifyPriceMismatch++; // price change must be CANCEL+ADD
            return false;
        }
        long newQty = ev.qty;
        long oldQty = o.qty;
        if (newQty <= 0) {
            removeOrder(o);
            return true;
        }
        if (newQty <= oldQty) {
            // Decrease: keep queue position.
            o.qty = newQty;
        } else {
            if (addOverflows(level.totalQty, newQty - oldQty)) {
                invalidPayloadDropped++;
                return false;
            }
            // Increase: move to tail of the level.
            unlink(level, o);
            appendTail(level, o);
            o.qty = newQty;
        }
        level.totalQty += newQty - oldQty;
        return true;
    }

    private boolean applyCancel(MarketEvent ev) {
        Order o = orders.get(ev.orderId);
        if (o == null) {
            unknownOrderEvents++;
            return false;
        }
        removeOrder(o);
        return true;
    }

    private boolean applyExecute(MarketEvent ev) {
        Order o = orders.get(ev.orderId);
        if (o == null) {
            unknownOrderEvents++;
            return false;
        }
        long fill = Math.min(ev.qty, o.qty);
        if (fill >= o.qty) {
            removeOrder(o);
        } else {
            o.qty -= fill;
            o.level.totalQty -= fill;
        }
        return true;
    }

    /** FX QUOTE: replace this venue's whole side at L1. */
    private boolean applyQuote(MarketEvent ev) {
        long oid = ev.orderId != 0 ? ev.orderId : syntheticOrderId(ev.side, 0);
        Order resting = orders.get(oid);
        if (resting != null && resting.level.side != ev.side) {
            unknownOrderEvents++; // id rests on the other side
            return false;
        }
        clearSide(sideTree(ev.side));
        insertOrder(ev.side, ev.priceTicks, oid, ev.qty);
        return true;
    }

    private boolean applySnapshot(MarketEvent ev) {
        if (snapshotActive) {
            if (Long.compareUnsigned(ev.tradeId, snapshotCountdown) >= 0) {
                // Countdown went up (or repeated): the previous burst was
                // interrupted and this record starts a new burst.
                snapshotActive = false;
                snapshotRestarts++;
            } else if (ev.tradeId != snapshotCountdown - 1) {
                // Countdown skipped ahead: records missing — burst broken.
                snapshotBroken = true;
            }
        }
        if (!snapshotActive) {
            // Burst start: clear the whole book state (levels + orders).
            clearSide(bids);
            clearSide(asks);
            orders.clear();
            snapshotSyntheticNext[0] = 0;
            snapshotSyntheticNext[1] = 0;
            snapshotActive = true;
            snapshotBroken = false;
        }
        snapshotCountdown = ev.tradeId;
        long oid;
        if (ev.orderId != 0) {
            oid = ev.orderId;
        } else {
            oid = syntheticOrderId(ev.side, snapshotSyntheticNext[ev.side]++);
        }
        boolean ok = true;
        if (orders.get(oid) != null) {
            unknownOrderEvents++; // repeated id inside a burst
            ok = false;
        } else if (addOverflows(levelTotal(ev.side, ev.priceTicks), ev.qty)) {
            invalidPayloadDropped++;
            ok = false;
        } else {
            insertOrder(ev.side, ev.priceTicks, oid, ev.qty);
        }
        if (ev.tradeId == 0) { // last record of the burst
            snapshotActive = false;
            if (!snapshotBroken) {
                stale = false;
            }
            snapshotBroken = false;
        }
        return ok;
    }

    private void clearSide(TreeMap<Long, Level> tree) {
        for (Level level : tree.values()) {
            Order o = level.head;
            while (o != null) {
                Order next = o.next;
                orders.remove(o.orderId);
                freeOrder(o);
                o = next;
            }
            freeLevel(level);
        }
        tree.clear();
    }

    // ---------------------------------------------------------- derived state

    /** {price_ticks, total_size} of the best bid, or null. */
    public long[] bestBid() {
        Map.Entry<Long, Level> e = bids.firstEntry();
        return e == null ? null : new long[] {e.getValue().price, e.getValue().totalQty};
    }

    /** {price_ticks, total_size} of the best ask, or null. */
    public long[] bestAsk() {
        Map.Entry<Long, Level> e = asks.firstEntry();
        return e == null ? null : new long[] {e.getValue().price, e.getValue().totalQty};
    }

    /** Top-N {price_ticks, total_size} best-first. */
    public long[][] depth(int side, int levels) {
        return collect(side, levels, false);
    }

    /** Top-N {price_ticks, order_count} best-first. */
    public long[][] orderCounts(int side, int levels) {
        return collect(side, levels, true);
    }

    /** All levels of one side, best-first: {price_ticks, total_size, order_count}. */
    public long[][] sideLevels(int side) {
        TreeMap<Long, Level> tree = sideTree(side);
        long[][] out = new long[tree.size()][];
        int i = 0;
        for (Level level : tree.values()) {
            out[i++] = new long[] {level.price, level.totalQty, level.orderCount};
        }
        return out;
    }

    private long[][] collect(int side, int levels, boolean counts) {
        TreeMap<Long, Level> tree = sideTree(side);
        int n = Math.min(levels, tree.size());
        long[][] out = new long[n][];
        int i = 0;
        for (Level level : tree.values()) {
            if (i >= n) {
                break;
            }
            out[i++] = new long[] {level.price, counts ? level.orderCount : level.totalQty};
        }
        return out;
    }

    /** Total number of resting orders in the book. */
    public int orderCountTotal() {
        return orders.size();
    }

    /** Number of events currently held back in the reorder buffer. */
    public int pendingCount() {
        return pending.size();
    }

    /** True when best bid &gt; best ask (call phase / crossed feed). */
    public boolean isCrossed() {
        long[] bb = bestBid();
        long[] ba = bestAsk();
        return bb != null && ba != null && bb[0] > ba[0];
    }

    /** True when best bid == best ask. */
    public boolean isLocked() {
        long[] bb = bestBid();
        long[] ba = bestAsk();
        return bb != null && ba != null && bb[0] == ba[0];
    }

    /** Not stale and the last event was received within maxAgeNs of nowNs. */
    public boolean isFresh(long nowNs, long maxAgeNs) {
        return !stale && hasSequence && nowNs - receiveTs <= maxAgeNs;
    }

    /** Golden-comparable exact-integer state (expected_book_states.json shape). */
    public BookState stateSummary() {
        long[] bb = bestBid();
        long[] ba = bestAsk();
        return new BookState(
                bb == null ? 0 : bb[0], bb == null ? 0 : bb[1],
                ba == null ? 0 : ba[0], ba == null ? 0 : ba[1],
                depth(Side.BID, 5), depth(Side.ASK, 5),
                orderCounts(Side.BID, 3), orderCounts(Side.ASK, 3),
                tradeFlow, lastSequence);
    }

    public long lastSequence() {
        return lastSequence;
    }

    public boolean hasSequence() {
        return hasSequence;
    }

    public long sequenceEpoch() {
        return sequenceEpoch;
    }

    public int reorderWindow() {
        return reorderWindow;
    }

    public long exchangeTs() {
        return exchangeTs;
    }

    public long receiveTs() {
        return receiveTs;
    }

    public long tradeFlow() {
        return tradeFlow;
    }

    public long status() {
        return status;
    }

    public boolean isStale() {
        return stale;
    }

    public long duplicatesDropped() {
        return duplicatesDropped;
    }

    public long gapsDetected() {
        return gapsDetected;
    }

    public long droppedWhileStale() {
        return droppedWhileStale;
    }

    public long unknownOrderEvents() {
        return unknownOrderEvents;
    }

    public long invalidSideDropped() {
        return invalidSideDropped;
    }

    public long invalidPayloadDropped() {
        return invalidPayloadDropped;
    }

    public long unknownTypeDropped() {
        return unknownTypeDropped;
    }

    public long modifyPriceMismatch() {
        return modifyPriceMismatch;
    }

    public long snapshotRestarts() {
        return snapshotRestarts;
    }

    public long sequenceResets() {
        return sequenceResets;
    }

    public long lateRecovered() {
        return lateRecovered;
    }

    public long eventsApplied() {
        return eventsApplied;
    }

    /** The 12 QC counters in pinned order. */
    public BookCheckpoint.Counters counters() {
        return new BookCheckpoint.Counters(duplicatesDropped, gapsDetected, droppedWhileStale,
                unknownOrderEvents, invalidSideDropped, invalidPayloadDropped,
                unknownTypeDropped, modifyPriceMismatch, snapshotRestarts, sequenceResets,
                lateRecovered, eventsApplied);
    }

    // ------------------------------------------------------------ checkpoints

    /** Full deterministic serialization (levels in sorted (side, price) order). */
    public BookCheckpoint checkpoint() {
        List<BookCheckpoint.LevelCheckpoint> levels = new ArrayList<>(bids.size() + asks.size());
        // (side, price) ascending: side 0 by ascending price, then side 1.
        for (Level level : bids.descendingMap().values()) {
            levels.add(levelCheckpoint(level));
        }
        for (Level level : asks.values()) {
            levels.add(levelCheckpoint(level));
        }
        return new BookCheckpoint(instrumentId, venueId, levels, arrivalOrder(),
                lastSequence, hasSequence, sequenceEpoch, exchangeTs, receiveTs,
                tradeFlow, status, stale, snapshotActive, snapshotBroken,
                snapshotCountdown, snapshotSyntheticNext, reorderWindow,
                pending.values().toArray(new MarketEvent[0]), counters());
    }

    /**
     * Order ids of every resting order in global arrival (insertion) order —
     * the checkpoint field that lets {@link #restore} round-trip the global
     * queue-arrival order exactly (levels alone only pin per-level FIFO).
     */
    private long[] arrivalOrder() {
        int n = orders.size();
        long[][] stamped = new long[n][];
        int i = 0;
        for (Level level : bids.values()) {
            for (Order o = level.head; o != null; o = o.next) {
                stamped[i++] = new long[] {o.arrival, o.orderId};
            }
        }
        for (Level level : asks.values()) {
            for (Order o = level.head; o != null; o = o.next) {
                stamped[i++] = new long[] {o.arrival, o.orderId};
            }
        }
        if (i != n) {
            throw new IllegalStateException("order map out of sync with levels");
        }
        java.util.Arrays.sort(stamped, Comparator.comparingLong(a -> a[0]));
        long[] out = new long[n];
        for (int j = 0; j < n; j++) {
            out[j] = stamped[j][1];
        }
        return out;
    }

    private static BookCheckpoint.LevelCheckpoint levelCheckpoint(Level level) {
        long[] ids = new long[level.orderCount];
        long[] qtys = new long[level.orderCount];
        int i = 0;
        for (Order o = level.head; o != null; o = o.next) {
            ids[i] = o.orderId;
            qtys[i] = o.qty;
            i++;
        }
        if (i != level.orderCount) {
            throw new IllegalStateException("level order_count out of sync");
        }
        return new BookCheckpoint.LevelCheckpoint(level.side, level.price, ids, qtys);
    }

    /** Rebuild an identical book from {@link #checkpoint()} output. */
    public static OrderBook restore(BookCheckpoint cp) {
        OrderBook book = new OrderBook(cp.instrumentId, cp.venueId, cp.reorderWindow);
        for (BookCheckpoint.LevelCheckpoint lvl : cp.levels) {
            if (lvl.side < 0 || lvl.side > 1) {
                throw new IllegalArgumentException("invalid side in checkpoint level");
            }
            for (int i = 0; i < lvl.orderIds.length; i++) {
                if (book.orders.get(lvl.orderIds[i]) != null) {
                    throw new IllegalArgumentException("duplicate order_id "
                            + Long.toUnsignedString(lvl.orderIds[i]) + " in checkpoint");
                }
                book.insertOrder(lvl.side, lvl.priceTicks, lvl.orderIds[i], lvl.qtys[i]);
            }
        }
        // Rebuild the global arrival order (level insertion above fixed the
        // per-level FIFO order; arrival stamps must follow the checkpointed
        // global insertion order so checkpoints round-trip exactly).
        if (cp.arrivalOrder.length != book.orders.size()) {
            throw new IllegalArgumentException(
                    "checkpoint arrival_order inconsistent with levels");
        }
        long stamp = 0;
        for (long orderId : cp.arrivalOrder) {
            Order o = book.orders.get(orderId);
            if (o == null || o.arrival < 0) {
                throw new IllegalArgumentException(
                        "checkpoint arrival_order inconsistent with levels");
            }
            o.arrival = -(++stamp); // mark as assigned (negative) during the pass
        }
        for (long orderId : cp.arrivalOrder) {
            Order o = book.orders.get(orderId);
            o.arrival = -o.arrival;
        }
        book.arrivalCounter = stamp;
        book.lastSequence = cp.lastSequence;
        book.hasSequence = cp.hasSequence;
        book.sequenceEpoch = cp.sequenceEpoch;
        book.exchangeTs = cp.exchangeTs;
        book.receiveTs = cp.receiveTs;
        book.tradeFlow = cp.tradeFlow;
        book.status = cp.status;
        book.stale = cp.stale;
        book.snapshotActive = cp.snapshotActive;
        book.snapshotBroken = cp.snapshotBroken;
        book.snapshotCountdown = cp.snapshotCountdown;
        book.snapshotSyntheticNext[0] = cp.snapshotSyntheticNext[0];
        book.snapshotSyntheticNext[1] = cp.snapshotSyntheticNext[1];
        if (cp.reorderPending.length > cp.reorderWindow) {
            throw new IllegalArgumentException(
                    "checkpoint reorder_pending exceeds reorder_window");
        }
        for (MarketEvent pev : cp.reorderPending) {
            if (book.pending.put(pev.sequence, pev) != null) {
                throw new IllegalArgumentException("duplicate pending sequence "
                        + Long.toUnsignedString(pev.sequence) + " in checkpoint");
            }
        }
        BookCheckpoint.Counters c = cp.counters;
        book.duplicatesDropped = c.duplicatesDropped;
        book.gapsDetected = c.gapsDetected;
        book.droppedWhileStale = c.droppedWhileStale;
        book.unknownOrderEvents = c.unknownOrderEvents;
        book.invalidSideDropped = c.invalidSideDropped;
        book.invalidPayloadDropped = c.invalidPayloadDropped;
        book.unknownTypeDropped = c.unknownTypeDropped;
        book.modifyPriceMismatch = c.modifyPriceMismatch;
        book.snapshotRestarts = c.snapshotRestarts;
        book.sequenceResets = c.sequenceResets;
        book.lateRecovered = c.lateRecovered;
        book.eventsApplied = c.eventsApplied;
        return book;
    }
}
