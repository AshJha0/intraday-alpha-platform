package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import java.util.ArrayList;
import java.util.List;
import java.util.TreeMap;

import org.junit.Test;

import com.iap.core.MarketEvent;
import com.iap.execution.AlgoType;
import com.iap.execution.CancelReason;
import com.iap.execution.ChildOrder;
import com.iap.execution.ExecConfig;
import com.iap.execution.ExecPolicy;
import com.iap.execution.ExecutionReplay;
import com.iap.execution.Fill;
import com.iap.execution.InstrumentSpec;
import com.iap.execution.LatencyConfig;
import com.iap.execution.Liquidity;
import com.iap.execution.OrderState;
import com.iap.execution.OrderType;
import com.iap.execution.ParentOrder;
import com.iap.execution.PassiveParams;
import com.iap.execution.PassivePolicy;
import com.iap.execution.PassiveStats;
import com.iap.execution.VenueSpec;

/**
 * Execution policies NATIVE / AGGRESSIVE / PASSIVE: the pure posting rules
 * and the POST, REST, REPRICE / CROSS state machine on synthetic streams
 * (the rows of python/tests/test_passive_policy.py and
 * cpp/tests/test_replay_fills_passive.cpp).
 */
public class PassivePolicyTest {
    private static final long T0 = 1_700_000_000_000_000_000L;
    private static final long SEC = 1_000_000_000L;

    private static long[] lvl(long price) {
        return new long[] {price, 500};
    }

    @Test
    public void postPriceJoinsImprovesAndNeverReachesTheOppositeTouch() {
        assertEquals(100, PassivePolicy.postPrice(0, lvl(100), lvl(101), 3));
        assertEquals(101, PassivePolicy.postPrice(1, lvl(100), lvl(101), 3));
        assertEquals(100, PassivePolicy.postPrice(0, lvl(100), lvl(102), 3));
        assertEquals(101, PassivePolicy.postPrice(0, lvl(100), lvl(103), 3));
        assertEquals(102, PassivePolicy.postPrice(1, lvl(100), lvl(103), 3));
        assertEquals(101, PassivePolicy.postPrice(0, lvl(100), lvl(110), 3));
        assertEquals(100, PassivePolicy.postPrice(0, lvl(100), lvl(110), 0));
        assertEquals(101, PassivePolicy.postPrice(0, lvl(100), lvl(102), 2));
        // no same-side quote: cannot post; a missing far side is tolerated
        assertEquals(0, PassivePolicy.postPrice(0, null, lvl(101), 3));
        assertEquals(0, PassivePolicy.postPrice(1, lvl(100), null, 3));
        assertEquals(100, PassivePolicy.postPrice(0, lvl(100), null, 3));
        assertEquals(101, PassivePolicy.postPrice(1, null, lvl(101), 3));
        assertEquals(0, PassivePolicy.postPrice(0, lvl(1), lvl(1), 3));
        for (long bid = 95; bid <= 105; bid++) {
            for (long ask = 95; ask <= 105; ask++) {
                for (long improve = 0; improve <= 3; improve++) {
                    long buy = PassivePolicy.postPrice(0, lvl(bid), lvl(ask), improve);
                    long sell = PassivePolicy.postPrice(1, lvl(bid), lvl(ask), improve);
                    assertTrue(buy > 0 && buy < ask);
                    assertTrue(sell > bid);
                    if (ask > bid) {
                        assertTrue(buy >= bid && sell <= ask);
                    }
                }
            }
        }
    }

    @Test
    public void patienceFollowsUrgencyAndTheIsRiskAversion() {
        PassiveParams p = PassiveParams.DEFAULT;
        assertEquals(30 * SEC, PassivePolicy.patienceNs(p, 0.0, false, 1.0));
        assertEquals(15 * SEC, PassivePolicy.patienceNs(p, 0.5, false, 1.0));
        assertEquals(0, PassivePolicy.patienceNs(p, 1.0, false, 1.0));
        assertEquals(0, PassivePolicy.patienceNs(p, 7.0, false, 1.0));
        assertEquals(30 * SEC, PassivePolicy.patienceNs(p, -3.0, false, 1.0));
        assertEquals(8_829_106_588L, PassivePolicy.patienceNs(p, 0.2, true, 1.0));
        assertEquals(24 * SEC, PassivePolicy.patienceNs(p, 0.2, true, 0.0));
        assertEquals(40, PassivePolicy.maxBehindQty(p, 405));
    }

    @Test
    public void passiveParamsAreValidated() {
        int rejected = 0;
        long[][] bad = {{-1, 1, 3, 1}, {1, -1, 3, 1}, {1, 1, -1, 1}, {1, 1, 3, -1}};
        for (long[] b : bad) {
            try {
                new PassiveParams(b[0], (int) b[1], 0.1, b[2], b[3]);
            } catch (IllegalArgumentException expected) {
                rejected++;
            }
        }
        try {
            new PassiveParams(1, 1, 1.5, 3, 1);
        } catch (IllegalArgumentException expected) {
            rejected++;
        }
        assertEquals(5, rejected);
    }

    // ------------------------------------------------------ replay-driven

    private static ExecConfig algoConfig() {
        TreeMap<Integer, VenueSpec> venues = new TreeMap<>();
        venues.put(1, new VenueSpec(1, "TST", false, 0.003, 0.002, 0.0, 100_000, 0));
        venues.put(2, new VenueSpec(2, "TS2", false, 0.001, 0.0025, 0.0, 100_000, 0));
        TreeMap<Long, InstrumentSpec> instruments = new TreeMap<>();
        instruments.put(7L, new InstrumentSpec(7, 0.01, 1.0, 1_000_000.0));
        return new ExecConfig(LatencyConfig.DEFAULT, 7, 2.0, instruments, venues);
    }

    private static ParentOrder parent(AlgoType algo, long qty, int slices,
            ExecPolicy policy, double urgency, PassiveParams params) {
        ParentOrder p = new ParentOrder();
        p.parentId = 1;
        p.instrumentId = 7;
        p.venueId = 1;
        p.side = 0;
        p.qty = qty;
        p.algo = algo;
        p.startTs = T0;
        p.endTs = T0 + 100 * SEC;
        p.slices = slices;
        p.policy = policy;
        p.urgency = urgency;
        p.passive = params;
        return p;
    }

    private static MarketEvent ev(long seq, long ts, int type, int side, long px,
            long qty, long oid, long tid) {
        return new MarketEvent(seq, 7, 1, ts, ts, seq, type, side, px, qty, oid, tid);
    }

    /**
     * Venue-1 book bid x ask (100,000 a side) and one event per half second
     * for 200 s; {@code hitBidEvery > 0} adds a marketable ASK ADD of
     * {@code hitQty} at the bid every that many seconds, {@code bidMoveAt >=
     * 0} a better bid one tick up at that second.
     */
    private static List<MarketEvent> stream(long bid, long ask, int hitBidEvery,
            long hitQty, int bidMoveAt) {
        List<MarketEvent> evs = new ArrayList<>();
        long seq = 0;
        evs.add(ev(++seq, T0 - SEC, 1, 0, bid, 100_000, 1, 0));
        evs.add(ev(++seq, T0 - SEC + 1, 1, 1, ask, 100_000, 2, 0));
        for (int i = 0; i < 200; i++) {
            long ts = T0 + i * SEC;
            evs.add(ev(++seq, ts, 5, 0, bid, 40, 0, 1000 + i));
            if (hitBidEvery > 0 && i % hitBidEvery == hitBidEvery - 1) {
                evs.add(ev(++seq, ts + SEC / 4, 1, 1, bid, hitQty, 5000 + i, 0));
            }
            if (i == bidMoveAt) {
                evs.add(ev(++seq, ts + SEC / 4, 1, 0, bid + 1, 500, 9000, 0));
            }
            evs.add(ev(++seq, ts + SEC / 2, 9, 0, 0, 0, 0, 0));
        }
        return evs;
    }

    private static List<ChildOrder> children(ExecutionReplay replay) {
        return new ArrayList<>(replay.simulator().orders().values());
    }

    private static void assertStats(PassiveStats s, long posts, long reprices,
            long extensions, long timeout, long behind, long immediate) {
        assertEquals("posts", posts, s.posts);
        assertEquals("reprices", reprices, s.reprices);
        assertEquals("rest_extensions", extensions, s.restExtensions);
        assertEquals("crosses_timeout", timeout, s.crossesTimeout);
        assertEquals("crosses_behind", behind, s.crossesBehind);
        assertEquals("crosses_immediate", immediate, s.crossesImmediate);
    }

    @Test
    public void nativeIsTheDefaultAndAggressiveSendsMarketChildren() {
        assertEquals(ExecPolicy.NATIVE, new ParentOrder().policy);
        ExecutionReplay nat = new ExecutionReplay(algoConfig(), List.of(
                parent(AlgoType.TWAP, 400, 4, ExecPolicy.NATIVE, 0.9,
                        PassiveParams.DEFAULT)));
        ExecutionReplay.Result nres = nat.run(stream(100, 101, 3, 50, -1));
        assertTrue(nres.passive.isEmpty());
        for (ChildOrder o : children(nat)) {
            assertEquals(OrderType.LIMIT, o.type); // joins the bid and waits
        }
        ExecutionReplay agg = new ExecutionReplay(algoConfig(), List.of(
                parent(AlgoType.TWAP, 400, 4, ExecPolicy.AGGRESSIVE, 0.5,
                        PassiveParams.DEFAULT)));
        ExecutionReplay.Result ares = agg.run(stream(100, 101, 0, 0, -1));
        assertEquals(400, ares.parents.get(1L).filledQty);
        for (ChildOrder o : children(agg)) {
            assertEquals(OrderType.MARKET, o.type);
        }
        for (Fill f : ares.fills) {
            assertEquals(Liquidity.TAKER, f.liquidity());
            assertEquals(101, f.priceTicks());
            assertEquals(0.003 * f.qty(), f.fee(), 0.0); // taker fee: a cost
            assertTrue(f.impactCost() > 0.0);
        }
    }

    @Test
    public void restExtensionThenCrossWhenTheQueueNeverClears() {
        PassiveParams params = new PassiveParams(20 * SEC, 1, 0.1, 3, SEC);
        ExecutionReplay replay = new ExecutionReplay(algoConfig(), List.of(
                parent(AlgoType.TWAP, 400, 4, ExecPolicy.PASSIVE, 0.5, params)));
        ExecutionReplay.Result res = replay.run(stream(100, 101, 0, 0, -1));
        int posted = 0;
        int crossed = 0;
        for (ChildOrder o : children(replay)) {
            if (o.type == OrderType.LIMIT) {
                posted++;
                assertEquals(100, o.limitTicks); // the near touch, not through 101
                assertEquals(100_000, o.entryAheadQty);
                assertEquals(OrderState.CANCELLED, o.state);
                assertEquals(CancelReason.USER, o.cancelReason);
                assertEquals(100, o.remaining);
            } else {
                long due = T0 + crossed * 25 * SEC;
                assertTrue(o.decisionTs >= due + 20 * SEC);
                assertTrue(o.decisionTs <= due + 21 * SEC);
                crossed++;
            }
        }
        assertEquals(4, posted);
        assertEquals(4, crossed);
        assertStats(res.passive.get(1L), 4, 0, 4, 4, 0, 0);
        assertEquals(400, res.parents.get(1L).filledQty);
        for (Fill f : res.fills) {
            assertEquals(Liquidity.TAKER, f.liquidity());
            assertEquals(101, f.priceTicks());
        }
    }

    @Test
    public void postsInsideAWideSpreadAndEarnsTheRebate() {
        ExecutionReplay replay = new ExecutionReplay(algoConfig(), List.of(
                parent(AlgoType.TWAP, 400, 4, ExecPolicy.PASSIVE, 0.0,
                        PassiveParams.DEFAULT)));
        ExecutionReplay.Result res = replay.run(stream(100, 104, 2, 60, -1));
        List<ChildOrder> kids = children(replay);
        assertEquals(4, kids.size());
        for (ChildOrder o : kids) {
            assertEquals(OrderType.LIMIT, o.type);
            assertEquals(101, o.limitTicks);
            assertEquals(0, o.entryAheadQty);
        }
        ExecutionReplay.ParentReport rep = res.parents.get(1L);
        assertEquals(400, rep.filledQty);
        for (Fill f : res.fills) {
            assertEquals(Liquidity.MAKER, f.liquidity());
            assertEquals(101, f.priceTicks()); // our limit, never better
            assertTrue(f.qty() <= 60);         // bounded by the traded volume
            assertEquals(-0.002 * f.qty(), f.fee(), 0.0); // rebate: negative
            assertEquals(0.0, f.impactCost(), 0.0);
        }
        assertEquals(0.002 * 400, rep.rebates, 1e-12);
        assertEquals(0.0, rep.fees, 0.0);
        assertEquals(0, res.passive.get(1L).crossesTimeout);
        assertEquals(0, res.passive.get(1L).crossesBehind);
    }

    @Test
    public void repricesWhenTheTouchMovesAway() {
        PassiveParams params = new PassiveParams(20 * SEC, 1, 0.1, 3, SEC);
        ExecutionReplay replay = new ExecutionReplay(algoConfig(), List.of(
                parent(AlgoType.TWAP, 100, 1, ExecPolicy.PASSIVE, 0.5, params)));
        ExecutionReplay.Result res = replay.run(stream(100, 102, 0, 0, 5));
        List<ChildOrder> kids = children(replay);
        assertEquals(3, kids.size());
        assertEquals(OrderType.LIMIT, kids.get(0).type);
        assertEquals(100, kids.get(0).limitTicks);
        assertEquals(CancelReason.USER, kids.get(0).cancelReason);
        assertEquals(OrderType.LIMIT, kids.get(1).type);
        assertEquals(101, kids.get(1).limitTicks);
        assertEquals(100, kids.get(1).qty); // the cancelled remainder, not more
        assertTrue(kids.get(1).decisionTs > kids.get(0).decisionTs + 10 * SEC);
        assertEquals(OrderType.MARKET, kids.get(2).type);
        assertStats(res.passive.get(1L), 2, 1, 0, 1, 0, 0);
        assertEquals(100, res.parents.get(1L).filledQty);
    }

    @Test
    public void crossesAsSoonAsTheScheduleIsBehind() {
        PassiveParams params = new PassiveParams(60 * SEC, 1, 0.1, 3, SEC);
        ExecutionReplay replay = new ExecutionReplay(algoConfig(), List.of(
                parent(AlgoType.TWAP, 400, 4, ExecPolicy.PASSIVE, 0.0, params)));
        ExecutionReplay.Result res = replay.run(stream(100, 101, 0, 0, -1));
        ChildOrder firstCross = null;
        for (ChildOrder o : children(replay)) {
            if (o.type == OrderType.MARKET) {
                firstCross = o;
                break;
            }
        }
        assertTrue(firstCross != null);
        assertTrue(firstCross.decisionTs >= T0 + 25 * SEC);
        assertTrue(firstCross.decisionTs < T0 + 26 * SEC);
        assertTrue(res.passive.get(1L).crossesBehind >= 3);
        assertEquals(0, res.passive.get(1L).restExtensions);
        // a tolerance of the whole order never declares the schedule behind
        PassiveParams lax = new PassiveParams(60 * SEC, 1, 1.0, 3, SEC);
        ExecutionReplay.Result res2 = new ExecutionReplay(algoConfig(), List.of(
                parent(AlgoType.TWAP, 400, 4, ExecPolicy.PASSIVE, 0.0, lax)))
                        .run(stream(100, 101, 0, 0, -1));
        assertEquals(0, res2.passive.get(1L).crossesBehind);
    }

    @Test
    public void urgencyOneCrossesImmediatelyAndQuantityIsConserved() {
        ExecutionReplay replay = new ExecutionReplay(algoConfig(), List.of(
                parent(AlgoType.TWAP, 400, 4, ExecPolicy.PASSIVE, 1.0,
                        PassiveParams.DEFAULT)));
        ExecutionReplay.Result res = replay.run(stream(100, 101, 0, 0, -1));
        for (ChildOrder o : children(replay)) {
            assertEquals(OrderType.MARKET, o.type);
        }
        assertStats(res.passive.get(1L), 0, 0, 0, 0, 0, 4);

        double[] urgencies = {0.0, 0.3, 0.7, 1.0};
        double[] fractions = {0.0, 0.1, 1.0};
        AlgoType[] algos = {AlgoType.TWAP, AlgoType.VWAP, AlgoType.IS, AlgoType.POV};
        for (double u : urgencies) {
            for (double fr : fractions) {
                PassiveParams params = new PassiveParams(40 * SEC, 1, fr, 3, SEC);
                List<ParentOrder> parents = new ArrayList<>();
                for (int i = 0; i < algos.length; i++) {
                    ParentOrder p = parent(algos[i], 300, 3, ExecPolicy.PASSIVE, u, params);
                    p.parentId = i + 1;
                    p.side = i % 2;
                    p.participation = 0.2;
                    parents.add(p);
                }
                ExecutionReplay rp = new ExecutionReplay(algoConfig(), parents);
                ExecutionReplay.Result r = rp.run(stream(100, 103, 3, 50, 7));
                for (ParentOrder p : parents) {
                    ExecutionReplay.ParentReport rep = r.parents.get(p.parentId);
                    assertTrue(rep.filledQty >= 0 && rep.filledQty <= p.qty);
                    assertEquals(p.qty, rep.filledQty + rep.unfilledQty);
                    assertEquals(rep.fees - rep.rebates + rep.impact, rep.totalCost, 1e-12);
                }
                for (Fill f : r.fills) {
                    assertEquals(f.liquidity() == Liquidity.MAKER, f.fee() < 0.0);
                    assertTrue(f.ts() >= T0 && f.ts() <= T0 + 100 * SEC);
                }
                for (ChildOrder o : children(rp)) {
                    assertTrue(o.state == OrderState.FILLED
                            || o.state == OrderState.CANCELLED);
                }
            }
        }
    }
}
