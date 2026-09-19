package com.iap.lifecycle;

import java.util.Map;

import com.iap.contracts.Trees;

/**
 * Everything the gates may read for one {@code advance} call
 * ({@code iap.lifecycle.evidence.Evidence}). One block per lifecycle
 * stage, every block optional: {@code research} (an
 * {@link ExperimentResultRec}) + {@code capacityUsd} for RESEARCH /
 * CANDIDATE, {@code validation} for VALIDATING, {@code paper} for PAPER,
 * {@code live} for ACTIVE / WATCH. Every scalar is finite (NaN / infinity
 * is rejected on construction: a metric a runner could not compute is
 * absent, never a number). A missing block means "no evidence for that
 * stage" — silence is not evidence.
 */
public record Evidence(ExperimentResultRec research, Double capacityUsd,
        Validation validation, Paper paper, Live live) {
    private static final String[] KEYS = {"research", "capacity_usd",
        "validation", "paper", "live"};

    /** VALIDATING-stage evidence: the held-out replay through the production path. */
    public record Validation(double holdoutIc, double researchIc,
            boolean replayHashMatch, boolean parity) {
        private static final String[] V_KEYS = {"holdout_ic", "research_ic",
            "replay_hash_match", "parity"};

        public Validation {
            Trees.finite(holdoutIc, "validation.holdout_ic");
            Trees.finite(researchIc, "validation.research_ic");
        }

        /** JSON form. */
        public Map<String, Object> toTree() {
            Map<String, Object> t = Trees.ordered();
            t.put("holdout_ic", holdoutIc);
            t.put("research_ic", researchIc);
            t.put("replay_hash_match", replayHashMatch);
            t.put("parity", parity);
            return t;
        }

        /** Strict inverse of {@link #toTree}. */
        public static Validation fromTree(Map<String, Object> t) {
            String p = "validation";
            Trees.checkKeys(t, V_KEYS, p);
            return new Validation(Trees.num(t, "holdout_ic", p),
                    Trees.num(t, "research_ic", p),
                    Trees.bool(t, "replay_hash_match", p), Trees.bool(t, "parity", p));
        }
    }

    /** PAPER-stage evidence: the alpha traded in the paper environment. */
    public record Paper(long nSessions, double realizedIc, double researchIc,
            double netPnl, long nKillEvents, double trackingError) {
        private static final String[] P_KEYS = {"n_sessions", "realized_ic",
            "research_ic", "net_pnl", "n_kill_events", "tracking_error"};

        public Paper {
            if (nSessions < 0 || nKillEvents < 0) {
                throw new IllegalArgumentException(
                        "paper: n_sessions/n_kill_events must be >= 0");
            }
            Trees.finite(realizedIc, "paper.realized_ic");
            Trees.finite(researchIc, "paper.research_ic");
            Trees.finite(netPnl, "paper.net_pnl");
            Trees.finite(trackingError, "paper.tracking_error");
            if (trackingError < 0.0) {
                throw new IllegalArgumentException("paper.tracking_error must be >= 0");
            }
        }

        /** JSON form. */
        public Map<String, Object> toTree() {
            Map<String, Object> t = Trees.ordered();
            t.put("n_sessions", nSessions);
            t.put("realized_ic", realizedIc);
            t.put("research_ic", researchIc);
            t.put("net_pnl", netPnl);
            t.put("n_kill_events", nKillEvents);
            t.put("tracking_error", trackingError);
            return t;
        }

        /** Strict inverse of {@link #toTree}. */
        public static Paper fromTree(Map<String, Object> t) {
            String p = "paper";
            Trees.checkKeys(t, P_KEYS, p);
            return new Paper(Trees.ranged(t, "n_sessions", p, 0, Long.MAX_VALUE),
                    Trees.num(t, "realized_ic", p), Trees.num(t, "research_ic", p),
                    Trees.num(t, "net_pnl", p),
                    Trees.ranged(t, "n_kill_events", p, 0, Long.MAX_VALUE),
                    Trees.num(t, "tracking_error", p));
        }
    }

    /**
     * One live rolling-IC evaluation (API_ADAPTIVE.md §4/§6):
     * {@code rollingIc} null when too few buckets exist; {@code informative}
     * false when the matured set gained no new rows since the last counted
     * evaluation (a re-read of a frozen window moves nothing).
     */
    public record Live(Double rollingIc, long nBuckets, long evalIndex,
            boolean informative) {
        private static final String[] L_KEYS = {"rolling_ic", "n_buckets",
            "eval_index", "informative"};

        public Live {
            if (rollingIc != null) {
                Trees.finite(rollingIc, "live.rolling_ic");
            }
            if (nBuckets < 0 || evalIndex < 0) {
                throw new IllegalArgumentException(
                        "live: n_buckets/eval_index must be >= 0");
            }
        }

        /** JSON form. */
        public Map<String, Object> toTree() {
            Map<String, Object> t = Trees.ordered();
            t.put("rolling_ic", rollingIc);
            t.put("n_buckets", nBuckets);
            t.put("eval_index", evalIndex);
            t.put("informative", informative);
            return t;
        }

        /** Strict inverse of {@link #toTree}. */
        public static Live fromTree(Map<String, Object> t) {
            String p = "live";
            Trees.checkKeys(t, L_KEYS, p);
            return new Live(Trees.optNum(t, "rolling_ic", p),
                    Trees.ranged(t, "n_buckets", p, 0, Long.MAX_VALUE),
                    Trees.ranged(t, "eval_index", p, 0, Long.MAX_VALUE),
                    Trees.bool(t, "informative", p));
        }
    }

    public Evidence {
        if (capacityUsd != null) {
            Trees.finite(capacityUsd, "evidence.capacity_usd");
            if (capacityUsd < 0.0) {
                throw new IllegalArgumentException("evidence.capacity_usd must be >= 0");
            }
        }
    }

    /** No evidence at all (every block absent). */
    public static Evidence empty() {
        return new Evidence(null, null, null, null, null);
    }

    /** JSON form; absent blocks are {@code null}. */
    public Map<String, Object> toTree() {
        Map<String, Object> t = Trees.ordered();
        t.put("research", research == null ? null : research.toTree());
        t.put("capacity_usd", capacityUsd);
        t.put("validation", validation == null ? null : validation.toTree());
        t.put("paper", paper == null ? null : paper.toTree());
        t.put("live", live == null ? null : live.toTree());
        return t;
    }

    /** Strict inverse of {@link #toTree}. */
    public static Evidence fromTree(Map<String, Object> t) {
        String p = "evidence";
        Trees.checkKeys(t, KEYS, p);
        Object r = t.get("research");
        Object v = t.get("validation");
        Object pa = t.get("paper");
        Object l = t.get("live");
        return new Evidence(
                r == null ? null : ExperimentResultRec.fromTree(Trees.obj(r, p + ".research")),
                Trees.optNum(t, "capacity_usd", p),
                v == null ? null : Validation.fromTree(Trees.obj(v, p + ".validation")),
                pa == null ? null : Paper.fromTree(Trees.obj(pa, p + ".paper")),
                l == null ? null : Live.fromTree(Trees.obj(l, p + ".live")));
    }
}
