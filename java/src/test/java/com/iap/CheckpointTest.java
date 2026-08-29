package com.iap;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;

import java.util.List;

import org.junit.Test;

import com.iap.core.MarketEvent;
import com.iap.core.Side;
import com.iap.orderbook.BookCheckpoint;
import com.iap.orderbook.OrderBook;

/** Book checkpoint/restore: bit-identical subsequent behavior (pinned). */
public class CheckpointTest {

    @Test
    public void checkpointRoundTripsExactly() {
        Ev ev = new Ev();
        OrderBook b = new OrderBook(Ev.INST, Ev.VENUE);
        b.apply(ev.add(Side.BID, 100, 10, 1));
        b.apply(ev.add(Side.BID, 100, 20, 2));
        b.apply(ev.add(Side.ASK, 105, 5, 3));
        b.apply(ev.trade(Side.BID, 100, 9, 1));
        b.apply(ev.cancel(42)); // unknown, counted
        BookCheckpoint cp = b.checkpoint();
        OrderBook restored = OrderBook.restore(cp);
        assertEquals(cp, restored.checkpoint());
        assertEquals(b.stateSummary(), restored.stateSummary());
        assertEquals(b.unknownOrderEvents(), restored.unknownOrderEvents());
        assertEquals(b.eventsApplied(), restored.eventsApplied());
    }

    @Test
    public void checkpointLevelsSortedBySideThenPrice() {
        Ev ev = new Ev();
        OrderBook b = new OrderBook(Ev.INST, Ev.VENUE);
        b.apply(ev.add(Side.ASK, 105, 5, 1));
        b.apply(ev.add(Side.BID, 99, 5, 2));
        b.apply(ev.add(Side.BID, 100, 5, 3));
        b.apply(ev.add(Side.ASK, 104, 5, 4));
        BookCheckpoint cp = b.checkpoint();
        assertEquals(4, cp.levels.size());
        assertEquals(Side.BID, cp.levels.get(0).side);
        assertEquals(99, cp.levels.get(0).priceTicks);
        assertEquals(100, cp.levels.get(1).priceTicks);
        assertEquals(Side.ASK, cp.levels.get(2).side);
        assertEquals(104, cp.levels.get(2).priceTicks);
        assertEquals(105, cp.levels.get(3).priceTicks);
    }

    @Test
    public void checkpointPreservesFifoOrderWithinLevel() {
        Ev ev = new Ev();
        OrderBook b = new OrderBook(Ev.INST, Ev.VENUE);
        b.apply(ev.add(Side.ASK, 200, 10, 1));
        b.apply(ev.add(Side.ASK, 200, 20, 2));
        b.apply(ev.modify(1, 15)); // moves order 1 to tail
        BookCheckpoint cp = b.checkpoint();
        assertArrayEquals(new long[] {2, 1}, cp.levels.get(0).orderIds);
        assertArrayEquals(new long[] {20, 15}, cp.levels.get(0).qtys);
        // Restored book must fill order 2 first (it is the FIFO head).
        OrderBook restored = OrderBook.restore(cp);
        restored.apply(ev.add(Side.BID, 200, 20, 3));
        assertArrayEquals(new long[] {200, 15}, restored.bestAsk());
        assertEquals(cp.levels.get(0).orderIds.length, 2);
    }

    @Test
    public void restoredBookReplaysIdenticallyOnGoldenVector() {
        List<MarketEvent> events = Golden.eq();
        int split = 1234;
        OrderBook full = new OrderBook(1, 1);
        OrderBook prefix = new OrderBook(1, 1);
        for (int i = 0; i < split; i++) {
            prefix.apply(events.get(i));
        }
        OrderBook resumed = OrderBook.restore(prefix.checkpoint());
        for (int i = 0; i < events.size(); i++) {
            full.apply(events.get(i));
            if (i >= split) {
                resumed.apply(events.get(i));
            }
        }
        assertEquals(full.checkpoint(), resumed.checkpoint());
        assertEquals(full.stateSummary(), resumed.stateSummary());
    }

    @Test
    public void checkpointCarriesGlobalArrivalOrder() {
        // arrival_order (checkpoint schema addition, schemas/MIGRATIONS.md):
        // the ids of every resting order in global insertion order, so
        // restore() round-trips the arrival order exactly (levels alone
        // only pin per-level FIFO).
        Ev ev = new Ev();
        OrderBook b = new OrderBook(Ev.INST, Ev.VENUE);
        b.apply(ev.add(Side.ASK, 105, 5, 9));
        b.apply(ev.add(Side.BID, 100, 10, 4));
        b.apply(ev.add(Side.BID, 101, 10, 2));
        b.apply(ev.add(Side.BID, 100, 20, 7));
        b.apply(ev.cancel(4)); // removal must drop the id from the order
        BookCheckpoint cp = b.checkpoint();
        assertArrayEquals(new long[] {9, 2, 7}, cp.arrivalOrder);
        OrderBook restored = OrderBook.restore(cp);
        assertArrayEquals(new long[] {9, 2, 7},
                restored.checkpoint().arrivalOrder);
        // New orders arrive after the restored ones.
        restored.apply(ev.add(Side.BID, 99, 1, 12));
        assertArrayEquals(new long[] {9, 2, 7, 12},
                restored.checkpoint().arrivalOrder);
        assertEquals(cp, OrderBook.restore(cp).checkpoint());
    }

    @Test
    public void restoreRejectsInconsistentArrivalOrder() {
        Ev ev = new Ev();
        OrderBook b = new OrderBook(Ev.INST, Ev.VENUE);
        b.apply(ev.add(Side.BID, 100, 10, 1));
        b.apply(ev.add(Side.ASK, 105, 5, 2));
        BookCheckpoint good = b.checkpoint();
        BookCheckpoint bad = new BookCheckpoint(good.instrumentId,
                good.venueId, good.levels, new long[] {1, 3},
                good.lastSequence, good.exchangeTs, good.receiveTs,
                good.tradeFlow, good.status, good.stale, good.snapshotActive,
                good.snapshotBroken, good.duplicatesDropped, good.gapsDetected,
                good.droppedWhileStale, good.unknownOrderEvents,
                good.invalidSideDropped, good.eventsApplied);
        try {
            OrderBook.restore(bad);
            throw new AssertionError("inconsistent arrival_order must throw");
        } catch (IllegalArgumentException expected) {
            // pinned: mirrors the Python reference's ValueError
        }
    }

    @Test
    public void brokenBurstStateSurvivesCheckpointRestore() {
        // snapshot_broken (checkpoint schema addition): a restore taken
        // mid-burst after an interior gap must still refuse to clear stale
        // at the burst's completion record.
        Ev ev = new Ev();
        OrderBook b = new OrderBook(Ev.INST, Ev.VENUE);
        b.apply(ev.add(Side.BID, 100, 10, 1));
        b.apply(ev.atSeq(9, com.iap.core.EventType.ADD, Side.BID, 99, 1, 2,
                0)); // gap => stale
        b.apply(ev.snapshot(Side.BID, 101, 7, 11, 2)); // burst starts
        b.apply(ev.atSeq(b.lastSequence() + 2, com.iap.core.EventType.SNAPSHOT,
                Side.BID, 100, 3, 12, 1)); // gap INSIDE the burst
        OrderBook restored = OrderBook.restore(b.checkpoint());
        var done = ev.snapshot(Side.ASK, 104, 9, 13, 0); // completes broken burst
        b.apply(done);
        restored.apply(done);
        assertEquals(true, b.isStale());
        assertEquals(true, restored.isStale());
        assertEquals(b.checkpoint(), restored.checkpoint());
    }

    @Test
    public void invalidSideCounterSurvivesCheckpointRestore() {
        Ev ev = new Ev();
        OrderBook b = new OrderBook(Ev.INST, Ev.VENUE);
        b.apply(ev.add(Side.BID, 100, 10, 1));
        b.apply(ev.raw(com.iap.core.EventType.ADD, 9, 101, 5, 2, 0));
        BookCheckpoint cp = b.checkpoint();
        assertEquals(1, cp.invalidSideDropped);
        OrderBook restored = OrderBook.restore(cp);
        assertEquals(1, restored.invalidSideDropped());
        assertEquals(cp, restored.checkpoint());
    }

    @Test
    public void checkpointCarriesStaleAndStatusFlags() {
        Ev ev = new Ev();
        OrderBook b = new OrderBook(Ev.INST, Ev.VENUE);
        b.apply(ev.add(Side.BID, 100, 10, 1));
        b.apply(ev.status(com.iap.core.SessionStatus.HALT));
        b.apply(ev.atSeq(b.lastSequence() + 5, com.iap.core.EventType.ADD,
                Side.BID, 101, 5, 2, 0)); // gap => stale
        BookCheckpoint cp = b.checkpoint();
        assertEquals(true, cp.stale);
        assertEquals(com.iap.core.SessionStatus.HALT, cp.status);
        OrderBook restored = OrderBook.restore(cp);
        assertEquals(true, restored.isStale());
        assertEquals(com.iap.core.SessionStatus.HALT, restored.status());
        assertEquals(1, restored.gapsDetected());
    }
}
