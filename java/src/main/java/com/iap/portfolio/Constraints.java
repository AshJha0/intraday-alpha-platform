package com.iap.portfolio;

/**
 * Constraint set for the production portfolio optimizer
 * (API_PORTFOLIO_TCA.md §1.2). Any member other than the mandatory position
 * box may be {@code null} (inactive). Instances are plain data; call
 * {@link #validate} before use (the solver does).
 */
public final class Constraints {
    /** Position box lower bound (length n, mandatory). */
    public double[] wMin;
    /** Position box upper bound (length n, mandatory). */
    public double[] wMax;
    /** Gross-exposure L1 cap ({@code sum |w_i| <= gross_cap}). */
    public Double grossCap;
    /** Net-exposure cap ({@code |sum w_i| <= net_cap}). */
    public Double netCap;
    /** Per-asset participation (trade box) cap {@code |w - w_prev|}. */
    public double[] participation;
    /** Turnover L1 cap ({@code sum |w_i - w_prev_i| <= turnover_cap}). */
    public Double turnoverCap;
    /** Volatility target ({@code sqrt(w' Sigma w) <= vol_target}). */
    public Double volTarget;
    /** Currency exposure matrix E (currencies x assets, sorted rows). */
    public double[][] currencyMatrix;
    /** Per-currency exposure bounds ({@code |E w| <= bounds}). */
    public double[] currencyBounds;

    public Constraints(double[] wMin, double[] wMax) {
        this.wMin = wMin;
        this.wMax = wMax;
    }

    /**
     * Validate shapes, finiteness and signs against dimension n (mirrors
     * the reference {@code iap.portfolio.optimizer.Constraints.validate}).
     *
     * <p>CROSS-LANGUAGE DIVERGENCE this closes: the sign tests below are all
     * false for NaN ({@code NaN > x}, {@code NaN < 0} and {@code NaN <= 0}
     * are each false), so a NaN bound or cap used to pass validation here
     * while the reference rejected it, then surfaced as a NaN
     * {@code PgdResult.maxViolation()} — a book frozen mid-session instead
     * of a loud failure at config load. Finiteness is therefore checked
     * FIRST for every array and scalar.
     */
    public void validate(int n) {
        if (wMin == null || wMax == null || wMin.length != n || wMax.length != n) {
            throw new IllegalArgumentException("w_min/w_max must have length n");
        }
        for (int i = 0; i < n; i++) {
            if (!Double.isFinite(wMin[i]) || !Double.isFinite(wMax[i])) {
                throw new IllegalArgumentException("w_min/w_max must be finite");
            }
        }
        for (int i = 0; i < n; i++) {
            if (wMin[i] > wMax[i]) {
                throw new IllegalArgumentException("w_min > w_max for asset " + i);
            }
        }
        requireFinite(grossCap, "gross_cap");
        requireFinite(netCap, "net_cap");
        requireFinite(turnoverCap, "turnover_cap");
        requireFinite(volTarget, "vol_target");
        if (grossCap != null && grossCap <= 0.0) {
            throw new IllegalArgumentException("gross_cap must be > 0");
        }
        if (netCap != null && netCap < 0.0) {
            throw new IllegalArgumentException("net_cap must be >= 0");
        }
        if (participation != null) {
            for (double p : participation) {
                if (!Double.isFinite(p)) {
                    throw new IllegalArgumentException("participation must be finite");
                }
            }
            if (participation.length != n) {
                throw new IllegalArgumentException("participation must have length n");
            }
            for (double p : participation) {
                if (p < 0.0) {
                    throw new IllegalArgumentException("participation must be >= 0");
                }
            }
        }
        if (turnoverCap != null && turnoverCap < 0.0) {
            throw new IllegalArgumentException("turnover_cap must be >= 0");
        }
        if (volTarget != null && volTarget <= 0.0) {
            throw new IllegalArgumentException("vol_target must be > 0");
        }
        if ((currencyMatrix == null) != (currencyBounds == null)) {
            throw new IllegalArgumentException(
                    "currency_matrix and currency_bounds go together");
        }
        if (currencyMatrix != null) {
            for (double[] row : currencyMatrix) {
                if (row.length != n) {
                    throw new IllegalArgumentException(
                            "currency_matrix must have n columns");
                }
            }
            if (currencyBounds.length != currencyMatrix.length) {
                throw new IllegalArgumentException("currency_bounds shape mismatch");
            }
            for (double[] row : currencyMatrix) {
                for (double x : row) {
                    if (!Double.isFinite(x)) {
                        throw new IllegalArgumentException(
                                "currency_matrix/currency_bounds must be finite");
                    }
                }
            }
            for (double b : currencyBounds) {
                if (!Double.isFinite(b)) {
                    throw new IllegalArgumentException(
                            "currency_matrix/currency_bounds must be finite");
                }
            }
            for (double b : currencyBounds) {
                if (b < 0.0) {
                    throw new IllegalArgumentException("currency_bounds must be >= 0");
                }
            }
        }
    }

    private static void requireFinite(Double value, String name) {
        if (value != null && !Double.isFinite(value)) {
            throw new IllegalArgumentException(name + " must be finite");
        }
    }
}
