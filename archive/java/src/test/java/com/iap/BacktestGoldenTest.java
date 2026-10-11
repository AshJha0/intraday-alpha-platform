package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.Arrays;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;

import org.junit.Test;

import com.iap.alpha.Alphas;
import com.iap.alpha.LinearZParams;
import com.iap.backtest.CostModel;
import com.iap.backtest.ResearchBacktester;
import com.iap.features.FeatureEngine;
import com.iap.features.FeatureVector;
import com.iap.features.Features;

/**
 * Research-backtester golden parity against
 * tests/golden/expected_backtest.json (x-version 3), END-TO-END through the
 * native Java feature engine + alpha scorer, under both rule sets:
 *
 * <ul>
 *   <li>the LEGACY rules its top-level {@code config} names (EQ01: sign
 *       policy, uncapped fills, every row, linear impact);</li>
 *   <li>the v1.5.0 DEFAULT rules of {@code default_rules} (EQ06: cost-aware
 *       positions, L1 fill cap, scored rows only, square-root impact), with
 *       every position change checked exactly, and the same run at full
 *       costs ({@code default_rules_1x}), which makes no trade;</li>
 *   <li>the scalar {@code cost_model_cases} under both impact rules.</li>
 * </ul>
 *
 * Money at 1e-9 abs/rel; counts and positions exact.
 */
public class BacktestGoldenTest {
    private static final double TICK = 0.01;
    private static final double ADV = 38_000_000.0;
    private static final Path EXECUTION_CONFIG =
            Paths.get("..", "configs", "execution", "execution.json");

    /** The golden EQ frame columns the research backtester reads. */
    private static final class Frame {
        List<FeatureVector> rows;
        long[] ts;
        double[] mid;
        double[] hs;
        double[] bidSize;
        double[] askSize;
    }

    private static Frame frame;

    private static double column(FeatureVector v, int feature) {
        return v.valid[feature] ? v.values[feature] : Double.NaN;
    }

    private static synchronized Frame frame() {
        if (frame == null) {
            TreeMap<Long, Double> ticks = new TreeMap<>();
            ticks.put(1L, TICK);
            Frame f = new Frame();
            f.rows = new FeatureEngine(ticks, 0).run(Golden.eq());
            int n = f.rows.size();
            f.ts = new long[n];
            f.mid = new double[n];
            f.hs = new double[n];
            f.bidSize = new double[n];
            f.askSize = new double[n];
            for (int i = 0; i < n; i++) {
                FeatureVector v = f.rows.get(i);
                f.ts[i] = v.timestamp;
                f.mid[i] = column(v, Features.MID_PRICE);
                f.hs[i] = v.valid[Features.SPREAD_TICKS]
                        ? v.values[Features.SPREAD_TICKS] * TICK / 2.0 : Double.NaN;
                f.bidSize[i] = column(v, Features.DEPTH_BID_L1);
                f.askSize[i] = column(v, Features.DEPTH_ASK_L1);
            }
            frame = f;
        }
        return frame;
    }

    /** {expected_return, confidence} per row of the golden EQ frame. */
    private static double[][] scores(String alphaId) {
        Frame f = frame();
        LinearZParams p = Alphas.loadParams(
                Paths.get("..", "configs", "strategies", "alpha_params.json"))
                .get(alphaId);
        int n = f.rows.size();
        double[] er = new double[n];
        double[] conf = new double[n];
        for (int i = 0; i < n; i++) {
            var sig = Alphas.scoreRow(p, f.rows.get(i));
            er[i] = sig.expectedReturn();
            conf[i] = sig.confidence();
        }
        return new double[][] {er, conf};
    }

    private static void near(double got, double want, String what) {
        assertTrue(what + ": got " + got + " want " + want,
                Math.abs(got - want) <= 1e-9 + 1e-9 * Math.abs(want));
    }

    private static void checkNumbers(ResearchBacktester.Result r,
            Map<String, Object> g) {
        assertEquals(Json.asLong(g.get("n_rows")), r.nRows);
        assertEquals(Json.asLong(g.get("trade_count")), r.tradeCount);
        assertEquals(Json.asLong(g.get("traded_qty")), r.tradedQty);
        near(r.totalPnl, Json.asDouble(g.get("total_pnl")), "total_pnl");
        near(r.grossPnl, Json.asDouble(g.get("gross_pnl")), "gross_pnl");
        near(r.totalCosts, Json.asDouble(g.get("total_costs")), "total_costs");
        near(r.spreadCost, Json.asDouble(g.get("spread_cost")), "spread_cost");
        near(r.feeCost, Json.asDouble(g.get("fee_cost")), "fee_cost");
        near(r.impactCost, Json.asDouble(g.get("impact_cost")), "impact_cost");
        // accounting identity on the golden run itself
        near(r.totalCosts, r.spreadCost + r.feeCost + r.impactCost,
                "cost components");
        near(r.totalPnl, r.grossPnl - r.totalCosts, "identity");
        assertEquals(r.nRows, r.equity.length);
        assertEquals(r.totalPnl, r.equity[r.nRows - 1], 0.0);
    }

    private static Map<String, Object> golden() {
        Map<String, Object> g = Golden.json("expected_backtest.json");
        assertEquals(3L, Json.asLong(g.get("x-version")));
        return g;
    }

    @Test
    public void legacyRulesGoldenReproduced() {
        Map<String, Object> g = golden();
        assertEquals("EQ01", g.get("alpha_id"));
        Map<String, Object> cfg = Json.object(g.get("config"));
        // Config.legacy / withLinearImpact ARE the rule set the golden names.
        ResearchBacktester.Config config = ResearchBacktester.Config.legacy(
                Json.asLong(cfg.get("max_pos_qty")),
                Json.asDouble(cfg.get("conf_min")),
                (int) Json.asLong(cfg.get("latency_rows")));
        assertEquals(ResearchBacktester.LEGACY_POSITION_POLICY,
                cfg.get("position_policy"));
        assertEquals(config.positionPolicy(), cfg.get("position_policy"));
        assertEquals(config.capFillsAtL1(), cfg.get("cap_fills_at_l1"));
        assertFalse(config.blockRows());
        assertNull(cfg.get("block_rows_column"));
        CostModel cm = CostModel.load(EXECUTION_CONFIG,
                Json.asDouble(cfg.get("cost_multiplier"))).withLinearImpact();
        assertEquals(CostModel.LEGACY_IMPACT_MODEL, cfg.get("impact_model"));
        assertEquals(cm.impactModel(), cfg.get("impact_model"));
        Frame f = frame();
        double[][] s = scores("EQ01");
        // the legacy rules need neither displayed sizes nor a row mask
        ResearchBacktester.Result r = new ResearchBacktester(cm, config)
                .run(1, f.ts, f.mid, f.hs, s[0], s[1], "EQUITY", ADV, 100.0);
        checkNumbers(r, g);
    }

    private static ResearchBacktester.Result defaultRulesRun(
            Map<String, Object> g, Map<String, Object> blob) {
        assertEquals("EQ06", blob.get("alpha_id"));
        Map<String, Object> cfg = Json.object(blob.get("config"));
        // Config.defaults + the config's own cost model ARE the rules named.
        ResearchBacktester.Config config = ResearchBacktester.Config.defaults(
                Json.asLong(cfg.get("max_pos_qty")),
                Json.asDouble(cfg.get("conf_min")),
                (int) Json.asLong(cfg.get("latency_rows")))
                .forHorizon(Json.asLong(blob.get("horizon_ns")));
        assertEquals(ResearchBacktester.DEFAULT_POSITION_POLICY,
                cfg.get("position_policy"));
        assertEquals(config.positionPolicy(), cfg.get("position_policy"));
        assertEquals(config.capFillsAtL1(), cfg.get("cap_fills_at_l1"));
        assertTrue(config.blockRows());
        assertEquals("auto", cfg.get("block_rows_column"));
        assertEquals(Json.asDouble(blob.get("hysteresis")), config.hysteresis(), 0.0);
        CostModel cm = CostModel.load(EXECUTION_CONFIG,
                Json.asDouble(cfg.get("cost_multiplier")));
        assertEquals(CostModel.DEFAULT_IMPACT_MODEL, cfg.get("impact_model"));
        assertEquals(cm.impactModel(), cfg.get("impact_model"));

        // the scored-row mask of the reference (both default runs share it)
        Map<String, Object> rows = Json.object(
                Json.object(g.get("default_rules")).get("scored_rows"));
        Frame f = frame();
        int n = f.ts.length;
        assertEquals(Json.asLong(rows.get("n_rows")), n);
        boolean[] allowed = new boolean[n];
        Arrays.fill(allowed, true);
        List<Object> blocked = Json.array(rows.get("blocked_rows"));
        assertFalse(blocked.isEmpty());
        for (Object row : blocked) {
            allowed[(int) Json.asLong(row)] = false;
        }
        assertEquals(Json.asLong(rows.get("n_scored")), n - blocked.size());

        double[][] s = scores("EQ06");
        ResearchBacktester.Result r = new ResearchBacktester(cm, config).run(1,
                f.ts, f.mid, f.hs, s[0], s[1], f.bidSize, f.askSize, allowed,
                "EQUITY", ADV, 100.0);
        // every position change, exact: [row, position after the row]
        List<Object> changes = Json.array(blob.get("position_changes"));
        int next = 0;
        double before = 0.0;
        for (int i = 0; i < n; i++) {
            if (r.positions[i] != before) {
                assertTrue("unexpected position change at row " + i + " to "
                        + r.positions[i], next < changes.size());
                List<Object> want = Json.array(changes.get(next++));
                assertEquals("row of position change " + next,
                        Json.asLong(want.get(0)), i);
                assertEquals("position after row " + i,
                        Json.asDouble(want.get(1)), r.positions[i], 0.0);
                before = r.positions[i];
            }
        }
        assertEquals("position changes", changes.size(), next);
        checkNumbers(r, blob);
        return r;
    }

    @Test
    public void defaultRulesGoldenReproduced() {
        Map<String, Object> g = golden();
        ResearchBacktester.Result r =
                defaultRulesRun(g, Json.object(g.get("default_rules")));
        assertTrue("the vector exists to pin a run that trades", r.tradeCount >= 5);
    }

    @Test
    public void defaultRulesAtFullCostsMakeNoTrade() {
        Map<String, Object> g = golden();
        Map<String, Object> blob = Json.object(g.get("default_rules_1x"));
        assertEquals(1.0, Json.asDouble(
                Json.object(blob.get("config")).get("cost_multiplier")), 0.0);
        ResearchBacktester.Result r = defaultRulesRun(g, blob);
        assertEquals(0, r.tradeCount);
        assertEquals(0.0, r.totalPnl, 0.0);
        assertEquals(0.0, r.totalCosts, 0.0);
    }

    @Test
    public void costModelCasesReproduced() {
        Map<String, Object> block = Json.object(golden().get("cost_model_cases"));
        CostModel base = CostModel.load(EXECUTION_CONFIG, 1.0);
        assertEquals(Json.asDouble(block.get("impact_coeff_bps_per_pct_adv")),
                base.impactCoeffBpsPerPctAdv(), 0.0);
        assertEquals(Json.asDouble(block.get("equity_taker_fee_per_share")),
                base.equityTakerFeePerShare(), 0.0);
        assertEquals(Json.asDouble(block.get("fx_commission_per_million")),
                base.fxCommissionPerMillion(), 0.0);
        assertEquals(Json.asDouble(block.get("sqrt_impact_coeff_bps")),
                base.sqrtImpactCoeffBps(), 0.0);
        Set<String> seen = new HashSet<>();
        int index = 0;
        for (Object o : Json.array(block.get("cases"))) {
            Map<String, Object> c = Json.object(o);
            String model = (String) c.get("impact_model");
            String where = "case " + index++ + " (" + model + ") ";
            CostModel cm = base.withMultiplier(Json.asDouble(c.get("multiplier")));
            if (model.equals(CostModel.LEGACY_IMPACT_MODEL)) {
                cm = cm.withLinearImpact();
            }
            assertEquals(model, cm.impactModel());
            seen.add(model);
            String assetClass = (String) c.get("asset_class");
            double qty = Json.asDouble(c.get("qty"));
            double mid = Json.asDouble(c.get("mid"));
            double hs = Json.asDouble(c.get("half_spread"));
            double adv = Json.asDouble(c.get("adv"));
            double lot = Json.asDouble(c.get("lot_size"));
            double edge = Json.asDouble(c.get("edge_return"));
            double[] comp = cm.costComponents(qty, mid, hs, assetClass, adv, lot);
            near(comp[0], Json.asDouble(c.get("spread")), where + "spread");
            near(comp[1], Json.asDouble(c.get("fee")), where + "fee");
            near(comp[2], Json.asDouble(c.get("impact")), where + "impact");
            double unit = assetClass.equals("FX") ? lot : 1.0;
            near(cm.impactBps(Math.abs(qty) * unit / adv),
                    Json.asDouble(c.get("impact_bps")), where + "impact_bps");
            near(cm.roundTripCostReturn(mid, hs, assetClass),
                    Json.asDouble(c.get("round_trip_cost_return")),
                    where + "round_trip_cost_return");
            near(cm.breakevenSize(edge, mid, hs, assetClass, adv, lot),
                    Json.asDouble(c.get("breakeven_size")),
                    where + "breakeven_size");
            Map<String, Object> cap = Json.object(c.get("capacity"));
            double[] got = cm.capacityBreakeven(edge, mid, hs, assetClass, adv, lot);
            near(got[0], Json.asDouble(cap.get("units")), where + "units");
            near(got[1], Json.asDouble(cap.get("notional")), where + "notional");
            near(got[2], Json.asDouble(cap.get("participation")),
                    where + "participation");
        }
        assertEquals(Set.of("sqrt", "linear"), seen);
    }
}
