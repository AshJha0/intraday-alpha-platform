package com.iap.lifecycle;

import java.util.ArrayList;
import java.util.List;
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
 *
 * <p>{@code significanceThreshold} (v1.5.0) is the PROMOTE t threshold the
 * research result was judged at — the multiple-testing ledger's threshold
 * under the default research methods. It sits beside {@code research}
 * because an experiment result has no field for it, and it is what the
 * {@code statistical_significance} gate compares {@code research.t_stat}
 * with under the default policy ({@link Gates}); {@code null} means the
 * evidence carries none. The key is always present on the wire.
 *
 * <p>{@code crossAlpha} (v1.5.0) carries what the
 * {@code cross_alpha_correlation} gate needs and nothing a port would have to
 * recompute: one {@link Peer} per OTHER registered alpha — its id, its
 * lifecycle state when the evidence was built and the correlation of the two
 * alphas' out-of-sample signals, in [-1, 1]. The peers are strictly
 * increasing by {@code alpha_id} (sorted, unique). The gate filters them by
 * state itself, so one document can be judged under another
 * {@code cross_alpha_min_state}. {@code null} means nobody measured the
 * correlations: the gate FAILS. An empty peer list is a statement — there is
 * no other alpha — and passes vacuously. The key is always present on the
 * wire.
 *
 * <p>{@code pnlBootstrap} (v1.5.0) carries the confidence interval the
 * {@code net_pnl_bootstrap_ci} gate reads, with everything that pins it, so
 * that no port resamples ({@link PnlBootstrap}). {@code null} means no
 * interval was computed: the gate FAILS. The key is always present on the
 * wire.
 */
public record Evidence(ExperimentResultRec research, Double capacityUsd,
        Double significanceThreshold, PnlBootstrap pnlBootstrap, CrossAlpha crossAlpha,
        Validation validation, Paper paper, Live live) {
    private static final String[] KEYS = {"research", "capacity_usd",
        "significance_threshold", "pnl_bootstrap", "cross_alpha", "validation", "paper",
        "live"};

    /**
     * The bootstrap interval of the pooled net P&amp;L at 1x costs, with what
     * pins it: {@code ciLow} / {@code ciHigh} (both {@code null} when the
     * series was too short for an interval), the confidence {@code level} in
     * (0, 1), the number of resamples, the seed, the mean block length of the
     * stationary bootstrap, the length of the resampled bar series and the
     * number of trades at 1x costs over the same folds.
     */
    public record PnlBootstrap(Double ciLow, Double ciHigh, double level,
            long nResamples, long seed, double meanBlock, long nBars, long nTrades) {
        private static final String[] B_KEYS = {"ci_low", "ci_high", "level",
            "n_resamples", "seed", "mean_block", "n_bars", "n_trades"};

        public PnlBootstrap {
            if ((ciLow == null) != (ciHigh == null)) {
                throw new IllegalArgumentException(
                        "pnl_bootstrap: ci_low and ci_high are both numbers or both null");
            }
            if (ciLow != null) {
                Trees.finite(ciLow, "pnl_bootstrap.ci_low");
                Trees.finite(ciHigh, "pnl_bootstrap.ci_high");
                if (ciHigh < ciLow) {
                    throw new IllegalArgumentException("pnl_bootstrap: ci_high < ci_low");
                }
            }
            Trees.finite(level, "pnl_bootstrap.level");
            if (!(level > 0.0 && level < 1.0)) {
                throw new IllegalArgumentException("pnl_bootstrap.level must lie in (0, 1)");
            }
            if (nResamples < 1) {
                throw new IllegalArgumentException("pnl_bootstrap.n_resamples must be >= 1");
            }
            if (seed < 0 || nBars < 0 || nTrades < 0) {
                throw new IllegalArgumentException(
                        "pnl_bootstrap: seed/n_bars/n_trades must be >= 0");
            }
            Trees.finite(meanBlock, "pnl_bootstrap.mean_block");
            if (meanBlock < 1.0) {
                throw new IllegalArgumentException("pnl_bootstrap.mean_block must be >= 1");
            }
        }

        /**
         * What the gate compares: {@code ciLow} — or {@code null} (no usable
         * interval: the gate fails closed) when the alpha made no trade, when
         * the interval was not taken at exactly {@code requiredLevel}, or when
         * it has no bounds.
         */
        public Double gateValue(double requiredLevel) {
            if (nTrades == 0 || level != requiredLevel) {
                return null;
            }
            return ciLow;
        }

        /** JSON form. */
        public Map<String, Object> toTree() {
            Map<String, Object> t = Trees.ordered();
            t.put("ci_low", ciLow);
            t.put("ci_high", ciHigh);
            t.put("level", level);
            t.put("n_resamples", nResamples);
            t.put("seed", seed);
            t.put("mean_block", meanBlock);
            t.put("n_bars", nBars);
            t.put("n_trades", nTrades);
            return t;
        }

        /** Strict inverse of {@link #toTree}. */
        public static PnlBootstrap fromTree(Map<String, Object> t) {
            String p = "pnl_bootstrap";
            Trees.checkKeys(t, B_KEYS, p);
            return new PnlBootstrap(Trees.optNum(t, "ci_low", p),
                    Trees.optNum(t, "ci_high", p), Trees.num(t, "level", p),
                    Trees.ranged(t, "n_resamples", p, 1, Long.MAX_VALUE),
                    Trees.ranged(t, "seed", p, 0, Long.MAX_VALUE),
                    Trees.num(t, "mean_block", p),
                    Trees.ranged(t, "n_bars", p, 0, Long.MAX_VALUE),
                    Trees.ranged(t, "n_trades", p, 0, Long.MAX_VALUE));
        }
    }

    /**
     * One other alpha as the correlation gate sees it: its id, its lifecycle
     * state when the evidence was built and the signal correlation with the
     * alpha under evaluation, in [-1, 1].
     */
    public record Peer(String alphaId, LifecycleState state, double correlation) {
        private static final String[] PEER_KEYS = {"alpha_id", "state", "correlation"};

        public Peer {
            if (alphaId == null || alphaId.isEmpty()) {
                throw new IllegalArgumentException(
                        "cross_alpha.peers[].alpha_id: expected a non-empty string");
            }
            if (state == null) {
                throw new IllegalArgumentException(
                        "cross_alpha.peers[].state: expected a lifecycle state name");
            }
            Trees.finite(correlation, "cross_alpha.peers[].correlation");
            if (correlation < -1.0 || correlation > 1.0) {
                throw new IllegalArgumentException(
                        "cross_alpha.peers[].correlation must lie in [-1, 1]");
            }
        }

        /**
         * True when the gate counts this peer under {@code minState}: its
         * state is at or beyond {@code minState} and it is not RETIRED.
         */
        public boolean eligible(LifecycleState minState) {
            return minState.index() <= state.index()
                    && state.index() < LifecycleState.RETIRED.index();
        }

        /** JSON form. */
        public Map<String, Object> toTree() {
            Map<String, Object> t = Trees.ordered();
            t.put("alpha_id", alphaId);
            t.put("state", state.name());
            t.put("correlation", correlation);
            return t;
        }

        /** Strict inverse of {@link #toTree}. */
        public static Peer fromTree(Map<String, Object> t) {
            String p = "cross_alpha.peers[]";
            Trees.checkKeys(t, PEER_KEYS, p);
            return new Peer(Trees.str(t, "alpha_id", p),
                    LifecycleState.parse(Trees.str(t, "state", p), p + ".state"),
                    Trees.num(t, "correlation", p));
        }
    }

    /**
     * The signal correlation of one alpha with every other registered alpha
     * (class docs). A peer is <em>eligible</em> under a policy when its state
     * is at or beyond the policy's {@code cross_alpha_min_state} and it is
     * not RETIRED (a retired alpha holds no allocation, so there is nothing
     * to be redundant with).
     */
    public record CrossAlpha(List<Peer> peers) {
        private static final String[] C_KEYS = {"peers"};

        public CrossAlpha {
            if (peers == null) {
                throw new IllegalArgumentException("cross_alpha.peers: expected an array");
            }
            for (Peer peer : peers) {
                if (peer == null) {
                    throw new IllegalArgumentException(
                            "cross_alpha.peers: expected peer entries");
                }
            }
            peers = List.copyOf(peers);
            for (int i = 1; i < peers.size(); i++) {
                if (peers.get(i).alphaId().compareTo(peers.get(i - 1).alphaId()) <= 0) {
                    throw new IllegalArgumentException(
                            "cross_alpha.peers: must be sorted by alpha_id, "
                                    + "without duplicates");
                }
            }
        }

        /** "There is no other alpha": the vacuous pass of the correlation gate. */
        public static CrossAlpha noPeers() {
            return new CrossAlpha(List.of());
        }

        /**
         * The gate statistic: the largest {@code |correlation|} over the
         * eligible peers, {@code 0.0} when there is none (the vacuous case).
         */
        public double maxAbsCorrelation(LifecycleState minState) {
            double best = 0.0;
            for (Peer peer : peers) {
                if (peer.eligible(minState)) {
                    best = Math.max(best, Math.abs(peer.correlation()));
                }
            }
            return best;
        }

        /** JSON form. */
        public Map<String, Object> toTree() {
            List<Object> rows = new ArrayList<>(peers.size());
            for (Peer peer : peers) {
                rows.add(peer.toTree());
            }
            Map<String, Object> t = Trees.ordered();
            t.put("peers", rows);
            return t;
        }

        /** Strict inverse of {@link #toTree}. */
        public static CrossAlpha fromTree(Map<String, Object> t) {
            String p = "cross_alpha";
            Trees.checkKeys(t, C_KEYS, p);
            List<Object> rows = Trees.arr(t, "peers", p);
            List<Peer> peers = new ArrayList<>(rows.size());
            for (Object row : rows) {
                peers.add(Peer.fromTree(Trees.obj(row, p + ".peers[]")));
            }
            return new CrossAlpha(peers);
        }
    }

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
     * evaluation (a re-read of a frozen window moves nothing);
     * {@code newFraction} in (0, 1] the share of the reading's window that is
     * new since the last counted one (v1.5.0: what the CUSUM retirement rule
     * weights the reading by; required under either rule).
     */
    public record Live(Double rollingIc, long nBuckets, long evalIndex,
            boolean informative, double newFraction) {
        private static final String[] L_KEYS = {"rolling_ic", "n_buckets",
            "eval_index", "informative", "new_fraction"};

        public Live {
            if (rollingIc != null) {
                Trees.finite(rollingIc, "live.rolling_ic");
            }
            if (nBuckets < 0 || evalIndex < 0) {
                throw new IllegalArgumentException(
                        "live: n_buckets/eval_index must be >= 0");
            }
            Trees.finite(newFraction, "live.new_fraction");
            if (!(newFraction > 0.0 && newFraction <= 1.0)) {
                throw new IllegalArgumentException("live.new_fraction must be in (0, 1]");
            }
        }

        /** JSON form. */
        public Map<String, Object> toTree() {
            Map<String, Object> t = Trees.ordered();
            t.put("rolling_ic", rollingIc);
            t.put("n_buckets", nBuckets);
            t.put("eval_index", evalIndex);
            t.put("informative", informative);
            t.put("new_fraction", newFraction);
            return t;
        }

        /** Strict inverse of {@link #toTree}. */
        public static Live fromTree(Map<String, Object> t) {
            String p = "live";
            Trees.checkKeys(t, L_KEYS, p);
            return new Live(Trees.optNum(t, "rolling_ic", p),
                    Trees.ranged(t, "n_buckets", p, 0, Long.MAX_VALUE),
                    Trees.ranged(t, "eval_index", p, 0, Long.MAX_VALUE),
                    Trees.bool(t, "informative", p), Trees.num(t, "new_fraction", p));
        }
    }

    public Evidence {
        if (capacityUsd != null) {
            Trees.finite(capacityUsd, "evidence.capacity_usd");
            if (capacityUsd < 0.0) {
                throw new IllegalArgumentException("evidence.capacity_usd must be >= 0");
            }
        }
        if (significanceThreshold != null) {
            Trees.finite(significanceThreshold, "evidence.significance_threshold");
            if (significanceThreshold <= 0.0) {
                throw new IllegalArgumentException(
                        "evidence.significance_threshold must be > 0");
            }
        }
    }

    /** No evidence at all (every block absent). */
    public static Evidence empty() {
        return new Evidence(null, null, null, null, null, null, null, null);
    }

    /** JSON form; absent blocks are {@code null}. */
    public Map<String, Object> toTree() {
        Map<String, Object> t = Trees.ordered();
        t.put("research", research == null ? null : research.toTree());
        t.put("capacity_usd", capacityUsd);
        t.put("significance_threshold", significanceThreshold);
        t.put("pnl_bootstrap", pnlBootstrap == null ? null : pnlBootstrap.toTree());
        t.put("cross_alpha", crossAlpha == null ? null : crossAlpha.toTree());
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
        Object b = t.get("pnl_bootstrap");
        Object c = t.get("cross_alpha");
        Object v = t.get("validation");
        Object pa = t.get("paper");
        Object l = t.get("live");
        return new Evidence(
                r == null ? null : ExperimentResultRec.fromTree(Trees.obj(r, p + ".research")),
                Trees.optNum(t, "capacity_usd", p),
                Trees.optNum(t, "significance_threshold", p),
                b == null ? null : PnlBootstrap.fromTree(Trees.obj(b, p + ".pnl_bootstrap")),
                c == null ? null : CrossAlpha.fromTree(Trees.obj(c, p + ".cross_alpha")),
                v == null ? null : Validation.fromTree(Trees.obj(v, p + ".validation")),
                pa == null ? null : Paper.fromTree(Trees.obj(pa, p + ".paper")),
                l == null ? null : Live.fromTree(Trees.obj(l, p + ".live")));
    }
}
