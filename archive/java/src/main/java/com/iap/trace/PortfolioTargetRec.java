package com.iap.trace;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import com.iap.contracts.Trees;

/**
 * {@code schemas/portfolio/portfolio_target.schema.json} (x-version 1):
 * one portfolio solve. {@code solverStatus} is OPTIMAL / INFEASIBLE /
 * MAX_ITER; legs are sorted by unique {@code instrumentId}.
 */
public record PortfolioTargetRec(String strategyId, long timestampNs,
        String portfolioVersion, String featureVersion, String modelVersion,
        String solverStatus, double objectiveValue, double turnover,
        List<Leg> targets) {
    private static final String[] KEYS = {"strategy_id", "timestamp_ns",
        "portfolio_version", "feature_version", "model_version",
        "solver_status", "objective_value", "turnover", "targets"};

    /** {@code $defs/PortfolioLeg}: one instrument's target. */
    public record Leg(long instrumentId, long targetQty, double targetWeight,
            double expectedReturnBps, long prevQty) {
        private static final String[] LEG_KEYS = {"instrument_id", "target_qty",
            "target_weight", "expected_return_bps", "prev_qty"};

        public Leg {
            Trees.finite(targetWeight, "PortfolioLeg.target_weight");
            Trees.finite(expectedReturnBps, "PortfolioLeg.expected_return_bps");
        }

        /** Schema-ordered tree. */
        public Map<String, Object> toTree() {
            Map<String, Object> t = Trees.ordered();
            t.put("instrument_id", instrumentId);
            t.put("target_qty", targetQty);
            t.put("target_weight", targetWeight);
            t.put("expected_return_bps", expectedReturnBps);
            t.put("prev_qty", prevQty);
            return t;
        }

        /** Strict inverse of {@link #toTree}. */
        public static Leg fromTree(Map<String, Object> t) {
            String p = "PortfolioLeg";
            Trees.checkKeys(t, LEG_KEYS, p);
            return new Leg(Trees.u32(t, "instrument_id", p),
                    Trees.i64(t, "target_qty", p),
                    Trees.num(t, "target_weight", p),
                    Trees.num(t, "expected_return_bps", p),
                    Trees.i64(t, "prev_qty", p));
        }
    }

    public PortfolioTargetRec {
        Trees.ident(strategyId, "PortfolioTarget.strategy_id");
        Trees.sha256(portfolioVersion, "PortfolioTarget.portfolio_version");
        Trees.sha256(featureVersion, "PortfolioTarget.feature_version");
        Trees.sha256(modelVersion, "PortfolioTarget.model_version");
        if (!("OPTIMAL".equals(solverStatus) || "INFEASIBLE".equals(solverStatus)
                || "MAX_ITER".equals(solverStatus))) {
            throw new IllegalArgumentException(
                    "PortfolioTarget.solver_status: unknown " + solverStatus);
        }
        Trees.finite(objectiveValue, "PortfolioTarget.objective_value");
        Trees.finite(turnover, "PortfolioTarget.turnover");
        if (turnover < 0.0) {
            throw new IllegalArgumentException("PortfolioTarget.turnover < 0");
        }
        targets = List.copyOf(targets);
        for (int i = 1; i < targets.size(); i++) {
            if (targets.get(i).instrumentId() <= targets.get(i - 1).instrumentId()) {
                throw new IllegalArgumentException(
                        "PortfolioTarget: targets must be sorted by unique instrument_id");
            }
        }
    }

    /** Schema-ordered tree. */
    public Map<String, Object> toTree() {
        Map<String, Object> t = Trees.ordered();
        t.put("strategy_id", strategyId);
        t.put("timestamp_ns", timestampNs);
        t.put("portfolio_version", portfolioVersion);
        t.put("feature_version", featureVersion);
        t.put("model_version", modelVersion);
        t.put("solver_status", solverStatus);
        t.put("objective_value", objectiveValue);
        t.put("turnover", turnover);
        List<Object> legs = new ArrayList<>(targets.size());
        for (Leg l : targets) {
            legs.add(l.toTree());
        }
        t.put("targets", legs);
        return t;
    }

    /** Strict inverse of {@link #toTree}. */
    public static PortfolioTargetRec fromTree(Map<String, Object> t) {
        String p = "PortfolioTarget";
        Trees.checkKeys(t, KEYS, p);
        List<Leg> legs = new ArrayList<>();
        for (Object o : Trees.arr(t, "targets", p)) {
            legs.add(Leg.fromTree(Trees.obj(o, p + ".targets[]")));
        }
        return new PortfolioTargetRec(Trees.str(t, "strategy_id", p),
                Trees.i64(t, "timestamp_ns", p),
                Trees.str(t, "portfolio_version", p),
                Trees.str(t, "feature_version", p),
                Trees.str(t, "model_version", p),
                Trees.str(t, "solver_status", p),
                Trees.num(t, "objective_value", p),
                Trees.num(t, "turnover", p), legs);
    }
}
