package com.iap;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import java.util.List;
import java.util.Map;

import org.junit.Test;

import com.iap.portfolio.ConstraintAudit;
import com.iap.portfolio.Constraints;
import com.iap.portfolio.PgdResult;
import com.iap.portfolio.PortfolioOptimizer;
import com.iap.portfolio.SolverParams;

/**
 * Golden parity of the production optimizer with
 * tests/golden/expected_portfolio.json (1e-9), determinism, feasibility /
 * optimality (KKT-style projected-candidate) checks and the constraint
 * audit of the golden solution.
 */
public class PortfolioGoldenTest {
    static final class Problem {
        final double[] alpha;
        final double[][] sigma;
        final double[] wPrev;
        final double riskAversion;
        final double[] tc;
        final Constraints cons;
        final SolverParams params;

        Problem(Map<String, Object> p) {
            alpha = vec(p.get("alpha"));
            sigma = mat(p.get("sigma"));
            wPrev = vec(p.get("w_prev"));
            riskAversion = Json.asDouble(p.get("risk_aversion"));
            tc = vec(p.get("tc_linear"));
            Map<String, Object> c = Json.object(p.get("constraints"));
            cons = new Constraints(vec(c.get("w_min")), vec(c.get("w_max")));
            cons.grossCap = Json.asDouble(c.get("gross_cap"));
            cons.netCap = Json.asDouble(c.get("net_cap"));
            cons.participation = vec(c.get("participation"));
            cons.turnoverCap = Json.asDouble(c.get("turnover_cap"));
            cons.volTarget = Json.asDouble(c.get("vol_target"));
            cons.currencyMatrix = mat(p.get("currency_matrix"));
            cons.currencyBounds = vec(c.get("currency_bounds"));
            Map<String, Object> s = Json.object(p.get("solver"));
            params = new SolverParams(Json.asDouble(s.get("eta0")),
                    Json.asDouble(s.get("step_decay")),
                    (int) Json.asLong(s.get("iters")),
                    (int) Json.asLong(s.get("proj_passes")), 1e-7);
        }

        PgdResult solve() {
            return PortfolioOptimizer.solve(alpha, sigma, wPrev, riskAversion,
                    tc, cons, params);
        }

        double f(double[] w) {
            return PortfolioOptimizer.objective(w, alpha, sigma, wPrev,
                    riskAversion, tc);
        }
    }

    static double[] vec(Object v) {
        List<Object> a = Json.array(v);
        double[] out = new double[a.size()];
        for (int i = 0; i < out.length; i++) {
            out[i] = Json.asDouble(a.get(i));
        }
        return out;
    }

    static double[][] mat(Object v) {
        List<Object> rows = Json.array(v);
        double[][] out = new double[rows.size()][];
        for (int i = 0; i < out.length; i++) {
            out[i] = vec(rows.get(i));
        }
        return out;
    }

    private static Map<String, Object> golden() {
        return Golden.json("expected_portfolio.json");
    }

    @Test
    public void goldenWeightsObjectiveAndIterationMatch() {
        Map<String, Object> g = golden();
        Problem p = new Problem(Json.object(g.get("problem")));
        Map<String, Object> exp = Json.object(g.get("expected"));
        double tol = Json.asDouble(exp.get("tolerance"));
        PgdResult r = p.solve();
        double[] want = vec(exp.get("weights"));
        for (int i = 0; i < want.length; i++) {
            assertEquals("weight " + i, want[i], r.weights()[i], tol);
        }
        assertEquals("objective", Json.asDouble(exp.get("objective")),
                r.objective(), tol);
        assertEquals("best_iteration", Json.asLong(exp.get("best_iteration")),
                r.bestIteration());
        assertEquals("max_violation", Json.asDouble(exp.get("max_violation")),
                r.maxViolation(), tol);
    }

    @Test
    public void solveIsBitDeterministic() {
        Problem p = new Problem(Json.object(golden().get("problem")));
        PgdResult a = p.solve();
        PgdResult b = p.solve();
        assertArrayEquals(a.weights(), b.weights(), 0.0);
        assertEquals(a.objective(), b.objective(), 0.0);
        assertEquals(a.bestIteration(), b.bestIteration());
    }

    @Test
    public void goldenSolutionIsFeasible() {
        Map<String, Object> g = golden();
        Problem p = new Problem(Json.object(g.get("problem")));
        double[] w = vec(Json.object(g.get("expected")).get("weights"));
        assertTrue("expected weights feasible",
                PortfolioOptimizer.maxViolation(w, p.cons, p.wPrev, p.sigma)
                        <= 1e-9);
    }

    @Test
    public void projectedCandidateSweepFindsNoBetterPoint() {
        // KKT-style local optimality: projecting perturbations of the
        // solution in every coordinate direction (and the gradient
        // direction) never improves the objective materially.
        Problem p = new Problem(Json.object(golden().get("problem")));
        PgdResult r = p.solve();
        double fBest = r.objective();
        int n = p.alpha.length;
        double[][] dirs = new double[2 * n + 2][n];
        for (int i = 0; i < n; i++) {
            dirs[2 * i][i] = 1.0;
            dirs[2 * i + 1][i] = -1.0;
        }
        double[] sw = new double[n];
        for (int i = 0; i < n; i++) {
            double s = 0.0;
            for (int j = 0; j < n; j++) {
                s += p.sigma[i][j] * r.weights()[j];
            }
            sw[i] = s;
        }
        for (int i = 0; i < n; i++) {
            double gi = p.alpha[i] - 2.0 * p.riskAversion * sw[i];
            dirs[2 * n][i] = gi;
            dirs[2 * n + 1][i] = -gi;
        }
        for (double step : new double[] {1e-4, 1e-3, 1e-2}) {
            for (double[] d : dirs) {
                double[] cand = r.weights().clone();
                for (int i = 0; i < n; i++) {
                    cand[i] += step * d[i];
                }
                cand = PortfolioOptimizer.project(cand, p.cons, p.wPrev,
                        p.sigma, p.params.projPasses());
                if (PortfolioOptimizer.maxViolation(cand, p.cons, p.wPrev,
                        p.sigma) <= 1e-7) {
                    assertTrue("candidate beats solve by "
                            + (p.f(cand) - fBest),
                            p.f(cand) <= fBest + 1e-6);
                }
            }
        }
    }

    @Test
    public void goldenAuditReportsBindingConstraints() {
        Map<String, Object> g = golden();
        Problem p = new Problem(Json.object(g.get("problem")));
        double[] w = p.solve().weights();
        ConstraintAudit.Report rep =
                ConstraintAudit.audit(w, p.cons, p.wPrev, p.sigma);
        // aggregates are consistent with the weights
        double gross = 0.0;
        double net = 0.0;
        double turnover = 0.0;
        for (int i = 0; i < w.length; i++) {
            gross += Math.abs(w[i]);
            net += w[i];
            turnover += Math.abs(w[i] - p.wPrev[i]);
        }
        assertEquals(gross, rep.gross(), 1e-12);
        assertEquals(net, rep.net(), 1e-12);
        assertEquals(turnover, rep.turnover(), 1e-12);
        assertEquals(p.cons.volTarget, rep.targetVol());
        assertTrue("realized vol <= target + tol",
                rep.realizedVol() <= p.cons.volTarget + 1e-9);
        // aggregate rows always present: net, gross, turnover, 8 currency,
        // vol = 12 at least
        assertTrue("audit rows", rep.constraints().size() >= 12);
        // every reported row satisfies binding == slack <= 1e-6
        for (ConstraintAudit.Row row : rep.constraints()) {
            assertEquals(row.name(), row.binding(), row.slack() <= 1e-6);
            assertEquals(row.name(), row.slack(), row.bound() - row.value(), 0.0);
        }
        assertTrue("some constraint binds at the optimum", rep.nBinding() >= 1);
        // report JSON parses
        Map<String, Object> parsed = Json.object(Json.parse(rep.toJson()));
        assertEquals((long) rep.nBinding(), Json.asLong(parsed.get("n_binding")));
    }
}
