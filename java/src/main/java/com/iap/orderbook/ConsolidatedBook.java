package com.iap.orderbook;

import java.util.Comparator;
import java.util.Map;
import java.util.Objects;
import java.util.SortedMap;
import java.util.TreeMap;

import com.iap.core.MarketEvent;
import com.iap.core.Side;

/**
 * Consolidated view over per-venue books of one instrument. Routes events to
 * per-venue books by venue_id and merges derived state: same price across
 * venues means sizes and order counts are summed; best = best across venues.
 * Sequence/staleness remain per venue. Venues are always iterated in sorted
 * venue_id order (deterministic).
 */
public final class ConsolidatedBook {
    /** Deterministic serialization of the consolidated book. */
    public static final class Checkpoint {
        public final long instrumentId;
        public final TreeMap<Integer, BookCheckpoint> venues;

        public Checkpoint(long instrumentId, TreeMap<Integer, BookCheckpoint> venues) {
            this.instrumentId = instrumentId;
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
            return instrumentId == o.instrumentId && venues.equals(o.venues);
        }

        @Override
        public int hashCode() {
            return Objects.hash(instrumentId, venues);
        }
    }

    public final long instrumentId;
    private final TreeMap<Integer, OrderBook> books = new TreeMap<>();

    public ConsolidatedBook(long instrumentId) {
        this.instrumentId = instrumentId;
    }

    /** Get (or lazily create) the per-venue book. */
    public OrderBook venueBook(int venueId) {
        OrderBook book = books.get(venueId);
        if (book == null) {
            book = new OrderBook(instrumentId, venueId);
            books.put(venueId, book);
        }
        return book;
    }

    /** Read-only view of the per-venue books, sorted by venue_id. */
    public SortedMap<Integer, OrderBook> venues() {
        return java.util.Collections.unmodifiableSortedMap(books);
    }

    /** Route one event to its venue book. */
    public void apply(MarketEvent ev) {
        venueBook(ev.venueId).apply(ev);
    }

    private TreeMap<Long, long[]> merged(int side) {
        Comparator<Long> cmp = side == Side.BID
                ? Comparator.reverseOrder()
                : Comparator.naturalOrder();
        TreeMap<Long, long[]> agg = new TreeMap<>(cmp);
        for (OrderBook book : books.values()) { // sorted venue order
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

    /** Sum of per-venue cumulative signed trade flow. */
    public long tradeFlow() {
        long sum = 0;
        for (OrderBook book : books.values()) {
            sum += book.tradeFlow();
        }
        return sum;
    }

    /** Deterministic serialization (venues in sorted order). */
    public Checkpoint checkpoint() {
        TreeMap<Integer, BookCheckpoint> venues = new TreeMap<>();
        for (Map.Entry<Integer, OrderBook> e : books.entrySet()) {
            venues.put(e.getKey(), e.getValue().checkpoint());
        }
        return new Checkpoint(instrumentId, venues);
    }

    /** Rebuild an identical consolidated book from {@link #checkpoint()}. */
    public static ConsolidatedBook restore(Checkpoint cp) {
        ConsolidatedBook cons = new ConsolidatedBook(cp.instrumentId);
        for (Map.Entry<Integer, BookCheckpoint> e : cp.venues.entrySet()) {
            cons.books.put(e.getKey(), OrderBook.restore(e.getValue()));
        }
        return cons;
    }
}
