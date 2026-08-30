package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import java.util.Map;
import java.util.TreeMap;

import org.junit.Test;

import com.iap.adaptive.BaselineLoader;
import com.iap.adaptive.DriftMonitor;
import com.iap.adaptive.Psi;
import com.iap.config.Json;

/**
 * DriftMonitor: rolling live window per alpha, PSI recomputed on the pinned
 * cadence against the loaded baseline; unarmed alphas never produce a PSI.
 */
public class DriftMonitorTest {
    /** Baseline over the values 0..15 (16 distinct values). */
    private static Map<String, BaselineLoader.Baseline> baseline16() {
        StringBuilder sb = new StringBuilder(
                "{\"alpha_id\":\"T\",\"values\":[");
        for (int i = 0; i < 16; i++) {
            sb.append(i == 0 ? "" : ",").append(i);
        }
        sb.append("]}");
        Map<String, BaselineLoader.Baseline> m = new TreeMap<>();
        m.put("T", BaselineLoader.parse(Json.object(Json.parse(
                sb.toString()))));
        return m;
    }

    @Test
    public void identicalLiveWindowHasExactlyZeroPsi() {
        DriftMonitor mon = new DriftMonitor(baseline16(), 16, 4);
        for (int i = 0; i < 15; i++) {
            assertTrue("no PSI before the window fills",
                    Double.isNaN(mon.onSignal("T", i)));
        }
        // 16th observation fills the window with exactly the baseline
        // sample -> identical fractions -> PSI == 0 exactly
        assertEquals(0.0, mon.onSignal("T", 15), 0.0);
        assertEquals(0.0, mon.psi("T"), 0.0);
    }

    @Test
    public void recomputesOnTheCadenceAndSeesDrift() {
        DriftMonitor mon = new DriftMonitor(baseline16(), 16, 4);
        for (int i = 0; i < 16; i++) {
            mon.onSignal("T", i);
        }
        assertEquals(0.0, mon.psi("T"), 0.0);
        // shift the live distribution far above the baseline; the next
        // everyN-1 observations reuse the cached PSI, the everyN-th
        // recomputes and must see significant drift (> 0.25)
        assertEquals(0.0, mon.onSignal("T", 1000.0), 0.0);
        assertEquals(0.0, mon.onSignal("T", 1000.0), 0.0);
        assertEquals(0.0, mon.onSignal("T", 1000.0), 0.0);
        double psi = mon.onSignal("T", 1000.0);
        assertTrue("recomputed on the 4th observation: " + psi, psi > 0.25);

        // and the recomputed value equals the pinned formula over the
        // current window (12 baseline-like values + 4 shifted)
        BaselineLoader.Baseline b = baseline16().get("T");
        double[] window = new double[16];
        for (int i = 0; i < 12; i++) {
            window[i] = 4 + i; // values 4..15 survive; 0..3 evicted
        }
        for (int i = 12; i < 16; i++) {
            window[i] = 1000.0;
        }
        assertEquals(Psi.psi(b.fractions(),
                Psi.fractions(window, b.edges())), psi, 1e-15);
    }

    @Test
    public void alphaWithoutBaselineStaysUnarmed() {
        DriftMonitor mon = new DriftMonitor(baseline16(), 16, 4);
        assertTrue(mon.armed("T"));
        assertTrue(!mon.armed("EQ99"));
        for (int i = 0; i < 100; i++) {
            assertTrue(Double.isNaN(mon.onSignal("EQ99", i)));
        }
        assertTrue(Double.isNaN(mon.psi("EQ99")));
        assertTrue("unknown alpha reads NaN", Double.isNaN(mon.psi("ZZ")));
    }
}
