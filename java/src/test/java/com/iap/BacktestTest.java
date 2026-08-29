package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import java.nio.file.Paths;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

import org.junit.Test;

import com.iap.alpha.Alphas;
import com.iap.alpha.LinearZParams;
import com.iap.backtest.BacktestEngine;
import com.iap.backtest.CostModel;
import com.iap.backtest.ResearchBacktester;
import com.iap.core.MarketEvent;
import com.iap.execution.ChildOrder;
import com.iap.execution.ExecConfig;
import com.iap.execution.Fill;
import com.iap.execution.InstrumentSpec;
import com.iap.execution.LatencyConfig;
import com.iap.execution.OrderState;
import com.iap.execution.VenueSpec;
import com.iap.features.FeatureEngine;
import com.iap.features.FeatureVector;
import com.iap.features.Features;

/**
 * Backtest-engine tests (spec section 18), two layers:
 *
 * <ul>
 *   <li>{@link ResearchBacktester} golden parity — the EQ01 run of
 *       tests/golden/expected_backtest.json reproduced END-TO-END through
 *       the native Java feature engine + alpha scorer (1e-9; counts exact),
 *       plus the accounting identity and the pinned latency/carry rules on
 *       synthetic frames;</li>
 *   <li>the production event-driven {@link BacktestEngine} — accounting
 *       identity, determinism, checkpoint/restart equivalence, risk-hook
 *       clamping and child-order capping on the golden EQ vector.</li>
 * </ul>
 */
public class BacktestTest {
    private static final double TICK = 0.01;
    private static final double ADV = 38_000_000.0;

    // -- shared golden EQ frame (engine rows + EQ01 scores), cached --------

    private static List<FeatureVector> rows;

    private static synchronized List<FeatureVector> eqRows() {
        if (rows == null) {
            TreeMap<Long, Double> ticks = new TreeMap<>();
            ticks.put(1L, TICK);
            rows = new FeatureEngine(ticks, 0).run(Golden.eq());
        }
        return rows;
    }

    private static ResearchBacktester.Result goldenEq01Run() {
        List<FeatureVector> r = eqRows();
        int n = r.size();
        long[] ts = new long[n];
        double[] mid = new double[n];
        double[] hs = new double[n];
        double[] er = new double[n];
        double[] conf = new double[n];
        LinearZParams p = Alphas.loadParams(
                Paths.get("..", "configs", "strategies", "alpha_params.json"))
                .get("EQ01");
        for (int i = 0; i < n; i++) {
            FeatureVector v = r.get(i);
            ts[i] = v.timestamp;
            mid[i] = v.valid[Features.MID_PRICE]
                    ? v.values[Features.MID_PRICE] : Double.NaN;
            hs[i] = v.valid[Features.SPREAD_TICKS]
                    ? v.values[Features.SPREAD_TICKS] * TICK / 2.0 : Double.NaN;
            var sig = Alphas.scoreRow(p, v);
            er[i] = sig.expectedReturn();
            conf[i] = sig.confidence();
        }
        CostModel cm = CostModel.load(
                Paths.get("..", "configs", "execution.json"), 1.0);
        // Pinned golden config: max_pos 1000, conf_min 0.2, latency 1 row.
        ResearchBacktester bt = new ResearchBacktester(cm, 1000, 0.2, 1);
        return bt.run(1, ts, mid, hs, er, conf, "EQUITY", ADV, 100.0);
    }

    private static void near(double got, double want, String what) {
        assertTrue(what + ": got " + got + " want " + want,
                Math.abs(got - want) <= 1e-9 + 1e-9 * Math.abs(want));
    }

    @Test
    public void eq01GoldenBacktestReproduced() {
        Map<String, Object> g = Golden.json("expected_backtest.json");
        assertEquals("EQ01", g.get("alpha_id"));
        ResearchBacktester.Result r = goldenEq01Run();
        assertEquals(Json.asLong(g.get("n_rows")), r.nRows);
        assertEquals(Json.asLong(g.get("trade_count")), r.tradeCount);
        assertEquals(Json.asLong(g.get("traded_qty")), r.tradedQty);
        near(r.totalPnl, Json.asDouble(g.get("total_pnl")), "total_pnl");
        near(r.grossPnl, Json.asDouble(g.get("gross_pnl")), "gross_pnl");
        near(r.totalCosts, Json.asDouble(g.get("total_costs")), "total_costs");
        near(r.spreadCost, Json.asDouble(g.get("spread_cost")), "spread_cost");
        near(r.feeCost, Json.asDouble(g.get("fee_cost")), "fee_cost");
        near(r.impactCost, Json.asDouble(g.get("impact_cost")), "impact_cost");
    }

    @Test
    public void researchAccountingIdentity() {
        // total_pnl = gross_pnl - total_costs, and the cost components sum.
        ResearchBacktester.Result r = goldenEq01Run();
        near(r.totalCosts, r.spreadCost + r.feeCost + r.impactCost,
                "cost components");
        near(r.totalPnl, r.grossPnl - r.totalCosts, "identity");
        // The equity curve ends at total_pnl.
        assertEquals(r.nRows, r.equity.length);
        assertEquals(r.totalPnl, r.equity[r.nRows - 1], 0.0);
    }

    @Test
    public void researchInvalidRowCarriesPosition() {
        // Decision at row 0 (latency 1) targets row 1; row 1 is untradeable
        // (NaN mid) so the flat position CARRIES (stale target not queued);
        // the row-1 decision then executes at row 2.
        CostModel cm = new CostModel(0.0, 0.0, 0.0, 1.0);
        ResearchBacktester bt = new ResearchBacktester(cm, 10, 0.5, 1);
        long[] ts = {0, 1, 2, 3};
        double[] mid = {100.0, Double.NaN, 100.0, 101.0};
        double[] hs = {0.0, Double.NaN, 0.0, 0.0};
        double[] er = {1e-4, 1e-4, 1e-4, 1e-4};
        double[] conf = {1.0, 1.0, 1.0, 1.0};
        ResearchBacktester.Result r =
                bt.run(1, ts, mid, hs, er, conf, "EQUITY", ADV, 1.0);
        assertEquals(1, r.tradeCount);
        assertEquals(10, r.tradedQty);
        assertEquals(0.0, r.positions[0], 0.0);
        assertEquals(0.0, r.positions[1], 0.0); // carried, not queued
        assertEquals(10.0, r.positions[2], 0.0);
        // Gross = 10 * (101 - 100); costs zero by construction.
        assertEquals(10.0, r.grossPnl, 1e-12);
        assertEquals(r.grossPnl, r.totalPnl, 1e-12);
    }

    @Test
    public void researchZeroLatencyExecutesSameRow() {
        CostModel cm = new CostModel(0.0, 0.0, 0.0, 1.0);
        ResearchBacktester bt = new ResearchBacktester(cm, 5, 0.5, 0);
        long[] ts = {0, 1};
        double[] mid = {100.0, 102.0};
        double[] hs = {0.0, 0.0};
        double[] er = {1e-4, 1e-4};
        double[] conf = {1.0, 1.0};
        ResearchBacktester.Result r =
                bt.run(1, ts, mid, hs, er, conf, "EQUITY", ADV, 1.0);
        assertEquals(5.0, r.positions[0], 0.0); // filled on the decision row
        assertEquals(5.0 * 2.0, r.totalPnl, 1e-12);
    }

    @Test
    public void researchValidationThrows() {
        CostModel cm = new CostModel(0.0, 0.0, 0.0, 1.0);
        try {
            new ResearchBacktester(cm, 0, 0.5, 1);
            throw new AssertionError("max_pos 0 must throw");
        } catch (IllegalArgumentException expected) {
            // pinned
        }
        try {
            new ResearchBacktester(cm, 10, 0.5, -1);
            throw new AssertionError("negative latency must throw");
        } catch (IllegalArgumentException expected) {
            // pinned
        }
        try {
            new ResearchBacktester(cm, 10, 0.5, 1).run(1, new long[2],
                    new double[1], new double[2], new double[2], new double[2],
                    "EQUITY", ADV, 1.0);
            throw new AssertionError("misaligned arrays must throw");
        } catch (IllegalArgumentException expected) {
            // pinned
        }
    }

    // -- production event-driven engine -------------------------------------

    private static ExecConfig prodConfig() {
        TreeMap<Integer, VenueSpec> venues = VenueSpec.loadVenues(
                Paths.get("..", "configs", "venues.json"));
        TreeMap<Long, InstrumentSpec> instruments = new TreeMap<>();
        instruments.put(1L, new InstrumentSpec(1, TICK, 1.0, ADV));
        return new ExecConfig(LatencyConfig.DEFAULT, 20260829L, 2.0,
                instruments, venues);
    }

    /** Imbalance-threshold strategy: deterministic, trades on the vector. */
    private static final BacktestEngine.Strategy IMB_STRATEGY = vec -> {
        if (!vec.valid[Features.IMBALANCE_L1]) {
            return 0;
        }
        double x = vec.values[Features.IMBALANCE_L1];
        return x > 0.4 ? 100 : x < -0.4 ? -100 : 0;
    };

    private static BacktestEngine prodEngine(BacktestEngine.RiskHook risk) {
        return new BacktestEngine(prodConfig(), IMB_STRATEGY, risk, 50, 1);
    }

    @Test
    public void productionRunTradesAndSummarizes() {
        BacktestEngine.Summary s =
                prodEngine(BacktestEngine.PASSTHROUGH_RISK).run(Golden.eq());
        assertEquals(Golden.eq().size(), s.eventsProcessed);
        assertTrue("strategy must trade on the golden vector", s.fillCount > 0);
        BacktestEngine.Account a = s.accounts.get(1L);
        assertTrue(a.ordersSubmitted > 0);
        assertEquals(a.fillCount, s.fillCount);
        assertEquals(a.exposure.size(), a.fillCount);
        // Exposure timeline timestamps are non-decreasing.
        for (int i = 1; i < a.exposure.size(); i++) {
            assertTrue(a.exposure.get(i)[0] >= a.exposure.get(i - 1)[0]);
        }
    }

    @Test
    public void productionAccountingIdentity() {
        // total_pnl = gross - spread_cost - (fees - rebates) - impact, per
        // account and in the summary (identity-tested, spec section 18).
        BacktestEngine.Summary s =
                prodEngine(BacktestEngine.PASSTHROUGH_RISK).run(Golden.eq());
        near(s.totalPnl,
                s.grossPnl - s.spreadCost - s.feesNet - s.impact, "summary");
        BacktestEngine.Account a = s.accounts.get(1L);
        near(a.equity(1.0), a.grossPnl - a.spreadCost - (a.fees - a.rebates)
                - a.impact, "account");
        assertTrue(a.fees >= 0.0);
        assertTrue(a.rebates >= 0.0);
        assertTrue(a.impact >= 0.0);
    }

    @Test
    public void productionDeterminism() {
        // Same events + config + seed => identical fills and P&L.
        BacktestEngine e1 = prodEngine(BacktestEngine.PASSTHROUGH_RISK);
        BacktestEngine e2 = prodEngine(BacktestEngine.PASSTHROUGH_RISK);
        BacktestEngine.Summary s1 = e1.run(Golden.eq());
        BacktestEngine.Summary s2 = e2.run(Golden.eq());
        List<Fill> f1 = e1.simulator().fills();
        List<Fill> f2 = e2.simulator().fills();
        assertEquals(f1.size(), f2.size());
        for (int i = 0; i < f1.size(); i++) {
            assertEquals(f1.get(i), f2.get(i)); // record equality, exact
        }
        assertEquals(s1.totalPnl, s2.totalPnl, 0.0);
        assertEquals(s1.grossPnl, s2.grossPnl, 0.0);
        assertEquals(s1.feesNet, s2.feesNet, 0.0);
        assertEquals(s1.impact, s2.impact, 0.0);
        assertEquals(s1.spreadCost, s2.spreadCost, 0.0);
        assertEquals(s1.fillCount, s2.fillCount);
    }

    @Test
    public void productionCheckpointRestart() {
        List<MarketEvent> events = Golden.eq();
        int cut = events.size() / 2;

        // Reference: one uninterrupted run.
        BacktestEngine full = prodEngine(BacktestEngine.PASSTHROUGH_RISK);
        BacktestEngine.Summary want = full.run(events);

        // Checkpointed: run half, freeze, restore, run the rest.
        BacktestEngine first = prodEngine(BacktestEngine.PASSTHROUGH_RISK);
        for (int i = 0; i < cut; i++) {
            first.onEvent(events.get(i));
        }
        BacktestEngine.Checkpoint cp = first.checkpoint();
        assertEquals(cut, cp.eventsProcessed());
        // Mutating the original after the checkpoint must not matter.
        for (int i = cut; i < cut + 10; i++) {
            first.onEvent(events.get(i));
        }
        BacktestEngine resumed = BacktestEngine.restore(cp,
                IMB_STRATEGY, BacktestEngine.PASSTHROUGH_RISK);
        for (int i = cut; i < events.size(); i++) {
            resumed.onEvent(events.get(i));
        }
        BacktestEngine.Summary got = resumed.finish();

        assertEquals(want.eventsProcessed, got.eventsProcessed);
        assertEquals(want.fillCount, got.fillCount);
        assertEquals(want.totalPnl, got.totalPnl, 0.0); // bit-identical
        assertEquals(want.grossPnl, got.grossPnl, 0.0);
        assertEquals(want.feesNet, got.feesNet, 0.0);
        assertEquals(want.impact, got.impact, 0.0);
        assertEquals(want.spreadCost, got.spreadCost, 0.0);
        List<Fill> wf = full.simulator().fills();
        List<Fill> gf = resumed.simulator().fills();
        assertEquals(wf.size(), gf.size());
        for (int i = 0; i < wf.size(); i++) {
            assertEquals(wf.get(i), gf.get(i));
        }
        BacktestEngine.Account wa = want.accounts.get(1L);
        BacktestEngine.Account ga = got.accounts.get(1L);
        assertEquals(wa.position, ga.position);
        assertEquals(wa.cash, ga.cash, 0.0);
        assertEquals(wa.exposure.size(), ga.exposure.size());
    }

    @Test
    public void productionRiskHookClampsPosition() {
        long limit = 30;
        BacktestEngine e = prodEngine(BacktestEngine.maxPositionRisk(limit));
        BacktestEngine.Summary s = e.run(Golden.eq());
        BacktestEngine.Account a = s.accounts.get(1L);
        assertTrue("risk-capped run must still trade", a.fillCount > 0);
        for (long[] exp : a.exposure) {
            assertTrue("|pos| <= " + limit + " at ts " + exp[0],
                    Math.abs(exp[1]) <= limit);
        }
        // An unconstrained run breaches the cap (the hook is load-bearing).
        BacktestEngine.Account free = prodEngine(
                BacktestEngine.PASSTHROUGH_RISK).run(Golden.eq()).accounts.get(1L);
        boolean breached = false;
        for (long[] exp : free.exposure) {
            breached |= Math.abs(exp[1]) > limit;
        }
        assertTrue(breached);
    }

    @Test
    public void productionRejectAllRiskBlocksEverything() {
        BacktestEngine.RiskHook rejectAll = (iid, pos, inflight, delta, ts) -> 0;
        BacktestEngine.Summary s = prodEngine(rejectAll).run(Golden.eq());
        assertEquals(0, s.fillCount);
        assertEquals(0.0, s.totalPnl, 0.0);
        BacktestEngine.Account a = s.accounts.get(1L);
        assertEquals(0, a.ordersSubmitted);
        assertEquals(0, a.position);
    }

    @Test
    public void productionChildOrdersCappedAtMaxChildQty() {
        BacktestEngine e = prodEngine(BacktestEngine.PASSTHROUGH_RISK);
        e.run(Golden.eq());
        assertFalse(e.simulator().orders().isEmpty());
        for (ChildOrder o : e.simulator().orders().values()) {
            assertTrue("child qty " + o.qty + " <= 50", o.qty <= 50);
            assertTrue(o.qty > 0);
        }
    }

    @Test
    public void productionFinishCancelsResidualsAndFreezes() {
        BacktestEngine e = prodEngine(BacktestEngine.PASSTHROUGH_RISK);
        List<MarketEvent> events = Golden.eq();
        for (MarketEvent ev : events) {
            e.onEvent(ev);
        }
        e.finish();
        // No live orders remain after finish.
        assertTrue(e.simulator().pendingIds().isEmpty());
        for (long id : e.simulator().restingIds()) {
            assertTrue(e.simulator().orders().get(id).state
                    != OrderState.ACTIVE);
        }
        try {
            e.onEvent(events.get(0));
            throw new AssertionError("onEvent after finish must throw");
        } catch (IllegalStateException expected) {
            // pinned
        }
        // finish() is idempotent on the summary.
        BacktestEngine.Summary a = e.finish();
        BacktestEngine.Summary b = e.finish();
        assertEquals(a.totalPnl, b.totalPnl, 0.0);
        assertEquals(a.fillCount, b.fillCount);
    }

    @Test
    public void productionSorRoutingDeterministic() {
        // fixedVenueId 0: every child routes through the SOR; the run stays
        // deterministic and books fills on a golden-vector venue.
        BacktestEngine e1 = new BacktestEngine(prodConfig(), IMB_STRATEGY,
                BacktestEngine.PASSTHROUGH_RISK, 50, 0);
        BacktestEngine e2 = new BacktestEngine(prodConfig(), IMB_STRATEGY,
                BacktestEngine.PASSTHROUGH_RISK, 50, 0);
        BacktestEngine.Summary s1 = e1.run(Golden.eq());
        BacktestEngine.Summary s2 = e2.run(Golden.eq());
        assertTrue(s1.fillCount > 0);
        assertEquals(s1.fillCount, s2.fillCount);
        assertEquals(s1.totalPnl, s2.totalPnl, 0.0);
        List<Fill> f1 = e1.simulator().fills();
        List<Fill> f2 = e2.simulator().fills();
        for (int i = 0; i < f1.size(); i++) {
            assertEquals(f1.get(i), f2.get(i));
        }
    }

    @Test
    public void productionEngineValidation() {
        try {
            new BacktestEngine(prodConfig(), IMB_STRATEGY,
                    BacktestEngine.PASSTHROUGH_RISK, 0, 1);
            throw new AssertionError("max_child_qty 0 must throw");
        } catch (IllegalArgumentException expected) {
            // pinned
        }
        try {
            BacktestEngine.maxPositionRisk(0);
            throw new AssertionError("risk limit 0 must throw");
        } catch (IllegalArgumentException expected) {
            // pinned
        }
    }
}
