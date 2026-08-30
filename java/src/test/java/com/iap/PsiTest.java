package com.iap;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import java.util.Arrays;

import org.junit.Test;

import com.iap.adaptive.Psi;
import com.iap.core.SplitMix64;

/**
 * Pinned PSI formula (API_ADAPTIVE.md): baseline-quantile bucket edges
 * (linear interpolation), per-bucket fractions, and
 * {@code sum((live-base) * ln(live/base))} with the 1e-6 fraction floor —
 * hand-computed exact cases plus a brute-force recomputation on random
 * data at 1e-12.
 */
public class PsiTest {
    @Test
    public void handComputedExactCase() {
        // identical fractions: every term is (x-x)*ln(1) = 0 exactly
        double[] base = new double[Psi.BUCKETS];
        Arrays.fill(base, 0.1);
        assertEquals(0.0, Psi.psi(base, base.clone()), 0.0);

        // mass moved from the last bucket to the first:
        // live = [0.2, 0.1 x 8, 0.0]
        //   bucket 0: (0.2 - 0.1) * ln(0.2 / 0.1)
        //   bucket 9: (1e-6 - 0.1) * ln(1e-6 / 0.1)   (live floored at EPS)
        //   others: 0
        double[] live = new double[Psi.BUCKETS];
        Arrays.fill(live, 0.1);
        live[0] = 0.2;
        live[9] = 0.0;
        double want = 0.1 * Math.log(2.0)
                + (Psi.EPS - 0.1) * Math.log(Psi.EPS / 0.1);
        assertEquals(want, Psi.psi(base, live), 0.0);
        assertTrue("empty live bucket stays finite and positive",
                Psi.psi(base, live) > 0.0
                        && Double.isFinite(Psi.psi(base, live)));
    }

    @Test
    public void edgesAreLinearInterpolationQuantilesOfTheBaseline() {
        // 11 evenly spaced values 0..10: quantile p of [0..10] is exactly
        // 10p under linear interpolation, so the edges are 1, 2, ..., 9.
        double[] baseline = new double[11];
        for (int i = 0; i < 11; i++) {
            baseline[i] = 10 - i; // unsorted on purpose (edges() sorts)
        }
        assertArrayEquals(new double[] {1, 2, 3, 4, 5, 6, 7, 8, 9},
                Psi.edges(baseline), 0.0);
        // interpolated case: [0, 1] -> q(0.1) = 0.1
        assertEquals(0.1,
                Psi.quantileSorted(new double[] {0.0, 1.0}, 0.1), 1e-15);
    }

    @Test
    public void bucketAssignmentIsFirstEdgeAtOrAboveValue() {
        double[] edges = {1, 2, 3, 4, 5, 6, 7, 8, 9};
        assertEquals(0, Psi.bucketOf(-100.0, edges));
        assertEquals(0, Psi.bucketOf(1.0, edges)); // at edge -> lower bucket
        assertEquals(1, Psi.bucketOf(1.5, edges));
        assertEquals(9, Psi.bucketOf(9.5, edges));
        assertEquals(9, Psi.bucketOf(Double.MAX_VALUE, edges));
        double[] f = Psi.fractions(new double[] {0.5, 1.0, 1.5, 9.5}, edges);
        assertArrayEquals(new double[] {0.5, 0.25, 0, 0, 0, 0, 0, 0, 0, 0.25},
                f, 0.0);
        try {
            Psi.fractions(new double[] {1.0}, new double[] {1, 2});
            fail("wrong edge count");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("edges"));
        }
    }

    @Test
    public void psiMatchesBruteForceRecomputationOnRandomData() {
        SplitMix64 rng = new SplitMix64(20260830L);
        for (int trial = 0; trial < 20; trial++) {
            int nBase = 200 + (int) rng.below(300);
            int nLive = 50 + (int) rng.below(300);
            double[] base = new double[nBase];
            double[] live = new double[nLive];
            for (int i = 0; i < nBase; i++) {
                base[i] = rng.uniform() * 4.0 - 2.0;
            }
            for (int i = 0; i < nLive; i++) {
                // shifted + scaled so the trials cover drifted regimes too
                live[i] = rng.uniform() * 5.0 - 1.5;
            }

            double[] edges = Psi.edges(base);
            double got = Psi.psi(Psi.fractions(base, edges),
                    Psi.fractions(live, edges));

            // brute force, written independently of the Psi helpers:
            // sort the baseline, take interpolated quantiles, count with
            // plain loops, floor, and sum the pinned formula.
            double[] sorted = base.clone();
            Arrays.sort(sorted);
            double[] edges2 = new double[9];
            for (int q = 1; q <= 9; q++) {
                double h = (sorted.length - 1) * (q / 10.0);
                int lo = (int) h;
                double frac = h - lo;
                edges2[q - 1] = lo + 1 < sorted.length
                        ? sorted[lo] * (1.0 - frac) + sorted[lo + 1] * frac
                        : sorted[lo];
            }
            double want = 0.0;
            for (int b = 0; b < 10; b++) {
                long cb = 0;
                for (double v : base) {
                    if (inBucket(v, b, edges2)) {
                        cb++;
                    }
                }
                long cl = 0;
                for (double v : live) {
                    if (inBucket(v, b, edges2)) {
                        cl++;
                    }
                }
                double fb = Math.max((double) cb / nBase, 1e-6);
                double fl = Math.max((double) cl / nLive, 1e-6);
                want += (fl - fb) * Math.log(fl / fb);
            }
            assertEquals("trial " + trial, want, got, 1e-12);
            assertTrue("PSI is non-negative", got >= 0.0);
        }
    }

    private static boolean inBucket(double v, int b, double[] edges) {
        boolean aboveLower = b == 0 || v > edges[b - 1];
        boolean atOrBelowUpper = b == edges.length || v <= edges[b];
        return aboveLower && atOrBelowUpper;
    }
}
