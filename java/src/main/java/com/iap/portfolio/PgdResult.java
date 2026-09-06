package com.iap.portfolio;

/**
 * Solver output (API_PORTFOLIO_TCA.md §1.3): the best feasible iterate,
 * its objective, the (1-based) iteration it came from (0 = the initial
 * projection of w_prev), its residual constraint violation and the
 * {@code feasible} flag. When NO iterate satisfies {@code feas_tol} the
 * problem is INFEASIBLE (pinned): {@code weights == w_prev} (hold the
 * book), {@code objective = f(w_prev)}, {@code bestIteration = 0},
 * {@code maxViolation} = the violation of w_prev, {@code feasible =
 * false}. Weights are never NaN/inf and the objective is never -inf; a
 * caller must check {@code feasible} before acting.
 */
public record PgdResult(double[] weights, double objective, int iterations,
        int bestIteration, double maxViolation, boolean feasible) {
    /** "OPTIMAL" or "INFEASIBLE" (audit wording, pinned). */
    public String status() {
        return feasible ? "OPTIMAL" : "INFEASIBLE";
    }
}
