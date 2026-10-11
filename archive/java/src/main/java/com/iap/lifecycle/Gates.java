package com.iap.lifecycle;

import java.util.function.BiFunction;
import java.util.function.Function;

/**
 * The lifecycle gate table ({@code iap.lifecycle.gates.GATE_SPECS}): every
 * gate is one row — name, evidence block, metric, comparison kind and the
 * config key holding its threshold. Comparison kinds (pinned):
 * {@code MIN} ⇒ {@code value >= threshold}; {@code MAX} ⇒
 * {@code value <= threshold}; {@code GT} ⇒ {@code value > threshold}
 * (strict); {@code BOOL} ⇒ the metric itself with {@code value} and
 * {@code threshold} {@code null}. A gate whose evidence block is absent, or
 * whose metric is absent, FAILS with {@code value = null} (a missing number
 * never passes); the machine decides separately whether an absent block
 * counts as a failure or as silence.
 *
 * <p><b>The significance threshold</b> (v1.5.0). {@code statistical_significance}
 * is the one gate whose threshold is not a config constant. Under the default
 * policy ({@code PolicyConfig.tstatThreshold = "ledger"}) it is
 * {@code max(min_nw_tstat, evidence.significance_threshold)} — the
 * multiple-testing threshold the research result was judged at, never below
 * the configured floor — and when the evidence carries no threshold the gate
 * FAILS with {@code threshold = null} (the value is still reported). Under
 * {@code "fixed"} — the rule up to v1.4.0 — the threshold is
 * {@code min_nw_tstat} and the evidence field is not read. The gate table
 * (names, blocks, kinds, edges) is unchanged.
 *
 * <p><b>The net P&amp;L bootstrap gate</b> (v1.5.0). {@code net_pnl_bootstrap_ci}
 * sits on the CANDIDATE -> VALIDATING edge directly after
 * {@code net_pnl_after_costs} and requires
 * {@code ci_low > min_net_pnl_ci_low} (strict: a lower bound of exactly 0.0
 * fails) for the interval in {@code evidence.pnl_bootstrap}. The metric is
 * ABSENT — the gate FAILS with {@code value = null}, it does not pass
 * vacuously — when the block is null, when the alpha made no trade
 * ({@code n_trades = 0}), when the interval was taken at another level than
 * {@code gates.net_pnl_ci_level} (exact comparison) or when it has no bounds.
 * Under {@code PolicyConfig.netPnlCiGate = "absent"} — the legacy policy —
 * the machine does not evaluate the gate and it is part of no result
 * ({@link AlphaLifecycle#edgeGates}).
 *
 * <p><b>The cross-alpha correlation gate</b> (v1.5.0). A new alpha must be
 * additive: {@code cross_alpha_correlation} sits on the CANDIDATE -> VALIDATING
 * edge (last) and requires
 * {@code max |correlation| <= max_cross_alpha_correlation} over the ELIGIBLE
 * peers of {@code evidence.cross_alpha} — every other registered alpha whose
 * lifecycle state is at or beyond {@code PolicyConfig.crossAlphaMinState}
 * (VALIDATING by default) and is not RETIRED. The absolute value is taken:
 * the mirror image of an allocated alpha adds nothing either. With no
 * eligible peer the statistic is 0.0 and the gate passes — vacuously, and
 * the result says so by its value. {@code MAX} is inclusive: a correlation
 * exactly at the threshold passes. An absent {@code cross_alpha} block —
 * nobody measured the correlations — FAILS with {@code value = null} (fail
 * closed).
 *
 * <p>The stability rule {@code |ic - rank_ic| / max(|ic|, eps) <= max_ic_rank_gap}
 * requires the rank IC to lie in {@code [0, 2 * ic]} for a positive IC: same
 * sign, at most twice the linear IC — the one pair of numbers in an
 * experiment result that measures the shape of the signal–label relation
 * rather than its strength.
 */
public enum Gates {
    LEDGER_ENTRY_EXISTS("ledger_entry_exists", "research", Kind.MIN,
            "min_experiments_in_ledger",
            (ev, c) -> ev.research() == null ? null
                    : (double) ev.research().nExperimentsInLedger(),
            c -> (double) c.gates().minExperimentsInLedger()),
    LEAKAGE_CLEAN("leakage_clean", "research", Kind.BOOL, null,
            (ev, c) -> ev.research() == null ? null : bool(ev.research().leakagePassed()),
            null),
    OOS_IC("oos_ic", "research", Kind.MIN, "min_oos_ic",
            (ev, c) -> ev.research() == null ? null : ev.research().ic(),
            c -> c.gates().minOosIc()),
    STATISTICAL_SIGNIFICANCE("statistical_significance", "research", Kind.MIN,
            "min_nw_tstat",
            (ev, c) -> ev.research() == null ? null : ev.research().tStat(),
            c -> c.gates().minNwTstat()),
    FOLD_CONSISTENCY("fold_consistency", "research", Kind.MIN,
            "min_fold_sign_consistency",
            (ev, c) -> ev.research() == null ? null : ev.research().foldConsistency(),
            c -> c.gates().minFoldSignConsistency()),
    FOLD_COUNT("fold_count", "research", Kind.MIN, "min_folds",
            (ev, c) -> ev.research() == null ? null : (double) ev.research().nFolds(),
            c -> (double) c.gates().minFolds()),
    HYPOTHESIS_SIGN("hypothesis_sign", "research", Kind.BOOL, null,
            (ev, c) -> ev.research() == null ? null
                    : bool(Boolean.TRUE.equals(ev.research().hypothesisSignConfirmed())),
            null),
    NET_PNL_AFTER_COSTS("net_pnl_after_costs", "research", Kind.GT,
            "min_net_return_bps",
            (ev, c) -> ev.research() == null ? null : ev.research().netReturnBps(),
            c -> c.gates().minNetReturnBps()),
    NET_PNL_BOOTSTRAP_CI("net_pnl_bootstrap_ci", "pnl_bootstrap", Kind.GT,
            "min_net_pnl_ci_low",
            (ev, c) -> ev.pnlBootstrap() == null ? null
                    : ev.pnlBootstrap().gateValue(c.gates().netPnlCiLevel()),
            c -> c.gates().minNetPnlCiLow()),
    CAPACITY("capacity", "capacity", Kind.MIN, "min_capacity_usd",
            (ev, c) -> ev.capacityUsd(), c -> c.gates().minCapacityUsd()),
    STABILITY("stability", "research", Kind.MAX, "max_ic_rank_gap",
            (ev, c) -> ev.research() == null ? null
                    : icRankGap(ev.research().ic(), ev.research().rankIc(),
                            c.gates().icRankGapEps()),
            c -> c.gates().maxIcRankGap()),
    CROSS_ALPHA_CORRELATION("cross_alpha_correlation", "cross_alpha", Kind.MAX,
            "max_cross_alpha_correlation",
            (ev, c) -> ev.crossAlpha() == null ? null
                    : ev.crossAlpha().maxAbsCorrelation(c.crossAlphaMinState()),
            c -> c.gates().maxCrossAlphaCorrelation()),
    HOLDOUT_IC_TRACKS_RESEARCH("holdout_ic_tracks_research", "validation", Kind.MAX,
            "max_holdout_ic_gap",
            (ev, c) -> ev.validation() == null ? null
                    : Math.abs(ev.validation().holdoutIc() - ev.validation().researchIc()),
            c -> c.gates().maxHoldoutIcGap()),
    REPLAY_REPRODUCIBLE("replay_reproducible", "validation", Kind.BOOL, null,
            (ev, c) -> ev.validation() == null ? null
                    : bool(ev.validation().replayHashMatch()),
            null),
    CROSS_LANGUAGE_PARITY("cross_language_parity", "validation", Kind.BOOL, null,
            (ev, c) -> ev.validation() == null ? null : bool(ev.validation().parity()),
            null),
    PAPER_MIN_SESSIONS("paper_min_sessions", "paper", Kind.MIN, "min_paper_sessions",
            (ev, c) -> ev.paper() == null ? null : (double) ev.paper().nSessions(),
            c -> (double) c.gates().minPaperSessions()),
    PAPER_IC_TRACKING("paper_ic_tracking", "paper", Kind.MAX, "max_paper_ic_gap",
            (ev, c) -> ev.paper() == null ? null
                    : Math.abs(ev.paper().realizedIc() - ev.paper().researchIc()),
            c -> c.gates().maxPaperIcGap()),
    PAPER_NET_PNL("paper_net_pnl", "paper", Kind.MIN, "min_paper_net_pnl",
            (ev, c) -> ev.paper() == null ? null : ev.paper().netPnl(),
            c -> c.gates().minPaperNetPnl()),
    NO_KILL_EVENTS("no_kill_events", "paper", Kind.MAX, "max_kill_events",
            (ev, c) -> ev.paper() == null ? null : (double) ev.paper().nKillEvents(),
            c -> (double) c.gates().maxKillEvents()),
    ROLLING_IC("rolling_ic", "live", Kind.MIN, "watch_ic_gate",
            (ev, c) -> ev.live() == null || !ev.live().informative() ? null
                    : ev.live().rollingIc(),
            c -> c.live().watchIcGate());

    /** Comparison kind. */
    public enum Kind {
        MIN, MAX, GT, BOOL
    }

    private static final double BOOL_TRUE = 1.0;

    private final String gateName;
    private final String block;
    private final Kind kind;
    private final String thresholdKey;
    /** Numeric metric (null = absent); BOOL gates encode true as 1.0. */
    private final BiFunction<Evidence, PolicyConfig, Double> metric;
    private final Function<PolicyConfig, Double> threshold;

    Gates(String gateName, String block, Kind kind, String thresholdKey,
            BiFunction<Evidence, PolicyConfig, Double> metric,
            Function<PolicyConfig, Double> threshold) {
        this.gateName = gateName;
        this.block = block;
        this.kind = kind;
        this.thresholdKey = thresholdKey;
        this.metric = metric;
        this.threshold = threshold;
    }

    private static Double bool(boolean b) {
        return b ? BOOL_TRUE : 0.0;
    }

    /** The pinned stability statistic {@code |ic - rank_ic| / max(|ic|, eps)}. */
    public static double icRankGap(double ic, double rankIc, double eps) {
        return Math.abs(ic - rankIc) / Math.max(Math.abs(ic), eps);
    }

    /** Gate name on the wire. */
    public String gateName() {
        return gateName;
    }

    /** Evidence block the gate reads. */
    public String block() {
        return block;
    }

    /** Comparison kind. */
    public Kind kind() {
        return kind;
    }

    /** Config key of the threshold ({@code null} for BOOL gates). */
    public String thresholdKey() {
        return thresholdKey;
    }

    /**
     * The configured threshold ({@code null} for a BOOL gate). For
     * {@code statistical_significance} this is the floor {@code min_nw_tstat};
     * {@link #thresholdFor} gives the threshold applied to an evidence.
     */
    public Double threshold(PolicyConfig config) {
        return threshold == null ? null : threshold.apply(config);
    }

    /**
     * The threshold this gate applies to {@code evidence} (class docs, "The
     * significance threshold"); {@code null} for a BOOL gate and for the
     * ledger policy when the evidence carries no significance threshold.
     */
    public Double thresholdFor(Evidence evidence, PolicyConfig config) {
        Double configured = threshold(config);
        if (this != STATISTICAL_SIGNIFICANCE
                || PolicyConfig.TSTAT_FIXED.equals(config.tstatThreshold())) {
            return configured;
        }
        Double carried = evidence.significanceThreshold();
        if (carried == null) {
            return null;
        }
        return Math.max(configured, carried);
    }

    /** Pure: the same evidence always yields the same result. */
    public GateResult evaluate(Evidence evidence, PolicyConfig config) {
        Double m = metric.apply(evidence, config);
        if (kind == Kind.BOOL) {
            return new GateResult(m != null && m == BOOL_TRUE, null, null);
        }
        Double th = thresholdFor(evidence, config);
        if (m == null) {
            return new GateResult(false, null, th);
        }
        double value = m;
        if (th == null) { // the ledger policy with no threshold in the evidence
            return new GateResult(false, value, null);
        }
        boolean passed = switch (kind) {
            case MIN -> value >= th;
            case MAX -> value <= th;
            case GT -> value > th;
            case BOOL -> throw new IllegalStateException("unreachable");
        };
        return new GateResult(passed, value, th);
    }

    /** Look a gate up by wire name. */
    public static Gates byName(String name) {
        for (Gates g : values()) {
            if (g.gateName.equals(name)) {
                return g;
            }
        }
        throw new IllegalArgumentException("unknown lifecycle gate '" + name + "'");
    }
}
