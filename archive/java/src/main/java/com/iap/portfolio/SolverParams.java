package com.iap.portfolio;

/**
 * Pinned solver parameters (API_PORTFOLIO_TCA.md §1.5) — part of the wire
 * contract: a request either pins them or gets these defaults.
 * {@code eta0 == null} selects the auto step
 * {@code 1 / max(2*lambda*maxRowSum(|Sigma|), 1e-6)}.
 */
public record SolverParams(Double eta0, double stepDecay, int iters,
        int projPasses, double feasTol) {
    /** The pinned defaults (eta0 auto, decay 0.01, 500 iters, 8 passes). */
    public static SolverParams defaults() {
        return new SolverParams(null, 0.01, 500, 8, 1e-7);
    }
}
