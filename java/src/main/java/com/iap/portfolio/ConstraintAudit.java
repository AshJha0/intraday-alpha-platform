package com.iap.portfolio;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * Constraint audit for a solved weight vector (API_PORTFOLIO_TCA.md §1.7,
 * spec §15 "constraint-auditable"). Every solve response carries this
 * report: for each active-or-binding constraint
 * {@code {name, value, bound, slack, binding}} with
 * {@code binding = slack <= 1e-6}, plus turnover / gross / net /
 * realized_vol / target_vol aggregates. Position and participation rows are
 * emitted only when binding (n can be large); aggregate rows always.
 */
public final class ConstraintAudit {
    /** Default binding tolerance (pinned). */
    public static final double BIND_TOL = 1e-6;

    /** One audited constraint. */
    public record Row(String name, double value, double bound, double slack,
            boolean binding) {
    }

    /**
     * The full audit report. {@code feasible} is {@code maxViolation <=
     * bindTol}: an INFEASIBLE solve (API_PORTFOLIO_TCA.md §1.3) audits
     * w_prev and shows negative slack on the violated rows.
     */
    public record Report(List<Row> constraints, int nBinding, double turnover,
            double gross, double net, Double realizedVol, Double targetVol,
            boolean feasible, double maxViolation) {
        /** Compact JSON for solve responses / session reports. */
        public String toJson() {
            StringBuilder sb = new StringBuilder(256);
            sb.append("{\"constraints\":[");
            for (int i = 0; i < constraints.size(); i++) {
                Row r = constraints.get(i);
                if (i > 0) {
                    sb.append(',');
                }
                sb.append(String.format(Locale.ROOT,
                        "{\"binding\":%b,\"bound\":%s,\"name\":\"%s\","
                                + "\"slack\":%s,\"value\":%s}",
                        r.binding(), r.bound(), r.name(), r.slack(), r.value()));
            }
            sb.append("],\"feasible\":").append(feasible)
                    .append(",\"gross\":").append(gross)
                    .append(",\"max_violation\":").append(maxViolation)
                    .append(",\"n_binding\":").append(nBinding)
                    .append(",\"net\":").append(net)
                    .append(",\"realized_vol\":")
                    .append(realizedVol == null ? "null" : realizedVol)
                    .append(",\"target_vol\":")
                    .append(targetVol == null ? "null" : targetVol)
                    .append(",\"turnover\":").append(turnover).append('}');
            return sb.toString();
        }
    }

    private ConstraintAudit() {
    }

    /** Audit with the pinned binding tolerance. */
    public static Report audit(double[] w, Constraints cons, double[] wPrev,
            double[][] sigma) {
        return audit(w, cons, wPrev, sigma, BIND_TOL);
    }

    /** Audit a weight vector against the constraint set. */
    public static Report audit(double[] w, Constraints cons, double[] wPrev,
            double[][] sigma, double bindTol) {
        int n = w.length;
        cons.validate(n);
        List<Row> rows = new ArrayList<>();
        for (int i = 0; i < n; i++) {
            if (w[i] - cons.wMin[i] <= bindTol) {
                addRow(rows, "position_min[" + i + "]", -w[i], -cons.wMin[i], bindTol);
            }
            if (cons.wMax[i] - w[i] <= bindTol) {
                addRow(rows, "position_max[" + i + "]", w[i], cons.wMax[i], bindTol);
            }
        }
        if (cons.participation != null) {
            for (int i = 0; i < n; i++) {
                double trade = Math.abs(w[i] - wPrev[i]);
                if (cons.participation[i] - trade <= bindTol) {
                    addRow(rows, "participation[" + i + "]", trade,
                            cons.participation[i], bindTol);
                }
            }
        }
        double net = 0.0;
        double gross = 0.0;
        double turnover = 0.0;
        for (int i = 0; i < n; i++) {
            net += w[i];
            gross += Math.abs(w[i]);
            turnover += Math.abs(w[i] - wPrev[i]);
        }
        if (cons.netCap != null) {
            addRow(rows, "net_exposure", Math.abs(net), cons.netCap, bindTol);
        }
        if (cons.grossCap != null) {
            addRow(rows, "gross_exposure", gross, cons.grossCap, bindTol);
        }
        if (cons.turnoverCap != null) {
            addRow(rows, "turnover", turnover, cons.turnoverCap, bindTol);
        }
        if (cons.currencyMatrix != null) {
            for (int c = 0; c < cons.currencyMatrix.length; c++) {
                double expo = 0.0;
                for (int i = 0; i < n; i++) {
                    expo += cons.currencyMatrix[c][i] * w[i];
                }
                addRow(rows, "currency[" + c + "]", Math.abs(expo),
                        cons.currencyBounds[c], bindTol);
            }
        }
        Double realizedVol = null;
        if (sigma != null) {
            realizedVol = Math.sqrt(
                    Math.max(PortfolioOptimizer.quadraticForm(w, sigma), 0.0));
            if (cons.volTarget != null) {
                addRow(rows, "volatility", realizedVol, cons.volTarget, bindTol);
            }
        }
        int nBinding = 0;
        double maxViolation = 0.0;
        for (Row r : rows) {
            if (r.binding()) {
                nBinding++;
            }
            maxViolation = Math.max(maxViolation, -r.slack());
        }
        return new Report(List.copyOf(rows), nBinding, turnover, gross, net,
                realizedVol, cons.volTarget, maxViolation <= bindTol,
                maxViolation);
    }

    private static void addRow(List<Row> rows, String name, double value,
            double bound, double bindTol) {
        double slack = bound - value;
        rows.add(new Row(name, value, bound, slack, slack <= bindTol));
    }
}
