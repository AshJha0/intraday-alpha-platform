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

/**
 * MBO order book for one instrument on one venue (venueId=0: accept any venue).
 * Pinned semantics from PLATFORM_CONVENTIONS.md section 4 / API_CORE.md
 * section 4, mirroring the Python reference {@code iap/orderbook/book.py}:
 *
 * <ul>
 *   <li>ADD: FIFO tail of its (side, price) level; a crossing limit ADD
 *       executes against the opposite side from the best level's FIFO head
 *       (marketable), leftover posts; duplicate order_id dropped + counted.</li>
 *   <li>MODIFY: qty change only (event price ignored); decrease keeps queue
 *       position, increase moves to level tail, &lt;= 0 removes.</li>
 *   <li>CANCEL: remove by order_id; EXECUTE: partial fill keeps position,
 *       removed at 0, never touches trade_flow.</li>
 *   <li>TRADE: trade_flow += qty (BID aggressor) / -= qty (ASK aggressor).</li>
 *   <li>QUOTE (FX): replaces the venue's whole side at L1.</li>
 *   <li>SNAPSHOT: recovery burst; first record clears both sides, the record
 *       with trade_id == 0 ends the burst and clears {@code stale} — unless a
 *       sequence gap occurred INSIDE the burst, which marks it broken: a
 *       broken burst still ends at its trade_id == 0 record but leaves
 *       {@code stale} set; only a later complete gap-free burst clears it.</li>
 *   <li>Side domain: for side-indexed event types (ADD/QUOTE/SNAPSHOT/TRADE)
 *       {@code side} must be BID (0) or ASK (1); side &gt; 1 is malformed:
 *       dropped + counted ({@code invalid_side_dropped}) after its sequence
 *       number is consumed, never raised mid-stream.</li>
 *   <li>Sequencing: duplicates (sequence &lt;= last) dropped + counted; gaps
 *       mark the book stale + counted; while stale only SNAPSHOT/STATUS/TRADE/
 *       HEARTBEAT are applied, the rest dropped + counted.</li>
 * </ul>
 *
 * <p>Allocation-conscious: order nodes are pooled (free list) and looked up in
 * an open-addressing primitive-long hash map; per-level FIFO queues are
 * intrusive doubly-linked lists. Price levels live in TreeMaps (sorted, and
 * never iterated in unordered fashion on any deterministic path).
 */
public final class OrderBook {
    public static final int DEPTH_LEVELS = 10;

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

    private final TreeMap<Long, Level> bids = new TreeMap<>(Comparator.reverseOrder());
    private final TreeMap<Long, Level> asks = new TreeMap<>();
    private final OrderMap orders = new OrderMap();
    private final ArrayDeque<Order> orderPool = new ArrayDeque<>();
    private final ArrayDeque<Level> levelPool = new ArrayDeque<>();

    private long lastSequence;
    private long exchangeTs;
    private long receiveTs;
    private long tradeFlow;
    private long status = SessionStatus.TRADING;
    private boolean stale;
    private boolean snapshotActive;
    private boolean snapshotBroken;
    private long arrivalCounter;
    private long duplicatesDropped;
    private long gapsDetected;
    private long droppedWhileStale;
    private long unknownOrderEvents;
    private long invalidSideDropped;
    private long eventsApplied;

    public OrderBook(long instrumentId, int venueId) {
        this.instrumentId = instrumentId;
        this.venueId = venueId;
    }

    // -------------------------------------------------------------- applying

    /** Apply one event (sequence-checked). Throws on routing errors. */
    public void apply(MarketEvent ev) {
        if (ev.instrumentId != instrumentId || (venueId != 0 && ev.venueId != venueId)) {
            throw new IllegalArgumentException("event routed to wrong book: event "
                    + ev.instrumentId + "@" + ev.venueId + ", book "
                    + instrumentId + "@" + venueId);
        }
        // Sequence handling (duplicates dropped, gaps => stale).
        if (Long.compareUnsigned(ev.sequence, lastSequence) <= 0) {
            duplicatesDropped++;
            return;
        }
        if (lastSequence != 0 && Long.compareUnsigned(ev.sequence, lastSequence + 1) > 0) {
            gapsDetected++;
            stale = true;
            if (snapshotActive) {
                // Gap inside an active SNAPSHOT burst: the burst is broken —
                // its completion record must NOT clear `stale` (records are
                // missing). Only a later complete gap-free burst recovers.
                snapshotBroken = true;
            }
        }
        lastSequence = ev.sequence;
        exchangeTs = ev.exchangeTs;
        receiveTs = ev.receiveTs;

        int et = ev.eventType;
        // Side-domain validation for side-indexed event types: malformed
        // side => dropped + counted (never raised mid-stream), same path as
        // other malformed events; the sequence number above is consumed.
        if (ev.side > 1 && (et == EventType.ADD || et == EventType.QUOTE
                || et == EventType.SNAPSHOT || et == EventType.TRADE)) {
            invalidSideDropped++;
            return;
        }
        if (stale && et != EventType.SNAPSHOT && et != EventType.STATUS
                && et != EventType.TRADE && et != EventType.HEARTBEAT) {
            droppedWhileStale++;
            return;
        }

        switch (et) {
            case EventType.ADD -> applyAdd(ev);
            case EventType.MODIFY -> applyModify(ev);
            case EventType.CANCEL -> applyCancel(ev);
            case EventType.EXECUTE -> applyExecute(ev);
            case EventType.TRADE -> tradeFlow += ev.side == Side.BID ? ev.qty : -ev.qty;
            case EventType.QUOTE -> applyQuote(ev);
            case EventType.SNAPSHOT -> applySnapshot(ev);
            case EventType.STATUS -> status = ev.qty;
            case EventType.HEARTBEAT -> {
                // timestamps/sequence only, already updated above
            }
            default -> throw new IllegalArgumentException("unknown event_type " + et
                    + " (event_id=" + Long.toUnsignedString(ev.eventId) + ")");
        }
        eventsApplied++;
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

    private void applyAdd(MarketEvent ev) {
        if (orders.get(ev.orderId) != null) {
            unknownOrderEvents++; // duplicate order id: drop, count
            return;
        }
        long remaining = matchMarketable(ev.side, ev.priceTicks, ev.qty);
        if (remaining > 0) {
            insertOrder(ev.side, ev.priceTicks, ev.orderId, remaining);
        }
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

    private void applyModify(MarketEvent ev) {
        Order o = orders.get(ev.orderId);
        if (o == null) {
            unknownOrderEvents++;
            return;
        }
        long newQty = ev.qty;
        long oldQty = o.qty;
        if (newQty <= 0) {
            removeOrder(o);
            return;
        }
        Level level = o.level;
        if (newQty <= oldQty) {
            // Decrease: keep queue position.
            o.qty = newQty;
        } else {
            // Increase: move to tail of the level.
            unlink(level, o);
            appendTail(level, o);
            o.qty = newQty;
        }
        level.totalQty += newQty - oldQty;
    }

    private void applyCancel(MarketEvent ev) {
        Order o = orders.get(ev.orderId);
        if (o == null) {
            unknownOrderEvents++;
            return;
        }
        removeOrder(o);
    }

    private void applyExecute(MarketEvent ev) {
        Order o = orders.get(ev.orderId);
        if (o == null) {
            unknownOrderEvents++;
            return;
        }
        long fill = Math.min(ev.qty, o.qty);
        if (fill >= o.qty) {
            removeOrder(o);
        } else {
            o.qty -= fill;
            o.level.totalQty -= fill;
        }
    }

    /** FX QUOTE: replace this venue's whole side at L1. */
    private void applyQuote(MarketEvent ev) {
        clearSide(sideTree(ev.side));
        insertOrder(ev.side, ev.priceTicks, ev.orderId, ev.qty);
    }

    private void applySnapshot(MarketEvent ev) {
        if (!snapshotActive) {
            // Burst start: clear the whole book state (levels + orders).
            clearSide(bids);
            clearSide(asks);
            orders.clear();
            snapshotActive = true;
            snapshotBroken = false;
        }
        Order existing = orders.get(ev.orderId);
        if (existing != null) {
            removeOrder(existing);
        }
        insertOrder(ev.side, ev.priceTicks, ev.orderId, ev.qty);
        if (ev.tradeId == 0) { // last record of the burst
            snapshotActive = false;
            if (!snapshotBroken) {
                stale = false;
            }
            snapshotBroken = false;
        }
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

    public long eventsApplied() {
        return eventsApplied;
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
                lastSequence, exchangeTs, receiveTs, tradeFlow, status, stale,
                snapshotActive, snapshotBroken, duplicatesDropped, gapsDetected,
                droppedWhileStale, unknownOrderEvents, invalidSideDropped,
                eventsApplied);
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
        OrderBook book = new OrderBook(cp.instrumentId, cp.venueId);
        for (BookCheckpoint.LevelCheckpoint lvl : cp.levels) {
            for (int i = 0; i < lvl.orderIds.length; i++) {
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
            if (o == null) {
                throw new IllegalArgumentException(
                        "checkpoint arrival_order inconsistent with levels");
            }
            o.arrival = ++stamp;
        }
        book.arrivalCounter = stamp;
        book.lastSequence = cp.lastSequence;
        book.exchangeTs = cp.exchangeTs;
        book.receiveTs = cp.receiveTs;
        book.tradeFlow = cp.tradeFlow;
        book.status = cp.status;
        book.stale = cp.stale;
        book.snapshotActive = cp.snapshotActive;
        book.snapshotBroken = cp.snapshotBroken;
        book.duplicatesDropped = cp.duplicatesDropped;
        book.gapsDetected = cp.gapsDetected;
        book.droppedWhileStale = cp.droppedWhileStale;
        book.unknownOrderEvents = cp.unknownOrderEvents;
        book.invalidSideDropped = cp.invalidSideDropped;
        book.eventsApplied = cp.eventsApplied;
        return book;
    }
}
