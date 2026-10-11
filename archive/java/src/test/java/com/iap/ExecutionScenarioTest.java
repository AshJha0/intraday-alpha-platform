package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import java.util.ArrayList;
import java.util.List;
import java.util.TreeMap;

import org.junit.Test;

import com.iap.backtest.BacktestEngine;
import com.iap.core.MarketEvent;
import com.iap.core.SessionStatus;
import com.iap.execution.AlgoType;
import com.iap.execution.CancelReason;
import com.iap.execution.ChildOrder;
import com.iap.execution.ExecConfig;
import com.iap.execution.ExecutionReplay;
import com.iap.execution.Fill;
import com.iap.execution.InstrumentSpec;
import com.iap.execution.LatencyConfig;
import com.iap.execution.Liquidity;
import com.iap.execution.OrderState;
import com.iap.execution.ParentOrder;
import com.iap.execution.VenueSpec;
import com.iap.features.Features;
import com.iap.orderbook.ConsolidatedBook;
import com.iap.sor.SmartOrderRouter;
import com.iap.sor.SorOptions;

/**
 * Round-3 execution scenarios mirroring the C++ reference tests: SOR
 * eligibility / no-route, algo slice splitting, children expiring at
 * end_ts, POV re-sending after a cancelled remainder, the backtest
 * engine's execution controls (participation, slice interval, latency
 * budget) and multi-currency P&amp;L conversion.
 */
public class ExecutionScenarioTest {
    private static final long T0 = 1_700_000_000_000_000_000L;
    private static final long SEC = 1_000_000_000L;

    private static ParentOrder parent(AlgoType algo, long qty, int slices) {
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
        return p;
    }

    private static ExecConfig algoConfig() {
        TreeMap<Integer, VenueSpec> venues = new TreeMap<>();
        venues.put(1, new VenueSpec(1, "TST", false, 0.003, 0.002, 0.0, 100_000, 0));
        venues.put(2, new VenueSpec(2, "TS2", false, 0.001, 0.0025, 0.0, 100_000, 0));
        TreeMap<Long, InstrumentSpec> instruments = new TreeMap<>();
        instruments.put(7L, new InstrumentSpec(7, 0.01, 1.0, 1_000_000.0));
        return new ExecConfig(LatencyConfig.DEFAULT, 7, 2.0, instruments, venues);
    }

    private static MarketEvent ev(long seq, int venue, long ts, int type, int side,
            long px, long qty, long oid, long tid) {
        return new MarketEvent(seq, 7, venue, ts, ts, seq, type, side, px, qty,
                oid, tid);
    }

    /** Two-sided venue-1 book plus periodic TRADEs of 40 every second. */
    private static List<MarketEvent> algoStream() {
        List<MarketEvent> evs = new ArrayList<>();
        long seq = 0;
        evs.add(ev(++seq, 1, T0 - SEC, 1, 0, 100, 100000, 1, 0));
        evs.add(ev(++seq, 1, T0 - SEC + 1, 1, 1, 101, 100000, 2, 0));
        for (int i = 0; i < 200; i++) {
            long ts = T0 + i * SEC;
            evs.add(ev(++seq, 1, ts, 5, i % 2 == 0 ? 0 : 1, 100, 40, 0, 1000 + i));
            evs.add(ev(++seq, 1, ts + SEC / 2, 9, 0, 0, 0, 0, 0));
        }
        return evs;
    }

    /** Scenario: both venues gap; the SOR never routes to a stale/halted venue. */
    @Test
    public void sorNeverRoutesToStaleOrHaltedVenues() {
        ExecConfig cfg = algoConfig();
        SmartOrderRouter sor = new SmartOrderRouter(cfg.venues);
        ConsolidatedBook book = new ConsolidatedBook(7);
        long[] seq = {0, 0};
        java.util.function.BiConsumer<int[], Boolean> push = (a, gap) -> {
            int vid = a[0];
            if (gap) {
                seq[vid - 1]++;
            }
            long s = ++seq[vid - 1];
            book.apply(ev(s, vid, T0 + s, a[1], a[2], a[3], a[4], a[5], 0));
        };
        push.accept(new int[] {1, 1, 1, 100, 500, 11}, false);
        push.accept(new int[] {2, 1, 1, 101, 500, 21}, false);
        assertEquals(1, sor.routeAggressive(book, 0, List.of(1, 2)));
        push.accept(new int[] {1, 1, 1, 100, 100, 12}, true); // venue 1 gaps
        assertTrue(book.venues().get(1).isStale());
        assertEquals("skip stale", 2, sor.routeAggressive(book, 0, List.of(1, 2)));
        push.accept(new int[] {2, 8, 0, 0, SessionStatus.HALT, 0}, false);
        assertEquals("all gated", SmartOrderRouter.NO_ROUTE,
                sor.routeAggressive(book, 0, List.of(1, 2)));
        assertEquals(SmartOrderRouter.NO_ROUTE,
                sor.routePassive(book, 0, List.of(1, 2)));
        push.accept(new int[] {2, 8, 0, 0, SessionStatus.TRADING, 0}, false);
        assertEquals(2, sor.routeAggressive(book, 0, List.of(1, 2)));
        // latency budget makes both venues (100 us) ineligible
        SmartOrderRouter strict = new SmartOrderRouter(cfg.venues,
                new SorOptions(true, 50_000));
        assertEquals(SmartOrderRouter.NO_ROUTE,
                strict.routeAggressive(book, 0, List.of(1, 2)));
        // FX tie-break: equal price and per-share fee -> lower commission
        TreeMap<Integer, VenueSpec> fx = new TreeMap<>();
        fx.put(1, new VenueSpec(1, "LP1", true, 0.0, 0.0, 4.0, 100_000, 0));
        fx.put(2, new VenueSpec(2, "PRI", true, 0.0, 0.0, 2.5, 100_000, 0));
        SmartOrderRouter fxSor = new SmartOrderRouter(fx);
        ConsolidatedBook fxBook = new ConsolidatedBook(7);
        fxBook.apply(ev(1, 1, T0, 1, 1, 100, 500, 11, 0));
        fxBook.apply(ev(1, 2, T0 + 1, 1, 1, 100, 500, 21, 0));
        assertEquals("2.5/M beats 4.0/M", 2, fxSor.routeAggressive(fxBook, 0, List.of(1, 2)));
        try {
            sor.routeAggressive(book, 0, List.of());
            throw new AssertionError("empty candidates must throw");
        } catch (IllegalArgumentException expected) {
            // pinned
        }
    }

    /** No eligible venue: the replay submits nothing and counts it. */
    @Test
    public void noRouteChildrenAreSkippedAndCounted() {
        ParentOrder p = parent(AlgoType.IS, 300, 3);
        p.venueId = 0;
        List<MarketEvent> evs = new ArrayList<>();
        long seq = 0;
        evs.add(ev(++seq, 1, T0 - SEC, 1, 0, 100, 100000, 1, 0));
        evs.add(ev(++seq, 1, T0 - SEC + 1, 1, 1, 101, 100000, 2, 0));
        seq++; // gap: venue 1 stale from here
        evs.add(ev(++seq, 1, T0 - SEC + 2, 1, 1, 102, 100, 3, 0));
        for (int i = 0; i < 200; i++) {
            evs.add(ev(++seq, 1, T0 + i * SEC, 9, 0, 0, 0, 0, 0));
        }
        ExecutionReplay.Result res = new ExecutionReplay(algoConfig(), List.of(p)).run(evs);
        assertEquals(3, res.sorNoRoute);
        assertEquals(0, res.parents.get(1L).children);
        assertEquals(300, res.parents.get(1L).unfilledQty);
        assertTrue(res.fills.isEmpty());
    }

    /** Pinned: slices above max_child_qty are split, never dropped. */
    @Test
    public void algoSliceLargerThanMaxChildIsSplit() {
        ParentOrder p = parent(AlgoType.IS, 20000, 8);
        p.maxChildQty = 1000;
        p.riskAversion = 0.0;
        ExecutionReplay replay = new ExecutionReplay(algoConfig(), List.of(p));
        ExecutionReplay.Result res = replay.run(algoStream());
        assertEquals(24, res.parents.get(1L).children);
        assertEquals(20000, res.parents.get(1L).filledQty);
        assertEquals(0, res.parents.get(1L).unfilledQty);
        for (ChildOrder o : replay.simulator().orders().values()) {
            assertTrue(o.qty <= 1000);
            assertEquals(p.endTs, o.expireTs);
        }
    }

    /** Pinned: no child outlives end_ts; a late crossing print does not fill. */
    @Test
    public void algoChildrenCancelledAtEndTs() {
        ParentOrder p = parent(AlgoType.VWAP, 400, 4);
        List<MarketEvent> evs = algoStream();
        long seq = evs.size() + 1;
        evs.add(ev(seq, 1, T0 + 150 * SEC, 1, 1, 99, 100000, 77, 0));
        ExecutionReplay replay = new ExecutionReplay(algoConfig(), List.of(p));
        ExecutionReplay.Result res = replay.run(evs);
        assertEquals(4, res.parents.get(1L).children);
        assertEquals(0, res.parents.get(1L).filledQty);
        assertEquals(400, res.parents.get(1L).unfilledQty);
        for (ChildOrder o : replay.simulator().orders().values()) {
            assertEquals(OrderState.CANCELLED, o.state);
            assertEquals(CancelReason.EXPIRED, o.cancelReason);
        }
        assertEquals(4, replay.simulator().counters().expiredOrders);
    }

    /** Pinned: POV deficit uses filled + in-flight qty; remainders are re-sent. */
    @Test
    public void algoPovResendsAfterCancelledRemainder() {
        ParentOrder p = parent(AlgoType.POV, 500, 1);
        p.participation = 0.10;
        p.maxChildQty = 25;
        List<MarketEvent> evs = new ArrayList<>();
        long seq = 0;
        evs.add(ev(++seq, 1, T0 - SEC, 1, 0, 100, 100000, 1, 0));
        evs.add(ev(++seq, 1, T0 - SEC + 1, 1, 1, 101, 30, 2, 0)); // thin ask
        for (int i = 0; i < 200; i++) {
            long ts = T0 + i * SEC;
            if (i == 30) {
                evs.add(ev(++seq, 1, ts - 1, 1, 1, 101, 100000, 3, 0));
            }
            evs.add(ev(++seq, 1, ts, 5, 0, 100, 40, 0, 1000 + i));
            evs.add(ev(++seq, 1, ts + SEC / 2, 9, 0, 0, 0, 0, 0));
        }
        ExecutionReplay replay = new ExecutionReplay(algoConfig(), List.of(p));
        ExecutionReplay.Result res = replay.run(evs);
        assertEquals(400, res.parents.get(1L).filledQty);
        assertTrue(res.parents.get(1L).children > 16);
        boolean sawPartial = false;
        for (ChildOrder o : replay.simulator().orders().values()) {
            if (o.state == OrderState.CANCELLED && o.remaining > 0 && o.remaining < o.qty) {
                sawPartial = true;
            }
        }
        assertTrue(sawPartial);
        for (Fill f : res.fills) {
            assertEquals(Liquidity.TAKER, f.liquidity());
            assertTrue(f.ts() <= p.endTs);
        }
    }

    // -- backtest engine execution controls / currency ---------------------

    private static ExecConfig fxConfig() {
        TreeMap<Integer, VenueSpec> venues = new TreeMap<>();
        venues.put(12, new VenueSpec(12, "PRI", true, 0.0, 0.0, 2.5, 100_000, 0));
        TreeMap<Long, InstrumentSpec> instruments = new TreeMap<>();
        instruments.put(101L, new InstrumentSpec(101, 1e-05, 1000.0, 4e9, "USD"));
        instruments.put(103L, new InstrumentSpec(103, 0.001, 1000.0, 4e9, "JPY"));
        return new ExecConfig(LatencyConfig.DEFAULT, 7, 2.0, instruments, venues);
    }

    private static MarketEvent quote(long seq, long iid, long ts, int side, long px,
            long qty) {
        return new MarketEvent(seq, iid, 12, ts, ts, seq, 6, side, px, qty, 0, 0);
    }

    /**
     * Scenario: a USD gain on EUR/USD and a JPY loss on USD/JPY. Summed
     * natively the JPY loss dominates; converted at the prevailing USD/JPY
     * mid (150) the book is flat: +1,000 USD and -150,000 JPY.
     */
    @Test
    public void backtestMultiCurrencyPnlConverted() {
        // Strategy: long 100 lots of both pairs from the first vector on.
        BacktestEngine.Strategy longBoth = vec ->
                vec.valid[Features.MID_PRICE] ? 100 : 0;
        BacktestEngine.FxConverter fx = (ccy, ts) -> switch (ccy) {
            case "USD" -> 1.0;
            case "JPY" -> 1.0 / 150.0;
            default -> throw new IllegalStateException("no rate for " + ccy);
        };
        BacktestEngine eng = new BacktestEngine(fxConfig(), longBoth,
                BacktestEngine.PASSTHROUGH_RISK, BacktestEngine.NO_LISTENER, fx,
                1000, 12, BacktestEngine.ExecutionLimits.NONE, SorOptions.DEFAULT);
        List<MarketEvent> evs = new ArrayList<>();
        long[] seq = {0, 0}; // per (venue, instrument) sequence streams
        long t = T0;
        // EUR/USD 1.00000/1.00002 ; USD/JPY 150.000/150.002 (mids 1.00001 / 150.001)
        evs.add(quote(++seq[0], 101, t, 0, 100000, 1_000_000));
        evs.add(quote(++seq[0], 101, t, 1, 100002, 1_000_000));
        evs.add(quote(++seq[1], 103, t, 0, 150000, 1_000_000));
        evs.add(quote(++seq[1], 103, t, 1, 150002, 1_000_000));
        // let the MARKET buys arrive and fill at the asks
        t += SEC;
        evs.add(quote(++seq[0], 101, t, 1, 100002, 1_000_000));
        evs.add(quote(++seq[1], 103, t, 1, 150002, 1_000_000));
        // marks: EUR/USD +0.00010 (100 lots x 1000 x 1e-4 = +10 USD); USD/JPY
        // -1.500 JPY (100 x 1000 x -1.5 = -150,000 JPY = -1,000 USD at 150)
        t += SEC;
        evs.add(quote(++seq[0], 101, t, 0, 100010, 1_000_000));
        evs.add(quote(++seq[0], 101, t, 1, 100012, 1_000_000));
        evs.add(quote(++seq[1], 103, t, 0, 148500, 1_000_000));
        evs.add(quote(++seq[1], 103, t, 1, 148502, 1_000_000));
        BacktestEngine.Summary s = eng.run(evs);
        BacktestEngine.Account eur = s.accounts.get(101L);
        BacktestEngine.Account jpy = s.accounts.get(103L);
        assertTrue(eur.fillCount > 0 && jpy.fillCount > 0);
        // native equities: USD account small positive-ish gross, JPY account
        // a -150,000 JPY mark loss (both net of spread/commission)
        assertEquals(-150_000.0, jpy.grossPnl, 1e-6);
        double nativeSum = eur.equity(1000.0) + jpy.equity(1000.0);
        assertTrue("native sum is JPY-dominated", nativeSum < -100_000.0);
        // converted: JPY loss = -1,000 USD; EUR/USD gain +10 USD minus costs
        assertEquals(jpy.pnlReporting, jpy.equity(1000.0) / 150.0, 1e-6);
        assertTrue(Math.abs(s.totalPnl + 1000.0 - 10.0) < 5.0);
        assertEquals(s.totalPnlNative, nativeSum, 1e-9);
        // a missing conversion fails closed
        BacktestEngine usdOnly = new BacktestEngine(fxConfig(), longBoth,
                BacktestEngine.PASSTHROUGH_RISK, 1000, 12);
        try {
            usdOnly.run(evs);
            throw new AssertionError("JPY without a rate must throw");
        } catch (IllegalStateException expected) {
            assertTrue(expected.getMessage().contains("JPY"));
        }
    }

    /** The declared execution controls are enforced and counted. */
    @Test
    public void backtestEnforcesParticipationSliceIntervalAndLatencyBudget() {
        ExecConfig cfg = algoConfig();
        BacktestEngine.Strategy wantBig = vec ->
                vec.valid[Features.MID_PRICE] ? 1000 : 0;
        List<MarketEvent> evs = new ArrayList<>();
        long seq = 0;
        evs.add(ev(++seq, 1, T0, 1, 0, 100, 500, 1, 0));
        evs.add(ev(++seq, 1, T0 + 1, 1, 1, 101, 200, 2, 0)); // thin touch: 200
        for (int i = 1; i <= 10; i++) {
            evs.add(ev(++seq, 1, T0 + i * 100_000_000L, 4, 0, 100, 10, 1, 0)); // EXECUTEs
        }
        // participation 5% of displayed contra depth (200) = 10 shares; the
        // cumulative cap follows session volume (10 per EXECUTE)
        BacktestEngine eng = new BacktestEngine(cfg, wantBig,
                BacktestEngine.PASSTHROUGH_RISK, BacktestEngine.NO_LISTENER,
                BacktestEngine.USD_ONLY, 1000, 1,
                new BacktestEngine.ExecutionLimits(0.05, 0, Long.MAX_VALUE),
                SorOptions.DEFAULT);
        BacktestEngine.Summary s = eng.run(evs);
        assertTrue("blocked while no volume", s.counters.participationBlocked > 0);
        for (ChildOrder o : eng.simulator().orders().values()) {
            assertTrue("child " + o.qty + " within 5% of 200", o.qty <= 10);
        }
        assertTrue(s.counters.participationCapped > 0);
        // slice interval: 500 ms between children
        BacktestEngine paced = new BacktestEngine(cfg, wantBig,
                BacktestEngine.PASSTHROUGH_RISK, BacktestEngine.NO_LISTENER,
                BacktestEngine.USD_ONLY, 5, 1,
                new BacktestEngine.ExecutionLimits(1.0, 500_000_000L, Long.MAX_VALUE),
                SorOptions.DEFAULT);
        BacktestEngine.Summary ps = paced.run(evs);
        assertTrue(ps.counters.sliceIntervalBlocked > 0);
        long prev = Long.MIN_VALUE;
        for (ChildOrder o : paced.simulator().orders().values()) {
            assertTrue(prev == Long.MIN_VALUE || o.decisionTs - prev >= 500_000_000L);
            prev = o.decisionTs;
        }
        // latency budget below the venue path (300 us): nothing is sent
        BacktestEngine slow = new BacktestEngine(cfg, wantBig,
                BacktestEngine.PASSTHROUGH_RISK, BacktestEngine.NO_LISTENER,
                BacktestEngine.USD_ONLY, 5, 1,
                new BacktestEngine.ExecutionLimits(1.0, 0, 250_000),
                SorOptions.DEFAULT);
        BacktestEngine.Summary ss = slow.run(evs);
        assertTrue(ss.counters.latencyBudgetBlocked > 0);
        assertEquals(0, ss.fillCount);
        // a SOR-routed engine on a stale venue sends nothing and counts it
        List<MarketEvent> stale = new ArrayList<>(evs);
        stale.add(1, ev(100, 1, T0, 1, 0, 99, 500, 3, 0)); // seq jump -> stale
        BacktestEngine routed = new BacktestEngine(cfg, vec -> 1000,
                BacktestEngine.PASSTHROUGH_RISK, 5, 0);
        BacktestEngine.Summary rs = routed.run(stale);
        assertTrue(rs.counters.sorNoRoute > 0);
        assertEquals(0, rs.fillCount);
    }
}
