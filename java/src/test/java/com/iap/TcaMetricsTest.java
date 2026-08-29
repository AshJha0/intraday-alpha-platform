package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import java.util.List;
import java.util.Map;

import org.junit.Test;

import com.iap.execution.Fill;
import com.iap.tca.MarketTimeline;
import com.iap.tca.Tca;
import com.iap.tca.TcaFill;
import com.iap.tca.TcaParentOrder;
import com.iap.tca.TcaService;

/**
 * TCA benchmarks and attribution beyond the golden decomposition:
 * VWAP/TWAP interval benchmarks, spread/impact/timing identity, adverse
 * selection markouts, the impact regression, and the TcaService bridge
 * from execution-simulator fills over the golden EQ event vector.
 */
public class TcaMetricsTest {
    private static final long S = 1_000_000_000L;

    /** Simple two-state timeline: mid 100.00 then 100.10 at t=10s. */
    private static MarketTimeline timeline() {
        MarketTimeline t = new MarketTimeline();
        t.append(0, 99.99, 100.01, 500, 400);
        t.append(10 * S, 100.09, 100.11, 300, 200);
        return t;
    }

    @Test
    public void prevailingLookupIsNoLookahead() {
        MarketTimeline t = timeline();
        assertEquals(-1, t.prevailing(-1));
        assertEquals(0, t.prevailing(0));
        assertEquals(0, t.prevailing(10 * S - 1));
        assertEquals(1, t.prevailing(10 * S));
        assertEquals(1, t.prevailing(999 * S));
        assertTrue(Double.isNaN(t.midAt(-5)));
        assertEquals(100.0, t.midAt(3 * S), 1e-12);
        assertEquals(100.1, t.midAt(11 * S), 1e-12);
    }

    @Test
    public void intervalTwapTimeWeightsThePrevailingMid() {
        MarketTimeline t = timeline();
        // [5s, 15s): 5s at 100.00 + 5s at 100.10 = 100.05
        assertEquals(100.05, t.intervalTwap(5 * S, 15 * S), 1e-12);
        // fully inside the first state
        assertEquals(100.0, t.intervalTwap(1 * S, 2 * S), 1e-12);
        // before the first state: undefined
        assertNull(t.intervalTwap(-10 * S, -5 * S));
    }

    @Test
    public void intervalVwapIsTradeWeightedInclusive() {
        MarketTimeline t = timeline();
        t.addTrade(1 * S, 100.00, 100);
        t.addTrade(10 * S, 100.20, 300);
        t.addTrade(20 * S, 105.00, 500); // outside window
        double want = (100.00 * 100 + 100.20 * 300) / 400.0;
        assertEquals(want, t.intervalVwap(0, 10 * S), 1e-12);
        assertNull("no trades in window", t.intervalVwap(2 * S, 9 * S));
    }

    @Test
    public void spreadImpactTimingDecomposeTradingCostExactly() {
        MarketTimeline t = timeline();
        TcaParentOrder o = new TcaParentOrder(1, 1, 0, 1000, 0, 1 * S, 15 * S);
        // buy at the ask (spread only) then 2 ticks through (impact)
        o.fills.add(new TcaFill(2 * S, 100.01, 400, 100.00, 0.01, 400));
        o.fills.add(new TcaFill(11 * S, 100.13, 600, 100.10, 0.01, 200));
        Tca.SpreadImpact split = Tca.spreadAndImpactCost(o);
        assertEquals(400 * 0.01 + 600 * 0.01, split.spreadCost(), 1e-9);
        assertEquals(600 * 0.02, split.impactCost(), 1e-9);
        Tca.OrderTca rec = Tca.orderTca(o, t);
        // trading = spread + impact + timing (exact identity)
        assertEquals(rec.perold().tradingCost(),
                rec.spreadCost() + rec.impactCost() + rec.timingCost(), 1e-9);
        // timing here = mid drift after arrival captured by the late fill
        assertTrue(rec.timingCost() > 0.0);
        assertEquals("BUY", rec.side());
        assertEquals(2, rec.nFills());
    }

    @Test
    public void adverseSelectionMarkoutsUsePinnedDeltas() {
        MarketTimeline t = timeline();
        TcaParentOrder o = new TcaParentOrder(1, 1, 0, 400, 0, 0, 15 * S);
        // fill at 9.5s: mid at +100ms/1s still 100.00, at +10s = 100.10
        o.fills.add(new TcaFill((long) (9.5 * S), 100.01, 400, 100.00, 0.01, 400));
        Map<String, Double> as = Tca.adverseSelection(o, t);
        assertEquals(List.of("100ms", "1s", "10s"),
                List.copyOf(as.keySet()));
        assertEquals(1e4 * (100.00 - 100.01) / 100.01, as.get("100ms"), 1e-9);
        assertEquals(1e4 * (100.10 - 100.01) / 100.01, as.get("10s"), 1e-9);
        // a fill before the first state has no markout anywhere -> null
        TcaParentOrder none = new TcaParentOrder(2, 1, 1, 100, 0, 0, S);
        MarketTimeline empty = new MarketTimeline();
        none.fills.add(new TcaFill(0, 100.0, 100, 100.0, 0.0, 1));
        assertNull(Tca.adverseSelection(none, empty).get("1s"));
    }

    @Test
    public void impactRegressionRecoversPlantedSlope() {
        // y = 3 + 40x exactly -> slope 40, intercept 3, r2 1
        double[] x = {0.01, 0.02, 0.05, 0.10};
        double[] y = new double[x.length];
        for (int i = 0; i < x.length; i++) {
            y[i] = 3.0 + 40.0 * x[i];
        }
        Tca.ImpactRegression r = Tca.impactRegression(x, y);
        assertEquals(40.0, r.slopeBpsPerParticipation(), 1e-9);
        assertEquals(3.0, r.interceptBps(), 1e-9);
        assertEquals(1.0, r.r2(), 1e-12);
        assertEquals(4, r.n());
        // zero x-variance -> slope 0, intercept = mean(y)
        Tca.ImpactRegression flat = Tca.impactRegression(
                new double[] {0.02, 0.02, 0.02}, new double[] {1.0, 2.0, 3.0});
        assertEquals(0.0, flat.slopeBpsPerParticipation(), 0.0);
        assertEquals(2.0, flat.interceptBps(), 1e-12);
        try {
            Tca.impactRegression(new double[] {1, 2}, new double[] {1, 2});
            org.junit.Assert.fail("n < 3");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("3 fills"));
        }
    }

    @Test
    public void executionAlphaSignConvention() {
        MarketTimeline t = timeline();
        t.addTrade(2 * S, 100.05, 1000);
        TcaParentOrder o = new TcaParentOrder(1, 1, 0, 100, 0, 1 * S, 15 * S);
        o.fills.add(new TcaFill(2 * S, 100.01, 100, 100.00, 0.01, 400));
        Tca.OrderTca rec = Tca.orderTca(o, t);
        // bought below market VWAP -> negative slippage, positive exec alpha
        assertNotNull(rec.vwapSlippageBps());
        assertTrue(rec.vwapSlippageBps() < 0.0);
        assertEquals(-rec.vwapSlippageBps(), rec.executionAlphaVsVwapBps(), 0.0);
        assertNotNull(rec.twapSlippageBps());
        assertNotNull(rec.arrivalSlippageBps());
    }

    @Test
    public void tcaServiceConsumesBacktestFillsOverGoldenEvents() {
        // timeline from the golden EQ vector via the consolidated book
        MarketTimeline t = TcaService.timelineFromEvents(Golden.eq(), 1, 0.01);
        assertTrue("timeline has states", t.size() > 100);
        assertTrue("golden vector prints trades", t.tradeCount() > 0);
        long t0 = t.ts(0);
        long tEnd = t.ts(t.size() - 1);
        // a synthetic child-fill record shaped like the execution simulator's
        double p1 = t.midAt(t0 + 60 * S) + 0.01;
        List<Fill> fills = List.of(
                new Fill(1, 1, 1, 1, 1, 0, Math.round(p1 * 100), 100,
                        t0 + 60 * S, com.iap.execution.Liquidity.TAKER,
                        0.3, 0.01),
                new Fill(2, 2, 1, 1, 1, 0, Math.round(p1 * 100) + 1, 200,
                        t0 + 120 * S, com.iap.execution.Liquidity.TAKER,
                        0.6, 0.02));
        TcaParentOrder parent = TcaService.parentFromFills(1, 1, 0, 400,
                t0 + 30 * S, t0 + 45 * S, tEnd, fills, 0.01, t);
        assertEquals(300, parent.qtyFilled());
        Tca.OrderTca rec = TcaService.analyze(parent, t);
        // the full record is defined and internally consistent
        assertEquals(rec.perold().totalIs(), rec.perold().delayCost()
                + rec.perold().tradingCost() + rec.perold().opportunityCost(),
                0.0);
        assertEquals(rec.perold().tradingCost(), rec.spreadCost()
                + rec.impactCost() + rec.timingCost(), 1e-9);
        assertEquals(0.75, rec.perold().fillRate(), 1e-12);
        assertNotNull(rec.fillVwap());
        assertEquals(2, rec.nFills());
        // fills were stamped with the prevailing state (positive depth)
        for (TcaFill f : parent.fills) {
            assertTrue(f.oppDepthAtFill() > 0);
            assertTrue(f.halfSpreadAtFill() >= 0.0);
        }
    }
}
