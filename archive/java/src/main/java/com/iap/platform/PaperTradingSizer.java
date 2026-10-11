package com.iap.platform;

import java.util.List;
import java.util.Map;
import java.util.TreeMap;

import com.iap.portfolio.Constraints;
import com.iap.portfolio.EwmaCovariance;
import com.iap.portfolio.PgdResult;
import com.iap.portfolio.PortfolioOptimizer;
import com.iap.portfolio.SolverParams;

/**
 * Portfolio sizing of {@link PaperTrading}: the pinned single-asset problem
 * (PLATFORM_CONVENTIONS.md §12, "Portfolio sizing") and its content hash.
 * Package-private: split out of PaperTrading (v1.13 E4) without behaviour change.
 */
final class PaperTradingSizer {
    private PaperTradingSizer() {
    }

    /**
     * Single-asset portfolio solve: |weight| in [0, 1] from EWMA variance.
     * An infeasible problem (API_PORTFOLIO_TCA.md §1.3) holds the previous
     * weight scale: the optimizer returns w_prev with {@code feasible ==
     * false} and the caller must not act on it.
     */
    static PgdResult solveWeight(List<Double> barReturns,
            double expectedReturn, double positionWeight) {
        double[][] rets = new double[barReturns.size()][1];
        for (int i = 0; i < barReturns.size(); i++) {
            rets[i][0] = barReturns.get(i);
        }
        double[][] sigma = EwmaCovariance.estimate(rets);
        Constraints cons = new Constraints(new double[] {-1.0},
                new double[] {1.0});
        cons.participation = new double[] {SIZING_PARTICIPATION};
        cons.volTarget = SIZING_VOL_TARGET;
        return PortfolioOptimizer.solve(
                new double[] {expectedReturn}, sigma,
                new double[] {positionWeight}, SIZING_RISK_AVERSION,
                new double[] {SIZING_COST_BPS}, cons,
                new SolverParams(null, SIZING_STEP_DECAY, SIZING_ITERS,
                        SIZING_PROJ_PASSES, SIZING_FEAS_TOL));
    }

    // The pinned single-asset sizing problem (PLATFORM_CONVENTIONS.md §12,
    // "Portfolio sizing"); its content hash is the traces' portfolio_version.
    static final double SIZING_PARTICIPATION = 0.5;
    static final double SIZING_VOL_TARGET = 5e-4; // per-bar vol target
    static final double SIZING_RISK_AVERSION = 6.0;
    static final double SIZING_COST_BPS = 1e-4;
    // Named after the SolverParams components they feed (eta0 is auto):
    // stepDecay, iters, projPasses — not a step size / patience.
    static final double SIZING_STEP_DECAY = 0.01;
    static final int SIZING_ITERS = 100;
    static final int SIZING_PROJ_PASSES = 4;
    static final double SIZING_FEAS_TOL = 1e-7;

    /** {@code portfolio_version}: content hash of the sizing problem (constraints + solver). */
    static String sizingProblemVersion(long maxPos) {
        Map<String, Object> problem = new TreeMap<>();
        problem.put("box", List.of(-1.0, 1.0));
        problem.put("participation", SIZING_PARTICIPATION);
        problem.put("vol_target", SIZING_VOL_TARGET);
        problem.put("risk_aversion", SIZING_RISK_AVERSION);
        problem.put("cost_bps", SIZING_COST_BPS);
        problem.put("step_decay", SIZING_STEP_DECAY);
        problem.put("iters", (long) SIZING_ITERS);
        problem.put("proj_passes", (long) SIZING_PROJ_PASSES);
        problem.put("feas_tol", SIZING_FEAS_TOL);
        problem.put("covariance", "ewma_1min_bars");
        problem.put("max_pos", maxPos);
        return com.iap.contracts.CanonicalJson.contentHash(problem);
    }
}
