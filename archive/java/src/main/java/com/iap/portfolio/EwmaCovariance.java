package com.iap.portfolio;

/**
 * RiskMetrics EWMA covariance (API_PORTFOLIO_TCA.md §1.5): pinned decay
 * {@code lam = 0.94}, initialized with the ddof=0 sample covariance of the
 * first {@code init_window = 20} return rows, then
 * {@code S_t = lam*S_(t-1) + (1-lam) r_t r_t'}, symmetrized, plus ridge
 * {@code 1e-6 * trace(S)/n} on the diagonal. Deterministic: fixed loops,
 * no unordered iteration. Bars are built by the caller as 1-minute
 * last-observation mids ({@code floor(ts / 60e9) * 60e9} buckets); rows of
 * {@code returns} are log returns between consecutive common bars.
 */
public final class EwmaCovariance {
    /** Pinned 1-minute bar width in nanoseconds. */
    public static final long BAR_NS = 60_000_000_000L;

    private EwmaCovariance() {
    }

    /** Pinned-defaults estimate (lam 0.94, init 20, ridge 1e-6). */
    public static double[][] estimate(double[][] returns) {
        return estimate(returns, 0.94, 20, 1e-6);
    }

    /** EWMA covariance of a (T, N) return matrix (per-bar units). */
    public static double[][] estimate(double[][] returns, double lam,
            int initWindow, double ridge) {
        int t = returns.length;
        if (t < 2) {
            throw new IllegalArgumentException("need at least 2 return rows");
        }
        int n = returns[0].length;
        for (double[] row : returns) {
            if (row.length != n) {
                throw new IllegalArgumentException("ragged return matrix");
            }
        }
        if (!(lam > 0.0 && lam < 1.0)) {
            throw new IllegalArgumentException("lam must be in (0, 1)");
        }
        int w0 = Math.min(Math.max(initWindow, 2), t);
        // ddof=0 sample covariance of the first w0 rows
        double[] mean = new double[n];
        for (int r = 0; r < w0; r++) {
            for (int j = 0; j < n; j++) {
                mean[j] += returns[r][j];
            }
        }
        for (int j = 0; j < n; j++) {
            mean[j] /= w0;
        }
        double[][] s = new double[n][n];
        for (int r = 0; r < w0; r++) {
            for (int i = 0; i < n; i++) {
                double di = returns[r][i] - mean[i];
                for (int j = 0; j < n; j++) {
                    s[i][j] += di * (returns[r][j] - mean[j]);
                }
            }
        }
        for (int i = 0; i < n; i++) {
            for (int j = 0; j < n; j++) {
                s[i][j] /= w0;
            }
        }
        // RiskMetrics recursion over the remaining rows
        for (int r = w0; r < t; r++) {
            double[] row = returns[r];
            for (int i = 0; i < n; i++) {
                for (int j = 0; j < n; j++) {
                    s[i][j] = lam * s[i][j] + (1.0 - lam) * row[i] * row[j];
                }
            }
        }
        // symmetrize + ridge
        for (int i = 0; i < n; i++) {
            for (int j = i + 1; j < n; j++) {
                double v = 0.5 * (s[i][j] + s[j][i]);
                s[i][j] = v;
                s[j][i] = v;
            }
        }
        double trace = 0.0;
        for (int i = 0; i < n; i++) {
            trace += s[i][i];
        }
        double bump = ridge * (trace > 0.0 ? trace / n : 1.0);
        for (int i = 0; i < n; i++) {
            s[i][i] += bump;
        }
        return s;
    }
}
