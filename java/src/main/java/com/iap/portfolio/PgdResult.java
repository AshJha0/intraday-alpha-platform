package com.iap.portfolio;

/**
 * Solver output: the best feasible iterate, its objective, the (1-based)
 * iteration it came from (0 = the initial projection of w_prev), and its
 * residual constraint violation.
 */
public record PgdResult(double[] weights, double objective, int iterations,
        int bestIteration, double maxViolation) {
}
