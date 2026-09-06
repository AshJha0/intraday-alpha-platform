package com.iap.alpha;

/**
 * FX05 cross-pair relative value — currency-exposure machinery (API_ALPHA.md
 * section 5, pinned):
 *
 * <ul>
 *   <li>currencies sorted {AUD, CAD, CHF, EUR, GBP, JPY, NZD, USD};
 *       numeraire USD dropped from the solve;</li>
 *   <li>pairs (instrument_id): 101 EUR/USD, 102 GBP/USD, 103 USD/JPY,
 *       104 AUD/USD, 105 USD/CAD, 106 USD/CHF, 107 NZD/USD, 108 EUR/GBP;
 *       exposure row = +1 base, -1 quote;</li>
 *   <li>factor solve per grid point: {@code f = pinv(A_free[valid]) @
 *       r[valid]} — minimum-norm least squares matching numpy.linalg.pinv;
 *       residual_i = r_i - (A_free f)_i; raw_i = -residual_i;</li>
 *   <li>fewer than 2 valid pairs: no signal (all raws NaN).</li>
 * </ul>
 *
 * <p>The pseudo-inverse is computed deterministically via a cyclic Jacobi
 * eigendecomposition of {@code A^T A} (7x7 symmetric PSD): with
 * {@code A^T A = V L V^T}, the minimum-norm solution is
 * {@code f = V L+ V^T (A^T r)} where {@code L+} zeroes eigenvalues whose
 * singular value falls below numpy's default cutoff
 * ({@code 1e-15 * max(m, n) * sigma_max}). No RNG anywhere.
 */
public final class Fx05 {
    /** Pinned FX pair universe (instrument_id order 101..108). */
    public static final long[] PAIR_IDS = {101, 102, 103, 104, 105, 106, 107, 108};
    public static final int NUM_PAIRS = 8;
    /** AUD CAD CHF EUR GBP JPY NZD (USD dropped). */
    public static final int NUM_FREE_CCY = 7;
    public static final long GRID_STEP_NS = 30_000_000_000L;
    public static final long MAX_AGE_NS = 120_000_000_000L;

    private static final int AUD = 0;
    private static final int CAD = 1;
    private static final int CHF = 2;
    private static final int EUR = 3;
    private static final int GBP = 4;
    private static final int JPY = 5;
    private static final int NZD = 6;

    private Fx05() {
    }

    /**
     * Exposure matrix restricted to non-numeraire currencies:
     * {@code A[i][j]} = exposure of pair PAIR_IDS[i] to free currency j.
     */
    public static double[][] freeExposureMatrix() {
        double[][] a = new double[NUM_PAIRS][NUM_FREE_CCY];
        a[0][EUR] = 1.0;   // EUR/USD
        a[1][GBP] = 1.0;   // GBP/USD
        a[2][JPY] = -1.0;  // USD/JPY
        a[3][AUD] = 1.0;   // AUD/USD
        a[4][CAD] = -1.0;  // USD/CAD
        a[5][CHF] = -1.0;  // USD/CHF
        a[6][NZD] = 1.0;   // NZD/USD
        a[7][EUR] = 1.0;   // EUR/GBP
        a[7][GBP] = -1.0;
        return a;
    }

    /**
     * Minimum-norm least-squares currency factor solve for one
     * cross-section. {@code returns[i]} is pair PAIR_IDS[i]'s ret_log_1m
     * sample (NaN = missing). {@code factors} (length 7) and {@code fitted}
     * (length 8, A@f per pair) are written; returns false when no valid pair
     * exists (factors/fitted then all NaN).
     */
    public static boolean solveFactors(double[] returns, double[] factors,
            double[] fitted) {
        double[][] aAll = freeExposureMatrix();
        int nValid = 0;
        for (double r : returns) {
            if (Double.isFinite(r)) {
                nValid++;
            }
        }
        if (nValid == 0) {
            java.util.Arrays.fill(factors, Double.NaN);
            java.util.Arrays.fill(fitted, Double.NaN);
            return false;
        }
        // Build the valid-row system: gram = A^T A, aty = A^T r.
        double[][] gram = new double[NUM_FREE_CCY][NUM_FREE_CCY];
        double[] aty = new double[NUM_FREE_CCY];
        for (int i = 0; i < NUM_PAIRS; i++) {
            if (!Double.isFinite(returns[i])) {
                continue;
            }
            for (int j = 0; j < NUM_FREE_CCY; j++) {
                aty[j] += aAll[i][j] * returns[i];
                for (int k = 0; k < NUM_FREE_CCY; k++) {
                    gram[j][k] += aAll[i][j] * aAll[i][k];
                }
            }
        }
        // Jacobi eigendecomposition of the symmetric PSD gram matrix.
        double[] eig = new double[NUM_FREE_CCY];
        double[][] v = new double[NUM_FREE_CCY][NUM_FREE_CCY];
        jacobiEigen(gram, eig, v);
        // numpy pinv cutoff: sigma > 1e-15 * max(m, n) * sigma_max.
        double sigMax = 0.0;
        for (double e : eig) {
            sigMax = Math.max(sigMax, Math.sqrt(Math.max(e, 0.0)));
        }
        double cutoff = 1e-15 * Math.max(nValid, NUM_FREE_CCY) * sigMax;
        // f = V L+ V^T aty
        double[] vty = new double[NUM_FREE_CCY];
        for (int j = 0; j < NUM_FREE_CCY; j++) {
            double s = 0.0;
            for (int k = 0; k < NUM_FREE_CCY; k++) {
                s += v[k][j] * aty[k];
            }
            double sig = Math.sqrt(Math.max(eig[j], 0.0));
            vty[j] = sig > cutoff ? s / eig[j] : 0.0;
        }
        for (int j = 0; j < NUM_FREE_CCY; j++) {
            double s = 0.0;
            for (int k = 0; k < NUM_FREE_CCY; k++) {
                s += v[j][k] * vty[k];
            }
            factors[j] = s;
        }
        for (int i = 0; i < NUM_PAIRS; i++) {
            double s = 0.0;
            for (int j = 0; j < NUM_FREE_CCY; j++) {
                s += aAll[i][j] * factors[j];
            }
            fitted[i] = s;
        }
        return true;
    }

    /**
     * Which observable pairs carry IDENTIFIABLE relative-value information
     * (pinned, API_ALPHA.md section 5): a pair is identified iff every FREE
     * currency it touches appears in at least two observable pairs. A
     * currency seen in a single observable pair has its factor absorb that
     * pair's whole return, so the residual is 0 by construction — a
     * constant, not a signal. On this universe AUD, CAD, CHF, JPY and NZD
     * each appear in one pair, leaving the EUR/USD-GBP/USD-EUR/GBP triangle.
     */
    public static boolean[] identifiedPairs(boolean[] observable) {
        double[][] a = freeExposureMatrix();
        int[] counts = new int[NUM_FREE_CCY];
        for (int i = 0; i < NUM_PAIRS; i++) {
            if (!observable[i]) {
                continue;
            }
            for (int j = 0; j < NUM_FREE_CCY; j++) {
                if (a[i][j] != 0.0) {
                    counts[j]++;
                }
            }
        }
        boolean[] out = new boolean[NUM_PAIRS];
        for (int i = 0; i < NUM_PAIRS; i++) {
            if (!observable[i]) {
                continue;
            }
            boolean ok = true;
            for (int j = 0; j < NUM_FREE_CCY; j++) {
                if (a[i][j] != 0.0 && counts[j] < 2) {
                    ok = false;
                    break;
                }
            }
            out[i] = ok;
        }
        return out;
    }

    /**
     * FX05 raw signals for one grid cross-section:
     * {@code raw_i = -(r_i - fitted_i)} for IDENTIFIED pairs, NaN otherwise
     * (missing return, fewer than 2 valid pairs, or no identifiable
     * relative value).
     */
    public static double[] rawSignals(double[] returns) {
        double[] raw = new double[NUM_PAIRS];
        java.util.Arrays.fill(raw, Double.NaN);
        boolean[] observable = new boolean[NUM_PAIRS];
        int nValid = 0;
        for (int i = 0; i < NUM_PAIRS; i++) {
            observable[i] = Double.isFinite(returns[i]);
            if (observable[i]) {
                nValid++;
            }
        }
        if (nValid < 2) {
            return raw; // no cross-pair information
        }
        boolean[] identified = identifiedPairs(observable);
        double[] factors = new double[NUM_FREE_CCY];
        double[] fitted = new double[NUM_PAIRS];
        solveFactors(returns, factors, fitted);
        for (int i = 0; i < NUM_PAIRS; i++) {
            if (identified[i]) {
                raw[i] = -(returns[i] - fitted[i]);
            }
        }
        return raw;
    }

    /**
     * Cyclic Jacobi eigendecomposition of a symmetric matrix (in-place on a
     * copy): {@code m = V diag(eig) V^T} with V's columns the eigenvectors.
     * Deterministic sweep order; converges far below 1e-12 for these tiny
     * exposure systems.
     */
    private static void jacobiEigen(double[][] m, double[] eig, double[][] v) {
        int n = m.length;
        double[][] a = new double[n][];
        for (int i = 0; i < n; i++) {
            a[i] = m[i].clone();
            for (int j = 0; j < n; j++) {
                v[i][j] = i == j ? 1.0 : 0.0;
            }
        }
        for (int sweep = 0; sweep < 100; sweep++) {
            double off = 0.0;
            for (int p = 0; p < n; p++) {
                for (int q = p + 1; q < n; q++) {
                    off += a[p][q] * a[p][q];
                }
            }
            if (off <= 1e-300) {
                break;
            }
            for (int p = 0; p < n; p++) {
                for (int q = p + 1; q < n; q++) {
                    double apq = a[p][q];
                    if (apq == 0.0) {
                        continue;
                    }
                    double theta = (a[q][q] - a[p][p]) / (2.0 * apq);
                    double t = Math.signum(theta) == 0.0
                            ? 1.0
                            : Math.signum(theta)
                                    / (Math.abs(theta)
                                            + Math.sqrt(theta * theta + 1.0));
                    if (theta == 0.0) {
                        t = 1.0;
                    }
                    double c = 1.0 / Math.sqrt(t * t + 1.0);
                    double s = t * c;
                    for (int k = 0; k < n; k++) {
                        double akp = a[k][p];
                        double akq = a[k][q];
                        a[k][p] = c * akp - s * akq;
                        a[k][q] = s * akp + c * akq;
                    }
                    for (int k = 0; k < n; k++) {
                        double apk = a[p][k];
                        double aqk = a[q][k];
                        a[p][k] = c * apk - s * aqk;
                        a[q][k] = s * apk + c * aqk;
                    }
                    for (int k = 0; k < n; k++) {
                        double vkp = v[k][p];
                        double vkq = v[k][q];
                        v[k][p] = c * vkp - s * vkq;
                        v[k][q] = s * vkp + c * vkq;
                    }
                }
            }
        }
        for (int i = 0; i < n; i++) {
            eig[i] = a[i][i];
        }
    }
}
