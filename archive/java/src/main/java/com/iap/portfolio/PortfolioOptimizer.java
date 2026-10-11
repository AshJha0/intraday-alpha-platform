package com.iap.portfolio;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Deterministic projected-gradient portfolio optimizer — the production
 * implementation of the pinned research algorithm (API_PORTFOLIO_TCA.md §1;
 * Python reference {@code iap.portfolio.optimizer}). Maximizes
 *
 * <pre>f(w) = alpha·w - lambda * w' Sigma w - sum_i tc_i * |w_i - w_prev_i|</pre>
 *
 * with PGD + a proximal (soft-threshold) step for the L1 transaction cost,
 * projecting after every step with {@code proj_passes} fixed passes of
 * cyclic projections in the pinned order box → participation → net →
 * currency → gross → turnover → vol. Gross/turnover use the exact
 * sort-based L1-ball projection; net/currency exact halfspace projections;
 * the vol cap uses radial scaling (pinned retraction, NOT the exact
 * ellipsoidal projection). The best FEASIBLE iterate by objective wins
 * (ties keep the earliest; the initial projection of w_prev is iteration 0).
 * When NO iterate is feasible the result carries the LEAST-VIOLATING
 * candidate — w_prev (bestIteration -1), the initial projection (0) or an
 * iterate (k + 1) — ranked by (risk violation, total violation, candidate
 * order); see {@link PgdResult}. No RNG, no unordered iteration —
 * bit-deterministic for given inputs, and must match
 * tests/golden/expected_portfolio.json to 1e-9.
 */
public final class PortfolioOptimizer {
    /**
     * Constraints that bound the BOOK's exposure. A residual breach here is
     * live over-exposure, so holding w_prev perpetuates it — this is why the
     * INFEASIBLE result ranks on the risk violation before the total.
     */
    public static final Set<String> RISK_CONSTRAINTS =
            Set.of("BOX", "NET", "CURRENCY", "GROSS", "VOL");
    /**
     * Constraints that bound the TRADE. A residual breach here only means
     * the step exceeds one bar's cap; the execution layer slices it.
     */
    public static final Set<String> TRADING_CONSTRAINTS =
            Set.of("PARTICIPATION", "TURNOVER");

    private PortfolioOptimizer() {
    }

    /** The research objective f(w). */
    public static double objective(double[] w, double[] alpha, double[][] sigma,
            double[] wPrev, double riskAversion, double[] tcLinear) {
        int n = w.length;
        double lin = 0.0;
        for (int i = 0; i < n; i++) {
            lin += alpha[i] * w[i];
        }
        double quad = quadraticForm(w, sigma);
        double cost = 0.0;
        for (int i = 0; i < n; i++) {
            cost += tcLinear[i] * Math.abs(w[i] - wPrev[i]);
        }
        return lin - riskAversion * quad - cost;
    }

    /** w' Sigma w (matvec then dot, fixed order). */
    static double quadraticForm(double[] w, double[][] sigma) {
        double[] sw = matVec(sigma, w);
        double q = 0.0;
        for (int i = 0; i < w.length; i++) {
            q += w[i] * sw[i];
        }
        return q;
    }

    static double[] matVec(double[][] m, double[] v) {
        double[] out = new double[m.length];
        for (int i = 0; i < m.length; i++) {
            double s = 0.0;
            double[] row = m[i];
            for (int j = 0; j < row.length; j++) {
                s += row[j] * v[j];
            }
            out[i] = s;
        }
        return out;
    }

    /**
     * Exact Euclidean projection onto the L1 ball of radius {@code r}
     * (sort-based; API_PORTFOLIO_TCA.md §1.4). Returns a new array.
     */
    public static double[] projectL1Ball(double[] v, double radius) {
        if (radius < 0.0) {
            throw new IllegalArgumentException("radius must be >= 0");
        }
        int n = v.length;
        double[] a = new double[n];
        double sum = 0.0;
        for (int i = 0; i < n; i++) {
            a[i] = Math.abs(v[i]);
            sum += a[i];
        }
        if (sum <= radius) {
            return v.clone();
        }
        if (radius == 0.0) {
            return new double[n];
        }
        double[] u = a.clone();
        Arrays.sort(u); // ascending
        // descending cumulative sums; rho = max{j : u_j - (c_j - r)/j > 0}
        double c = 0.0;
        int rho = 0;
        double theta = 0.0;
        for (int j = 1; j <= n; j++) {
            double uj = u[n - j]; // j-th largest
            c += uj;
            if (uj - (c - radius) / j > 0.0) {
                rho = j;
                theta = (c - radius) / j;
            }
        }
        if (rho == 0) {
            return new double[n];
        }
        double[] out = new double[n];
        for (int i = 0; i < n; i++) {
            out[i] = Math.signum(v[i]) * Math.max(a[i] - theta, 0.0);
        }
        return out;
    }

    /**
     * {@code proj_passes} passes of the pinned cyclic projections
     * (API_PORTFOLIO_TCA.md §1.3). Returns a new array.
     */
    public static double[] project(double[] v, Constraints cons, double[] wPrev,
            double[][] sigma, int passes) {
        int n = v.length;
        double[] w = v.clone();
        for (int pass = 0; pass < passes; pass++) {
            // 1. position box
            for (int i = 0; i < n; i++) {
                w[i] = Math.min(Math.max(w[i], cons.wMin[i]), cons.wMax[i]);
            }
            // 2. participation box around w_prev
            if (cons.participation != null) {
                for (int i = 0; i < n; i++) {
                    double lo = wPrev[i] - cons.participation[i];
                    double hi = wPrev[i] + cons.participation[i];
                    w[i] = Math.min(Math.max(w[i], lo), hi);
                }
            }
            // 3. net-exposure halfspace
            if (cons.netCap != null) {
                double s = 0.0;
                for (double x : w) {
                    s += x;
                }
                if (Math.abs(s) > cons.netCap) {
                    double shift = (s - Math.signum(s) * cons.netCap) / n;
                    for (int i = 0; i < n; i++) {
                        w[i] -= shift;
                    }
                }
            }
            // 4. currency halfspaces (row index ascending, pinned)
            if (cons.currencyMatrix != null) {
                for (int c = 0; c < cons.currencyMatrix.length; c++) {
                    double[] e = cons.currencyMatrix[c];
                    double denom = 0.0;
                    for (double x : e) {
                        denom += x * x;
                    }
                    if (denom == 0.0) {
                        continue;
                    }
                    double val = 0.0;
                    for (int i = 0; i < n; i++) {
                        val += e[i] * w[i];
                    }
                    double bound = cons.currencyBounds[c];
                    if (Math.abs(val) > bound) {
                        double scale = (val - Math.signum(val) * bound) / denom;
                        for (int i = 0; i < n; i++) {
                            w[i] -= scale * e[i];
                        }
                    }
                }
            }
            // 5. gross L1 ball
            if (cons.grossCap != null) {
                w = projectL1Ball(w, cons.grossCap);
            }
            // 6. turnover L1 ball around w_prev
            if (cons.turnoverCap != null) {
                double[] d = new double[n];
                for (int i = 0; i < n; i++) {
                    d[i] = w[i] - wPrev[i];
                }
                d = projectL1Ball(d, cons.turnoverCap);
                for (int i = 0; i < n; i++) {
                    w[i] = wPrev[i] + d[i];
                }
            }
            // 7. vol target (radial retraction, pinned)
            if (cons.volTarget != null) {
                if (sigma == null) {
                    throw new IllegalArgumentException("vol_target requires Sigma");
                }
                double q = quadraticForm(w, sigma);
                double cap = cons.volTarget * cons.volTarget;
                if (q > cap) {
                    double scale = cons.volTarget / Math.sqrt(q);
                    for (int i = 0; i < n; i++) {
                        w[i] *= scale;
                    }
                }
            }
        }
        return w;
    }

    /**
     * Per-constraint violation of w, keyed by the names in
     * {@link #RISK_CONSTRAINTS} / {@link #TRADING_CONSTRAINTS}, in the
     * pinned projection order. An inactive constraint is absent; an active
     * one that holds maps to a value &lt;= 0. {@link #maxViolation} is the
     * maximum of these (floored at 0), so the two can never disagree.
     */
    public static LinkedHashMap<String, Double> violationBreakdown(double[] w,
            Constraints cons, double[] wPrev, double[][] sigma) {
        int n = w.length;
        LinkedHashMap<String, Double> out = new LinkedHashMap<>();
        double box = 0.0;
        for (int i = 0; i < n; i++) {
            box = Math.max(box, cons.wMin[i] - w[i]);
            box = Math.max(box, w[i] - cons.wMax[i]);
        }
        out.put("BOX", box);
        if (cons.participation != null) {
            double p = 0.0;
            for (int i = 0; i < n; i++) {
                p = Math.max(p, Math.abs(w[i] - wPrev[i]) - cons.participation[i]);
            }
            out.put("PARTICIPATION", p);
        }
        if (cons.netCap != null) {
            double s = 0.0;
            for (double x : w) {
                s += x;
            }
            out.put("NET", Math.abs(s) - cons.netCap);
        }
        if (cons.currencyMatrix != null) {
            double c1 = 0.0;
            for (int c = 0; c < cons.currencyMatrix.length; c++) {
                double val = 0.0;
                for (int i = 0; i < n; i++) {
                    val += cons.currencyMatrix[c][i] * w[i];
                }
                c1 = Math.max(c1, Math.abs(val) - cons.currencyBounds[c]);
            }
            out.put("CURRENCY", c1);
        }
        if (cons.grossCap != null) {
            double g = 0.0;
            for (double x : w) {
                g += Math.abs(x);
            }
            out.put("GROSS", g - cons.grossCap);
        }
        if (cons.turnoverCap != null) {
            double t = 0.0;
            for (int i = 0; i < n; i++) {
                t += Math.abs(w[i] - wPrev[i]);
            }
            out.put("TURNOVER", t - cons.turnoverCap);
        }
        if (cons.volTarget != null && sigma != null) {
            double q = Math.max(quadraticForm(w, sigma), 0.0);
            out.put("VOL", Math.sqrt(q) - cons.volTarget);
        }
        return out;
    }

    /** Largest constraint violation of w (0 when feasible). */
    public static double maxViolation(double[] w, Constraints cons,
            double[] wPrev, double[][] sigma) {
        double v = 0.0;
        for (double value : violationBreakdown(w, cons, wPrev, sigma).values()) {
            v = Math.max(v, value);
        }
        return Math.max(v, 0.0);
    }

    /**
     * Largest violation among the RISK constraints only (0 when none). This
     * is the figure a caller alarms on: it is live book exposure, not a
     * trade that merely exceeds one bar's participation/turnover cap.
     */
    static double riskViolation(LinkedHashMap<String, Double> breakdown) {
        double v = 0.0;
        for (String name : RISK_CONSTRAINTS) {
            Double value = breakdown.get(name);
            if (value != null) {
                v = Math.max(v, value);
            }
        }
        return Math.max(v, 0.0);
    }

    /**
     * Names of the constraints violated by more than {@code tol}, in the
     * pinned projection order (deterministic, never unordered).
     */
    static List<String> violated(LinkedHashMap<String, Double> breakdown, double tol) {
        List<String> names = new ArrayList<>();
        for (Map.Entry<String, Double> e : breakdown.entrySet()) {
            if (e.getValue() > tol) {
                names.add(e.getKey());
            }
        }
        return List.copyOf(names);
    }

    /**
     * Deterministic PGD solve of the pinned portfolio problem. Identical
     * inputs produce bit-identical outputs (no RNG, fixed iteration
     * structure). Inputs failing validation (asymmetric Sigma beyond 1e-12,
     * bad shapes, negative parameters) are rejected.
     */
    public static PgdResult solve(double[] alpha, double[][] sigma,
            double[] wPrev, double riskAversion, double[] tcLinear,
            Constraints constraints, SolverParams params) {
        int n = alpha.length;
        if (sigma.length != n) {
            throw new IllegalArgumentException("Sigma must be (n, n)");
        }
        for (double[] row : sigma) {
            if (row.length != n) {
                throw new IllegalArgumentException("Sigma must be (n, n)");
            }
        }
        if (wPrev.length != n || tcLinear.length != n) {
            throw new IllegalArgumentException("w_prev/tc_linear must have length n");
        }
        // Every input must be finite (pinned: NaN/inf never propagate into
        // weights; API_PORTFOLIO_TCA.md §1.3).
        for (int i = 0; i < n; i++) {
            if (!Double.isFinite(alpha[i]) || !Double.isFinite(wPrev[i])
                    || !Double.isFinite(tcLinear[i])) {
                throw new IllegalArgumentException(
                        "alpha/w_prev/tc_linear must be finite (index " + i + ")");
            }
            for (int j = 0; j < n; j++) {
                if (!Double.isFinite(sigma[i][j])) {
                    throw new IllegalArgumentException(
                            "Sigma must be finite (" + i + "," + j + ")");
                }
            }
        }
        if (!Double.isFinite(riskAversion)) {
            throw new IllegalArgumentException("risk_aversion must be finite");
        }
        if (riskAversion < 0.0) {
            throw new IllegalArgumentException("risk_aversion must be >= 0");
        }
        for (double tc : tcLinear) {
            if (tc < 0.0) {
                throw new IllegalArgumentException("tc_linear must be >= 0");
            }
        }
        double eta0;
        if (params.eta0() == null) {
            double maxRowSum = 0.0;
            for (double[] row : sigma) {
                double s = 0.0;
                for (double x : row) {
                    s += Math.abs(x);
                }
                maxRowSum = Math.max(maxRowSum, s);
            }
            eta0 = 1.0 / Math.max(2.0 * riskAversion * maxRowSum, 1e-6);
        } else {
            eta0 = params.eta0();
        }
        if (params.iters() < 1 || params.projPasses() < 1 || eta0 <= 0.0
                || params.stepDecay() < 0.0) {
            throw new IllegalArgumentException("bad solver parameters");
        }
        for (int i = 0; i < n; i++) {
            for (int j = 0; j < n; j++) {
                if (Math.abs(sigma[i][j] - sigma[j][i]) > 1e-12) {
                    throw new IllegalArgumentException("Sigma must be symmetric");
                }
            }
        }
        constraints.validate(n);

        int passes = params.projPasses();
        // Least-violating fallback for the INFEASIBLE result. Candidates are
        // considered in order — holding w_prev first, then the initial
        // projection, then each iterate — and ranked by (risk violation,
        // total violation); a strict "better" keeps the EARLIEST on a tie,
        // so holding wins only when nothing computed later is any better.
        //
        // DEFECT this replaces: the solver used to discard every iterate and
        // return w_prev unconditionally. But w_prev cannot violate
        // participation or turnover (its own trade is identically zero), so
        // an infeasible solve means w_prev is breaching a RISK constraint —
        // the one situation in which holding is the WORST available action.
        // A vol spike then parked the book at many times the vol target
        // indefinitely, and the returned answer could be strictly worse than
        // an iterate the solver had already computed and thrown away.
        double[] leastW = wPrev.clone();
        LinkedHashMap<String, Double> leastB =
                violationBreakdown(leastW, constraints, wPrev, sigma);
        double leastRisk = riskViolation(leastB);
        double leastTotal = maxOf(leastB);
        int leastK = -1;

        double[] w = project(wPrev, constraints, wPrev, sigma, passes);
        double[] bestW = w.clone();
        LinkedHashMap<String, Double> b0 = violationBreakdown(w, constraints, wPrev, sigma);
        boolean feasible = maxOf(b0) <= params.feasTol();
        double bestF = feasible
                ? objective(w, alpha, sigma, wPrev, riskAversion, tcLinear)
                : Double.NEGATIVE_INFINITY;
        int bestK = 0;
        if (betterCandidate(riskViolation(b0), maxOf(b0), leastRisk, leastTotal)) {
            leastW = w.clone();
            leastRisk = riskViolation(b0);
            leastTotal = maxOf(b0);
            leastK = 0;
        }
        for (int k = 0; k < params.iters(); k++) {
            double eta = eta0 / (1.0 + params.stepDecay() * k);
            double[] sw = matVec(sigma, w);
            double[] v = new double[n];
            for (int i = 0; i < n; i++) {
                v[i] = w[i] + eta * (alpha[i] - 2.0 * riskAversion * sw[i]);
            }
            // proximal soft-threshold of the trade around w_prev
            for (int i = 0; i < n; i++) {
                double d = v[i] - wPrev[i];
                d = Math.signum(d) * Math.max(Math.abs(d) - eta * tcLinear[i], 0.0);
                v[i] = wPrev[i] + d;
            }
            w = project(v, constraints, wPrev, sigma, passes);
            double fw = objective(w, alpha, sigma, wPrev, riskAversion, tcLinear);
            LinkedHashMap<String, Double> bk =
                    violationBreakdown(w, constraints, wPrev, sigma);
            double totalK = maxOf(bk);
            if (totalK <= params.feasTol() && fw > bestF) {
                bestF = fw;
                bestW = w.clone();
                bestK = k + 1;
                feasible = true;
            }
            double riskK = riskViolation(bk);
            if (betterCandidate(riskK, totalK, leastRisk, leastTotal)) {
                leastW = w.clone();
                leastRisk = riskK;
                leastTotal = totalK;
                leastK = k + 1;
            }
        }
        if (!feasible) {
            // INFEASIBLE (pinned §1.3): the least-violating candidate, never
            // -inf/NaN, with the residual breach named so a caller can alarm.
            LinkedHashMap<String, Double> b =
                    violationBreakdown(leastW, constraints, wPrev, sigma);
            return new PgdResult(leastW,
                    objective(leastW, alpha, sigma, wPrev, riskAversion, tcLinear),
                    params.iters(), leastK, leastTotal, false,
                    violated(b, params.feasTol()), leastRisk);
        }
        return new PgdResult(bestW, bestF, params.iters(), bestK,
                maxViolation(bestW, constraints, wPrev, sigma), true,
                List.of(), 0.0);
    }

    /** Maximum of a breakdown's values, floored at 0 (= {@link #maxViolation}). */
    private static double maxOf(LinkedHashMap<String, Double> breakdown) {
        double v = 0.0;
        for (double value : breakdown.values()) {
            v = Math.max(v, value);
        }
        return Math.max(v, 0.0);
    }

    /**
     * Lexicographic candidate ranking for the INFEASIBLE result: smaller
     * RISK violation wins, then smaller total violation; equal ranks keep
     * the incumbent, which is the earlier candidate.
     */
    private static boolean betterCandidate(double risk, double total,
            double bestRisk, double bestTotal) {
        if (risk != bestRisk) {
            return risk < bestRisk;
        }
        return total < bestTotal;
    }
}
