package com.iap.lifecycle;

import java.util.List;
import java.util.Map;

import com.iap.contracts.Trees;

/**
 * The merged lifecycle policy ({@code iap.lifecycle.config.PolicyConfig}):
 * the promotion-gate thresholds and the demotion counter from
 * {@code configs/strategies/lifecycle.json} (x-version 3) plus the live
 * ACTIVE / WATCH / RETIRED gates and retirement rule from
 * {@code configs/strategies/strategies.json} {@code adaptive.lifecycle} (one
 * pin, one file — reused unchanged by
 * {@link com.iap.adaptive.LifecycleGauge}). Loading is fail-fast: a wrong
 * {@code x-version}, a missing or unknown key, a non-finite number, a
 * negative count or an inconsistent threshold raises
 * {@link IllegalArgumentException} naming the file and the key.
 *
 * <p>{@code tstatThreshold} (x-version 2, v1.5.0) names how the
 * {@code statistical_significance} gate gets its threshold:
 * {@link #TSTAT_LEDGER} — the default — is
 * {@code max(min_nw_tstat, evidence.significance_threshold)} and fails when
 * the evidence carries no threshold; {@link #TSTAT_FIXED} — the rule up to
 * v1.4.0 — is {@code min_nw_tstat} alone.
 *
 * <p>{@code crossAlphaMinState} and {@code gates.maxCrossAlphaCorrelation}
 * (x-version 3) configure the {@code cross_alpha_correlation} gate
 * ({@link Gates}): a peer counts when its lifecycle state is at or beyond
 * {@code crossAlphaMinState} (one of {@link #CROSS_ALPHA_MIN_STATES}) and it
 * is not RETIRED, and the candidate's largest absolute signal correlation
 * with such a peer must not exceed the threshold.
 *
 * <p>{@code netPnlCiGate} (x-version 3) says whether the
 * {@code net_pnl_bootstrap_ci} gate is part of the CANDIDATE -> VALIDATING
 * evaluation: {@link #NET_PNL_CI_REQUIRED} — the default — evaluates it,
 * {@link #NET_PNL_CI_ABSENT} — the legacy policy, which had no such gate —
 * leaves it out, by name. {@code gates.netPnlCiLevel} is the confidence level
 * the interval in the evidence must have been taken at and
 * {@code gates.minNetPnlCiLow} the value its lower bound must exceed.
 */
public record PolicyConfig(String policy, Gates gates, int maxConsecutiveFailures,
        Live live, String tstatThreshold, LifecycleState crossAlphaMinState,
        String netPnlCiGate) {
    /**
     * {@code x-version} of {@code configs/strategies/lifecycle.json}: 2 since
     * v1.5.0 ({@code tstat_threshold}), 3 with the cross-alpha correlation
     * gate ({@code cross_alpha_min_state},
     * {@code gates.max_cross_alpha_correlation}) and the net P&amp;L bootstrap
     * gate ({@code net_pnl_ci_gate}, {@code gates.net_pnl_ci_level},
     * {@code gates.min_net_pnl_ci_low}).
     */
    public static final long LIFECYCLE_CONFIG_VERSION = 3;

    /**
     * Lifecycle states {@code cross_alpha_min_state} may name: the first
     * state an alpha holds AFTER passing the gate, and the two beyond it
     * before the live sub-machine. CANDIDATE and below are excluded by
     * construction — two candidates gating each other would block both.
     */
    public static final List<LifecycleState> CROSS_ALPHA_MIN_STATES = List.of(
            LifecycleState.VALIDATING, LifecycleState.PAPER, LifecycleState.ACTIVE);

    /** The default significance-threshold policy (class docs). */
    public static final String TSTAT_LEDGER = "ledger";

    /** The legacy significance-threshold policy (class docs). */
    public static final String TSTAT_FIXED = "fixed";

    /** The default bootstrap-gate policy: the gate is evaluated (class docs). */
    public static final String NET_PNL_CI_REQUIRED = "required";

    /** The legacy bootstrap-gate policy: the gate is left out (class docs). */
    public static final String NET_PNL_CI_ABSENT = "absent";

    private static final String[] GATE_KEYS = {"min_experiments_in_ledger",
        "min_oos_ic", "min_nw_tstat", "min_fold_sign_consistency", "min_folds",
        "min_net_return_bps", "min_capacity_usd", "max_ic_rank_gap",
        "ic_rank_gap_eps", "max_holdout_ic_gap", "min_paper_sessions",
        "max_paper_ic_gap", "min_paper_net_pnl", "max_kill_events",
        "max_cross_alpha_correlation", "min_net_pnl_ci_low", "net_pnl_ci_level"};

    /** Every promotion-gate threshold, one field per config key. */
    public record Gates(long minExperimentsInLedger, double minOosIc,
            double minNwTstat, double minFoldSignConsistency, long minFolds,
            double minNetReturnBps, double minCapacityUsd, double maxIcRankGap,
            double icRankGapEps, double maxHoldoutIcGap, long minPaperSessions,
            double maxPaperIcGap, double minPaperNetPnl, long maxKillEvents,
            double maxCrossAlphaCorrelation, double minNetPnlCiLow,
            double netPnlCiLevel) {
        public Gates {
            if (!(netPnlCiLevel > 0.0 && netPnlCiLevel < 1.0)) {
                throw new IllegalArgumentException(
                        "gates.net_pnl_ci_level must lie in (0, 1)");
            }
            Trees.finite(minNetPnlCiLow, "gates.min_net_pnl_ci_low");
            if (!(maxCrossAlphaCorrelation >= 0.0 && maxCrossAlphaCorrelation <= 1.0)) {
                throw new IllegalArgumentException(
                        "gates.max_cross_alpha_correlation must lie in [0, 1]");
            }
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
                    count(g, "max_kill_events", where, 0),
                    Trees.num(g, "max_cross_alpha_correlation", where),
                    Trees.num(g, "min_net_pnl_ci_low", where),
                    Trees.num(g, "net_pnl_ci_level", where));
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
            t.put("max_cross_alpha_correlation", maxCrossAlphaCorrelation);
            t.put("min_net_pnl_ci_low", minNetPnlCiLow);
            t.put("net_pnl_ci_level", netPnlCiLevel);
            return t;
        }
    }

    /**
     * The live sub-machine gates and retirement rule
     * ({@code strategies.json adaptive.lifecycle}), exactly the
     * {@link com.iap.adaptive.LifecycleGauge} factory arguments.
     * {@code breachRule} must be named; under {@code CONSECUTIVE} the two
     * CUSUM parameters may be absent from the block and are 0.
     */
    public record Live(double watchIcGate, double reactivateIcGate,
            int retireBreachEvals, int reactivateEvals,
            com.iap.adaptive.LifecycleGauge.BreachRule breachRule, double cusumK,
            double cusumH) {
        private static final String[] GATE_KEYS = {"watch_ic_gate",
            "reactivate_ic_gate", "retire_breach_evals", "reactivate_evals"};

        public Live {
            Trees.finite(watchIcGate, "adaptive.lifecycle.watch_ic_gate");
            Trees.finite(reactivateIcGate, "adaptive.lifecycle.reactivate_ic_gate");
            Trees.finite(cusumK, "adaptive.lifecycle.cusum_k");
            Trees.finite(cusumH, "adaptive.lifecycle.cusum_h");
            if (retireBreachEvals < 1 || reactivateEvals < 1) {
                throw new IllegalArgumentException("lifecycle eval counts must be >= 1");
            }
            if (reactivateIcGate < watchIcGate) {
                throw new IllegalArgumentException(
                        "reactivate_ic_gate must be >= watch_ic_gate");
            }
            if (breachRule == null) {
                throw new IllegalArgumentException(
                        "adaptive.lifecycle.breach_rule must be named");
            }
            if (cusumK < 0.0) {
                throw new IllegalArgumentException("cusum_k must be >= 0");
            }
            if (breachRule == com.iap.adaptive.LifecycleGauge.BreachRule.CUSUM
                    && !(cusumH > 0.0)) {
                throw new IllegalArgumentException("breach_rule 'cusum' needs cusum_h > 0");
            }
        }

        /**
         * Parse the block ({@code where} names file + key path). The four
         * gate keys and {@code breach_rule} are required; {@code cusum_k} and
         * {@code cusum_h} are required under {@code "cusum"} and optional
         * under {@code "consecutive"}; nothing else is accepted.
         */
        public static Live fromTree(Map<String, Object> l, String where) {
            for (String k : l.keySet()) {
                boolean known = k.equals("breach_rule") || k.equals("cusum_k")
                        || k.equals("cusum_h");
                for (String g : GATE_KEYS) {
                    known = known || g.equals(k);
                }
                if (!known) {
                    throw new IllegalArgumentException(where + ": unknown key '" + k + "'");
                }
            }
            for (String g : GATE_KEYS) {
                if (!l.containsKey(g)) {
                    throw new IllegalArgumentException(where + ": missing key '" + g + "'");
                }
            }
            if (!l.containsKey("breach_rule")) {
                throw new IllegalArgumentException(where + ": missing key 'breach_rule' "
                        + "('cusum', the default since v1.5.0, or 'consecutive', the "
                        + "rule up to v1.4.0)");
            }
            com.iap.adaptive.LifecycleGauge.BreachRule rule =
                    com.iap.adaptive.LifecycleGauge.BreachRule.parse(
                            Trees.str(l, "breach_rule", where), where);
            boolean cusum = rule == com.iap.adaptive.LifecycleGauge.BreachRule.CUSUM;
            if (cusum && (!l.containsKey("cusum_k") || !l.containsKey("cusum_h"))) {
                throw new IllegalArgumentException(where
                        + ": breach_rule 'cusum' needs cusum_k and cusum_h");
            }
            double k = l.containsKey("cusum_k") ? Trees.num(l, "cusum_k", where) : 0.0;
            double h = l.containsKey("cusum_h") ? Trees.num(l, "cusum_h", where) : 0.0;
            return new Live(Trees.num(l, "watch_ic_gate", where),
                    Trees.num(l, "reactivate_ic_gate", where),
                    (int) count(l, "retire_breach_evals", where, 1),
                    (int) count(l, "reactivate_evals", where, 1), rule, k, h);
        }

        /** These gates under the LEGACY consecutive-breach rule, named. */
        public static Live legacyConsecutive(double watchIcGate, double reactivateIcGate,
                int retireBreachEvals, int reactivateEvals) {
            return new Live(watchIcGate, reactivateIcGate, retireBreachEvals,
                    reactivateEvals,
                    com.iap.adaptive.LifecycleGauge.BreachRule.CONSECUTIVE, 0.0, 0.0);
        }

        /** A gauge with these gates and this rule, starting ACTIVE. */
        public com.iap.adaptive.LifecycleGauge gauge() {
            return com.iap.adaptive.LifecycleGauge.of(breachRule, watchIcGate,
                    reactivateIcGate, retireBreachEvals, reactivateEvals, cusumK, cusumH);
        }

        /**
         * JSON-ready view: every key, in the reference's order
         * ({@code LifecycleConfig.to_dict}).
         */
        public Map<String, Object> toTree() {
            Map<String, Object> t = Trees.ordered();
            t.put("watch_ic_gate", watchIcGate);
            t.put("reactivate_ic_gate", reactivateIcGate);
            t.put("retire_breach_evals", (long) retireBreachEvals);
            t.put("reactivate_evals", (long) reactivateEvals);
            t.put("breach_rule", breachRule.wire());
            t.put("cusum_k", cusumK);
            t.put("cusum_h", cusumH);
            return t;
        }
    }

    public PolicyConfig {
        if (policy == null || policy.isEmpty()) {
            throw new IllegalArgumentException("policy name must not be empty");
        }
        if (!TSTAT_LEDGER.equals(tstatThreshold) && !TSTAT_FIXED.equals(tstatThreshold)) {
            throw new IllegalArgumentException("tstat_threshold '" + tstatThreshold
                    + "' unknown; known: ledger, fixed");
        }
        if (maxConsecutiveFailures < 1) {
            throw new IllegalArgumentException(
                    "demotion.max_consecutive_failures must be >= 1");
        }
        if (gates == null || live == null) {
            throw new IllegalArgumentException("PolicyConfig: gates/live missing");
        }
        if (crossAlphaMinState == null
                || !CROSS_ALPHA_MIN_STATES.contains(crossAlphaMinState)) {
            throw new IllegalArgumentException("cross_alpha_min_state '"
                    + crossAlphaMinState + "' unknown; known: VALIDATING, PAPER, ACTIVE");
        }
        if (!NET_PNL_CI_REQUIRED.equals(netPnlCiGate)
                && !NET_PNL_CI_ABSENT.equals(netPnlCiGate)) {
            throw new IllegalArgumentException("net_pnl_ci_gate '" + netPnlCiGate
                    + "' unknown; known: required, absent");
        }
    }

    /** True when the policy evaluates the {@code net_pnl_bootstrap_ci} gate. */
    public boolean netPnlCiRequired() {
        return NET_PNL_CI_REQUIRED.equals(netPnlCiGate);
    }

    /** Parse {@code net_pnl_ci_gate} ({@code where} names file + key path). */
    private static String ciGate(Map<String, Object> doc, String where) {
        String name = Trees.str(doc, "net_pnl_ci_gate", where);
        if (!NET_PNL_CI_REQUIRED.equals(name) && !NET_PNL_CI_ABSENT.equals(name)) {
            throw new IllegalArgumentException(where + ".net_pnl_ci_gate: expected one "
                    + "of [required, absent], got '" + name + "'");
        }
        return name;
    }

    /** Parse {@code cross_alpha_min_state} ({@code where} names file + key path). */
    private static LifecycleState minState(Map<String, Object> doc, String where) {
        String name = Trees.str(doc, "cross_alpha_min_state", where);
        for (LifecycleState s : CROSS_ALPHA_MIN_STATES) {
            if (s.name().equals(name)) {
                return s;
            }
        }
        throw new IllegalArgumentException(where + ".cross_alpha_min_state: expected one "
                + "of [VALIDATING, PAPER, ACTIVE], got '" + name + "'");
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
        long version = Trees.i64(lifecycle, "x-version", lifecycleName);
        if (version != LIFECYCLE_CONFIG_VERSION) {
            throw new IllegalArgumentException(lifecycleName + ": x-version " + version
                    + " != " + LIFECYCLE_CONFIG_VERSION);
        }
        Trees.checkKeys(lifecycle, new String[] {"x-version", "description", "policy",
            "tstat_threshold", "cross_alpha_min_state", "net_pnl_ci_gate", "gates",
            "demotion"}, lifecycleName);
        String tstatThreshold = Trees.str(lifecycle, "tstat_threshold", lifecycleName);
        LifecycleState crossAlphaMinState = minState(lifecycle, lifecycleName);
        String netPnlCiGate = ciGate(lifecycle, lifecycleName);
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
        return new PolicyConfig(policy, gates, (int) maxFailures, live, tstatThreshold,
                crossAlphaMinState, netPnlCiGate);
    }

    /**
     * Inverse of {@link #toTree}: the merged view embedded in
     * {@code tests/golden/expected_lifecycle.json} ({@code policy},
     * {@code tstat_threshold}, {@code cross_alpha_min_state},
     * {@code net_pnl_ci_gate}, {@code gates}, {@code demotion}, {@code live}).
     */
    public static PolicyConfig fromTree(Map<String, Object> doc) {
        String where = "config";
        Trees.checkKeys(doc, new String[] {"policy", "tstat_threshold",
            "cross_alpha_min_state", "net_pnl_ci_gate", "gates", "demotion", "live"},
                where);
        Map<String, Object> demotion = Trees.obj(doc, "demotion", where);
        Trees.checkKeys(demotion, new String[] {"max_consecutive_failures"},
                "config.demotion");
        return new PolicyConfig(Trees.str(doc, "policy", where),
                Gates.fromTree(Trees.obj(doc, "gates", where), "config.gates"),
                (int) count(demotion, "max_consecutive_failures", "config.demotion", 1),
                Live.fromTree(Trees.obj(doc, "live", where), "config.live"),
                Trees.str(doc, "tstat_threshold", where), minState(doc, where),
                ciGate(doc, where));
    }

    /** JSON-ready merged view. */
    public Map<String, Object> toTree() {
        Map<String, Object> t = Trees.ordered();
        t.put("policy", policy);
        t.put("tstat_threshold", tstatThreshold);
        t.put("cross_alpha_min_state", crossAlphaMinState.name());
        t.put("net_pnl_ci_gate", netPnlCiGate);
        t.put("gates", gates.toTree());
        Map<String, Object> d = Trees.ordered();
        d.put("max_consecutive_failures", (long) maxConsecutiveFailures);
        t.put("demotion", d);
        t.put("live", live.toTree());
        return t;
    }
}
