package com.iap.portfolio;

import java.util.List;

/**
 * Solver output (API_PORTFOLIO_TCA.md §1.3): the best feasible iterate,
 * its objective, the (1-based) iteration it came from (0 = the initial
 * projection of w_prev, -1 = w_prev itself was held), its residual
 * constraint violation and the {@code feasible} flag. When NO iterate
 * satisfies {@code feas_tol} the problem is INFEASIBLE (pinned) and the
 * result carries the LEAST-VIOLATING candidate — w_prev, the initial
 * projection or any iterate — ranked by (risk violation, total violation,
 * candidate order). {@code violations} names the residual breaches in the
 * pinned order and {@code riskViolation} is the number to alarm on: RISK
 * constraints (box, net, currency, gross, vol) bound the BOOK, TRADING
 * constraints (participation, turnover) only bound the TRADE. Weights are
 * never NaN/inf and the objective is never -inf; a caller must check
 * {@code feasible} before acting.
 */
public record PgdResult(double[] weights, double objective, int iterations,
        int bestIteration, double maxViolation, boolean feasible,
        List<String> violations, double riskViolation) {
    /** "OPTIMAL" or "INFEASIBLE" (audit wording, pinned). */
    public String status() {
        return feasible ? "OPTIMAL" : "INFEASIBLE";
    }

    /**
     * "NONE" / "RISK" / "TRADING" / "RISK_AND_TRADING" — the alarm class of
     * an INFEASIBLE result. RISK means the book is over-exposed right now;
     * TRADING means only that the step exceeds a participation/turnover cap
     * and the execution layer must slice it.
     */
    public String violationKind() {
        boolean risk = false;
        boolean trading = false;
        for (String name : violations) {
            if (PortfolioOptimizer.RISK_CONSTRAINTS.contains(name)) {
                risk = true;
            } else if (PortfolioOptimizer.TRADING_CONSTRAINTS.contains(name)) {
                trading = true;
            }
        }
        if (risk && trading) {
            return "RISK_AND_TRADING";
        }
        if (risk) {
            return "RISK";
        }
        return trading ? "TRADING" : "NONE";
    }
}
