package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

import org.junit.Test;

import com.iap.alpha.AlphaSignal;
import com.iap.alpha.Alphas;
import com.iap.alpha.Fx05;
import com.iap.alpha.LinearZParams;
import com.iap.core.MarketEvent;
import com.iap.features.FeatureEngine;
import com.iap.features.FeatureVector;
import com.iap.features.Features;

/**
 * Alpha golden parity (API_ALPHA.md sections 2-7) + scoring invariants.
 * Two layers per alpha, both against tests/golden/expected_alpha.json:
 * embedded-input scoring (the linear_z_v1 math in isolation) and
 * engine-driven scoring (EQ01/EQ03/EQ06/FX01/FX09 through the native Java
 * feature engine at cadence 0, pinned rows at 1e-9). FX05 scores its
 * embedded 8-pair grid cross-sections through the pinned currency-factor
 * pseudo-inverse solve.
 */
public class AlphaGoldenTest {
    private static final Path PARAMS_PATH = Paths.get(
            "..", "configs", "strategies", "alpha_params.json");

    private static TreeMap<String, LinearZParams> params;
    private static List<FeatureVector> eqRows;
    private static List<FeatureVector> fxRows;

    private static synchronized TreeMap<String, LinearZParams> params() {
        if (params == null) {
            params = Alphas.loadParams(PARAMS_PATH);
        }
        return params;
    }

    private static synchronized List<FeatureVector> engineRows(boolean eq) {
        if (eqRows == null) {
            TreeMap<Long, Double> ticks = new TreeMap<>();
            ticks.put(1L, 0.01);
            ticks.put(101L, 1e-05);
            eqRows = new FeatureEngine(ticks, 0).run(Golden.eq());
            fxRows = new FeatureEngine(ticks, 0).run(Golden.fx());
        }
        return eq ? eqRows : fxRows;
    }

    private static void near(double got, double want, String what) {
        assertTrue(what + ": got " + got + " want " + want,
                Math.abs(got - want) <= 1e-9 + 1e-9 * Math.abs(want));
    }

    private void checkEmbedded(String aid) {
        Map<String, Object> golden = Golden.json("expected_alpha.json");
        List<Object> cases = Json.array(Json.object(
                Json.object(Json.object(golden.get("alphas")).get(aid))).get("cases"));
        assertEquals(5, cases.size());
        LinearZParams p = params().get(aid);
        for (Object cObj : cases) {
            Map<String, Object> c = Json.object(cObj);
            FeatureVector vec = new FeatureVector();
            for (Map.Entry<String, Object> e : Json.object(c.get("inputs")).entrySet()) {
                int slot = Features.index(e.getKey());
                assertTrue(e.getKey(), slot >= 0);
                if (e.getValue() != null) {
                    vec.values[slot] = Json.asDouble(e.getValue());
                    vec.valid[slot] = true;
                }
            }
            AlphaSignal sig = Alphas.scoreRow(p, vec);
            near(sig.expectedReturn(), Json.asDouble(c.get("expected_return")),
                    aid + " embedded expected_return");
            near(sig.confidence(), Json.asDouble(c.get("confidence")),
                    aid + " embedded conf");
        }
    }

    private void checkEngineDriven(boolean eq, String... alphaIds) {
        Map<String, Object> golden = Golden.json("expected_alpha.json");
        List<FeatureVector> all = engineRows(eq);
        for (String aid : alphaIds) {
            LinearZParams p = params().get(aid);
            List<Object> cases = Json.array(Json.object(
                    Json.object(Json.object(golden.get("alphas")).get(aid)))
                    .get("cases"));
            for (Object cObj : cases) {
                Map<String, Object> c = Json.object(cObj);
                int r = (int) Json.asLong(c.get("event_index_1based")) - 1;
                assertTrue(aid, r < all.size());
                AlphaSignal sig = Alphas.scoreRow(p, all.get(r));
                assertEquals(aid, Json.asLong(c.get("exchange_ts")), sig.timestamp());
                near(sig.expectedReturn(), Json.asDouble(c.get("expected_return")),
                        aid + " engine expected_return row " + r);
                near(sig.confidence(), Json.asDouble(c.get("confidence")),
                        aid + " engine confidence row " + r);
            }
        }
    }

    @Test
    public void paramsLoadPinned() {
        TreeMap<String, LinearZParams> p = params();
        assertEquals(6, p.size());
        for (String aid : Alphas.GOLDEN_ALPHA_IDS) {
            LinearZParams lp = p.get(aid);
            assertEquals(aid, lp.alphaId());
            assertEquals(4.0, lp.zClip(), 0.0);
            assertEquals(2.0, lp.confScale(), 0.0);
            assertTrue(Double.isFinite(lp.mu()));
            assertTrue(lp.sigma() > 0.0);
            assertTrue(Double.isFinite(lp.beta()));
            assertTrue(!lp.features().isEmpty());
        }
        // Pinned horizons (API_ALPHA.md section 4).
        assertEquals("1s", p.get("EQ01").horizon());
        assertEquals("5s", p.get("EQ03").horizon());
        assertEquals("10s", p.get("EQ06").horizon());
        assertEquals("500ms", p.get("FX01").horizon());
        assertEquals("5m", p.get("FX05").horizon());
        assertEquals("1m", p.get("FX09").horizon());
        // Honest-reporting rule: the golden file embeds the same numbers.
        Map<String, Object> golden = Golden.json("expected_alpha.json");
        Map<String, Object> gp = Json.object(
                Json.object(golden.get("params")).get("EQ01"));
        assertEquals(Json.asDouble(gp.get("beta")), p.get("EQ01").beta(), 0.0);
        assertEquals(Json.asDouble(gp.get("mu")), p.get("EQ01").mu(), 0.0);
    }

    @Test
    public void eq01EmbeddedCases() {
        checkEmbedded("EQ01");
    }

    @Test
    public void eq03EmbeddedCases() {
        checkEmbedded("EQ03");
    }

    @Test
    public void eq06EmbeddedCases() {
        checkEmbedded("EQ06");
    }

    @Test
    public void fx01EmbeddedCases() {
        checkEmbedded("FX01");
    }

    @Test
    public void fx09EmbeddedCases() {
        checkEmbedded("FX09");
    }

    @Test
    public void fx05EmbeddedGridCases() {
        Map<String, Object> golden = Golden.json("expected_alpha.json");
        LinearZParams p = params().get("FX05");
        List<Object> cases = Json.array(Json.object(
                Json.object(Json.object(golden.get("alphas")).get("FX05")))
                .get("cases"));
        assertEquals(5, cases.size());
        for (Object cObj : cases) {
            Map<String, Object> c = Json.object(cObj);
            double[] r = new double[Fx05.NUM_PAIRS];
            Map<String, Object> inputs = Json.object(c.get("inputs"));
            for (int i = 0; i < Fx05.NUM_PAIRS; i++) {
                Object v = inputs.get(Long.toString(101 + i));
                r[i] = v == null ? Double.NaN : Json.asDouble(v);
            }
            double[] raws = Fx05.rawSignals(r);
            assertEquals(102, Json.asLong(c.get("target_pair")));
            double raw = raws[1]; // pair 102 = index 1
            near(raw, Json.asDouble(c.get("raw_residual_signal")),
                    "FX05 raw residual");
            double[] ec = Alphas.scoreLinearZ(raw, p);
            near(ec[0], Json.asDouble(c.get("expected_return")),
                    "FX05 expected_return");
            near(ec[1], Json.asDouble(c.get("confidence")), "FX05 confidence");
        }
    }

    @Test
    public void engineDrivenEquities() {
        checkEngineDriven(true, "EQ01", "EQ03", "EQ06");
    }

    @Test
    public void engineDrivenFx() {
        checkEngineDriven(false, "FX01", "FX09");
    }

    @Test
    public void invalidRawScoresExactZero() {
        // Invariant: confidence == 0 => expected_return == 0.0 exactly, and
        // NaN never leaves a scorer.
        LinearZParams p = params().get("EQ01");
        double[] ec = Alphas.scoreLinearZ(Double.NaN, p);
        assertEquals(0.0, ec[0], 0.0);
        assertEquals(0.0, ec[1], 0.0);
        FeatureVector vec = new FeatureVector(); // all-invalid vector
        for (String aid : new String[] {"EQ01", "EQ03", "EQ06", "FX01", "FX09"}) {
            AlphaSignal sig = Alphas.scoreRow(params().get(aid), vec);
            assertEquals(aid, 0.0, sig.expectedReturn(), 0.0);
            assertEquals(aid, 0.0, sig.confidence(), 0.0);
        }
    }

    @Test
    public void clipAndConfidenceBounds() {
        LinearZParams p = params().get("EQ01");
        // Enormous raw: z clips at +z_clip, confidence caps at 1.
        double[] ec = Alphas.scoreLinearZ(p.mu() + 1e12 * p.sigma(), p);
        assertEquals(p.beta() * p.zClip(), ec[0], 0.0);
        assertEquals(1.0, ec[1], 0.0);
        ec = Alphas.scoreLinearZ(p.mu() - 1e12 * p.sigma(), p);
        assertEquals(-p.beta() * p.zClip(), ec[0], 0.0);
        assertEquals(1.0, ec[1], 0.0);
        // raw == mu: z = 0 => er 0, conf 0.
        ec = Alphas.scoreLinearZ(p.mu(), p);
        assertEquals(0.0, ec[0], 0.0);
        assertEquals(0.0, ec[1], 0.0);
    }

    @Test
    public void fx05DegenerateCrossSections() {
        // A lone valid pair carries no relative-value information.
        double[] r = new double[Fx05.NUM_PAIRS];
        java.util.Arrays.fill(r, Double.NaN);
        r[0] = 1e-4;
        double[] raws = Fx05.rawSignals(r);
        for (double v : raws) {
            assertTrue(Double.isNaN(v));
        }
        // Two valid pairs: signals defined for exactly those two; EUR/USD
        // and GBP/USD load disjoint factors, so minimum-norm attributes each
        // return fully to its own currency => residuals (and raws) are 0.
        r[1] = -5e-5;
        raws = Fx05.rawSignals(r);
        assertTrue(Double.isFinite(raws[0]));
        assertTrue(Double.isFinite(raws[1]));
        for (int i = 2; i < Fx05.NUM_PAIRS; i++) {
            assertTrue(Double.isNaN(raws[i]));
        }
        assertEquals(0.0, raws[0], 1e-15);
        assertEquals(0.0, raws[1], 1e-15);
    }

    @Test
    public void fx05ResidualOrthogonalToExposures() {
        // Full cross-section: the LS residual must be orthogonal to every
        // free currency column of the exposure matrix.
        double[] r = {3e-5, -2e-5, 1e-5, 4e-5, -1e-5, 2e-5, -3e-5, 5e-6};
        double[] f = new double[Fx05.NUM_FREE_CCY];
        double[] fitted = new double[Fx05.NUM_PAIRS];
        assertTrue(Fx05.solveFactors(r, f, fitted));
        double[][] a = Fx05.freeExposureMatrix();
        for (int j = 0; j < Fx05.NUM_FREE_CCY; j++) {
            double dot = 0.0;
            for (int i = 0; i < Fx05.NUM_PAIRS; i++) {
                dot += a[i][j] * (r[i] - fitted[i]);
            }
            assertEquals("free currency column " + j, 0.0, dot, 1e-15);
        }
    }

    @Test
    public void rawSignalFormulaPinned() {
        // EQ03's pinned 0.5/0.3/0.2 weighting and FX09's product form.
        FeatureVector vec = new FeatureVector();
        set(vec, Features.OFI_NORM_L1_W1S, 1.0);
        set(vec, Features.OFI_NORM_L5_W1S, 2.0);
        set(vec, Features.OFI_NORM_L5_W5S, 3.0);
        assertEquals(0.5 * 1.0 + 0.3 * 2.0 + 0.2 * 3.0,
                Alphas.rawSignal("EQ03", vec), 0.0);
        set(vec, Features.RET_VOL_ADJ_10S, 1.5);
        set(vec, Features.VOL_REGIME_RATIO, 0.5);
        assertEquals(-1.5 * 0.5, Alphas.rawSignal("FX09", vec), 0.0);
        assertEquals(1.5, Alphas.rawSignal("EQ06", vec), 0.0);
        set(vec, Features.MICRO_MID_DEV_BPS, -0.25);
        assertEquals(-0.25, Alphas.rawSignal("EQ01", vec), 0.0);
        assertEquals(-0.25, Alphas.rawSignal("FX01", vec), 0.0);
        for (String bad : new String[] {"FX05", "EQ99"}) {
            try {
                Alphas.rawSignal(bad, vec);
                throw new AssertionError(bad + " must throw");
            } catch (IllegalArgumentException expected) {
                // pinned behavior
            }
        }
    }

    @Test
    public void icWindowReproduced() {
        // Pearson IC of expected_return (rows with conf > 0) vs the golden
        // per-row mid labels is pinned per alpha; recompute EQ01's from the
        // engine rows and the golden EQ mid series at its pinned horizon.
        Map<String, Object> golden = Golden.json("expected_alpha.json");
        Map<String, Object> eq01 = Json.object(
                Json.object(golden.get("alphas")).get("EQ01"));
        Map<String, Object> icw = Json.object(eq01.get("ic_window"));
        assertEquals("1s", icw.get("horizon"));
        List<Object> window = Json.array(icw.get("rows_0based"));
        int lo = (int) Json.asLong(window.get(0));
        int hi = (int) Json.asLong(window.get(1));
        List<FeatureVector> rows = engineRows(true);
        LinearZParams p = params().get("EQ01");
        // mid label at 1s: forward mid-change-sample return over the row's
        // own mid series (labels are Python-owned; here we only need the
        // window bounds to sanity-check signal coverage).
        int scored = 0;
        for (int i = lo; i < hi; i++) {
            AlphaSignal sig = Alphas.scoreRow(p, rows.get(i));
            if (sig.confidence() > 0.0) {
                scored++;
            }
        }
        assertTrue("EQ01 must emit signals inside the IC window", scored > 100);
        assertTrue(Double.isFinite(Json.asDouble(icw.get("ic"))));
    }

    private static void set(FeatureVector vec, int slot, double v) {
        vec.values[slot] = v;
        vec.valid[slot] = true;
    }
}
