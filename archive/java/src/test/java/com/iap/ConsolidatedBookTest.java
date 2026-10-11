package com.iap;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;

import org.junit.Test;

import com.iap.core.EventType;
import com.iap.core.MarketEvent;
import com.iap.core.Side;
import com.iap.orderbook.ConsolidatedBook;

/** Consolidated (multi-venue) book merge semantics. */
public class ConsolidatedBookTest {

    private static final long INST = 55;

    private static MarketEvent ev(int venue, long seq, int type, int side,
            long price, long qty, long orderId, long tradeId) {
        return new MarketEvent(seq, INST, venue, 1000 + seq, 1001 + seq, seq,
                type, side, price, qty, orderId, tradeId);
    }

    @Test
    public void routesEventsToPerVenueBooks() {
        ConsolidatedBook cons = new ConsolidatedBook(INST);
        cons.apply(ev(1, 1, EventType.ADD, Side.BID, 100, 10, 1, 0));
        cons.apply(ev(2, 1, EventType.ADD, Side.BID, 101, 5, 2, 0));
        assertEquals(2, cons.venues().size());
        assertArrayEquals(new long[] {100, 10}, cons.venues().get(1).bestBid());
        assertArrayEquals(new long[] {101, 5}, cons.venues().get(2).bestBid());
    }

    @Test
    public void samePriceAcrossVenuesSumsSizesAndCounts() {
        ConsolidatedBook cons = new ConsolidatedBook(INST);
        cons.apply(ev(1, 1, EventType.ADD, Side.ASK, 200, 10, 1, 0));
        cons.apply(ev(1, 2, EventType.ADD, Side.ASK, 200, 5, 2, 0));
        cons.apply(ev(2, 1, EventType.ADD, Side.ASK, 200, 20, 3, 0));
        assertArrayEquals(new long[] {200, 35}, cons.bestAsk());
        assertArrayEquals(new long[] {200, 3}, cons.orderCounts(Side.ASK, 10)[0]);
    }

    @Test
    public void bestIsBestAcrossVenues() {
        ConsolidatedBook cons = new ConsolidatedBook(INST);
        cons.apply(ev(1, 1, EventType.ADD, Side.BID, 100, 10, 1, 0));
        cons.apply(ev(2, 1, EventType.ADD, Side.BID, 102, 4, 2, 0));
        cons.apply(ev(1, 2, EventType.ADD, Side.ASK, 105, 7, 3, 0));
        cons.apply(ev(2, 2, EventType.ADD, Side.ASK, 104, 2, 4, 0));
        assertArrayEquals(new long[] {102, 4}, cons.bestBid());
        assertArrayEquals(new long[] {104, 2}, cons.bestAsk());
    }

    @Test
    public void depthMergedBestFirst() {
        ConsolidatedBook cons = new ConsolidatedBook(INST);
        cons.apply(ev(1, 1, EventType.ADD, Side.BID, 100, 10, 1, 0));
        cons.apply(ev(1, 2, EventType.ADD, Side.BID, 99, 5, 2, 0));
        cons.apply(ev(2, 1, EventType.ADD, Side.BID, 100, 1, 3, 0));
        cons.apply(ev(2, 2, EventType.ADD, Side.BID, 98, 9, 4, 0));
        long[][] depth = cons.depth(Side.BID, 10);
        assertEquals(3, depth.length);
        assertArrayEquals(new long[] {100, 11}, depth[0]);
        assertArrayEquals(new long[] {99, 5}, depth[1]);
        assertArrayEquals(new long[] {98, 9}, depth[2]);
    }

    @Test
    public void tradeFlowSumsAcrossVenues() {
        ConsolidatedBook cons = new ConsolidatedBook(INST);
        cons.apply(ev(1, 1, EventType.TRADE, Side.BID, 100, 30, 0, 7));
        cons.apply(ev(2, 1, EventType.TRADE, Side.ASK, 100, 12, 0, 8));
        assertEquals(18, cons.tradeFlow());
    }

    @Test
    public void staleIsPerVenue() {
        ConsolidatedBook cons = new ConsolidatedBook(INST);
        cons.apply(ev(1, 1, EventType.ADD, Side.BID, 100, 10, 1, 0));
        cons.apply(ev(2, 1, EventType.ADD, Side.BID, 99, 5, 2, 0));
        cons.apply(ev(1, 9, EventType.ADD, Side.BID, 101, 5, 3, 0)); // gap on venue 1
        assertEquals(true, cons.venues().get(1).isStale());
        assertEquals(false, cons.venues().get(2).isStale());
    }

    @Test
    public void emptyConsolidatedBookHasNoBest() {
        ConsolidatedBook cons = new ConsolidatedBook(INST);
        assertNull(cons.bestBid());
        assertNull(cons.bestAsk());
        assertEquals(0, cons.depth(Side.BID, 10).length);
    }

    @Test
    public void checkpointRestoreRoundTrips() {
        ConsolidatedBook cons = new ConsolidatedBook(INST);
        cons.apply(ev(1, 1, EventType.ADD, Side.BID, 100, 10, 1, 0));
        cons.apply(ev(2, 1, EventType.ADD, Side.ASK, 105, 5, 2, 0));
        cons.apply(ev(2, 2, EventType.TRADE, Side.BID, 105, 3, 0, 9));
        ConsolidatedBook.Checkpoint cp = cons.checkpoint();
        ConsolidatedBook restored = ConsolidatedBook.restore(cp);
        assertEquals(cp, restored.checkpoint());
        assertEquals(cons.tradeFlow(), restored.tradeFlow());
        assertArrayEquals(cons.bestBid(), restored.bestBid());
        assertArrayEquals(cons.bestAsk(), restored.bestAsk());
    }
}
