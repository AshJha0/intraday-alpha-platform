package com.iap.adaptive;

import java.util.Arrays;

/**
 * Pinned Population Stability Index (PSI) formula shared with the Python
 * research layer (API_ADAPTIVE.md): 10 quantile buckets whose edges are the
 * BASELINE distribution's quantiles 0.1..0.9 (linear-interpolation
 * quantiles, the numpy default), live fraction vs baseline fraction per
 * bucket, and
 *
 * <pre>PSI = sum_i (live_i - base_i) * ln(live_i / base_i)</pre>
 *
 * with every fraction floored at {@link #EPS} before the formula so empty
 * buckets never produce infinities. PSI is &gt;= 0; the industry rule of
 * thumb reads &lt; 0.1 as stable, 0.1&ndash;0.25 as moderate shift and
 * &gt; 0.25 as significant shift (the alerting threshold).
 */
public final class Psi {
    /** Number of buckets (bucket edges = baseline quantiles 0.1..0.9). */
    public static final int BUCKETS = 10;

    /** Floor applied to every fraction before the log-ratio. */
    public static final double EPS = 1e-6;

    private Psi() {
    }

    /**
     * Linear-interpolation quantile of a SORTED sample (numpy's default
     * method: index {@code h = (n-1)p}, interpolate between the neighbors).
     */
    public static double quantileSorted(double[] sorted, double p) {
        if (sorted.length == 0) {
            throw new IllegalArgumentException("quantile of empty sample");
        }
        if (!(p >= 0.0 && p <= 1.0)) {
            throw new IllegalArgumentException("p must be in [0, 1]: " + p);
        }
        double h = (sorted.length - 1) * p;
        int lo = (int) Math.floor(h);
        int hi = (int) Math.ceil(h);
        return sorted[lo] + (h - lo) * (sorted[hi] - sorted[lo]);
    }

    /**
     * The 9 pinned bucket edges (baseline quantiles 0.1..0.9) of a baseline
     * sample (need not be sorted; the input is not modified).
     */
    public static double[] edges(double[] baseline) {
        double[] sorted = baseline.clone();
        Arrays.sort(sorted);
        double[] e = new double[BUCKETS - 1];
        for (int i = 1; i < BUCKETS; i++) {
            e[i - 1] = quantileSorted(sorted, i / (double) BUCKETS);
        }
        return e;
    }

    /**
     * Bucket index of a value: the first {@code i} with
     * {@code v <= edges[i]}, else the last bucket (values above the 0.9
     * quantile).
     */
    public static int bucketOf(double v, double[] edges) {
        for (int i = 0; i < edges.length; i++) {
            if (v <= edges[i]) {
                return i;
            }
        }
        return edges.length;
    }

    /**
     * Per-bucket fractions of {@code count} values starting at {@code from}
     * (a ring-buffer-friendly view over {@code values}, wrapping at the
     * array length).
     */
    public static double[] fractions(double[] values, int from, int count,
            double[] edges) {
        if (edges.length != BUCKETS - 1) {
            throw new IllegalArgumentException("expected " + (BUCKETS - 1)
                    + " edges, got " + edges.length);
        }
        if (count <= 0) {
            throw new IllegalArgumentException("fractions of empty sample");
        }
        double[] f = new double[BUCKETS];
        for (int i = 0; i < count; i++) {
            f[bucketOf(values[(from + i) % values.length], edges)]++;
        }
        for (int i = 0; i < BUCKETS; i++) {
            f[i] /= count;
        }
        return f;
    }

    /** Convenience: fractions over a whole array. */
    public static double[] fractions(double[] values, double[] edges) {
        return fractions(values, 0, values.length, edges);
    }

    /**
     * The pinned PSI of live fractions vs baseline fractions (both floored
     * at {@link #EPS} first).
     */
    public static double psi(double[] baseFractions, double[] liveFractions) {
        if (baseFractions.length != liveFractions.length) {
            throw new IllegalArgumentException("fraction lengths differ");
        }
        double sum = 0.0;
        for (int i = 0; i < baseFractions.length; i++) {
            double b = Math.max(baseFractions[i], EPS);
            double l = Math.max(liveFractions[i], EPS);
            sum += (l - b) * Math.log(l / b);
        }
        return sum;
    }
}
