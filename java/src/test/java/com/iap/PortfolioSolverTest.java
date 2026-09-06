package com.iap;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import java.util.List;

import org.junit.Test;

import com.iap.core.SplitMix64;
import com.iap.portfolio.Constraints;
import com.iap.portfolio.CurrencyExposure;
import com.iap.portfolio.EwmaCovariance;
import com.iap.portfolio.PgdResult;
import com.iap.portfolio.PortfolioOptimizer;
import com.iap.portfolio.SolverParams;

/**
 * Unit-level optimizer checks: exact L1-ball projection vs a brute
 * bisection reference, projection feasibility, input validation, EWMA
 * covariance vs a direct recursion, and the pinned currency matrix.
 */
public class PortfolioSolverTest {
    private static double[] randVec(SplitMix64 rng, int n, double scale) {
        double[] v = new double[n];
        for (int i = 0; i < n; i++) {
            v[i] = (rng.uniform() * 2.0 - 1.0) * scale;
        }
        return v;
    }

    /** Reference L1 projection via bisection on the threshold theta. */
    private static double[] l1Reference(double[] v, double r) {
        double sum = 0.0;
        double hi = 0.0;
        for (double x : v) {
            sum += Math.abs(x);
            hi = Math.max(hi, Math.abs(x));
        }
        if (sum <= r) {
            return v.clone();
        }
        double lo = 0.0;
        for (int it = 0; it < 200; it++) {
            double theta = 0.5 * (lo + hi);
            double s = 0.0;
            for (double x : v) {
                s += Math.max(Math.abs(x) - theta, 0.0);
            }
            if (s > r) {
                lo = theta;
            } else {
                hi = theta;
            }
        }
        double theta = 0.5 * (lo + hi);
        double[] out = new double[v.length];
        for (int i = 0; i < v.length; i++) {
            out[i] = Math.signum(v[i]) * Math.max(Math.abs(v[i]) - theta, 0.0);
        }
        return out;
    }

    @Test
    public void l1ProjectionMatchesBisectionReference() {
        SplitMix64 rng = new SplitMix64(20260829L);
        for (int trial = 0; trial < 50; trial++) {
            int n = 2 + trial % 9;
            double[] v = randVec(rng, n, 2.0);
            double r = 0.1 + rng.uniform() * 2.0;
            double[] got = PortfolioOptimizer.projectL1Ball(v, r);
            double[] want = l1Reference(v, r);
            assertArrayEquals("trial " + trial, want, got, 1e-9);
            double norm = 0.0;
            for (double x : got) {
                norm += Math.abs(x);
            }
            assertTrue("inside ball", norm <= r + 1e-12);
        }
    }

    @Test
    public void l1ProjectionEdgeCases() {
        // inside the ball: unchanged
        double[] v = {0.1, -0.2, 0.05};
        assertArrayEquals(v, PortfolioOptimizer.projectL1Ball(v, 1.0), 0.0);
        // radius 0: zero vector
        assertArrayEquals(new double[3],
                PortfolioOptimizer.projectL1Ball(v, 0.0), 0.0);
        try {
            PortfolioOptimizer.projectL1Ball(v, -1.0);
            fail("negative radius must throw");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("radius"));
        }
    }

    @Test
    public void projectionOutputIsFeasibleForRandomInputs() {
        SplitMix64 rng = new SplitMix64(7L);
        int n = 6;
        double[][] sigma = new double[n][n];
        for (int i = 0; i < n; i++) {
            sigma[i][i] = 1e-4;
        }
        double[] wPrev = randVec(rng, n, 0.05);
        Constraints cons = new Constraints(fill(n, -0.3), fill(n, 0.3));
        cons.grossCap = 0.8;
        cons.netCap = 0.2;
        cons.participation = fill(n, 0.1);
        cons.turnoverCap = 0.4;
        cons.volTarget = 0.01;
        for (int t = 0; t < 25; t++) {
            double[] v = randVec(rng, n, 1.5);
            double[] w = PortfolioOptimizer.project(v, cons, wPrev, sigma, 12);
            assertTrue("violation small after 12 passes",
                    PortfolioOptimizer.maxViolation(w, cons, wPrev, sigma)
                            <= 1e-7);
        }
    }

    private static double[] fill(int n, double v) {
        double[] out = new double[n];
        java.util.Arrays.fill(out, v);
        return out;
    }

    @Test
    public void asymmetricSigmaIsRejected() {
        double[][] sigma = {{1e-4, 1e-5}, {2e-5, 1e-4}};
        Constraints cons = new Constraints(fill(2, -1), fill(2, 1));
        try {
            PortfolioOptimizer.solve(new double[] {0.0, 0.0}, sigma,
                    new double[2], 1.0, new double[2], cons,
                    SolverParams.defaults());
            fail("asymmetric Sigma must be rejected");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("symmetric"));
        }
    }

    @Test
    public void badInputsAreRejected() {
        double[][] sigma = {{1e-4, 0.0}, {0.0, 1e-4}};
        Constraints cons = new Constraints(fill(2, -1), fill(2, 1));
        try {
            PortfolioOptimizer.solve(new double[] {0.0, 0.0}, sigma,
                    new double[2], -1.0, new double[2], cons,
                    SolverParams.defaults());
            fail("negative risk aversion");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("risk_aversion"));
        }
        try {
            PortfolioOptimizer.solve(new double[] {0.0, 0.0}, sigma,
                    new double[2], 1.0, new double[] {-0.1, 0.0}, cons,
                    SolverParams.defaults());
            fail("negative tc");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("tc_linear"));
        }
        try {
            Constraints bad = new Constraints(fill(2, 0.5), fill(2, -0.5));
            PortfolioOptimizer.solve(new double[] {0.0, 0.0}, sigma,
                    new double[2], 1.0, new double[2], bad,
                    SolverParams.defaults());
            fail("w_min > w_max");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("w_min"));
        }
        try {
            PortfolioOptimizer.solve(new double[] {0.0, 0.0}, sigma,
                    new double[2], 1.0, new double[2], cons,
                    new SolverParams(null, -0.1, 500, 8, 1e-7));
            fail("negative step decay");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("solver"));
        }
    }

    @Test
    public void unconstrainedQuadraticReachesClosedForm() {
        // max a·w - lam*w'Dw with wide box: w* = a / (2*lam*d)
        double[] alpha = {0.002, -0.001};
        double[][] sigma = {{2e-4, 0.0}, {0.0, 1e-4}};
        double lam = 5.0;
        Constraints cons = new Constraints(fill(2, -10), fill(2, 10));
        PgdResult r = PortfolioOptimizer.solve(alpha, sigma, new double[2],
                lam, new double[2], cons,
                new SolverParams(null, 0.0, 4000, 1, 1e-7));
        assertEquals(alpha[0] / (2 * lam * sigma[0][0]), r.weights()[0], 1e-6);
        assertEquals(alpha[1] / (2 * lam * sigma[1][1]), r.weights()[1], 1e-6);
        assertEquals(0.0, r.maxViolation(), 0.0);
    }

    @Test
    public void ewmaCovarianceMatchesDirectRecursion() {
        SplitMix64 rng = new SplitMix64(99L);
        int t = 60;
        int n = 3;
        double[][] rets = new double[t][n];
        for (int i = 0; i < t; i++) {
            for (int j = 0; j < n; j++) {
                rets[i][j] = (rng.uniform() * 2.0 - 1.0) * 1e-3;
            }
        }
        double lam = 0.94;
        int w0 = 20;
        // direct reference
        double[] mean = new double[n];
        for (int i = 0; i < w0; i++) {
            for (int j = 0; j < n; j++) {
                mean[j] += rets[i][j] / w0;
            }
        }
        double[][] s = new double[n][n];
        for (int i = 0; i < w0; i++) {
            for (int a = 0; a < n; a++) {
                for (int b = 0; b < n; b++) {
                    s[a][b] += (rets[i][a] - mean[a]) * (rets[i][b] - mean[b]) / w0;
                }
            }
        }
        for (int i = w0; i < t; i++) {
            for (int a = 0; a < n; a++) {
                for (int b = 0; b < n; b++) {
                    s[a][b] = lam * s[a][b] + (1 - lam) * rets[i][a] * rets[i][b];
                }
            }
        }
        double trace = s[0][0] + s[1][1] + s[2][2];
        double[][] got = EwmaCovariance.estimate(rets, lam, w0, 1e-6);
        for (int a = 0; a < n; a++) {
            for (int b = 0; b < n; b++) {
                double want = s[a][b] + (a == b ? 1e-6 * trace / n : 0.0);
                assertEquals("S[" + a + "][" + b + "]", want, got[a][b], 1e-15);
                assertEquals("symmetry", got[a][b], got[b][a], 0.0);
            }
        }
        try {
            EwmaCovariance.estimate(new double[][] {{0.1}}, lam, w0, 1e-6);
            fail("one row must be rejected");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("2 return rows"));
        }
    }

    @Test
    public void currencyMatrixMatchesGoldenProblem() {
        var g = Json.object(Golden.json("expected_portfolio.json").get("problem"));
        List<Object> pairs = Json.array(g.get("pair_symbols"));
        List<String> symbols = pairs.stream().map(o -> (String) o).toList();
        CurrencyExposure.Result r = CurrencyExposure.matrix(symbols);
        List<Object> wantCcy = Json.array(g.get("currencies"));
        assertEquals(wantCcy.size(), r.currencies().size());
        for (int i = 0; i < wantCcy.size(); i++) {
            assertEquals(wantCcy.get(i), r.currencies().get(i));
        }
        double[][] want = PortfolioGoldenTest.mat(g.get("currency_matrix"));
        for (int i = 0; i < want.length; i++) {
            assertArrayEquals("row " + i, want[i], r.matrix()[i], 0.0);
        }
        try {
            CurrencyExposure.matrix(List.of("EURUSD"));
            fail("bad symbol");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("pair"));
        }
    }

    // ------------------------------------------------ round-3 scenarios ---

    private static double[] fillv(int n, double v) {
        double[] out = new double[n];
        java.util.Arrays.fill(out, v);
        return out;
    }

    /** turnover_cap 0 with w_prev outside the gross cap: INFEASIBLE, hold. */
    @Test
    public void portfolioInfeasibleFlagsAndHoldsWPrev() {
        double[] wPrev = {0.5, 0.5};
        Constraints cons = new Constraints(fillv(2, -1.0), fillv(2, 1.0));
        cons.grossCap = 0.5;
        cons.turnoverCap = 0.0;
        double[][] sigma = {{1e-4, 0.0}, {0.0, 1e-4}};
        PgdResult res = PortfolioOptimizer.solve(new double[] {0.01, 0.02}, sigma,
                wPrev, 1.0, new double[2], cons,
                new SolverParams(null, 0.01, 50, 8, 1e-7));
        assertTrue(!res.feasible());
        assertEquals("INFEASIBLE", res.status());
        assertArrayEquals(wPrev, res.weights(), 0.0);
        assertTrue(Double.isFinite(res.objective()));
        assertEquals(0, res.bestIteration());
        assertEquals(0.5, res.maxViolation(), 1e-12);
        com.iap.portfolio.ConstraintAudit.Report audit =
                com.iap.portfolio.ConstraintAudit.audit(res.weights(), cons, wPrev, sigma);
        assertTrue(!audit.feasible());
        assertEquals(0.5, audit.maxViolation(), 1e-12);
        boolean negativeSlack = false;
        for (com.iap.portfolio.ConstraintAudit.Row r : audit.constraints()) {
            if (r.name().equals("gross_exposure") && r.slack() < 0) {
                negativeSlack = true;
            }
        }
        assertTrue(negativeSlack);
        assertTrue(audit.toJson().contains("\"feasible\":false"));
        Constraints ok = new Constraints(fillv(2, -1.0), fillv(2, 1.0));
        ok.grossCap = 0.5;
        PgdResult fine = PortfolioOptimizer.solve(new double[] {0.01, 0.02}, sigma,
                new double[2], 1.0, new double[2], ok,
                new SolverParams(null, 0.01, 50, 8, 1e-7));
        assertTrue(fine.feasible());
        assertEquals("OPTIMAL", fine.status());
    }

    /** NaN / inf anywhere in the inputs is rejected up front. */
    @Test
    public void portfolioRejectsNonFiniteInputs() {
        Constraints cons = new Constraints(fillv(2, -1.0), fillv(2, 1.0));
        double[][] eye = {{1.0, 0.0}, {0.0, 1.0}};
        SolverParams p = new SolverParams(null, 0.01, 10, 4, 1e-7);
        expectIae(() -> PortfolioOptimizer.solve(new double[] {Double.NaN, 0.0},
                eye, new double[2], 1.0, new double[2], cons, p), "alpha");
        expectIae(() -> PortfolioOptimizer.solve(new double[2],
                new double[][] {{Double.POSITIVE_INFINITY, 0.0}, {0.0, 1.0}},
                new double[2], 1.0, new double[2], cons, p), "Sigma");
        expectIae(() -> PortfolioOptimizer.solve(new double[2], eye,
                new double[] {Double.NaN, 0.0}, 1.0, new double[2], cons, p),
                "w_prev");
        expectIae(() -> PortfolioOptimizer.solve(new double[2], eye,
                new double[2], 1.0, new double[] {Double.POSITIVE_INFINITY, 0.0},
                cons, p), "tc_linear");
        expectIae(() -> PortfolioOptimizer.solve(new double[2], eye,
                new double[2], Double.NaN, new double[2], cons, p),
                "risk_aversion");
    }

    private static void expectIae(Runnable r, String what) {
        try {
            r.run();
            fail(what + " must throw");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage(), expected.getMessage().contains(what));
        }
    }

    /** Sigma = 0, lambda = 0, tc > 0: auto eta 1e6 still yields a finite,
     *  feasible, bang-bang solution inside the box. */
    @Test
    public void portfolioZeroSigmaLambdaZeroAutoEta() {
        int n = 3;
        Constraints cons = new Constraints(fillv(n, -1.0), fillv(n, 1.0));
        PgdResult res = PortfolioOptimizer.solve(new double[] {0.01, -0.02, 0.0},
                new double[n][n], new double[n], 0.0, fillv(n, 1e-4), cons,
                new SolverParams(null, 0.01, 100, 8, 1e-7));
        assertTrue(res.feasible());
        for (double w : res.weights()) {
            assertTrue(Double.isFinite(w));
        }
        assertTrue(Double.isFinite(res.objective()));
        assertEquals(1.0, res.weights()[0], 1e-12);
        assertEquals(-1.0, res.weights()[1], 1e-12);
        assertEquals(0.0, res.weights()[2], 1e-12);
        assertTrue(res.maxViolation() <= 1e-7);
    }
}
