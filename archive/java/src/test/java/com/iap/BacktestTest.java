package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import java.nio.file.Paths;
import java.util.List;
import java.util.TreeMap;

import org.junit.Test;

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
import com.iap.features.Features;

/**
 * Backtest-engine tests (spec section 18), two layers:
 *
 * <ul>
 *   <li>{@link ResearchBacktester} / {@link CostModel} rules on hand-built
 *       frames — the pinned latency/carry rules, the cost-aware position
 *       policy, the L1 fill cap, the row block, both impact rules and the
 *       breakeven capacity (the golden replay of both rule sets is
 *       {@link BacktestGoldenTest});</li>
 *   <li>the production event-driven {@link BacktestEngine} — accounting
 *       identity, determinism, checkpoint/restart equivalence, risk-hook
 *       clamping and child-order capping on the golden EQ vector.</li>
 * </ul>
 */
public class BacktestTest {
    private static final double TICK = 0.01;
    private static final double ADV = 38_000_000.0;

    /** No fee, no impact under either rule: only the spread is charged. */
    private static final CostModel ZERO_COST =
            new CostModel(0.0, 0.0, 0.0, 1.0, CostModel.LEGACY_IMPACT_MODEL, 0.0);

    private static ResearchBacktester legacyBacktester(long maxPosQty,
            double confMin, int latencyRows) {
        return new ResearchBacktester(ZERO_COST,
                ResearchBacktester.Config.legacy(maxPosQty, confMin, latencyRows));
    }

    private static void near(double got, double want, String what) {
        assertTrue(what + ": got " + got + " want " + want,
                Math.abs(got - want) <= 1e-9 + 1e-9 * Math.abs(want));
    }

    @Test
    public void researchInvalidRowCarriesPosition() {
        // Decision at row 0 (latency 1) targets row 1; row 1 is untradeable
        // (NaN mid) so the flat position CARRIES (stale target not queued);
        // the row-1 decision then executes at row 2.
        ResearchBacktester bt = legacyBacktester(10, 0.5, 1);
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
        ResearchBacktester bt = legacyBacktester(5, 0.5, 0);
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
        try {
            ResearchBacktester.Config.legacy(0, 0.5, 1);
            throw new AssertionError("max_pos 0 must throw");
        } catch (IllegalArgumentException expected) {
            // pinned
        }
        try {
            ResearchBacktester.Config.legacy(10, 0.5, -1);
            throw new AssertionError("negative latency must throw");
        } catch (IllegalArgumentException expected) {
            // pinned
        }
        try {
            new ResearchBacktester.Config(10, 0.5, 1, "momentum", 0L, 0.5, false,
                    false);
            throw new AssertionError("unknown position policy must throw");
        } catch (IllegalArgumentException expected) {
            // pinned
        }
        try {
            ResearchBacktester.Config.defaults(10, 0.5, 1).withHysteresis(1.5);
            throw new AssertionError("hysteresis above 1 must throw");
        } catch (IllegalArgumentException expected) {
            // pinned
        }
        try {
            ResearchBacktester.Config.defaults(10, 0.5, 1).forHorizon(0L);
            throw new AssertionError("horizon 0 must throw");
        } catch (IllegalArgumentException expected) {
            // pinned
        }
        try {
            legacyBacktester(10, 0.5, 1).run(1, new long[2],
                    new double[1], new double[2], new double[2], new double[2],
                    "EQUITY", ADV, 1.0);
            throw new AssertionError("misaligned arrays must throw");
        } catch (IllegalArgumentException expected) {
            // pinned
        }
    }

    @Test
    public void researchDefaultsAreTheV2RulesAndLegacyIsNamed() {
        ResearchBacktester.Config d = ResearchBacktester.Config.defaults(1000, 0.5, 1);
        assertEquals("cost_aware", d.positionPolicy());
        assertEquals(ResearchBacktester.DEFAULT_POSITION_POLICY, d.positionPolicy());
        assertTrue(d.capFillsAtL1());
        assertTrue(d.blockRows());
        assertEquals(0.5, d.hysteresis(), 0.0);
        assertEquals(ResearchBacktester.NO_HORIZON, d.horizonNs());
        assertEquals(10_000_000_000L, d.forHorizon(10_000_000_000L).horizonNs());
        ResearchBacktester.Config l = ResearchBacktester.Config.legacy(1000, 0.5, 1);
        assertEquals("sign", l.positionPolicy());
        assertFalse(l.capFillsAtL1());
        assertFalse(l.blockRows());
        // the cost model: square root by default, linear by name
        CostModel cm = new CostModel(2.0, 0.003, 2.5, 1.0);
        assertEquals("sqrt", cm.impactModel());
        assertEquals(CostModel.DEFAULT_SQRT_IMPACT_COEFF_BPS, cm.sqrtImpactCoeffBps(), 0.0);
        assertEquals("linear", cm.withLinearImpact().impactModel());
        assertEquals("sqrt", cm.withLinearImpact().withSqrtImpact(50.0).impactModel());
        assertEquals(2.0, cm.withMultiplier(2.0).multiplier(), 0.0);
        CostModel loaded = CostModel.load(
                Paths.get("..", "configs", "execution", "execution.json"), 1.0);
        assertEquals("sqrt", loaded.impactModel());
        assertEquals("linear", CostModel.loadLegacyLinear(
                Paths.get("..", "configs", "execution", "execution.json"), 1.0)
                .impactModel());
    }

    @Test
    public void researchCostAwareNeedsHorizon() {
        // The default policy without the label horizon is an error, never a
        // silent fall-back to the legacy rule.
        ResearchBacktester bt = new ResearchBacktester(ZERO_COST,
                new ResearchBacktester.Config(10, 0.5, 0, "cost_aware",
                        ResearchBacktester.NO_HORIZON, 0.5, false, false));
        try {
            bt.run(1, new long[] {0}, new double[] {100.0}, new double[] {0.01},
                    new double[] {1e-3}, new double[] {1.0}, "EQUITY", ADV, 1.0);
            throw new AssertionError("cost_aware without horizon_ns must throw");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("needs horizon_ns"));
        }
    }

    @Test
    public void researchCostAwareEntersHoldsFlipsRenewsAndCloses() {
        // mid 100, half-spread 0.01, no fee: the round-trip cost is
        // 2 * 0.01 / 100 = 2e-4 as a return; horizon 5 ns, hysteresis 0.5.
        ResearchBacktester bt = new ResearchBacktester(ZERO_COST,
                new ResearchBacktester.Config(10, 0.5, 0, "cost_aware", 5L, 0.5,
                        false, false));
        long[] ts = {0, 1, 2, 3, 8, 13, 14};
        double[] mid = {100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0};
        double[] hs = {0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01};
        double[] er = {
            1e-4,     // below the round-trip cost: stay flat
            3e-4,     // clears it: enter long at ts 1
            -1e-4,    // opposite but below the cost: the hold continues
            -3e-4,    // opposite and clears it: flip short, clock restarts at 3
            -1.5e-4,  // horizon elapsed; same side clears 0.5 * cost: renew
            -0.5e-4,  // horizon elapsed; does not clear the hysteresis: close
            3e-4,     // clears the cost but confidence is below conf_min
        };
        double[] conf = {1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 0.1};
        ResearchBacktester.Result r =
                bt.run(1, ts, mid, hs, er, conf, "EQUITY", ADV, 1.0);
        double[] want = {0.0, 10.0, 10.0, -10.0, -10.0, 0.0, 0.0};
        for (int i = 0; i < want.length; i++) {
            assertEquals("position at row " + i, want[i], r.positions[i], 0.0);
        }
        assertEquals(3, r.tradeCount);
        assertEquals(40, r.tradedQty);
        // flat mid: the run pays exactly the spread on 40 units
        near(r.spreadCost, 40 * 0.01, "spread");
        near(r.totalPnl, -40 * 0.01, "total");
        // an invalid half-spread has no threshold: the row cannot open
        double[] signs = ResearchBacktester.costAwareTargets(new long[] {0, 1},
                new double[] {1e-3, 1e-3}, new double[] {1.0, 1.0},
                new double[] {Double.NaN, 2e-4}, 0.5, 5L, 0.5);
        assertEquals(0.0, signs[0], 0.0);
        assertEquals(1.0, signs[1], 0.0);
    }

    @Test
    public void researchFillsAreCappedAtDisplayedSize() {
        // sign policy, latency 0, cap ON: a buy takes the ask size, a sell
        // the bid size; a missing or zero size fills nothing; the unfilled
        // remainder is not queued (the next row trades toward ITS target).
        ResearchBacktester bt = new ResearchBacktester(ZERO_COST,
                new ResearchBacktester.Config(1000, 0.5, 0, "sign", 0L, 0.5, true,
                        false));
        long[] ts = {0, 1, 2, 3, 4, 5};
        double[] mid = {100.0, 100.0, 100.0, 100.0, 100.0, 100.0};
        double[] hs = {0.0, 0.0, 0.0, 0.0, 0.0, 0.0};
        double[] er = {1e-4, 1e-4, 1e-4, 1e-4, -1e-4, -1e-4};
        double[] conf = {1.0, 1.0, 1.0, 1.0, 1.0, 1.0};
        double[] ask = {300.0, Double.NaN, 500.0, 400.0, 9000.0, 9000.0};
        double[] bid = {9000.0, 9000.0, 9000.0, 9000.0, 0.0, 2500.0};
        ResearchBacktester.Result r = bt.run(1, ts, mid, hs, er, conf, bid, ask,
                null, "EQUITY", ADV, 1.0);
        double[] want = {300.0, 300.0, 800.0, 1000.0, 1000.0, -1000.0};
        for (int i = 0; i < want.length; i++) {
            assertEquals("position at row " + i, want[i], r.positions[i], 0.0);
        }
        assertEquals(4, r.tradeCount);
        assertEquals(3000, r.tradedQty);
        try {
            bt.run(1, ts, mid, hs, er, conf, "EQUITY", ADV, 1.0);
            throw new AssertionError("cap_fills_at_l1 without sizes must throw");
        } catch (IllegalArgumentException expected) {
            // pinned
        }
    }

    @Test
    public void researchBlockedRowsMakeNoDecision() {
        // sign policy, latency 1, row block ON: the blocked row 1 produces
        // no target, so row 2 carries the position instead of flipping.
        ResearchBacktester bt = new ResearchBacktester(ZERO_COST,
                new ResearchBacktester.Config(10, 0.5, 1, "sign", 0L, 0.5, false,
                        true));
        long[] ts = {0, 1, 2, 3};
        double[] mid = {100.0, 100.0, 100.0, 100.0};
        double[] hs = {0.0, 0.0, 0.0, 0.0};
        double[] er = {1e-4, -1e-4, -1e-4, -1e-4};
        double[] conf = {1.0, 1.0, 1.0, 1.0};
        boolean[] allowed = {true, false, true, true};
        ResearchBacktester.Result r = bt.run(1, ts, mid, hs, er, conf, null, null,
                allowed, "EQUITY", ADV, 1.0);
        double[] want = {0.0, 10.0, 10.0, -10.0};
        for (int i = 0; i < want.length; i++) {
            assertEquals("position at row " + i, want[i], r.positions[i], 0.0);
        }
        try {
            bt.run(1, ts, mid, hs, er, conf, "EQUITY", ADV, 1.0);
            throw new AssertionError("block_rows without a mask must throw");
        } catch (IllegalArgumentException expected) {
            // pinned
        }
    }

    @Test
    public void costModelImpactRulesAndBreakeven() {
        CostModel sqrt = new CostModel(2.0, 0.003, 2.5, 1.0);
        CostModel linear = sqrt.withLinearImpact();
        // 1 000 shares in a 5 M-share ADV at 50: participation 2e-4
        double[] s = sqrt.costComponents(-1000, 50.0, 0.01, "EQUITY", 5e6, 100.0);
        double[] l = linear.costComponents(-1000, 50.0, 0.01, "EQUITY", 5e6, 100.0);
        assertEquals(1000 * 0.01, s[0], 1e-12);
        assertEquals(1000 * 0.003, s[1], 1e-12);
        assertEquals(100.0 * Math.sqrt(2e-4) * 1e-4 * 1000 * 50.0, s[2], 1e-9);
        assertEquals(2.0 * (2e-4 * 100.0) * 1e-4 * 1000 * 50.0, l[2], 1e-12);
        assertEquals(s[0], l[0], 0.0);
        assertEquals(s[1], l[1], 0.0);
        assertEquals(100.0 * Math.sqrt(2e-4), sqrt.impactBps(2e-4), 1e-12);
        assertEquals(0.04, linear.impactBps(2e-4), 1e-12);
        // round trip: (2 * 0.01 + 2 * 0.003) / 50 = 5.2e-4
        assertEquals(5.2e-4, sqrt.roundTripCostReturn(50.0, 0.01, "EQUITY"), 1e-15);
        assertEquals(2 * 5.2e-4,
                sqrt.withMultiplier(2.0).roundTripCostReturn(50.0, 0.01, "EQUITY"),
                1e-15);
        assertTrue(Double.isNaN(sqrt.roundTripCostReturn(Double.NaN, 0.01, "EQUITY")));
        assertTrue(Double.isNaN(sqrt.roundTripCostReturn(50.0, -0.01, "EQUITY")));
        assertTrue(Double.isNaN(sqrt.roundTripCostReturn(0.0, 0.01, "EQUITY")));
        // breakeven: an edge below spread + fee has no capacity
        assertEquals(0.0, sqrt.breakevenSize(5e-4, 50.0, 0.01, "EQUITY", 5e6, 100.0),
                0.0);
        // edge 7.2e-4 leaves 2e-4: 1 bp per leg; sqrt -> (1/100)^2 of ADV,
        // linear -> 1 / (2 * 100) of ADV
        near(sqrt.breakevenSize(7.2e-4, 50.0, 0.01, "EQUITY", 5e6, 100.0),
                1e-4 * 5e6, "sqrt breakeven");
        near(linear.breakevenSize(7.2e-4, 50.0, 0.01, "EQUITY", 5e6, 100.0),
                5e6 / 200.0, "linear breakeven");
        // no impact charged: nothing bounds the size
        assertTrue(Double.isInfinite(sqrt.withSqrtImpact(0.0)
                .breakevenSize(7.2e-4, 50.0, 0.01, "EQUITY", 5e6, 100.0)));
        double[] cap = sqrt.capacityBreakeven(7.2e-4, 50.0, 0.01, "EQUITY", 5e6, 100.0);
        near(cap[1], cap[0] * 50.0, "notional");
        near(cap[2], cap[0] / 5e6, "participation");
        try {
            new CostModel(2.0, 0.003, 2.5, 1.0, "quadratic", 100.0);
            throw new AssertionError("unknown impact model must throw");
        } catch (IllegalArgumentException expected) {
            // pinned
        }
        try {
            sqrt.withSqrtImpact(-1.0);
            throw new AssertionError("negative sqrt coefficient must throw");
        } catch (IllegalArgumentException expected) {
            // pinned
        }
        try {
            sqrt.costComponents(1, 50.0, 0.01, "BOND", 5e6, 1.0);
            throw new AssertionError("unknown asset class must throw");
        } catch (IllegalArgumentException expected) {
            // pinned
        }
    }

    // -- production event-driven engine -------------------------------------

    private static ExecConfig prodConfig() {
        TreeMap<Integer, VenueSpec> venues = VenueSpec.loadVenues(
                Paths.get("..", "configs", "venues", "venues.json"));
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
