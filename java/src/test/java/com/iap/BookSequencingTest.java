package com.iap;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

import com.iap.core.EventType;
import com.iap.core.SessionStatus;
import com.iap.core.Side;
import com.iap.orderbook.OrderBook;

/** Sequence QC: duplicates, gaps, stale mode, SNAPSHOT recovery (pinned). */
public class BookSequencingTest {

    private final Ev ev = new Ev();

    private OrderBook book() {
        return new OrderBook(Ev.INST, Ev.VENUE);
    }

    @Test
    public void duplicateSequenceDroppedCountedNoStateChange() {
        OrderBook b = book();
        var first = ev.add(Side.BID, 100, 10, 1);
        b.apply(first);
        long ts = b.exchangeTs();
        b.apply(ev.atSeq(1, EventType.ADD, Side.BID, 101, 5, 2, 0)); // dup seq
        assertEquals(1, b.duplicatesDropped());
        assertEquals(1, b.eventsApplied());
        assertArrayEquals(new long[] {100, 10}, b.bestBid());
        assertEquals(ts, b.exchangeTs()); // timestamps untouched on duplicate
        assertEquals(first.sequence, b.lastSequence());
    }

    @Test
    public void gapMarksStaleAndCounts() {
        OrderBook b = book();
        b.apply(ev.add(Side.BID, 100, 10, 1)); // seq 1
        b.apply(ev.atSeq(5, EventType.ADD, Side.BID, 101, 5, 2, 0)); // gap 1->5
        assertTrue(b.isStale());
        assertEquals(1, b.gapsDetected());
        // The gapped ADD itself is dropped while stale.
        assertEquals(1, b.droppedWhileStale());
        assertArrayEquals(new long[] {100, 10}, b.bestBid());
        assertEquals(5, b.lastSequence()); // sequence still advances
    }

    @Test
    public void firstEventWithLargeSequenceIsNotAGap() {
        OrderBook b = book();
        b.apply(ev.atSeq(1000, EventType.ADD, Side.BID, 100, 10, 1, 0));
        assertFalse(b.isStale());
        assertEquals(0, b.gapsDetected());
        assertArrayEquals(new long[] {100, 10}, b.bestBid());
    }

    @Test
    public void staleModeDropsBookEventsButAppliesTradeStatusHeartbeat() {
        OrderBook b = book();
        b.apply(ev.add(Side.BID, 100, 10, 1)); // seq 1
        b.apply(ev.atSeq(5, EventType.ADD, Side.BID, 101, 5, 2, 0)); // gap
        long dropped = b.droppedWhileStale();
        b.apply(ev.raw(EventType.MODIFY, 0, 0, 3, 1, 0));
        b.apply(ev.raw(EventType.CANCEL, 0, 0, 0, 1, 0));
        b.apply(ev.raw(EventType.EXECUTE, 0, 0, 5, 1, 0));
        b.apply(ev.raw(EventType.QUOTE, Side.ASK, 105, 5, 9, 0));
        assertEquals(dropped + 4, b.droppedWhileStale());
        assertArrayEquals(new long[] {100, 10}, b.bestBid()); // untouched
        b.apply(ev.trade(Side.BID, 100, 7, 1));
        assertEquals(7, b.tradeFlow());
        b.apply(ev.status(SessionStatus.AUCTION));
        assertEquals(SessionStatus.AUCTION, b.status());
        var hb = ev.heartbeat();
        b.apply(hb);
        assertEquals(hb.sequence, b.lastSequence());
        assertTrue(b.isStale()); // still stale until SNAPSHOT completes
    }

    @Test
    public void timestampsAndSequenceAdvanceOnDroppedWhileStale() {
        OrderBook b = book();
        b.apply(ev.add(Side.BID, 100, 10, 1));
        b.apply(ev.atSeq(5, EventType.ADD, Side.BID, 101, 5, 2, 0)); // gap, dropped
        var next = ev.raw(EventType.ADD, Side.BID, 102, 5, 3, 0); // dropped too
        b.apply(next);
        assertEquals(next.sequence, b.lastSequence());
        assertEquals(next.exchangeTs, b.exchangeTs());
        assertEquals(next.receiveTs, b.receiveTs());
    }

    @Test
    public void snapshotBurstRebuildsBookAndClearsStale() {
        OrderBook b = book();
        b.apply(ev.add(Side.BID, 100, 10, 1));
        b.apply(ev.add(Side.ASK, 105, 5, 2));
        b.apply(ev.atSeq(9, EventType.ADD, Side.BID, 99, 5, 3, 0)); // gap => stale
        assertTrue(b.isStale());
        // Recovery burst: bids then asks, best->worst, trade_id = remaining.
        b.apply(ev.snapshot(Side.BID, 101, 7, 11, 2));
        assertTrue(b.isStale()); // burst not complete yet
        b.apply(ev.snapshot(Side.BID, 100, 3, 12, 1));
        b.apply(ev.snapshot(Side.ASK, 104, 9, 13, 0)); // last record
        assertFalse(b.isStale());
        assertArrayEquals(new long[] {101, 7}, b.bestBid());
        assertArrayEquals(new long[] {104, 9}, b.bestAsk());
        assertEquals(3, b.orderCountTotal()); // pre-gap orders cleared
        long[][] bidDepth = b.depth(Side.BID, 10);
        assertEquals(2, bidDepth.length);
        assertArrayEquals(new long[] {100, 3}, bidDepth[1]);
    }

    @Test
    public void snapshotFirstRecordClearsBothSides() {
        OrderBook b = book();
        b.apply(ev.add(Side.BID, 100, 10, 1));
        b.apply(ev.add(Side.ASK, 105, 5, 2));
        b.apply(ev.snapshot(Side.BID, 101, 7, 11, 0)); // one-record burst
        assertArrayEquals(new long[] {101, 7}, b.bestBid());
        assertNull(b.bestAsk());
        assertEquals(1, b.orderCountTotal());
    }

    @Test
    public void gapDuringSnapshotBurstMarksItBroken() {
        // Broken-burst rule (conventions section 4, pinned): a gap INSIDE an
        // active SNAPSHOT burst marks it broken; the burst still ends at its
        // trade_id == 0 record but must NOT clear stale.
        OrderBook b = book();
        b.apply(ev.add(Side.BID, 100, 10, 1)); // seq 1
        b.apply(ev.atSeq(4, EventType.ADD, Side.BID, 99, 1, 2, 0)); // gap => stale
        b.apply(ev.snapshot(Side.BID, 101, 7, 11, 2)); // burst starts
        // Gap mid-burst: a record went missing => the burst is broken.
        b.apply(ev.atSeq(b.lastSequence() + 3, EventType.SNAPSHOT, Side.BID, 102, 8, 12, 1));
        b.apply(ev.snapshot(Side.ASK, 105, 4, 13, 0)); // burst ends, still broken
        assertTrue(b.isStale()); // broken burst must NOT clear stale
        assertEquals(2, b.gapsDetected());
        // Book events stay blocked until a complete burst arrives.
        long dropped = b.droppedWhileStale();
        b.apply(ev.add(Side.BID, 103, 5, 20));
        assertEquals(dropped + 1, b.droppedWhileStale());
        // A subsequent complete gap-free burst recovers.
        b.apply(ev.snapshot(Side.BID, 102, 8, 31, 1));
        b.apply(ev.snapshot(Side.ASK, 105, 4, 32, 0));
        assertFalse(b.isStale());
        assertArrayEquals(new long[] {102, 8}, b.bestBid());
        assertArrayEquals(new long[] {105, 4}, b.bestAsk());
        assertEquals(2, b.orderCountTotal());
    }

    @Test
    public void invalidSideDroppedAndCountedOnSideIndexedTypes() {
        // Side-domain rule (conventions section 4, pinned): side > 1 on
        // ADD/QUOTE/SNAPSHOT/TRADE => dropped + counted, never raised,
        // after the sequence number is consumed.
        OrderBook b = book();
        b.apply(ev.add(Side.BID, 100, 10, 1));
        b.apply(ev.add(Side.ASK, 105, 5, 2));
        long tsBefore = b.exchangeTs();
        b.apply(ev.raw(EventType.ADD, 9, 101, 5, 3, 0));
        b.apply(ev.raw(EventType.QUOTE, 5, 102, 7, 4, 0));
        b.apply(ev.raw(EventType.TRADE, 9, 100, 30, 0, 7));
        b.apply(ev.raw(EventType.SNAPSHOT, 3, 100, 30, 5, 0));
        assertEquals(4, b.invalidSideDropped());
        // Book state untouched: no order added, no side replaced, no burst
        // started, trade_flow unchanged; events_applied not incremented.
        assertArrayEquals(new long[] {100, 10}, b.bestBid());
        assertArrayEquals(new long[] {105, 5}, b.bestAsk());
        assertEquals(2, b.orderCountTotal());
        assertEquals(0, b.tradeFlow());
        assertEquals(2, b.eventsApplied());
        assertTrue(b.exchangeTs() > tsBefore); // timestamps consumed
        // Sequence numbers were consumed: the next in-order event applies
        // cleanly with no gap.
        b.apply(ev.add(Side.BID, 100, 5, 6));
        assertEquals(0, b.gapsDetected());
        assertFalse(b.isStale());
        assertArrayEquals(new long[] {100, 15}, b.bestBid());
        // MODIFY/CANCEL/EXECUTE address by order_id (not side-indexed): a
        // bogus side field does not block them.
        b.apply(ev.raw(EventType.MODIFY, 9, 0, 25, 6, 0));
        assertArrayEquals(new long[] {100, 35}, b.bestBid());
        assertEquals(4, b.invalidSideDropped());
    }

    @Test
    public void snapshotDuplicateOrderIdWithinBurstReplaces() {
        OrderBook b = book();
        b.apply(ev.snapshot(Side.BID, 100, 5, 11, 1));
        b.apply(ev.snapshot(Side.BID, 101, 9, 11, 0)); // same id, new price/qty
        assertArrayEquals(new long[] {101, 9}, b.bestBid());
        assertEquals(1, b.orderCountTotal());
    }

    @Test
    public void countersAccumulateIndependently() {
        OrderBook b = book();
        b.apply(ev.add(Side.BID, 100, 10, 1)); // applied
        b.apply(ev.atSeq(1, EventType.ADD, Side.BID, 100, 1, 2, 0)); // dup
        b.apply(ev.cancel(42)); // unknown order (seq 2)
        b.apply(ev.atSeq(9, EventType.ADD, Side.BID, 100, 1, 3, 0)); // gap + stale drop
        assertEquals(1, b.duplicatesDropped());
        assertEquals(1, b.unknownOrderEvents());
        assertEquals(1, b.gapsDetected());
        assertEquals(1, b.droppedWhileStale());
        assertEquals(2, b.eventsApplied()); // add + cancel(unknown) applied
    }
}
