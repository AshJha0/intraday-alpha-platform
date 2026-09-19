package com.iap.lifecycle;

import java.util.Map;

import com.iap.contracts.Trees;

/**
 * The merged lifecycle policy ({@code iap.lifecycle.config.PolicyConfig}):
 * the promotion-gate thresholds and the demotion counter from
 * {@code configs/strategies/lifecycle.json} (x-version 1) plus the live
 * ACTIVE / WATCH / RETIRED gates from {@code configs/strategies/strategies.json}
 * {@code adaptive.lifecycle} (one pin, one file — reused unchanged by
 * {@link com.iap.adaptive.LifecycleGauge}). Loading is fail-fast: a wrong
 * {@code x-version}, a missing or unknown key, a non-finite number, a
 * negative count or an inconsistent threshold raises
 * {@link IllegalArgumentException} naming the file and the key.
 */
public record PolicyConfig(String policy, Gates gates, int maxConsecutiveFailures,
        Live live) {
    /** {@code x-version} of {@code configs/strategies/lifecycle.json}. */
    public static final long LIFECYCLE_CONFIG_VERSION = 1;

    private static final String[] GATE_KEYS = {"min_experiments_in_ledger",
        "min_oos_ic", "min_nw_tstat", "min_fold_sign_consistency", "min_folds",
        "min_net_return_bps", "min_capacity_usd", "max_ic_rank_gap",
        "ic_rank_gap_eps", "max_holdout_ic_gap", "min_paper_sessions",
        "max_paper_ic_gap", "min_paper_net_pnl", "max_kill_events"};

    /** Every promotion-gate threshold, one field per config key. */
    public record Gates(long minExperimentsInLedger, double minOosIc,
            double minNwTstat, double minFoldSignConsistency, long minFolds,
            double minNetReturnBps, double minCapacityUsd, double maxIcRankGap,
            double icRankGapEps, double maxHoldoutIcGap, long minPaperSessions,
            double maxPaperIcGap, double minPaperNetPnl, long maxKillEvents) {
        public Gates {
            if (minFoldSignConsistency < 0.0 || minFoldSignConsistency > 1.0) {
                throw new IllegalArgumentException(
                        "gates.min_fold_sign_consistency must lie in [0, 1]");
            }
            if (icRankGapEps <= 0.0) {
                throw new IllegalArgumentException("gates.ic_rank_gap_eps must be > 0");
            }
            if (maxIcRankGap < 0.0) {
                throw new IllegalArgumentException("gates.max_ic_rank_gap must be >= 0");
            }
            if (maxHoldoutIcGap < 0.0 || maxPaperIcGap < 0.0) {
                throw new IllegalArgumentException("gates.max_*_ic_gap must be >= 0");
            }
            if (minCapacityUsd < 0.0) {
                throw new IllegalArgumentException("gates.min_capacity_usd must be >= 0");
            }
            if (minExperimentsInLedger < 1 || minFolds < 1 || minPaperSessions < 1) {
                throw new IllegalArgumentException(
                        "gates: min_experiments_in_ledger/min_folds/min_paper_sessions >= 1");
            }
            if (maxKillEvents < 0) {
                throw new IllegalArgumentException("gates.max_kill_events must be >= 0");
            }
        }

        /** Parse the {@code gates} block ({@code where} names file + key path). */
        public static Gates fromTree(Map<String, Object> g, String where) {
            Trees.checkKeys(g, GATE_KEYS, where);
            return new Gates(count(g, "min_experiments_in_ledger", where, 1),
                    Trees.num(g, "min_oos_ic", where), Trees.num(g, "min_nw_tstat", where),
                    Trees.num(g, "min_fold_sign_consistency", where),
                    count(g, "min_folds", where, 1),
                    Trees.num(g, "min_net_return_bps", where),
                    Trees.num(g, "min_capacity_usd", where),
                    Trees.num(g, "max_ic_rank_gap", where),
                    Trees.num(g, "ic_rank_gap_eps", where),
                    Trees.num(g, "max_holdout_ic_gap", where),
                    count(g, "min_paper_sessions", where, 1),
                    Trees.num(g, "max_paper_ic_gap", where),
                    Trees.num(g, "min_paper_net_pnl", where),
                    count(g, "max_kill_events", where, 0));
        }

        /** JSON-ready view in config key order. */
        public Map<String, Object> toTree() {
            Map<String, Object> t = Trees.ordered();
            t.put("min_experiments_in_ledger", minExperimentsInLedger);
            t.put("min_oos_ic", minOosIc);
            t.put("min_nw_tstat", minNwTstat);
            t.put("min_fold_sign_consistency", minFoldSignConsistency);
            t.put("min_folds", minFolds);
            t.put("min_net_return_bps", minNetReturnBps);
            t.put("min_capacity_usd", minCapacityUsd);
            t.put("max_ic_rank_gap", maxIcRankGap);
            t.put("ic_rank_gap_eps", icRankGapEps);
            t.put("max_holdout_ic_gap", maxHoldoutIcGap);
            t.put("min_paper_sessions", minPaperSessions);
            t.put("max_paper_ic_gap", maxPaperIcGap);
            t.put("min_paper_net_pnl", minPaperNetPnl);
            t.put("max_kill_events", maxKillEvents);
            return t;
        }
    }

    /**
     * The live sub-machine gates ({@code strategies.json adaptive.lifecycle}),
     * exactly the {@link com.iap.adaptive.LifecycleGauge} constructor arguments.
     */
    public record Live(double watchIcGate, double reactivateIcGate,
            int retireBreachEvals, int reactivateEvals) {
        private static final String[] LIVE_KEYS = {"watch_ic_gate",
            "reactivate_ic_gate", "retire_breach_evals", "reactivate_evals"};

        public Live {
            Trees.finite(watchIcGate, "adaptive.lifecycle.watch_ic_gate");
            Trees.finite(reactivateIcGate, "adaptive.lifecycle.reactivate_ic_gate");
            if (retireBreachEvals < 1 || reactivateEvals < 1) {
                throw new IllegalArgumentException("lifecycle eval counts must be >= 1");
            }
            if (reactivateIcGate < watchIcGate) {
                throw new IllegalArgumentException(
                        "reactivate_ic_gate must be >= watch_ic_gate");
            }
        }

        /** Parse the block ({@code where} names file + key path). */
        public static Live fromTree(Map<String, Object> l, String where) {
            Trees.checkKeys(l, LIVE_KEYS, where);
            return new Live(Trees.num(l, "watch_ic_gate", where),
                    Trees.num(l, "reactivate_ic_gate", where),
                    (int) count(l, "retire_breach_evals", where, 1),
                    (int) count(l, "reactivate_evals", where, 1));
        }

        /** A gauge with these gates, starting ACTIVE. */
        public com.iap.adaptive.LifecycleGauge gauge() {
            return new com.iap.adaptive.LifecycleGauge(watchIcGate, reactivateIcGate,
                    retireBreachEvals, reactivateEvals);
        }

        /** JSON-ready view. */
        public Map<String, Object> toTree() {
            Map<String, Object> t = Trees.ordered();
            t.put("watch_ic_gate", watchIcGate);
            t.put("reactivate_ic_gate", reactivateIcGate);
            t.put("retire_breach_evals", (long) retireBreachEvals);
            t.put("reactivate_evals", (long) reactivateEvals);
            return t;
        }
    }

    public PolicyConfig {
        if (policy == null || policy.isEmpty()) {
            throw new IllegalArgumentException("policy name must not be empty");
        }
        if (maxConsecutiveFailures < 1) {
            throw new IllegalArgumentException(
                    "demotion.max_consecutive_failures must be >= 1");
        }
        if (gates == null || live == null) {
            throw new IllegalArgumentException("PolicyConfig: gates/live missing");
        }
    }

    private static long count(Map<String, Object> m, String key, String where,
            long minimum) {
        long v = Trees.i64(m, key, where);
        if (v < minimum) {
            throw new IllegalArgumentException(where + "." + key + ": " + v + " < "
                    + minimum);
        }
        return v;
    }

    /**
     * Build from the two loaded config documents: {@code lifecycle} is
     * {@code configs/strategies/lifecycle.json}, {@code strategies} is
     * {@code configs/strategies/strategies.json} (its {@code adaptive.lifecycle}
     * block is the live pin). {@code lifecycleName} / {@code strategiesName}
     * are the file names used in error messages.
     */
    public static PolicyConfig fromDocs(Map<String, Object> lifecycle,
            String lifecycleName, Map<String, Object> strategies,
            String strategiesName) {
        Trees.checkKeys(lifecycle, new String[] {"x-version", "description", "policy",
            "gates", "demotion"}, lifecycleName);
        long version = Trees.i64(lifecycle, "x-version", lifecycleName);
        if (version != LIFECYCLE_CONFIG_VERSION) {
            throw new IllegalArgumentException(lifecycleName + ": x-version " + version
                    + " != " + LIFECYCLE_CONFIG_VERSION);
        }
        String policy = Trees.str(lifecycle, "policy", lifecycleName);
        if (policy.isEmpty()) {
            throw new IllegalArgumentException(lifecycleName
                    + ".policy: expected a non-empty string");
        }
        Gates gates = Gates.fromTree(Trees.obj(lifecycle, "gates", lifecycleName),
                lifecycleName + ".gates");
        Map<String, Object> demotion = Trees.obj(lifecycle, "demotion", lifecycleName);
        Trees.checkKeys(demotion, new String[] {"max_consecutive_failures"},
                lifecycleName + ".demotion");
        long maxFailures = count(demotion, "max_consecutive_failures",
                lifecycleName + ".demotion", 1);
        Object adaptive = strategies.get("adaptive");
        if (!(adaptive instanceof Map)) {
            throw new IllegalArgumentException(strategiesName
                    + ": missing adaptive.lifecycle block");
        }
        Object liveBlock = com.iap.config.Json.object(adaptive).get("lifecycle");
        if (!(liveBlock instanceof Map)) {
            throw new IllegalArgumentException(strategiesName
                    + ": missing adaptive.lifecycle block");
        }
        Live live = Live.fromTree(com.iap.config.Json.object(liveBlock),
                strategiesName + ".adaptive.lifecycle");
        return new PolicyConfig(policy, gates, (int) maxFailures, live);
    }

    /**
     * Inverse of {@link #toTree}: the merged view embedded in
     * {@code tests/golden/expected_lifecycle.json} ({@code policy}, {@code gates},
     * {@code demotion}, {@code live}).
     */
    public static PolicyConfig fromTree(Map<String, Object> doc) {
        String where = "config";
        Trees.checkKeys(doc, new String[] {"policy", "gates", "demotion", "live"}, where);
        Map<String, Object> demotion = Trees.obj(doc, "demotion", where);
        Trees.checkKeys(demotion, new String[] {"max_consecutive_failures"},
                "config.demotion");
        return new PolicyConfig(Trees.str(doc, "policy", where),
                Gates.fromTree(Trees.obj(doc, "gates", where), "config.gates"),
                (int) count(demotion, "max_consecutive_failures", "config.demotion", 1),
                Live.fromTree(Trees.obj(doc, "live", where), "config.live"));
    }

    /** JSON-ready merged view. */
    public Map<String, Object> toTree() {
        Map<String, Object> t = Trees.ordered();
        t.put("policy", policy);
        t.put("gates", gates.toTree());
        Map<String, Object> d = Trees.ordered();
        d.put("max_consecutive_failures", (long) maxConsecutiveFailures);
        t.put("demotion", d);
        t.put("live", live.toTree());
        return t;
    }
}
