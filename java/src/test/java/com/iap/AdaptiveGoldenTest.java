package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import java.io.IOException;
import java.nio.file.Paths;
import java.util.List;
import java.util.Map;

import org.junit.Test;

import com.iap.adaptive.BaselineLoader;
import com.iap.adaptive.LifecycleGauge;
import com.iap.adaptive.Psi;
import com.iap.adaptive.RollingIc;
import com.iap.core.SplitMix64;

/**
 * Cross-language adaptability parity against
 * tests/golden/expected_adaptive.json (API_ADAPTIVE.md: the Java
 * live-metric agent must reproduce the golden PSI values at 1e-10 and the
 * lifecycle state sequences exactly).
 */
public class AdaptiveGoldenTest {
    private static double[] doubles(Object v) {
        List<Object> arr = Json.array(v);
        double[] out = new double[arr.size()];
        for (int i = 0; i < out.length; i++) {
            out[i] = Json.asDouble(arr.get(i));
        }
        return out;
    }

    @Test
    public void psiMatchesTheGoldenSyntheticVectorsAt1e10() {
        Map<String, Object> golden =
                Json.object(Golden.json("expected_adaptive.json").get("psi_ks"));
        // pinned recipe: ONE continuous SplitMix64 stream, seed 20260830 —
        // 4000 uniform() baseline, 2000 uniform() (same_dist), then
        // 2000 x (0.25 + 0.75*uniform()) (shifted)
        SplitMix64 rng = new SplitMix64(20260830L);
        double[] base = new double[4000];
        for (int i = 0; i < base.length; i++) {
            base[i] = rng.uniform();
        }
        double[] same = new double[2000];
        for (int i = 0; i < same.length; i++) {
            same[i] = rng.uniform();
        }
        double[] shifted = new double[2000];
        for (int i = 0; i < shifted.length; i++) {
            shifted[i] = 0.25 + 0.75 * rng.uniform();
        }

        // baseline capture parity: edges + expected_frac at 1e-10
        Map<String, Object> gb = Json.object(golden.get("baseline"));
        double[] edges = Psi.edges(base);
        double[] frac = Psi.fractions(base, edges);
        double[] wantEdges = doubles(gb.get("edges"));
        double[] wantFrac = doubles(gb.get("expected_frac"));
        for (int i = 0; i < wantEdges.length; i++) {
            assertEquals("edge " + i, wantEdges[i], edges[i], 1e-10);
        }
        for (int i = 0; i < wantFrac.length; i++) {
            assertEquals("expected_frac " + i, wantFrac[i], frac[i], 1e-10);
        }
        assertEquals(4000, Json.asLong(gb.get("n")));

        // PSI parity on both cases
        Map<String, Object> cases = Json.object(golden.get("cases"));
        assertEquals(Json.asDouble(
                Json.object(cases.get("same_dist")).get("psi")),
                Psi.psi(frac, Psi.fractions(same, edges)), 1e-10);
        assertEquals(Json.asDouble(
                Json.object(cases.get("shifted")).get("psi")),
                Psi.psi(frac, Psi.fractions(shifted, edges)), 1e-10);
    }

    @Test
    public void lifecycleStateSequenceMatchesTheGoldenExactly() {
        Map<String, Object> golden = Json.object(
                Golden.json("expected_adaptive.json").get("lifecycle"));
        Map<String, Object> cfg = Json.object(golden.get("config"));
        LifecycleGauge g = new LifecycleGauge(
                Json.asDouble(cfg.get("watch_ic_gate")),
                Json.asDouble(cfg.get("reactivate_ic_gate")),
                (int) Json.asLong(cfg.get("retire_breach_evals")),
                (int) Json.asLong(cfg.get("reactivate_evals")));
        List<Object> icPath = Json.array(golden.get("ic_path"));
        List<Object> wantStates = Json.array(golden.get("expected_states"));
        List<Object> informative = Json.array(golden.get("ic_informative"));
        assertEquals(wantStates.size(), icPath.size());
        assertEquals(informative.size(), icPath.size());
        int transitions = 0;
        LifecycleGauge.State prev = g.state();
        for (int i = 0; i < icPath.size(); i++) {
            Object ic = icPath.get(i);
            // null rolling IC = no evidence (NaN on the Java side); an
            // uninformative evaluation re-read a frozen IC window
            LifecycleGauge.State got = g.update(
                    ic == null ? Double.NaN : Json.asDouble(ic),
                    (Boolean) informative.get(i));
            assertEquals("eval " + (i + 1), wantStates.get(i), got.name());
            if (got != prev) {
                transitions++;
                prev = got;
            }
        }
        assertEquals(Json.asLong(golden.get("expected_transition_count")),
                transitions);
    }

    @Test
    public void thePinnedParityBaselineFileLoads() throws IOException {
        Map<String, Object> golden = Json.object(
                Golden.json("expected_adaptive.json").get("signal_eq01"));
        // research/baselines/signal_eq01.json — the pinned live-parity
        // baseline the paper-trading DriftMonitor loads by default
        BaselineLoader.Baseline b = BaselineLoader.load(
                Paths.get("..", "research", "baselines", "signal_eq01.json"));
        assertEquals("EQ01", b.alphaId());
        assertEquals("signal_eq01", b.name());
        assertEquals(Json.asLong(golden.get("n")), b.count());
        double[] edges = b.edges();
        double[] frac = b.fractions();
        assertEquals(Psi.BUCKETS - 1, edges.length);
        assertEquals(Psi.BUCKETS, frac.length);
        double sum = 0.0;
        for (double f : frac) {
            assertTrue(f >= 0.0);
            sum += f;
        }
        assertEquals("fractions sum to 1", 1.0, sum, 1e-12);
        // a distribution against its own baseline has PSI exactly 0
        assertEquals(Json.asDouble(golden.get("psi_self")),
                Psi.psi(frac, frac.clone()), 0.0);
        // and the directory loader picks THIS file for EQ01, not the
        // walk-forward run baseline (deterministic precedence)
        Map<String, BaselineLoader.Baseline> all = BaselineLoader.loadDir(
                Paths.get("..", "research", "baselines"));
        assertEquals("signal_eq01", all.get("EQ01").name());
    }

    @Test
    public void rollingIcMatchesThePythonResearchLabel() {
        Map<String, Object> g = Json.object(
                Golden.json("expected_adaptive.json").get("rolling_ic"));
        long horizonNs = Json.asLong(g.get("horizon_ns"));
        assertEquals(horizonNs,
                RollingIc.parseHorizonNs((String) g.get("horizon")));
        RollingIc ric = new RollingIc(horizonNs,
                Json.asLong(g.get("window_ns")),
                Json.asLong(g.get("bucket_ns")),
                (int) Json.asLong(g.get("min_buckets")));

        List<Object> mids = Json.array(g.get("mid_series"));
        List<Object> signals = Json.array(g.get("signals"));
        List<Object> evals = Json.array(g.get("evaluations"));

        // merge the two event-time streams (mids first at equal ts, so a
        // signal always sees the mid prevailing at its own timestamp)
        int mi = 0;
        int si = 0;
        int ei = 0;
        while (mi < mids.size() || si < signals.size()) {
            long mts = mi < mids.size()
                    ? Json.asLong(Json.array(mids.get(mi)).get(0))
                    : Long.MAX_VALUE;
            long sts = si < signals.size()
                    ? Json.asLong(Json.array(signals.get(si)).get(0))
                    : Long.MAX_VALUE;
            long now;
            if (mts <= sts) {
                List<Object> row = Json.array(mids.get(mi++));
                now = mts;
                ric.onMid(now, Json.asDouble(row.get(1)));
            } else {
                List<Object> row = Json.array(signals.get(si++));
                now = sts;
                ric.onSignal(now, Json.asDouble(row.get(1)),
                        Json.asDouble(row.get(2)));
            }
            while (ei < evals.size()) {
                Map<String, Object> ev = Json.object(evals.get(ei));
                long t = Json.asLong(ev.get("t"));
                if (now < t) {
                    break;
                }
                Object want = ev.get("rolling_ic");
                double got = ric.ic(t);
                if (want == null) {
                    assertTrue("eval " + ei + ": expected NaN, got " + got,
                            Double.isNaN(got));
                } else {
                    assertEquals("eval " + ei, Json.asDouble(want), got, 1e-10);
                }
                ei++;
            }
        }
        assertEquals("every pinned evaluation was checked", evals.size(), ei);
    }
}
