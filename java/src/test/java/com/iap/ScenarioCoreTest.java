package com.iap;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;

import org.junit.Test;

import com.iap.codec.Iap1Codec;
import com.iap.core.EventType;
import com.iap.core.MarketEvent;
import com.iap.core.SessionStatus;
import com.iap.core.Side;
import com.iap.core.Validation;
import com.iap.orderbook.BookCheckpoint;
import com.iap.orderbook.ConsolidatedBook;
import com.iap.orderbook.OrderBook;
import com.iap.replay.CheckpointJson;
import com.iap.replay.ReplayEngine;

/**
 * Real-life market-data scenarios for the core layer (docs/SCENARIOS.md,
 * CORE). Mirrors python/tests/test_scenarios_core.py: every test is named
 * after the scenario and pins PLATFORM_CONVENTIONS.md section 4 /
 * API_CORE.md sections 3-5.
 */
public class ScenarioCoreTest {
    private static final long TS0 = 1_700_000_000_000_000_000L;

    private static MarketEvent mk(long seq, int type, int side, long price, long qty,
            long orderId, long tradeId, int venue, long ts) {
        if (ts == 0) {
            ts = TS0 + (Long.remainderUnsigned(seq, 1_000_000L)) * 1_000_000L;
        }
        return new MarketEvent(seq, 1, venue, ts, ts + 150_000, seq, type, side, price, qty,
                orderId, tradeId);
    }

    private static MarketEvent ev(long seq, int type, int side, long price, long qty,
            long orderId, long tradeId) {
        return mk(seq, type, side, price, qty, orderId, tradeId, 1, 0);
    }

    private static MarketEvent add(long seq, int side, long price, long qty, long oid) {
        return ev(seq, EventType.ADD, side, price, qty, oid, 0);
    }

    private static MarketEvent status(long seq, long code) {
        return ev(seq, EventType.STATUS, 0, 0, code, 0, 0);
    }

    private static final long[][] BURST = {
        {Side.BID, 2450, 500, 101}, {Side.BID, 2449, 400, 102}, {Side.ASK, 2451, 600, 103},
    };

    private static long burst(OrderBook b, long seq, long[][] recs, boolean ids, int venue) {
        int n = recs.length;
        for (int i = 0; i < n; i++) {
            long[] r = recs[i];
            b.apply(mk(seq + i, EventType.SNAPSHOT, (int) r[0], r[1], r[2], ids ? r[3] : 0,
                    n - 1 - i, venue, 0));
        }
        return seq + n;
    }

    /** Bids 2449 (11: 100, 12: 200) / 2448 (13: 300); asks 2451 (21: 150) / 2452 (22: 250). */
    private static OrderBook seeded(int window) {
        OrderBook b = new OrderBook(1, 1, window);
        b.apply(add(1, Side.BID, 2449, 100, 11));
        b.apply(add(2, Side.BID, 2449, 200, 12));
        b.apply(add(3, Side.BID, 2448, 300, 13));
        b.apply(add(4, Side.ASK, 2451, 150, 21));
        b.apply(add(5, Side.ASK, 2452, 250, 22));
        return b;
    }

    private static long[] ids(OrderBook b) {
        return b.checkpoint().arrivalOrder;
    }

    // ------------------------------------------------ #2 snapshot-after-gap variants

    @Test
    public void snapshotGapOnFirstBurstRecordRecovers() {
        OrderBook b = seeded(0);
        long nxt = burst(b, 9, BURST, true, 1);
        assertEquals(1, b.gapsDetected());
        assertFalse(b.isStale());
        assertArrayEquals(new long[] {2450, 500}, b.bestBid());
        assertArrayEquals(new long[] {2451, 600}, b.bestAsk());
        b.apply(add(nxt, Side.BID, 2450, 100, 105));
        assertArrayEquals(new long[] {2450, 600}, b.bestBid());
    }

    @Test
    public void snapshotGapBetweenTwoBursts() {
        OrderBook b = seeded(0);
        long nxt = burst(b, 6, BURST, true, 1);
        assertFalse(b.isStale());
        burst(b, nxt + 3, new long[][] {{Side.BID, 2447, 50, 201}, {Side.ASK, 2453, 60, 202}}, true, 1);
        assertEquals(1, b.gapsDetected());
        assertFalse(b.isStale());
        assertArrayEquals(new long[] {2447, 50}, b.bestBid());
        assertEquals(2, b.orderCountTotal());
    }

    @Test
    public void snapshotDuplicateInsideBurstIsIgnored() {
        OrderBook b = seeded(0);
        b.apply(add(9, Side.BID, 2449, 100, 44));
        MarketEvent rec = ev(10, EventType.SNAPSHOT, Side.BID, 2450, 500, 101, 2);
        b.apply(rec);
        b.apply(rec);
        b.apply(ev(11, EventType.SNAPSHOT, Side.BID, 2449, 400, 102, 1));
        b.apply(ev(12, EventType.SNAPSHOT, Side.ASK, 2451, 600, 103, 0));
        assertEquals(1, b.duplicatesDropped());
        assertFalse(b.isStale());
        assertEquals(3, b.orderCountTotal());
        assertEquals(0, b.snapshotRestarts());
    }

    @Test
    public void snapshotHeartbeatAndTradeInterleavedInsideBurst() {
        OrderBook b = seeded(0);
        b.apply(add(9, Side.BID, 2449, 100, 44));
        b.apply(ev(10, EventType.SNAPSHOT, Side.BID, 2450, 500, 101, 1));
        b.apply(ev(11, EventType.HEARTBEAT, 0, 0, 0, 0, 0));
        b.apply(ev(12, EventType.TRADE, Side.BID, 2451, 30, 0, 7));
        b.apply(ev(13, EventType.SNAPSHOT, Side.ASK, 2451, 600, 103, 0));
        assertFalse(b.isStale());
        assertEquals(30, b.tradeFlow());
        assertEquals(2, b.orderCountTotal());
    }

    // ------------------------------------------------------- #3 venue sequence reset

    @Test
    public void scenarioVenueSequenceResetDailyRestart() {
        OrderBook b = new OrderBook(1, 1);
        for (long s = 1; s <= 500; s++) {
            boolean bid = s % 2 == 1;
            b.apply(add(s, bid ? Side.BID : Side.ASK, bid ? 2400 - s % 7 : 2410 + s % 7, 100, 1000 + s));
        }
        assertEquals(500, b.lastSequence());
        assertFalse(b.isStale());
        burst(b, 1, BURST, true, 1);
        assertEquals(1, b.sequenceResets());
        assertEquals(1, b.sequenceEpoch());
        assertFalse(b.isStale());
        assertEquals(0, b.duplicatesDropped());
        assertArrayEquals(new long[] {2450, 500}, b.bestBid());
        assertEquals(3, b.orderCountTotal());
        b.apply(add(4, Side.BID, 2450, 100, 5001));
        assertArrayEquals(new long[] {2450, 600}, b.bestBid());
        assertEquals(4, b.lastSequence());
        b.apply(add(4, Side.BID, 2450, 100, 5002));
        assertEquals(1, b.duplicatesDropped());
    }

    @Test
    public void scenarioPartitionFailoverResetStaysStaleUntilBurstCompletes() {
        OrderBook b = seeded(0);
        b.apply(ev(1, EventType.SNAPSHOT, Side.BID, 2450, 500, 101, 2));
        assertEquals(1, b.sequenceResets());
        assertTrue(b.isStale());
        b.apply(ev(3, EventType.SNAPSHOT, Side.ASK, 2451, 600, 103, 0)); // gap inside
        assertTrue(b.isStale());
        assertEquals(1, b.gapsDetected());
        burst(b, 4, BURST, true, 1);
        assertFalse(b.isStale());
    }

    @Test
    public void scenarioSequenceResetExplicitApi() {
        OrderBook b = seeded(0);
        b.resetSequence();
        assertTrue(b.isStale());
        assertFalse(b.hasSequence());
        assertEquals(1, b.sequenceResets());
        b.apply(add(1, Side.BID, 2449, 100, 44));
        assertEquals(0, b.duplicatesDropped());
        assertEquals(1, b.droppedWhileStale());
        burst(b, 2, BURST, true, 1);
        assertFalse(b.isStale());
        assertEquals(4, b.lastSequence());
    }

    // --------------------------------------------------------- #5 auction call phase

    @Test
    public void scenarioHaltThenReopenAuction() {
        OrderBook b = seeded(0);
        b.apply(status(6, SessionStatus.HALT));
        b.apply(status(7, SessionStatus.AUCTION));
        b.apply(add(8, Side.BID, 2452, 100, 31)); // crosses both asks: rests
        assertArrayEquals(new long[] {2452, 100}, b.bestBid());
        assertArrayEquals(new long[] {2451, 150}, b.bestAsk());
        assertTrue(b.isCrossed());
        assertEquals(6, b.orderCountTotal());
        b.apply(ev(9, EventType.EXECUTE, Side.BID, 2452, 100, 31, 0));
        b.apply(ev(10, EventType.EXECUTE, Side.ASK, 2451, 100, 21, 0));
        assertFalse(b.isCrossed());
        assertEquals(0, b.unknownOrderEvents());
        assertArrayEquals(new long[] {2451, 50}, b.bestAsk());
        assertArrayEquals(new long[] {2449, 300}, b.bestBid());
        b.apply(status(11, SessionStatus.TRADING));
        b.apply(add(12, Side.BID, 2452, 100, 32)); // executes during TRADING
        assertArrayEquals(new long[] {2452, 200}, b.bestAsk());
        assertArrayEquals(new long[] {2449, 300}, b.bestBid());
        assertEquals(4, b.orderCountTotal());
    }

    @Test
    public void noMatchingWhileHaltedOrClosed() {
        for (long code : new long[] {SessionStatus.HALT, SessionStatus.CLOSE}) {
            OrderBook b = seeded(0);
            b.apply(status(6, code));
            b.apply(add(7, Side.ASK, 2448, 300, 31));
            assertTrue(b.isCrossed());
            assertEquals(6, b.orderCountTotal());
            assertArrayEquals(new long[] {2449, 300}, b.bestBid());
        }
    }

    // ------------------------------------------------ #6 payload-domain malformed events

    @Test
    public void payloadDomainMalformedEventsDroppedAndCounted() {
        OrderBook b = seeded(0);
        BookCheckpoint before = b.checkpoint();
        MarketEvent[] bad = {
            ev(6, EventType.EXECUTE, Side.BID, 2449, -50, 11, 0),
            ev(7, EventType.EXECUTE, Side.BID, 2449, 0, 11, 0),
            ev(8, EventType.QUOTE, Side.BID, 2449, 0, 77, 0),
            ev(9, EventType.SNAPSHOT, Side.BID, 0, 10, 78, 0),
            ev(10, EventType.TRADE, Side.ASK, 2449, -1, 0, 9),
            ev(11, EventType.ADD, Side.BID, 2447, 100, 0, 0),
            ev(12, EventType.ADD, Side.BID, 2447, 100, Validation.SYNTHETIC_ID_BASE + 1, 0),
            ev(13, EventType.STATUS, 0, 0, 7, 0, 0),
            ev(14, EventType.CANCEL, Side.BID, 0, 0, 0, 0),
            ev(15, EventType.ADD, Side.BID, -5, 100, 79, 0),
        };
        for (MarketEvent e : bad) {
            b.apply(e);
        }
        assertEquals(bad.length, b.invalidPayloadDropped());
        BookCheckpoint after = b.checkpoint();
        assertEquals(before.levels, after.levels);
        assertEquals(0, after.tradeFlow);
        assertEquals(15, after.lastSequence);
        assertEquals(SessionStatus.TRADING, b.status());
        assertArrayEquals(new long[] {2449, 300}, b.bestBid());
    }

    @Test
    public void unknownEventTypeDroppedNotThrown() {
        OrderBook b = seeded(0);
        b.apply(ev(6, 0, Side.BID, 2449, 100, 55, 0));
        b.apply(ev(7, 42, Side.BID, 2449, 100, 56, 0));
        assertEquals(2, b.unknownTypeDropped());
        assertEquals(7, b.lastSequence());
        b.apply(add(8, Side.BID, 2449, 50, 57));
        assertEquals(0, b.gapsDetected());
        assertArrayEquals(new long[] {2449, 350}, b.bestBid());
    }

    @Test
    public void modifyPriceMismatchDroppedAndCounted() {
        OrderBook b = seeded(0);
        b.apply(ev(6, EventType.MODIFY, Side.BID, 777, 10, 11, 0));
        assertEquals(1, b.modifyPriceMismatch());
        assertArrayEquals(new long[] {2449, 300}, b.bestBid());
        b.apply(ev(7, EventType.MODIFY, Side.BID, 0, 10, 11, 0));
        b.apply(ev(8, EventType.MODIFY, Side.BID, 2449, 20, 11, 0));
        assertArrayEquals(new long[] {2449, 220}, b.bestBid());
    }

    // ------------------------------------------- #7 interrupted SNAPSHOT burst restart

    @Test
    public void interruptedSnapshotBurstRestart() {
        OrderBook b = seeded(0);
        b.apply(ev(6, EventType.SNAPSHOT, Side.BID, 2430, 10, 301, 3));
        b.apply(ev(7, EventType.SNAPSHOT, Side.BID, 2429, 10, 302, 2));
        burst(b, 8, new long[][] {{Side.BID, 2440, 1, 401}, {Side.ASK, 2441, 2, 402},
            {Side.ASK, 2442, 3, 403}}, true, 1);
        assertEquals(1, b.snapshotRestarts());
        assertFalse(b.isStale());
        assertArrayEquals(new long[] {401, 402, 403}, ids(b));
    }

    @Test
    public void snapshotCountdownSkipMarksBurstBroken() {
        OrderBook b = seeded(0);
        b.apply(add(9, Side.BID, 2449, 100, 44));
        b.apply(ev(10, EventType.SNAPSHOT, Side.BID, 2450, 500, 101, 3));
        b.apply(ev(11, EventType.SNAPSHOT, Side.ASK, 2451, 600, 103, 0)); // skipped 2, 1
        assertTrue(b.isStale());
        assertEquals(0, b.snapshotRestarts());
        burst(b, 12, BURST, true, 1);
        assertFalse(b.isStale());
    }

    // ----------------------------------------- #8 consolidated excludes stale / crossed

    @Test
    public void scenarioBzxStallConsolidatedNbboExcludesStaleVenue() {
        ConsolidatedBook cons = new ConsolidatedBook(1);
        cons.apply(mk(1, EventType.ADD, Side.BID, 100, 10, 11, 0, 1, 0));
        cons.apply(mk(2, EventType.ADD, Side.ASK, 101, 10, 12, 0, 1, 0));
        cons.apply(mk(1, EventType.ADD, Side.BID, 105, 10, 21, 0, 2, 0));
        cons.apply(mk(2, EventType.ADD, Side.ASK, 106, 10, 22, 0, 2, 0));
        assertArrayEquals(new long[] {105, 10}, cons.bestBid());
        assertTrue(cons.isCrossed());
        cons.apply(mk(9, EventType.ADD, Side.BID, 107, 10, 23, 0, 2, 0)); // gap on venue 2
        assertArrayEquals(new int[] {2}, cons.staleVenues());
        assertArrayEquals(new int[] {1}, cons.activeVenues());
        assertArrayEquals(new long[] {100, 10}, cons.bestBid());
        assertArrayEquals(new long[] {101, 10}, cons.bestAsk());
        assertFalse(cons.isCrossed());
        assertFalse(cons.isLocked());
        assertArrayEquals(new long[][] {{100, 10}}, cons.depth(Side.BID, 10));
        assertArrayEquals(new long[][] {{101, 1}}, cons.orderCounts(Side.ASK, 10));
        assertEquals(SessionStatus.TRADING, cons.venueStatus(2));
        assertEquals(-1, cons.venueStatus(9));
        burst(cons.venueBook(2), 10, new long[][] {{Side.BID, 100, 5, 31}, {Side.ASK, 101, 5, 32}},
                true, 2);
        assertArrayEquals(new int[] {1, 2}, cons.activeVenues());
        assertArrayEquals(new long[] {100, 15}, cons.bestBid());
        assertArrayEquals(new long[][] {{100, 2}}, cons.orderCounts(Side.BID, 10));
    }

    @Test
    public void scenarioVenueDisconnectSilentFeed() {
        ConsolidatedBook cons = new ConsolidatedBook(1);
        cons.apply(mk(1, EventType.ADD, Side.BID, 100, 10, 11, 0, 1, TS0));
        cons.apply(mk(1, EventType.ADD, Side.BID, 99, 10, 21, 0, 2, TS0));
        long now = TS0 + 5_000_000_000L;
        cons.apply(mk(2, EventType.ADD, Side.ASK, 101, 10, 12, 0, 1, now));
        List<Integer> fresh = new ArrayList<>();
        for (Map.Entry<Integer, OrderBook> e : cons.venues().entrySet()) {
            if (e.getValue().isFresh(now + 150_000, 1_000_000_000L)) {
                fresh.add(e.getKey());
            }
        }
        assertEquals(List.of(1), fresh);
        assertArrayEquals(new int[] {1, 2}, cons.activeVenues());
    }

    // --------------------------------------------- #9 late retransmission (reorder window)

    @Test
    public void scenarioAbFeedRetransmissionWithReorderWindow() {
        OrderBook b = new OrderBook(1, 1, 3);
        for (long s : new long[] {1, 2, 3, 6, 4, 5, 7}) {
            b.apply(add(s, Side.BID, 2400 + s, 10, 100 + s));
        }
        assertEquals(0, b.gapsDetected());
        assertFalse(b.isStale());
        assertEquals(2, b.lateRecovered());
        assertEquals(0, b.pendingCount());
        assertEquals(7, b.lastSequence());
        assertArrayEquals(new long[] {101, 102, 103, 104, 105, 106, 107}, ids(b));
        assertEquals(7, b.eventsApplied());
        assertEquals(0, b.counters().drops());
    }

    @Test
    public void reorderWindowZeroReproducesStaleBehaviour() {
        OrderBook b = new OrderBook(1, 1);
        for (long s : new long[] {1, 2, 3, 6, 4, 5, 7}) {
            b.apply(add(s, Side.BID, 2400 + s, 10, 100 + s));
        }
        assertEquals(1, b.gapsDetected());
        assertTrue(b.isStale());
        assertEquals(2, b.duplicatesDropped());
        assertEquals(2, b.droppedWhileStale());
        assertEquals(0, b.lateRecovered());
    }

    @Test
    public void reorderWindowOverflowDeclaresGapAndFlushesInOrder() {
        OrderBook b = new OrderBook(1, 1, 2);
        b.apply(add(1, Side.BID, 2401, 10, 101));
        for (long s : new long[] {5, 3, 6}) {
            b.apply(add(s, Side.BID, 2400 + s, 10, 100 + s));
        }
        assertEquals(2, b.gapsDetected());
        assertTrue(b.isStale());
        assertEquals(0, b.pendingCount());
        assertEquals(6, b.lastSequence());
        assertEquals(3, b.droppedWhileStale());
    }

    @Test
    public void reorderWindowDuplicateInBufferAndCheckpointRoundTrip() {
        OrderBook b = new OrderBook(1, 1, 4);
        b.apply(add(1, Side.BID, 2401, 10, 101));
        b.apply(add(4, Side.BID, 2404, 10, 104));
        b.apply(add(4, Side.BID, 2404, 10, 104));
        assertEquals(1, b.duplicatesDropped());
        assertEquals(1, b.pendingCount());
        BookCheckpoint cp = b.checkpoint();
        assertEquals(4, cp.reorderWindow);
        assertEquals(1, cp.reorderPending.length);
        OrderBook r = OrderBook.restore(CheckpointJson.readBook(CheckpointJson.writeBook(cp)));
        for (OrderBook book : new OrderBook[] {b, r}) {
            book.apply(add(2, Side.BID, 2402, 10, 102));
            book.apply(add(3, Side.BID, 2403, 10, 103));
        }
        assertEquals(b.checkpoint(), r.checkpoint());
        assertEquals(0, r.pendingCount());
        assertEquals(2, r.lateRecovered());
        assertEquals(4, r.lastSequence());
    }

    @Test
    public void reorderWindowBoundsPinned() {
        new OrderBook(1, 1, OrderBook.MAX_REORDER_WINDOW);
        try {
            new OrderBook(1, 1, OrderBook.MAX_REORDER_WINDOW + 1);
            fail("window above the pinned maximum must throw");
        } catch (IllegalArgumentException expected) {
            // pinned
        }
    }

    // ----------------------------------------------------------- #10 first sequence 0

    @Test
    public void firstSequenceZeroBootstraps() {
        OrderBook b = new OrderBook(1, 1);
        b.apply(add(0, Side.BID, 100, 10, 1));
        b.apply(add(1, Side.BID, 101, 10, 2));
        b.apply(add(0, Side.BID, 102, 10, 3));
        assertEquals(2, b.orderCountTotal());
        assertEquals(1, b.duplicatesDropped());
        assertEquals(0, b.gapsDetected());
        assertFalse(b.isStale());
        assertTrue(b.hasSequence());
    }

    // ------------------------------------------------------------ #11 arithmetic limits

    @Test
    public void arithmeticLimitsNoWrapEventsDroppedAndCounted() {
        OrderBook b = new OrderBook(1, 1);
        b.apply(ev(1, EventType.TRADE, Side.BID, 100, Long.MAX_VALUE, 0, 1));
        assertEquals(Long.MAX_VALUE, b.tradeFlow());
        b.apply(ev(2, EventType.TRADE, Side.BID, 100, 1, 0, 2));
        assertEquals(Long.MAX_VALUE, b.tradeFlow());
        assertEquals(1, b.invalidPayloadDropped());
        b.apply(ev(3, EventType.TRADE, Side.ASK, 100, Long.MAX_VALUE, 0, 3));
        b.apply(ev(4, EventType.TRADE, Side.ASK, 100, Long.MAX_VALUE, 0, 4));
        b.apply(ev(5, EventType.TRADE, Side.ASK, 100, 2, 0, 5));
        assertEquals(-Long.MAX_VALUE, b.tradeFlow());
        assertEquals(2, b.invalidPayloadDropped());
        b.apply(add(6, Side.BID, 100, Long.MAX_VALUE, 1));
        b.apply(add(7, Side.BID, 100, Long.MAX_VALUE, 2));
        assertArrayEquals(new long[] {100, Long.MAX_VALUE}, b.bestBid());
        assertEquals(3, b.invalidPayloadDropped());
        b.apply(ev(8, EventType.MODIFY, Side.BID, 100, 1, 1, 0));
        b.apply(add(9, Side.BID, 100, Long.MAX_VALUE - 1, 3));
        b.apply(ev(10, EventType.MODIFY, Side.BID, 100, 2, 1, 0));
        assertEquals(4, b.invalidPayloadDropped());
        assertArrayEquals(new long[] {100, Long.MAX_VALUE}, b.bestBid());
        b.apply(ev(11, EventType.TRADE, Side.ASK, 100, Long.MIN_VALUE, 0, 11));
        assertEquals(5, b.invalidPayloadDropped());
        OrderBook c = new OrderBook(1, 1);
        c.apply(mk(-1L, EventType.ADD, Side.BID, 100, 10, 1, 0, 1, TS0)); // u64::MAX
        c.apply(mk(0, EventType.ADD, Side.BID, 101, 10, 2, 0, 1, TS0));
        assertEquals(1, c.duplicatesDropped());
        assertEquals(-1L, c.lastSequence());
        assertEquals(b.checkpoint(), OrderBook.restore(b.checkpoint()).checkpoint());
    }

    @Test
    public void consolidatedTradeFlowSaturates() {
        ConsolidatedBook cons = new ConsolidatedBook(1);
        cons.apply(mk(1, EventType.TRADE, Side.BID, 100, Long.MAX_VALUE, 0, 1, 1, 0));
        cons.apply(mk(1, EventType.TRADE, Side.BID, 100, Long.MAX_VALUE, 0, 1, 2, 0));
        assertEquals(Long.MAX_VALUE, cons.tradeFlow());
        cons.apply(mk(2, EventType.TRADE, Side.ASK, 100, Long.MAX_VALUE, 0, 2, 2, 0));
        cons.apply(mk(3, EventType.TRADE, Side.ASK, 100, 5, 0, 3, 2, 0));
        assertEquals(Long.MAX_VALUE - 5, cons.tradeFlow());
    }

    // --------------------------------------------------------- #13 ids >= 2^63 in the book

    @Test
    public void bookWithOrderIdsAbove2Pow63() {
        long base = Long.MIN_VALUE + 1; // 2^63 + 1 as u64
        long s0 = Long.MIN_VALUE;       // 2^63
        OrderBook b = new OrderBook(1, 1);
        b.apply(mk(s0, EventType.ADD, Side.BID, 100, 10, base, 0, 1, TS0));
        b.apply(mk(s0 + 1, EventType.ADD, Side.BID, 100, 20, base + 1, 0, 1, TS0));
        b.apply(mk(s0 + 2, EventType.MODIFY, Side.BID, 100, 30, base, 0, 1, TS0));
        b.apply(mk(s0 + 3, EventType.EXECUTE, Side.BID, 100, 5, base + 1, 0, 1, TS0));
        assertArrayEquals(new long[] {base, base + 1}, ids(b));
        BookCheckpoint cp = b.checkpoint();
        assertArrayEquals(new long[] {base + 1, base}, cp.levels.get(0).orderIds);
        assertArrayEquals(new long[] {15, 30}, cp.levels.get(0).qtys);
        b.apply(mk(s0 + 4, EventType.CANCEL, Side.BID, 100, 0, base + 1, 0, 1, TS0));
        String json = CheckpointJson.writeBook(b.checkpoint());
        assertTrue(json.contains("9223372036854775809")); // unsigned decimal on the wire
        assertEquals(b.checkpoint(), OrderBook.restore(CheckpointJson.readBook(json)).checkpoint());
        assertEquals(0, b.unknownOrderEvents());
        assertEquals(0, b.gapsDetected());
    }

    // ------------------------------------------------------- #14 replay universe validation

    @Test
    public void replayRejectsUnknownInstrumentAndVenue() {
        Map<Long, Set<Integer>> universe = Map.of(1L, new TreeSet<>(List.of(1, 2)));
        ReplayEngine engine = new ReplayEngine(0, 0, 4, 4, 0, universe);
        engine.apply(add(1, Side.BID, 100, 10, 9));
        engine.apply(new MarketEvent(2, 9999, 1, TS0, TS0, 1, EventType.ADD, 0, 100, 10, 9, 0));
        engine.apply(new MarketEvent(3, 1, 10, TS0, TS0, 2, EventType.ADD, 0, 100, 10, 9, 0));
        assertEquals(1, engine.unknownInstrumentDropped());
        assertEquals(1, engine.unknownVenueDropped());
        assertEquals(3, engine.eventsProcessed());
        assertEquals(1, engine.instruments().size());
        assertEquals(1, engine.instrumentBook(1).venues().size());
        String json = CheckpointJson.write(engine.checkpoint());
        ReplayEngine resumed = ReplayEngine.restore(CheckpointJson.read(json));
        assertEquals(new TreeSet<>(List.of(1, 2)), resumed.universe().get(1L));
        resumed.apply(new MarketEvent(4, 7777, 1, TS0, TS0, 2, EventType.ADD, 0, 100, 10, 9, 0));
        assertEquals(2, resumed.unknownInstrumentDropped());
        assertEquals(engine.checkpoint().universe, resumed.checkpoint().universe);
    }

    // ------------------------------------------------------ #15 corrupt record mid-file

    @Test
    public void scenarioBitFlipInIap1RecordIsDetected() {
        List<MarketEvent> events = new ArrayList<>();
        for (long s = 1; s <= 600; s++) {
            events.add(add(s, Side.BID, 100, 10, s));
        }
        byte[] data = Iap1Codec.encode(events);
        int off = 16 + 72 * 499;
        data[off + 14] = 0;             // event_type of record 500 -> 0
        data[off + 55] ^= (byte) 0x80;  // qty sign
        try {
            Iap1Codec.decode(data);
            fail("CRC mismatch must be rejected");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("CRC-32"));
        }
        // Legacy v1 (no trailer) cannot be verified: the book still never throws.
        byte[] legacy = java.util.Arrays.copyOf(data, data.length - 16);
        legacy[4] = 1;
        Iap1Codec.Decoded d = Iap1Codec.decodeEx(legacy);
        assertFalse(d.integrityChecked());
        OrderBook book = new OrderBook(1, 1);
        for (MarketEvent e : d.events()) {
            book.apply(e);
        }
        assertEquals(1, book.unknownTypeDropped());
        assertEquals(599, book.eventsApplied());
    }

    // ------------------------------------------------------------ #16 memory bounds

    @Test
    public void longSessionSnapshotRetentionBounded() {
        List<MarketEvent> events = new ArrayList<>();
        for (long s = 1; s <= 300; s++) {
            events.add(add(s, Side.BID, 100 + s % 5, 10, s));
        }
        ReplayEngine engine = new ReplayEngine(0, 1, 4, 3, 0, null);
        long[] seen = {0};
        for (int pass = 0; pass < 5; pass++) {
            engine.run(events, (i, snap) -> seen[0]++);
        }
        assertEquals(1500, seen[0]);
        assertEquals(1500, engine.snapshotsEmitted());
        assertEquals(3, engine.snapshots().size());
        assertEquals(1500, engine.snapshots().get(2).index);
    }

    // ------------------------------------------------------ #20 QUOTE feed without ids

    @Test
    public void scenarioFxLpQuoteFeedWithoutIds() {
        ConsolidatedBook cons = new ConsolidatedBook(1);
        long[] seqs = new long[3];
        for (int k = 0; k < 10_000; k++) {
            int vi = k % 3;
            seqs[vi]++;
            int side = ((k / 3) % 2 == 0) ? Side.BID : Side.ASK;
            long price = side == Side.BID ? 108650 - 3 + (k % 5) : 108650 + 3 + (k % 5);
            cons.apply(mk(seqs[vi], EventType.QUOTE, side, price, 5 + (k % 7), 0, 0, 10 + vi, 0));
        }
        for (Map.Entry<Integer, OrderBook> e : cons.venues().entrySet()) {
            OrderBook book = e.getValue();
            assertArrayEquals(new long[][] {{book.bestBid()[0], 1}}, book.orderCounts(Side.BID, 10));
            assertArrayEquals(new long[][] {{book.bestAsk()[0], 1}}, book.orderCounts(Side.ASK, 10));
            long[] got = ids(book).clone();
            java.util.Arrays.sort(got);
            long[] want = {OrderBook.syntheticOrderId(Side.ASK, 0), OrderBook.syntheticOrderId(Side.BID, 0)};
            java.util.Arrays.sort(want);
            assertArrayEquals(want, got);
            assertEquals(0, book.counters().drops());
            assertEquals(seqs[e.getKey() - 10], book.eventsApplied());
        }
        ConsolidatedBook.Checkpoint cp = cons.checkpoint();
        assertEquals(cp, ConsolidatedBook.restore(cp).checkpoint());
    }

    @Test
    public void quoteExplicitIdRules() {
        OrderBook b = new OrderBook(1, 1);
        b.apply(ev(1, EventType.QUOTE, Side.BID, 100, 5, 7, 0));
        b.apply(ev(2, EventType.QUOTE, Side.ASK, 101, 7, 7, 0)); // rests on BID: dropped
        assertEquals(1, b.unknownOrderEvents());
        assertNull(b.bestAsk());
        b.apply(ev(3, EventType.QUOTE, Side.BID, 99, 4, 7, 0)); // same side: replace
        assertArrayEquals(new long[] {99, 4}, b.bestBid());
        assertEquals(1, b.orderCountTotal());
        b.apply(ev(4, EventType.QUOTE, Side.ASK, 101, 7, 0, 0));
        b.apply(ev(5, EventType.QUOTE, Side.BID, 98, 3, 0, 0));
        assertArrayEquals(new long[] {OrderBook.syntheticOrderId(Side.ASK, 0),
            OrderBook.syntheticOrderId(Side.BID, 0)}, ids(b));
    }

    @Test
    public void snapshotWithZeroIdsAssignsDeterministicSyntheticIds() {
        OrderBook b = new OrderBook(1, 1);
        burst(b, 1, new long[][] {{Side.BID, 100, 5, 0}, {Side.BID, 99, 6, 0}, {Side.ASK, 101, 7, 0}},
                false, 1);
        assertArrayEquals(new long[] {OrderBook.syntheticOrderId(Side.BID, 0),
            OrderBook.syntheticOrderId(Side.BID, 1), OrderBook.syntheticOrderId(Side.ASK, 0)}, ids(b));
        b.apply(ev(4, EventType.EXECUTE, Side.BID, 100, 2, OrderBook.syntheticOrderId(Side.BID, 0), 0));
        assertArrayEquals(new long[] {100, 3}, b.bestBid());
        b.apply(ev(5, EventType.SNAPSHOT, Side.BID, 100, 5, 9, 1));
        b.apply(ev(6, EventType.SNAPSHOT, Side.ASK, 101, 5, 9, 0));
        assertEquals(1, b.unknownOrderEvents());
        assertEquals(1, b.orderCountTotal());
    }

    // ------------------------------------------------- #21 STATUS while stale, ADD after CLOSE

    @Test
    public void statusWhileStaleAndAddAfterClose() {
        OrderBook b = seeded(0);
        b.apply(add(9, Side.BID, 2449, 100, 44));
        for (long code : new long[] {SessionStatus.HALT, SessionStatus.AUCTION, SessionStatus.CLOSE}) {
            b.apply(status(b.lastSequence() + 1, code));
            assertEquals(code, b.status());
        }
        burst(b, b.lastSequence() + 1, BURST, true, 1);
        assertFalse(b.isStale());
        assertEquals(SessionStatus.CLOSE, b.status());
        b.apply(add(b.lastSequence() + 1, Side.BID, 2452, 100, 45));
        assertArrayEquals(new long[] {2452, 100}, b.bestBid());
        assertTrue(b.isCrossed());
        assertEquals(4, b.orderCountTotal());
    }
}
