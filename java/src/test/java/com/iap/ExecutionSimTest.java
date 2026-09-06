package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import java.util.List;
import java.util.TreeMap;

import org.junit.Test;

import com.iap.core.MarketEvent;
import com.iap.core.SplitMix64;
import com.iap.execution.ChildOrder;
import com.iap.execution.ExecConfig;
import com.iap.execution.ExecutionSimulator;
import com.iap.execution.Fill;
import com.iap.execution.InstrumentSpec;
import com.iap.execution.LatencyConfig;
import com.iap.execution.Liquidity;
import com.iap.execution.CancelReason;
import com.iap.execution.OrderState;
import com.iap.execution.OrderType;
import com.iap.execution.VenueSpec;

/**
 * Execution-simulator unit tests mirroring the C++ suite: queue-position
 * rule (incl. marketable-ADD expansion and the crossing exemption), order
 * types, partial fills, fee/rebate/impact arithmetic, determinism and
 * latency ordering (pinned rules: ExecutionSimulator javadoc / C++
 * execution.hpp).
 */
public class ExecutionSimTest {
    private static final long T0 = 1_700_000_000_000_000_000L;
    private static final long INS = 7;
    private static final int VEN = 1;
    /** Total internal + venue-mean latency of testConfig (jitter 0). */
    private static final long LAT = 50_000 + 50_000 + 100_000 + 150_000;

    private static ExecConfig testConfig(long jitterNs) {
        TreeMap<Integer, VenueSpec> venues = new TreeMap<>();
        venues.put(VEN, new VenueSpec(VEN, "TST", false, 0.003, 0.002, 0.0,
                150_000, jitterNs));
        TreeMap<Long, InstrumentSpec> instruments = new TreeMap<>();
        instruments.put(INS, new InstrumentSpec(INS, 0.01, 1.0, 1_000_000.0));
        return new ExecConfig(LatencyConfig.DEFAULT, 42, 2.0, instruments, venues);
    }

    /** Feeder of raw events on the (7, 1) stream. */
    private static final class Feeder {
        long seq;

        MarketEvent ev(long ts, int type, int side, long px, long qty, long oid) {
            seq++;
            return new MarketEvent(seq, INS, VEN, ts, ts, seq, type, side, px,
                    qty, oid, 0);
        }

        MarketEvent add(long ts, int side, long px, long qty, long oid) {
            return ev(ts, 1, side, px, qty, oid);
        }

        MarketEvent modify(long ts, int side, long px, long qty, long oid) {
            return ev(ts, 2, side, px, qty, oid);
        }

        MarketEvent cancel(long ts, int side, long px, long qty, long oid) {
            return ev(ts, 3, side, px, qty, oid);
        }

        MarketEvent exec(long ts, int side, long px, long qty, long oid) {
            return ev(ts, 4, side, px, qty, oid);
        }

        MarketEvent heartbeat(long ts) {
            return ev(ts, 9, 0, 0, 0, 0);
        }

        MarketEvent status(long ts, int st) {
            return ev(ts, 8, 0, 0, st, 0);
        }

        /** An ADD that skips one sequence number (venue gap -> stale). */
        MarketEvent gapAdd(long ts, int side, long px, long qty, long oid) {
            seq++;
            return add(ts, side, px, qty, oid);
        }

        MarketEvent snapshot(long ts, int side, long px, long qty, long oid,
                long countdown) {
            seq++;
            return new MarketEvent(seq, INS, VEN, ts, ts, seq, 7, side, px, qty,
                    oid, countdown);
        }
    }

    /**
     * Seed a two-sided book: bids 100x300 (order 11), 99x400 (12); asks
     * 101x200 (21), 102x500 (22).
     */
    private static void seedBook(ExecutionSimulator sim, Feeder f) {
        sim.onEvent(f.add(T0, 0, 100, 300, 11));
        sim.onEvent(f.add(T0 + 1, 0, 99, 400, 12));
        sim.onEvent(f.add(T0 + 2, 1, 101, 200, 21));
        sim.onEvent(f.add(T0 + 3, 1, 102, 500, 22));
    }

    private static ChildOrder child(int side, OrderType type, long px, long qty,
            long decisionTs) {
        ChildOrder c = new ChildOrder();
        c.parentId = 99;
        c.instrumentId = INS;
        c.venueId = VEN;
        c.side = side;
        c.type = type;
        c.limitTicks = px;
        c.qty = qty;
        c.decisionTs = decisionTs;
        return c;
    }

    @Test
    public void entryAheadEqualsDisplayedDepth() {
        ExecutionSimulator sim = new ExecutionSimulator(testConfig(0));
        Feeder f = new Feeder();
        seedBook(sim, f);
        long id = sim.submit(child(0, OrderType.LIMIT, 100, 50, T0 + 10));
        sim.onEvent(f.heartbeat(T0 + 10 + LAT + 1)); // activates the order
        ChildOrder o = sim.orders().get(id);
        assertEquals(OrderState.ACTIVE, o.state);
        assertTrue(o.resting);
        assertEquals(300, o.aheadQty);
        assertTrue(sim.fills().isEmpty());
    }

    @Test
    public void executeDepletesAheadThenFills() {
        ExecutionSimulator sim = new ExecutionSimulator(testConfig(0));
        Feeder f = new Feeder();
        seedBook(sim, f);
        long id = sim.submit(child(0, OrderType.LIMIT, 100, 50, T0 + 10));
        sim.onEvent(f.heartbeat(T0 + 10 + LAT + 1));
        // EXECUTE 200 at our level: all ahead (300 -> 100), no fill yet.
        sim.onEvent(f.exec(T0 + 2_000_000, 0, 100, 200, 11));
        assertEquals(100, sim.orders().get(id).aheadQty);
        assertTrue(sim.fills().isEmpty());
        // EXECUTE 130: 100 depletes the queue ahead, leftover 30 fills us.
        sim.onEvent(f.exec(T0 + 3_000_000, 0, 100, 130, 12));
        assertEquals(1, sim.fills().size());
        Fill fill = sim.fills().get(0);
        assertEquals(30, fill.qty());
        assertEquals(100, fill.priceTicks());
        assertEquals(T0 + 3_000_000, fill.ts());
        assertEquals(Liquidity.MAKER, fill.liquidity());
        assertEquals(20, sim.orders().get(id).remaining); // partial fill
        assertEquals(OrderState.ACTIVE, sim.orders().get(id).state);
        // Next EXECUTE fills the remainder (leftover capped at remaining).
        sim.onEvent(f.exec(T0 + 4_000_000, 0, 100, 500, 13));
        assertEquals(2, sim.fills().size());
        assertEquals(20, sim.fills().get(1).qty());
        assertEquals(OrderState.FILLED, sim.orders().get(id).state);
    }

    @Test
    public void cancelAheadReducesPositionDeterministically() {
        ExecutionSimulator sim = new ExecutionSimulator(testConfig(0));
        Feeder f = new Feeder();
        seedBook(sim, f);
        long id = sim.submit(child(0, OrderType.LIMIT, 100, 50, T0 + 10));
        sim.onEvent(f.heartbeat(T0 + 10 + LAT + 1));
        assertEquals(300, sim.orders().get(id).aheadQty);
        // Pinned: an observed CANCEL at our level reduces ahead by its FULL
        // qty (deterministic, no probabilistic split).
        sim.onEvent(f.cancel(T0 + 2_000_000, 0, 100, 250, 11));
        assertEquals(50, sim.orders().get(id).aheadQty);
        // A cancel at another level does nothing.
        sim.onEvent(f.cancel(T0 + 2_100_000, 0, 99, 400, 12));
        assertEquals(50, sim.orders().get(id).aheadQty);
        // MODIFY events never change queue position (pinned).
        sim.onEvent(f.modify(T0 + 2_200_000, 0, 100, 10, 11));
        assertEquals(50, sim.orders().get(id).aheadQty);
        // Now a 60-EXECUTE: 50 ahead, 10 to us.
        sim.onEvent(f.exec(T0 + 3_000_000, 0, 100, 60, 11));
        assertEquals(1, sim.fills().size());
        assertEquals(10, sim.fills().get(0).qty());
    }

    @Test
    public void tradeThroughFillsInFullAtOurPrice() {
        ExecutionSimulator sim = new ExecutionSimulator(testConfig(0));
        Feeder f = new Feeder();
        seedBook(sim, f);
        long id = sim.submit(child(0, OrderType.LIMIT, 100, 50, T0 + 10));
        sim.onEvent(f.heartbeat(T0 + 10 + LAT + 1));
        // EXECUTE on the bid side BELOW our price: the market traded through
        // our level — full fill at OUR limit.
        sim.onEvent(f.exec(T0 + 2_000_000, 0, 99, 100, 12));
        assertEquals(1, sim.fills().size());
        assertEquals(50, sim.fills().get(0).qty());
        assertEquals(100, sim.fills().get(0).priceTicks());
        assertEquals(Liquidity.MAKER, sim.fills().get(0).liquidity());
        assertEquals(OrderState.FILLED, sim.orders().get(id).state);
    }

    @Test
    public void marketableAddConsumesQueueAhead() {
        ExecutionSimulator sim = new ExecutionSimulator(testConfig(0));
        Feeder f = new Feeder();
        seedBook(sim, f);
        long id = sim.submit(child(0, OrderType.LIMIT, 100, 50, T0 + 10));
        sim.onEvent(f.heartbeat(T0 + 10 + LAT + 1));
        assertEquals(300, sim.orders().get(id).aheadQty);
        // A marketable sell ADD at 100 for 80 executes against the book with
        // no EXECUTE events; the expansion consumes 80 of the 300 ahead.
        sim.onEvent(f.add(T0 + 2_000_000, 1, 100, 80, 23));
        assertTrue(sim.fills().isEmpty());
        assertEquals(220, sim.orders().get(id).aheadQty);
        // A deep marketable sell ADD (limit 99, qty 300): consumes the
        // remaining 220 at 100, then walks to 99 — trading strictly through
        // our 100 bid, so our remaining 50 fills in full at 100.
        sim.onEvent(f.add(T0 + 3_000_000, 1, 99, 300, 24));
        assertEquals(1, sim.fills().size());
        assertEquals(100, sim.fills().get(0).priceTicks());
        assertEquals(50, sim.fills().get(0).qty());
        assertEquals(Liquidity.MAKER, sim.fills().get(0).liquidity());
        assertEquals(OrderState.FILLED, sim.orders().get(id).state);
    }

    @Test
    public void marketableAddTradingThroughFillsInFull() {
        ExecutionSimulator sim = new ExecutionSimulator(testConfig(0));
        Feeder f = new Feeder();
        seedBook(sim, f);
        // We bid 101 inside the spread (level not displayed): ahead_qty 0.
        long id = sim.submit(child(0, OrderType.LIMIT, 101, 50, T0 + 10));
        // Cancel the displayed ask at 101 so the limit rests in the spread.
        sim.onEvent(f.cancel(T0 + 1_000, 1, 101, 200, 21));
        sim.onEvent(f.heartbeat(T0 + 10 + LAT + 1));
        assertEquals(OrderState.ACTIVE, sim.orders().get(id).state);
        assertEquals(0, sim.orders().get(id).aheadQty);
        // Marketable sell ADD at 100 consumes the displayed 100-bid level —
        // strictly through our 101 bid, which must have filled first.
        sim.onEvent(f.add(T0 + 2_000_000, 1, 100, 120, 24));
        assertEquals(1, sim.fills().size());
        assertEquals(101, sim.fills().get(0).priceTicks());
        assertEquals(50, sim.fills().get(0).qty());
        assertEquals(OrderState.FILLED, sim.orders().get(id).state);
    }

    @Test
    public void crossingQuoteAfterL1ReplaceFills() {
        // FX-style: a QUOTE replaces the venue's L1; if the new opposite
        // best crosses our resting price, we fill (post-apply check).
        ExecutionSimulator sim = new ExecutionSimulator(testConfig(0));
        Feeder f = new Feeder();
        seedBook(sim, f);
        long id = sim.submit(child(0, OrderType.LIMIT, 100, 50, T0 + 10));
        sim.onEvent(f.heartbeat(T0 + 10 + LAT + 1));
        assertEquals(OrderState.ACTIVE, sim.orders().get(id).state);
        // QUOTE: ask side replaced at 100 <= our bid 100 -> crossed.
        sim.onEvent(f.ev(T0 + 2_000_000, 6, 1, 100, 250, 0));
        assertEquals(1, sim.fills().size());
        assertEquals(100, sim.fills().get(0).priceTicks());
        assertEquals(50, sim.fills().get(0).qty());
        assertEquals(OrderState.FILLED, sim.orders().get(id).state);
    }

    @Test
    public void marketWalksDisplayedDepth() {
        ExecutionSimulator sim = new ExecutionSimulator(testConfig(0));
        Feeder f = new Feeder();
        seedBook(sim, f);
        // Market buy 250: 200 @ 101, then 50 @ 102 — one fill per level.
        long id = sim.submit(child(0, OrderType.MARKET, 0, 250, T0 + 10));
        sim.onEvent(f.heartbeat(T0 + 10 + LAT + 1));
        assertEquals(2, sim.fills().size());
        assertEquals(101, sim.fills().get(0).priceTicks());
        assertEquals(200, sim.fills().get(0).qty());
        assertEquals(102, sim.fills().get(1).priceTicks());
        assertEquals(50, sim.fills().get(1).qty());
        assertEquals(Liquidity.TAKER, sim.fills().get(0).liquidity());
        // Aggressive fills are stamped with the order's arrival_ts.
        assertEquals(sim.orders().get(id).arrivalTs, sim.fills().get(0).ts());
        assertEquals(OrderState.FILLED, sim.orders().get(id).state);
    }

    @Test
    public void marketPartialRemainderCancelled() {
        ExecutionSimulator sim = new ExecutionSimulator(testConfig(0));
        Feeder f = new Feeder();
        seedBook(sim, f);
        // Displayed ask depth is 700; a 1000 market buy part-fills, cancels.
        long id = sim.submit(child(0, OrderType.MARKET, 0, 1000, T0 + 10));
        sim.onEvent(f.heartbeat(T0 + 10 + LAT + 1));
        assertEquals(2, sim.fills().size());
        assertEquals(700, sim.fills().get(0).qty() + sim.fills().get(1).qty());
        assertEquals(OrderState.CANCELLED, sim.orders().get(id).state);
        assertEquals(300, sim.orders().get(id).remaining);
    }

    @Test
    public void marketableLimitTakesThenRests() {
        ExecutionSimulator sim = new ExecutionSimulator(testConfig(0));
        Feeder f = new Feeder();
        seedBook(sim, f);
        // Buy limit 101 for 300: takes the 200 displayed at 101, remainder
        // 100 rests at 101 with nothing ahead.
        long id = sim.submit(child(0, OrderType.LIMIT, 101, 300, T0 + 10));
        sim.onEvent(f.heartbeat(T0 + 10 + LAT + 1));
        assertEquals(1, sim.fills().size());
        assertEquals(101, sim.fills().get(0).priceTicks());
        assertEquals(200, sim.fills().get(0).qty());
        assertEquals(Liquidity.TAKER, sim.fills().get(0).liquidity());
        ChildOrder o = sim.orders().get(id);
        assertEquals(OrderState.ACTIVE, o.state);
        assertEquals(100, o.remaining);
        assertEquals(0, o.aheadQty);
        // The display still shows the consumed ask liquidity (the book is
        // never mutated) => crossing-exempt: no double-count re-fill.
        assertTrue(o.crossExempt);
        sim.onEvent(f.heartbeat(T0 + 20_000_000));
        assertEquals(1, sim.fills().size()); // still only the aggressive fill
        // Once the display goes uncrossed the exemption ends: cancel the
        // stale 101 ask, then a fresh crossing quote fills us.
        sim.onEvent(f.cancel(T0 + 21_000_000, 1, 101, 200, 21));
        assertTrue(!sim.orders().get(id).crossExempt);
        sim.onEvent(f.ev(T0 + 22_000_000, 6, 1, 100, 300, 0));
        assertEquals(2, sim.fills().size());
        assertEquals(100, sim.fills().get(1).qty());
        assertEquals(101, sim.fills().get(1).priceTicks());
        assertEquals(OrderState.FILLED, sim.orders().get(id).state);
    }

    @Test
    public void iocFillsWhatItCanThenCancels() {
        ExecutionSimulator sim = new ExecutionSimulator(testConfig(0));
        Feeder f = new Feeder();
        seedBook(sim, f);
        // IOC buy limit 101 for 300: fills 200 @ 101, cancels the rest.
        long id = sim.submit(child(0, OrderType.IOC, 101, 300, T0 + 10));
        sim.onEvent(f.heartbeat(T0 + 10 + LAT + 1));
        assertEquals(1, sim.fills().size());
        assertEquals(200, sim.fills().get(0).qty());
        assertEquals(OrderState.CANCELLED, sim.orders().get(id).state);
        assertEquals(100, sim.orders().get(id).remaining);
    }

    @Test
    public void fokAllOrNone() {
        // Kill branch: 300 wanted within limit 101 but only 200 displayed.
        {
            ExecutionSimulator sim = new ExecutionSimulator(testConfig(0));
            Feeder f = new Feeder();
            seedBook(sim, f);
            long id = sim.submit(child(0, OrderType.FOK, 101, 300, T0 + 10));
            sim.onEvent(f.heartbeat(T0 + 10 + LAT + 1));
            assertTrue(sim.fills().isEmpty());
            assertEquals(OrderState.CANCELLED, sim.orders().get(id).state);
            assertEquals(300, sim.orders().get(id).remaining);
        }
        // Fill branch: limit 102 spans 200 + 500 displayed >= 300.
        {
            ExecutionSimulator sim = new ExecutionSimulator(testConfig(0));
            Feeder f = new Feeder();
            seedBook(sim, f);
            long id = sim.submit(child(0, OrderType.FOK, 102, 300, T0 + 10));
            sim.onEvent(f.heartbeat(T0 + 10 + LAT + 1));
            assertEquals(2, sim.fills().size());
            assertEquals(300,
                    sim.fills().get(0).qty() + sim.fills().get(1).qty());
            assertEquals(OrderState.FILLED, sim.orders().get(id).state);
        }
    }

    @Test
    public void takerMakerAndImpactArithmetic() {
        ExecutionSimulator sim = new ExecutionSimulator(testConfig(0));
        Feeder f = new Feeder();
        seedBook(sim, f);
        // Taker: sell 100 into the 100 bid.
        sim.submit(child(1, OrderType.MARKET, 0, 100, T0 + 10));
        sim.onEvent(f.heartbeat(T0 + 10 + LAT + 1));
        assertEquals(1, sim.fills().size());
        Fill taker = sim.fills().get(0);
        assertEquals(100, taker.priceTicks());
        assertEquals(0.003 * 100.0, taker.fee(), 0.0);
        // impact_bps = 2.0 * (100 / 1e6 * 100) = 0.02 bps over notional
        // 100 * 100 ticks * 0.01 = 100.0 => 0.02e-4 * 100 = 2e-4.
        assertEquals(2e-4, taker.impactCost(), 1e-15);
        // Maker: passive buy at 100, filled by trade-through.
        sim.submit(child(0, OrderType.LIMIT, 100, 40, T0 + 5_000_000));
        sim.onEvent(f.heartbeat(T0 + 5_000_000 + LAT + 1));
        sim.onEvent(f.exec(T0 + 8_000_000, 0, 99, 10, 12));
        assertEquals(2, sim.fills().size());
        Fill maker = sim.fills().get(1);
        assertEquals(Liquidity.MAKER, maker.liquidity());
        assertEquals(-0.002 * 40.0, maker.fee(), 0.0); // rebate: negative fee
        assertEquals(0.0, maker.impactCost(), 0.0);    // passive: no impact
    }

    @Test
    public void fxCommissionPerMillionNotional() {
        TreeMap<Integer, VenueSpec> venues = new TreeMap<>();
        venues.put(VEN, new VenueSpec(VEN, "TST", true, 0.0, 0.0, 2.5,
                150_000, 0));
        TreeMap<Long, InstrumentSpec> instruments = new TreeMap<>();
        instruments.put(INS, new InstrumentSpec(INS, 1e-05, 1000.0, 1_000_000.0));
        ExecConfig cfg = new ExecConfig(LatencyConfig.DEFAULT, 42, 2.0,
                instruments, venues);
        ExecutionSimulator sim = new ExecutionSimulator(cfg);
        Feeder f = new Feeder();
        sim.onEvent(f.add(T0, 0, 108650, 500, 11));
        sim.onEvent(f.add(T0 + 1, 1, 108660, 500, 21));
        sim.submit(child(0, OrderType.MARKET, 0, 100, T0 + 10));
        sim.onEvent(f.heartbeat(T0 + 10 + LAT + 1));
        assertEquals(1, sim.fills().size());
        // notional = 100 * 1000 * 108660 * 1e-5 = 108660.0
        assertEquals(2.5 * 108660.0 / 1e6, sim.fills().get(0).fee(), 1e-12);
    }

    @Test
    public void arrivalDecompositionAndJitterDraw() {
        // With jitter: arrival must equal decision + internal legs + venue
        // mean + SplitMix64(seed).below(jitter + 1), in submission order.
        long jitterNs = 50_000;
        ExecutionSimulator sim = new ExecutionSimulator(testConfig(jitterNs));
        Feeder f = new Feeder();
        seedBook(sim, f);
        SplitMix64 rng = new SplitMix64(42); // same seed as testConfig
        long id1 = sim.submit(child(0, OrderType.LIMIT, 99, 10, T0 + 10));
        long id2 = sim.submit(child(1, OrderType.LIMIT, 102, 10, T0 + 20));
        long j1 = rng.below(jitterNs + 1);
        long j2 = rng.below(jitterNs + 1);
        assertEquals(T0 + 10 + LAT + j1, sim.orders().get(id1).arrivalTs);
        assertEquals(T0 + 20 + LAT + j2, sim.orders().get(id2).arrivalTs);
        assertTrue(j1 >= 0 && j1 <= jitterNs);
    }

    @Test
    public void noFillBeforeArrivalAndOrderedActivation() {
        ExecutionSimulator sim = new ExecutionSimulator(testConfig(0));
        Feeder f = new Feeder();
        seedBook(sim, f);
        long id = sim.submit(child(0, OrderType.MARKET, 0, 50, T0 + 10));
        long arrival = sim.orders().get(id).arrivalTs;
        // Events strictly before arrival do NOT activate the order.
        sim.onEvent(f.heartbeat(arrival - 1));
        assertEquals(OrderState.PENDING, sim.orders().get(id).state);
        assertTrue(sim.fills().isEmpty());
        // First event at/after arrival activates; fill stamped at arrival.
        sim.onEvent(f.heartbeat(arrival + 500));
        assertEquals(1, sim.fills().size());
        assertEquals(arrival, sim.fills().get(0).ts());
        // Every fill in the log is at/after its order's arrival.
        for (Fill fill : sim.fills()) {
            assertTrue(fill.ts() >= sim.orders().get(fill.orderId()).arrivalTs);
        }
    }

    @Test
    public void sameConfigSameFills() {
        java.util.function.LongFunction<List<Fill>> run = seed -> {
            TreeMap<Integer, VenueSpec> venues = new TreeMap<>();
            venues.put(VEN, new VenueSpec(VEN, "TST", false, 0.003, 0.002,
                    0.0, 150_000, 50_000));
            TreeMap<Long, InstrumentSpec> instruments = new TreeMap<>();
            instruments.put(INS, new InstrumentSpec(INS, 0.01, 1.0, 1_000_000.0));
            ExecConfig cfg = new ExecConfig(LatencyConfig.DEFAULT, seed, 2.0,
                    instruments, venues);
            ExecutionSimulator sim = new ExecutionSimulator(cfg);
            Feeder f = new Feeder();
            seedBook(sim, f);
            sim.submit(child(0, OrderType.LIMIT, 100, 50, T0 + 10));
            sim.submit(child(1, OrderType.MARKET, 0, 120, T0 + 20));
            sim.onEvent(f.heartbeat(T0 + 1_000_000));
            sim.onEvent(f.exec(T0 + 2_000_000, 0, 100, 320, 11));
            sim.onEvent(f.exec(T0 + 3_000_000, 0, 100, 100, 12));
            sim.cancelAll();
            return sim.fills();
        };
        List<Fill> a = run.apply(42);
        List<Fill> b = run.apply(42);
        assertEquals(a.size(), b.size());
        for (int i = 0; i < a.size(); i++) {
            assertEquals(a.get(i), b.get(i)); // record equality: every field
        }
        assertTrue(!a.isEmpty());
    }

    @Test
    public void snapshotContinuesIdentically() {
        // Deep-copy snapshot mid-flight (with an order pending and one
        // resting): both simulators must evolve identically afterwards.
        ExecutionSimulator sim = new ExecutionSimulator(testConfig(50_000));
        Feeder f = new Feeder();
        seedBook(sim, f);
        sim.submit(child(0, OrderType.LIMIT, 100, 50, T0 + 10));
        sim.onEvent(f.heartbeat(T0 + 10 + LAT + 60_000)); // activates (rests)
        sim.submit(child(1, OrderType.MARKET, 0, 30, T0 + 2_000_000));
        ExecutionSimulator copy = sim.snapshot();
        Feeder f2 = new Feeder();
        f2.seq = f.seq;
        MarketEvent e1 = f.exec(T0 + 3_000_000, 0, 100, 320, 11);
        MarketEvent e2 = f.exec(T0 + 4_000_000, 0, 100, 100, 12);
        sim.onEvent(e1);
        sim.onEvent(e2);
        sim.cancelAll();
        copy.onEvent(e1);
        copy.onEvent(e2);
        copy.cancelAll();
        assertEquals(sim.fills().size(), copy.fills().size());
        for (int i = 0; i < sim.fills().size(); i++) {
            assertEquals(sim.fills().get(i), copy.fills().get(i));
        }
    }

    @Test
    public void cancelAndValidation() {
        ExecutionSimulator sim = new ExecutionSimulator(testConfig(0));
        Feeder f = new Feeder();
        seedBook(sim, f);
        long id = sim.submit(child(0, OrderType.LIMIT, 99, 10, T0 + 10));
        // Rule 7: the cancel travels the latency path; applied at the first
        // event at/after its arrival (never before the order's own).
        sim.cancel(id, T0 + 10);
        assertEquals(OrderState.PENDING, sim.orders().get(id).state);
        sim.onEvent(f.heartbeat(T0 + 10 + LAT + 1));
        assertEquals(OrderState.CANCELLED, sim.orders().get(id).state);
        assertEquals(CancelReason.USER, sim.orders().get(id).cancelReason);
        sim.cancel(id, T0 + 20); // idempotent on terminal states
        sim.onEvent(f.exec(T0 + 2_000_000, 0, 99, 500, 12));
        assertTrue(sim.fills().isEmpty()); // cancelled orders never fill
        expectThrow(() -> sim.cancel(9999, T0));
        expectThrow(() -> sim.submit(child(0, OrderType.LIMIT, 0, 10, T0)));
        expectThrow(() -> sim.submit(child(0, OrderType.MARKET, 0, 0, T0)));
        ChildOrder bad = child(0, OrderType.MARKET, 0, 10, T0);
        bad.venueId = 999;
        expectThrow(() -> sim.submit(bad));
    }

    private static void expectThrow(Runnable r) {
        try {
            r.run();
            throw new AssertionError("expected IllegalArgumentException");
        } catch (IllegalArgumentException expected) {
            // pinned behavior
        }
    }

    // ------------------------------------------------ round-3 scenarios ---

    /** Scenario: halt at 09:03, re-open auction; uncross fills at the touch. */
    @Test
    public void scenarioHaltThenReopenAuction() {
        ExecutionSimulator sim = new ExecutionSimulator(testConfig(0));
        Feeder f = new Feeder();
        seedBook(sim, f);
        sim.onEvent(f.status(T0 + 100, com.iap.core.SessionStatus.HALT));
        assertFalse(ExecutionSimulator.venueOpen(sim.venueBook(INS, VEN)));
        long mkt = sim.submit(child(0, OrderType.MARKET, 0, 50, T0 + 200));
        long lim = sim.submit(child(0, OrderType.LIMIT, 103, 60, T0 + 200));
        sim.onEvent(f.heartbeat(T0 + 200 + LAT + 1));
        assertEquals(OrderState.CANCELLED, sim.orders().get(mkt).state);
        assertEquals(CancelReason.VENUE_NOT_TRADING, sim.orders().get(mkt).cancelReason);
        assertEquals(1, sim.counters().venueNotTradingCancels);
        assertEquals(OrderState.ACTIVE, sim.orders().get(lim).state);
        assertTrue("no fill through a halt", sim.fills().isEmpty());
        sim.onEvent(f.exec(T0 + 300, 1, 101, 200, 21));
        assertTrue(sim.fills().isEmpty());
        sim.onEvent(f.status(T0 + 400, com.iap.core.SessionStatus.AUCTION));
        sim.onEvent(f.add(T0 + 500, 1, 100, 500, 23));
        assertTrue(sim.fills().isEmpty());
        sim.onEvent(f.status(T0 + 600, com.iap.core.SessionStatus.TRADING));
        assertEquals(1, sim.fills().size());
        assertEquals("touch, not the 103 limit", 100, sim.fills().get(0).priceTicks());
        assertEquals(60, sim.fills().get(0).qty());
        assertEquals(T0 + 600, sim.fills().get(0).ts());
        assertEquals(1, sim.counters().reopenTouchFills);
        assertEquals(OrderState.FILLED, sim.orders().get(lim).state);
    }

    /** Scenario: multicast storm gaps the feed; no fill while stale. */
    @Test
    public void scenarioStaleBookNoFillThenSnapshotRecovery() {
        ExecutionSimulator sim = new ExecutionSimulator(testConfig(0));
        Feeder f = new Feeder();
        seedBook(sim, f);
        sim.onEvent(f.gapAdd(T0 + 100, 0, 98, 100, 13));
        assertTrue(sim.venueBook(INS, VEN).isStale());
        long a = sim.submit(child(1, OrderType.MARKET, 0, 50, T0 + 200));
        sim.onEvent(f.heartbeat(T0 + 200 + LAT + 1));
        assertEquals(OrderState.CANCELLED, sim.orders().get(a).state);
        assertEquals(CancelReason.VENUE_NOT_TRADING, sim.orders().get(a).cancelReason);
        assertTrue(sim.fills().isEmpty());
        sim.onEvent(f.snapshot(T0 + 300, 0, 100, 300, 31, 2));
        sim.onEvent(f.snapshot(T0 + 301, 1, 101, 200, 32, 1));
        sim.onEvent(f.snapshot(T0 + 302, 1, 102, 500, 33, 0));
        assertFalse(sim.venueBook(INS, VEN).isStale());
        long b = sim.submit(child(1, OrderType.MARKET, 0, 50, T0 + 400));
        sim.onEvent(f.heartbeat(T0 + 400 + LAT + 1));
        assertEquals(1, sim.fills().size());
        assertEquals(b, sim.fills().get(0).orderId());
        assertEquals(100, sim.fills().get(0).priceTicks());
    }

    /** Rule 3b: two children on one display share one copy of the liquidity. */
    @Test
    public void scenarioLiquidityNotReusedAcrossChildren() {
        ExecutionSimulator sim = new ExecutionSimulator(testConfig(0));
        Feeder f = new Feeder();
        seedBook(sim, f); // asks 101x200, 102x500 = 700 displayed
        long c1 = sim.submit(child(0, OrderType.MARKET, 0, 1000, T0 + 10));
        long c2 = sim.submit(child(0, OrderType.MARKET, 0, 1000, T0 + 10));
        sim.onEvent(f.heartbeat(T0 + 10 + LAT + 1));
        long taken = 0;
        for (Fill fl : sim.fills()) {
            taken += fl.qty();
        }
        assertEquals("total taker qty <= displayed depth", 700, taken);
        assertEquals(300, sim.orders().get(c1).remaining);
        assertEquals(1000, sim.orders().get(c2).remaining);
        assertEquals(OrderState.CANCELLED, sim.orders().get(c2).state);
        assertTrue(sim.counters().overlayThinnedFills >= 1);
        // 150 new at 101 on top of the 200 we took: exactly 150 available
        sim.onEvent(f.add(T0 + 20, 1, 101, 150, 24));
        long c3 = sim.submit(child(0, OrderType.MARKET, 0, 200, T0 + 30));
        sim.onEvent(f.heartbeat(T0 + 30 + LAT + 1));
        assertEquals(OrderState.CANCELLED, sim.orders().get(c3).state);
        assertEquals(50, sim.orders().get(c3).remaining);
        Fill last = sim.fills().get(sim.fills().size() - 1);
        assertEquals(101, last.priceTicks());
        assertEquals(150, last.qty());
        long fok = sim.submit(child(0, OrderType.FOK, 102, 100, T0 + 40));
        sim.onEvent(f.heartbeat(T0 + 40 + LAT + 1));
        assertEquals(OrderState.CANCELLED, sim.orders().get(fok).state);
        assertEquals(100, sim.orders().get(fok).remaining);
        sim.onEvent(f.exec(T0 + 50, 1, 101, 300, 21));
        long c4 = sim.submit(child(0, OrderType.IOC, 101, 10, T0 + 60));
        sim.onEvent(f.heartbeat(T0 + 60 + LAT + 1));
        assertEquals(10, sim.orders().get(c4).remaining);
        sim.onEvent(f.cancel(T0 + 70, 1, 101, 50, 21));
        sim.onEvent(f.add(T0 + 80, 1, 101, 40, 25));
        long c5 = sim.submit(child(0, OrderType.IOC, 101, 10, T0 + 90));
        sim.onEvent(f.heartbeat(T0 + 90 + LAT + 1));
        assertEquals(OrderState.FILLED, sim.orders().get(c5).state);
    }

    /** Rule 6: FX impact in base units equals the research CostModel value. */
    @Test
    public void scenarioFxImpactScalesWithLotSize() {
        TreeMap<Integer, VenueSpec> venues = new TreeMap<>();
        venues.put(VEN, new VenueSpec(VEN, "PRI", true, 0.0, 0.0, 2.5, 150_000, 0));
        TreeMap<Long, InstrumentSpec> instruments = new TreeMap<>();
        instruments.put(INS, new InstrumentSpec(INS, 1e-05, 1000.0, 4e9));
        ExecutionSimulator sim = new ExecutionSimulator(
                new ExecConfig(LatencyConfig.DEFAULT, 42, 2.0, instruments, venues));
        Feeder f = new Feeder();
        sim.onEvent(f.add(T0, 0, 108650, 5000, 11));
        sim.onEvent(f.add(T0 + 1, 1, 108660, 5000, 21));
        sim.submit(child(0, OrderType.MARKET, 0, 1000, T0 + 10));
        sim.onEvent(f.heartbeat(T0 + 10 + LAT + 1));
        assertEquals(1, sim.fills().size());
        Fill fl = sim.fills().get(0);
        double price = 108660 * 1e-05;
        double[] research = new com.iap.backtest.CostModel(2.0, 0.003, 2.5, 1.0)
                .costComponents(1000, price, 0.0, "FX", 4e9, 1000.0);
        assertEquals(research[2], fl.impactCost(), 1e-12);
        assertEquals(research[1], fl.fee(), 1e-12);
        assertEquals(0.05e-4 * 1000.0 * 1000.0 * price, fl.impactCost(), 1e-12);
    }

    /** Rule 7: a cancel cannot undo a fill made before it arrives. */
    @Test
    public void scenarioCancelHasLatency() {
        ExecutionSimulator sim = new ExecutionSimulator(testConfig(0));
        Feeder f = new Feeder();
        seedBook(sim, f);
        long id = sim.submit(child(0, OrderType.LIMIT, 100, 50, T0 + 10));
        sim.onEvent(f.heartbeat(T0 + 10 + LAT + 1));
        sim.cancel(id, T0 + 1_000_000);
        assertEquals(T0 + 1_000_000 + LAT, sim.orders().get(id).cancelArrivalTs);
        sim.onEvent(f.exec(T0 + 1_100_000, 0, 99, 400, 12)); // trade-through
        assertEquals(1, sim.fills().size());
        assertEquals(OrderState.FILLED, sim.orders().get(id).state);
        sim.onEvent(f.heartbeat(T0 + 2_000_000));
        assertEquals(OrderState.FILLED, sim.orders().get(id).state);
        assertEquals(0, sim.counters().userCancels);
        long id2 = sim.submit(child(0, OrderType.LIMIT, 100, 50, T0 + 3_000_000));
        sim.onEvent(f.heartbeat(T0 + 3_000_000 + LAT + 1));
        sim.cancel(id2, T0 + 3_100_000);
        sim.onEvent(f.heartbeat(T0 + 3_100_000 + LAT + 1));
        assertEquals(OrderState.CANCELLED, sim.orders().get(id2).state);
        sim.onEvent(f.exec(T0 + 4_000_000, 0, 99, 400, 12));
        assertEquals("cancelled order never fills", 1, sim.fills().size());
        assertEquals(1, sim.counters().userCancels);
        // a cancel never overtakes its own order
        ExecutionSimulator jit = new ExecutionSimulator(testConfig(50_000));
        Feeder g = new Feeder();
        seedBook(jit, g);
        long m = jit.submit(child(0, OrderType.MARKET, 0, 50, T0 + 10));
        jit.cancel(m, T0 + 10);
        assertTrue(jit.orders().get(m).cancelArrivalTs >= jit.orders().get(m).arrivalTs);
        jit.onEvent(g.heartbeat(T0 + 10 + LAT + 100_000));
        assertEquals(OrderState.FILLED, jit.orders().get(m).state);
    }

    /** Time-in-force expires pending and resting orders with no latency. */
    @Test
    public void scenarioExpiryBeforeActivation() {
        ExecutionSimulator sim = new ExecutionSimulator(testConfig(0));
        Feeder f = new Feeder();
        seedBook(sim, f);
        ChildOrder rest = child(0, OrderType.LIMIT, 100, 50, T0 + 10);
        rest.expireTs = T0 + 5_000_000;
        long r = sim.submit(rest);
        sim.onEvent(f.heartbeat(T0 + 10 + LAT + 1));
        assertEquals(OrderState.ACTIVE, sim.orders().get(r).state);
        ChildOrder late = child(0, OrderType.MARKET, 0, 50, T0 + 4_900_000);
        late.expireTs = T0 + 5_000_000;
        long l = sim.submit(late);
        sim.onEvent(f.exec(T0 + 5_000_000, 0, 99, 400, 12));
        assertEquals(CancelReason.EXPIRED, sim.orders().get(r).cancelReason);
        assertEquals(CancelReason.EXPIRED, sim.orders().get(l).cancelReason);
        assertTrue(sim.fills().isEmpty());
        assertEquals(2, sim.counters().expiredOrders);
    }
}
