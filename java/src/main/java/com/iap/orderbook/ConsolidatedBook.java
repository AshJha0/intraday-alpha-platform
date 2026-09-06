package com.iap.orderbook;

import java.math.BigInteger;
import java.util.Comparator;
import java.util.Map;
import java.util.Objects;
import java.util.SortedMap;
import java.util.TreeMap;

import com.iap.core.MarketEvent;
import com.iap.core.Side;

/**
 * Consolidated view over per-venue books of one instrument. Routes events to
 * per-venue books by venue_id and merges derived state over the NON-STALE
 * venues only (pinned): same price across venues means sizes and order
 * counts are summed; best = best across venues; a venue whose book is stale
 * (sequence gap not yet recovered) contributes nothing until a complete
 * SNAPSHOT burst recovers it. Sequence/staleness/status remain per venue.
 * Venues are always iterated in sorted venue_id order (deterministic).
 */
public final class ConsolidatedBook {
    /** Deterministic serialization of the consolidated book. */
    public static final class Checkpoint {
        public final long instrumentId;
        public final int reorderWindow;
        public final TreeMap<Integer, BookCheckpoint> venues;

        public Checkpoint(long instrumentId, int reorderWindow,
                TreeMap<Integer, BookCheckpoint> venues) {
            this.instrumentId = instrumentId;
            this.reorderWindow = reorderWindow;
            this.venues = venues;
        }

        @Override
        public boolean equals(Object obj) {
            if (this == obj) {
                return true;
            }
            if (!(obj instanceof Checkpoint o)) {
                return false;
            }
            return instrumentId == o.instrumentId && reorderWindow == o.reorderWindow
                    && venues.equals(o.venues);
        }

        @Override
        public int hashCode() {
            return Objects.hash(instrumentId, reorderWindow, venues);
        }
    }

    public final long instrumentId;
    private final int reorderWindow;
    private final TreeMap<Integer, OrderBook> books = new TreeMap<>();

    public ConsolidatedBook(long instrumentId) {
        this(instrumentId, 0);
    }

    /** @param reorderWindow hold-back buffer used for every venue book. */
    public ConsolidatedBook(long instrumentId, int reorderWindow) {
        if (reorderWindow < 0 || reorderWindow > OrderBook.MAX_REORDER_WINDOW) {
            throw new IllegalArgumentException("reorderWindow out of range: " + reorderWindow);
        }
        this.instrumentId = instrumentId;
        this.reorderWindow = reorderWindow;
    }

    public int reorderWindow() {
        return reorderWindow;
    }

    /** Get (or lazily create) the per-venue book. */
    public OrderBook venueBook(int venueId) {
        OrderBook book = books.get(venueId);
        if (book == null) {
            book = new OrderBook(instrumentId, venueId, reorderWindow);
            books.put(venueId, book);
        }
        return book;
    }

    /** Explicit sequence reset on every venue book (session roll). */
    public void resetSequences() {
        for (OrderBook book : books.values()) {
            book.resetSequence();
        }
    }

    /** Sorted venue ids whose books are not stale (merged into the view). */
    public int[] activeVenues() {
        return books.entrySet().stream().filter(e -> !e.getValue().isStale())
                .mapToInt(Map.Entry::getKey).toArray();
    }

    /** Sorted venue ids whose books are stale (excluded from the view). */
    public int[] staleVenues() {
        return books.entrySet().stream().filter(e -> e.getValue().isStale())
                .mapToInt(Map.Entry::getKey).toArray();
    }

    /** Session status of one venue's book, or -1 when the venue is unknown. */
    public long venueStatus(int venueId) {
        OrderBook book = books.get(venueId);
        return book == null ? -1 : book.status();
    }

    /** True when the merged best bid &gt; merged best ask. */
    public boolean isCrossed() {
        long[] bb = bestBid();
        long[] ba = bestAsk();
        return bb != null && ba != null && bb[0] > ba[0];
    }

    /** True when the merged best bid == merged best ask. */
    public boolean isLocked() {
        long[] bb = bestBid();
        long[] ba = bestAsk();
        return bb != null && ba != null && bb[0] == ba[0];
    }

    /** Read-only view of the per-venue books, sorted by venue_id. */
    public SortedMap<Integer, OrderBook> venues() {
        return java.util.Collections.unmodifiableSortedMap(books);
    }

    /** Route one event to its venue book; returns the pinned verdict. */
    public ApplyStatus apply(MarketEvent ev) {
        return venueBook(ev.venueId).apply(ev);
    }

    private TreeMap<Long, long[]> merged(int side) {
        Comparator<Long> cmp = side == Side.BID
                ? Comparator.reverseOrder()
                : Comparator.naturalOrder();
        TreeMap<Long, long[]> agg = new TreeMap<>(cmp);
        for (OrderBook book : books.values()) { // sorted venue order
            if (book.isStale()) {
                continue; // non-stale venues only (pinned)
            }
            for (long[] level : book.sideLevels(side)) {
                long[] slot = agg.get(level[0]);
                if (slot == null) {
                    slot = new long[] {0, 0};
                    agg.put(level[0], slot);
                }
                slot[0] += level[1];
                slot[1] += level[2];
            }
        }
        return agg;
    }

    /** {price_ticks, total_size} of the consolidated best bid, or null. */
    public long[] bestBid() {
        return first(merged(Side.BID));
    }

    /** {price_ticks, total_size} of the consolidated best ask, or null. */
    public long[] bestAsk() {
        return first(merged(Side.ASK));
    }

    private static long[] first(TreeMap<Long, long[]> merged) {
        Map.Entry<Long, long[]> e = merged.firstEntry();
        return e == null ? null : new long[] {e.getKey(), e.getValue()[0]};
    }

    /** Top-N consolidated {price_ticks, total_size} best-first. */
    public long[][] depth(int side, int levels) {
        return take(merged(side), levels, 0);
    }

    /** Top-N consolidated {price_ticks, order_count} best-first. */
    public long[][] orderCounts(int side, int levels) {
        return take(merged(side), levels, 1);
    }

    private static long[][] take(TreeMap<Long, long[]> merged, int levels, int idx) {
        int n = Math.min(levels, merged.size());
        long[][] out = new long[n][];
        int i = 0;
        for (Map.Entry<Long, long[]> e : merged.entrySet()) {
            if (i >= n) {
                break;
            }
            out[i++] = new long[] {e.getKey(), e.getValue()[idx]};
        }
        return out;
    }

    /**
     * Sum of per-venue cumulative signed trade flow (all venues), saturated
     * to int64 (pinned).
     */
    public long tradeFlow() {
        long sum = 0;
        boolean exact = true;
        for (OrderBook book : books.values()) {
            long r = sum + book.tradeFlow();
            if (((sum ^ r) & (book.tradeFlow() ^ r)) < 0) {
                exact = false;
                break;
            }
            sum = r;
        }
        if (exact) {
            return sum;
        }
        BigInteger total = BigInteger.ZERO;
        for (OrderBook book : books.values()) {
            total = total.add(BigInteger.valueOf(book.tradeFlow()));
        }
        return total.max(BigInteger.valueOf(Long.MIN_VALUE))
                .min(BigInteger.valueOf(Long.MAX_VALUE)).longValueExact();
    }

    /** Deterministic serialization (venues in sorted order). */
    public Checkpoint checkpoint() {
        TreeMap<Integer, BookCheckpoint> venues = new TreeMap<>();
        for (Map.Entry<Integer, OrderBook> e : books.entrySet()) {
            venues.put(e.getKey(), e.getValue().checkpoint());
        }
        return new Checkpoint(instrumentId, reorderWindow, venues);
    }

    /** Rebuild an identical consolidated book from {@link #checkpoint()}. */
    public static ConsolidatedBook restore(Checkpoint cp) {
        ConsolidatedBook cons = new ConsolidatedBook(cp.instrumentId, cp.reorderWindow);
        for (Map.Entry<Integer, BookCheckpoint> e : cp.venues.entrySet()) {
            cons.books.put(e.getKey(), OrderBook.restore(e.getValue()));
        }
        return cons;
    }
}
