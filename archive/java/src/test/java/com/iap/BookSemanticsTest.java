package com.iap;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.fail;

import org.junit.Test;

import com.iap.core.SessionStatus;
import com.iap.core.Side;
import com.iap.orderbook.OrderBook;

/** Every pinned MBO/L1/L2 semantic from PLATFORM_CONVENTIONS.md section 4. */
public class BookSemanticsTest {

    private final Ev ev = new Ev();

    private OrderBook book() {
        return new OrderBook(Ev.INST, Ev.VENUE);
    }

    // ---------------------------------------------------------------- ADD

    @Test
    public void addRestsAtItsLevel() {
        OrderBook b = book();
        b.apply(ev.add(Side.BID, 100, 10, 1));
        assertArrayEquals(new long[] {100, 10}, b.bestBid());
        assertNull(b.bestAsk());
        assertEquals(1, b.orderCountTotal());
    }

    @Test
    public void addAppendsFifoTail() {
        OrderBook b = book();
        b.apply(ev.add(Side.ASK, 200, 10, 1));
        b.apply(ev.add(Side.ASK, 200, 20, 2));
        b.apply(ev.add(Side.ASK, 200, 30, 3));
        // Marketable buy for 15: consumes order 1 (10) then partially order 2.
        b.apply(ev.add(Side.BID, 200, 15, 4));
        assertArrayEquals(new long[] {200, 45}, b.bestAsk());
        assertEquals(2, b.orderCountTotal());
        // Next fill hits the reduced head (order 2 at 15 remaining).
        b.apply(ev.execute(2, 15));
        assertArrayEquals(new long[] {200, 30}, b.bestAsk());
    }

    @Test
    public void addDuplicateOrderIdDroppedAndCounted() {
        OrderBook b = book();
        b.apply(ev.add(Side.BID, 100, 10, 1));
        b.apply(ev.add(Side.BID, 101, 5, 1)); // same order_id
        assertEquals(1, b.unknownOrderEvents());
        assertArrayEquals(new long[] {100, 10}, b.bestBid());
        assertEquals(1, b.orderCountTotal());
    }

    @Test
    public void marketableAddFullyFilledDoesNotPost() {
        OrderBook b = book();
        b.apply(ev.add(Side.ASK, 200, 10, 1));
        b.apply(ev.add(Side.BID, 200, 10, 2)); // crosses, fully filled
        assertNull(b.bestAsk());
        assertNull(b.bestBid());
        assertEquals(0, b.orderCountTotal());
    }

    @Test
    public void marketableAddLeftoverPostsAtItsPrice() {
        OrderBook b = book();
        b.apply(ev.add(Side.ASK, 200, 10, 1));
        b.apply(ev.add(Side.BID, 201, 25, 2)); // fills 10, posts 15 @ 201
        assertNull(b.bestAsk());
        assertArrayEquals(new long[] {201, 15}, b.bestBid());
    }

    @Test
    public void marketableAddSweepsMultipleLevelsBestFirst() {
        OrderBook b = book();
        b.apply(ev.add(Side.ASK, 200, 10, 1));
        b.apply(ev.add(Side.ASK, 201, 10, 2));
        b.apply(ev.add(Side.ASK, 202, 10, 3));
        b.apply(ev.add(Side.BID, 201, 25, 4)); // sweeps 200 and 201, leftover 5 posts
        assertArrayEquals(new long[] {202, 10}, b.bestAsk());
        assertArrayEquals(new long[] {201, 5}, b.bestBid());
    }

    @Test
    public void marketableAddPartialHeadKeepsHeadPosition() {
        OrderBook b = book();
        b.apply(ev.add(Side.ASK, 200, 100, 1));
        b.apply(ev.add(Side.ASK, 200, 50, 2));
        b.apply(ev.add(Side.BID, 200, 30, 3)); // reduces head order 1 to 70
        assertArrayEquals(new long[] {200, 120}, b.bestAsk());
        // Order 1 still at head: next marketable hits it first.
        b.apply(ev.add(Side.BID, 200, 70, 4)); // exactly consumes order 1
        assertArrayEquals(new long[] {200, 50}, b.bestAsk());
        assertEquals(1, b.orderCountTotal());
    }

    @Test
    public void marketableAskAddCrossesDownIntoBids() {
        OrderBook b = book();
        b.apply(ev.add(Side.BID, 100, 10, 1));
        b.apply(ev.add(Side.BID, 99, 10, 2));
        b.apply(ev.add(Side.ASK, 99, 15, 3)); // fills 100x10, then 5 of 99
        assertArrayEquals(new long[] {99, 5}, b.bestBid());
        assertNull(b.bestAsk());
    }

    // ------------------------------------------------------------- MODIFY

    @Test
    public void modifyDecreaseKeepsQueuePosition() {
        OrderBook b = book();
        b.apply(ev.add(Side.ASK, 200, 10, 1));
        b.apply(ev.add(Side.ASK, 200, 20, 2));
        b.apply(ev.modify(1, 6)); // decrease: stays at head
        assertArrayEquals(new long[] {200, 26}, b.bestAsk());
        b.apply(ev.add(Side.BID, 200, 6, 3)); // consumes head == order 1 exactly
        assertArrayEquals(new long[] {200, 20}, b.bestAsk());
        b.apply(ev.execute(2, 20));
        assertNull(b.bestAsk());
    }

    @Test
    public void modifyIncreaseMovesToTail() {
        OrderBook b = book();
        b.apply(ev.add(Side.ASK, 200, 10, 1));
        b.apply(ev.add(Side.ASK, 200, 20, 2));
        b.apply(ev.modify(1, 15)); // increase: moves behind order 2
        assertArrayEquals(new long[] {200, 35}, b.bestAsk());
        b.apply(ev.add(Side.BID, 200, 20, 3)); // consumes order 2 (now head)
        assertArrayEquals(new long[] {200, 15}, b.bestAsk());
        b.apply(ev.execute(1, 15));
        assertNull(b.bestAsk());
    }

    @Test
    public void modifyToZeroOrNegativeRemoves() {
        OrderBook b = book();
        b.apply(ev.add(Side.BID, 100, 10, 1));
        b.apply(ev.add(Side.BID, 100, 10, 2));
        b.apply(ev.modify(1, 0));
        assertArrayEquals(new long[] {100, 10}, b.bestBid());
        b.apply(ev.modify(2, -5));
        assertNull(b.bestBid());
        assertEquals(0, b.orderCountTotal());
    }

    @Test
    public void modifyUnknownOrderDroppedAndCounted() {
        OrderBook b = book();
        b.apply(ev.modify(99, 10));
        assertEquals(1, b.unknownOrderEvents());
    }

    @Test
    public void modifyIgnoresEventPrice() {
        OrderBook b = book();
        b.apply(ev.add(Side.BID, 100, 10, 1));
        // MODIFY carries price 0 in our builder; order must stay at 100.
        b.apply(ev.modify(1, 5));
        assertArrayEquals(new long[] {100, 5}, b.bestBid());
    }

    // ------------------------------------------------------------- CANCEL

    @Test
    public void cancelRemovesOrder() {
        OrderBook b = book();
        b.apply(ev.add(Side.BID, 100, 10, 1));
        b.apply(ev.cancel(1));
        assertNull(b.bestBid());
        assertEquals(0, b.orderCountTotal());
    }

    @Test
    public void cancelUnknownOrderDroppedAndCounted() {
        OrderBook b = book();
        b.apply(ev.cancel(42));
        assertEquals(1, b.unknownOrderEvents());
    }

    @Test
    public void cancelLastOrderRemovesLevelFromDepth() {
        OrderBook b = book();
        b.apply(ev.add(Side.BID, 100, 10, 1));
        b.apply(ev.add(Side.BID, 99, 5, 2));
        b.apply(ev.cancel(1));
        assertArrayEquals(new long[] {99, 5}, b.bestBid());
        assertEquals(1, b.depth(Side.BID, 10).length);
    }

    // ------------------------------------------------------------ EXECUTE

    @Test
    public void executePartialKeepsPosition() {
        OrderBook b = book();
        b.apply(ev.add(Side.ASK, 200, 10, 1));
        b.apply(ev.execute(1, 4));
        assertArrayEquals(new long[] {200, 6}, b.bestAsk());
        assertEquals(1, b.orderCountTotal());
    }

    @Test
    public void executeFullRemoves() {
        OrderBook b = book();
        b.apply(ev.add(Side.ASK, 200, 10, 1));
        b.apply(ev.execute(1, 10));
        assertNull(b.bestAsk());
    }

    @Test
    public void executeOverfillClampsToOrderQty() {
        OrderBook b = book();
        b.apply(ev.add(Side.ASK, 200, 10, 1));
        b.apply(ev.add(Side.ASK, 200, 5, 2));
        b.apply(ev.execute(1, 999));
        assertArrayEquals(new long[] {200, 5}, b.bestAsk());
    }

    @Test
    public void executeUnknownOrderDroppedAndCounted() {
        OrderBook b = book();
        b.apply(ev.execute(42, 5));
        assertEquals(1, b.unknownOrderEvents());
    }

    @Test
    public void executeDoesNotTouchTradeFlow() {
        OrderBook b = book();
        b.apply(ev.add(Side.ASK, 200, 10, 1));
        b.apply(ev.execute(1, 4));
        assertEquals(0, b.tradeFlow());
    }

    // -------------------------------------------------------------- TRADE

    @Test
    public void tradeFlowSignedByAggressorSide() {
        OrderBook b = book();
        b.apply(ev.trade(Side.BID, 100, 30, 1));
        assertEquals(30, b.tradeFlow());
        b.apply(ev.trade(Side.ASK, 100, 12, 2));
        assertEquals(18, b.tradeFlow());
    }

    @Test
    public void tradeTouchesNothingElse() {
        OrderBook b = book();
        b.apply(ev.add(Side.BID, 100, 10, 1));
        b.apply(ev.trade(Side.ASK, 100, 5, 1));
        assertArrayEquals(new long[] {100, 10}, b.bestBid());
        assertEquals(1, b.orderCountTotal());
    }

    // -------------------------------------------------------------- QUOTE

    @Test
    public void quoteReplacesWholeSideAtL1() {
        OrderBook b = book();
        b.apply(ev.quote(Side.BID, 100, 10, 1));
        b.apply(ev.quote(Side.BID, 101, 7, 2));
        assertArrayEquals(new long[] {101, 7}, b.bestBid());
        assertEquals(1, b.depth(Side.BID, 10).length);
        assertEquals(1, b.orderCountTotal());
    }

    @Test
    public void quoteLeavesOtherSideUntouched() {
        OrderBook b = book();
        b.apply(ev.quote(Side.BID, 100, 10, 1));
        b.apply(ev.quote(Side.ASK, 105, 8, 2));
        assertArrayEquals(new long[] {100, 10}, b.bestBid());
        assertArrayEquals(new long[] {105, 8}, b.bestAsk());
    }

    // ----------------------------------------------------- STATUS/HEARTBEAT

    @Test
    public void statusStoredFromQty() {
        OrderBook b = book();
        assertEquals(SessionStatus.TRADING, b.status());
        b.apply(ev.status(SessionStatus.HALT));
        assertEquals(SessionStatus.HALT, b.status());
    }

    @Test
    public void heartbeatUpdatesSequenceAndTimestampsOnly() {
        OrderBook b = book();
        b.apply(ev.add(Side.BID, 100, 10, 1));
        var hb = ev.heartbeat();
        b.apply(hb);
        assertEquals(hb.sequence, b.lastSequence());
        assertEquals(hb.exchangeTs, b.exchangeTs());
        assertEquals(hb.receiveTs, b.receiveTs());
        assertArrayEquals(new long[] {100, 10}, b.bestBid());
        assertEquals(2, b.eventsApplied());
    }

    // ------------------------------------------------------- derived state

    @Test
    public void depthCappedAtRequestedLevels() {
        OrderBook b = book();
        for (int i = 0; i < 12; i++) {
            b.apply(ev.add(Side.BID, 100 - i, 10, i + 1));
        }
        assertEquals(10, b.depth(Side.BID, OrderBook.DEPTH_LEVELS).length);
        assertEquals(12, b.depth(Side.BID, 20).length);
        long[][] top = b.depth(Side.BID, 3);
        assertArrayEquals(new long[] {100, 10}, top[0]);
        assertArrayEquals(new long[] {99, 10}, top[1]);
        assertArrayEquals(new long[] {98, 10}, top[2]);
    }

    @Test
    public void orderCountPerLevelBestFirst() {
        OrderBook b = book();
        b.apply(ev.add(Side.ASK, 200, 10, 1));
        b.apply(ev.add(Side.ASK, 200, 10, 2));
        b.apply(ev.add(Side.ASK, 201, 10, 3));
        long[][] counts = b.orderCounts(Side.ASK, 10);
        assertArrayEquals(new long[] {200, 2}, counts[0]);
        assertArrayEquals(new long[] {201, 1}, counts[1]);
    }

    @Test
    public void emptyBookHasNoBest() {
        OrderBook b = book();
        assertNull(b.bestBid());
        assertNull(b.bestAsk());
        assertEquals(0, b.depth(Side.BID, 10).length);
        assertEquals(0, b.orderCounts(Side.ASK, 10).length);
    }

    // ------------------------------------------------------------- routing

    @Test
    public void wrongInstrumentRejected() {
        OrderBook b = book();
        try {
            b.apply(new com.iap.core.MarketEvent(1, Ev.INST + 1, Ev.VENUE, 10, 11, 1,
                    com.iap.core.EventType.HEARTBEAT, 0, 0, 0, 0, 0));
            fail("expected IllegalArgumentException");
        } catch (IllegalArgumentException expected) {
            // ok
        }
    }

    @Test
    public void wrongVenueRejected() {
        OrderBook b = book();
        try {
            b.apply(new com.iap.core.MarketEvent(1, Ev.INST, Ev.VENUE + 1, 10, 11, 1,
                    com.iap.core.EventType.HEARTBEAT, 0, 0, 0, 0, 0));
            fail("expected IllegalArgumentException");
        } catch (IllegalArgumentException expected) {
            // ok
        }
    }

    @Test
    public void venueZeroBookAcceptsAnyVenue() {
        OrderBook b = new OrderBook(Ev.INST, 0);
        b.apply(ev.add(Side.BID, 100, 10, 1)); // venue 3 event
        assertArrayEquals(new long[] {100, 10}, b.bestBid());
    }
}
