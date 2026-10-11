package com.iap.trace;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import com.iap.contracts.Trees;

/**
 * {@code schemas/execution/venue_decision.schema.json} (x-version 1): the
 * smart-order-router decision for one child order. {@code venueId} 0 =
 * NO_ROUTE; {@code candidates} sorted by unique venue id; a routed venue
 * must be an eligible candidate.
 */
public record VenueDecisionRec(long childOrderId, int venueId, String reason,
        List<VenueScore> candidates) {
    private static final String[] KEYS = {"child_order_id", "venue_id",
        "reason", "candidates"};

    /**
     * {@code $defs/VenueScore}: one candidate as the router saw it;
     * {@code rank} is 1-based among eligible venues and 0 when ineligible.
     */
    public record VenueScore(int venueId, boolean eligible,
            long displayedPriceTicks, long displayedQty, double takerFee,
            double makerRebate, double commissionPerMillion, long latencyMeanNs,
            int rank) {
        private static final String[] SCORE_KEYS = {"venue_id", "eligible",
            "displayed_price_ticks", "displayed_qty", "taker_fee", "maker_rebate",
            "commission_per_million", "latency_mean_ns", "rank"};

        public VenueScore {
            if (venueId < 0 || venueId > 0xFFFF || rank < 0 || rank > 0xFFFF) {
                throw new IllegalArgumentException("VenueScore: venue_id/rank outside u16");
            }
            if (displayedPriceTicks < 0 || displayedQty < 0 || latencyMeanNs < 0) {
                throw new IllegalArgumentException(
                        "VenueScore: displayed price/qty and latency must be >= 0");
            }
            Trees.finite(takerFee, "VenueScore.taker_fee");
            Trees.finite(makerRebate, "VenueScore.maker_rebate");
            Trees.finite(commissionPerMillion, "VenueScore.commission_per_million");
            if (eligible != (rank > 0)) {
                throw new IllegalArgumentException(
                        "VenueScore: eligible venues carry rank >= 1, ineligible ones rank 0");
            }
        }

        /** Schema-ordered tree. */
        public Map<String, Object> toTree() {
            Map<String, Object> t = Trees.ordered();
            t.put("venue_id", (long) venueId);
            t.put("eligible", eligible);
            t.put("displayed_price_ticks", displayedPriceTicks);
            t.put("displayed_qty", displayedQty);
            t.put("taker_fee", takerFee);
            t.put("maker_rebate", makerRebate);
            t.put("commission_per_million", commissionPerMillion);
            t.put("latency_mean_ns", latencyMeanNs);
            t.put("rank", (long) rank);
            return t;
        }

        /** Strict inverse of {@link #toTree}. */
        public static VenueScore fromTree(Map<String, Object> t) {
            String p = "VenueScore";
            Trees.checkKeys(t, SCORE_KEYS, p);
            return new VenueScore(Trees.u16(t, "venue_id", p),
                    Trees.bool(t, "eligible", p),
                    Trees.ranged(t, "displayed_price_ticks", p, 0, Long.MAX_VALUE),
                    Trees.ranged(t, "displayed_qty", p, 0, Long.MAX_VALUE),
                    Trees.num(t, "taker_fee", p), Trees.num(t, "maker_rebate", p),
                    Trees.num(t, "commission_per_million", p),
                    Trees.ranged(t, "latency_mean_ns", p, 0, Long.MAX_VALUE),
                    Trees.u16(t, "rank", p));
        }
    }

    public VenueDecisionRec {
        if (venueId < 0 || venueId > 0xFFFF) {
            throw new IllegalArgumentException("VenueDecision.venue_id outside u16");
        }
        if (reason == null) {
            throw new IllegalArgumentException("VenueDecision.reason is null");
        }
        candidates = List.copyOf(candidates);
        boolean routedEligible = false;
        for (int i = 0; i < candidates.size(); i++) {
            VenueScore c = candidates.get(i);
            if (i > 0 && c.venueId() <= candidates.get(i - 1).venueId()) {
                throw new IllegalArgumentException(
                        "VenueDecision: candidates must be sorted by unique venue_id");
            }
            routedEligible |= c.venueId() == venueId && c.eligible();
        }
        if (venueId != 0 && !routedEligible) {
            throw new IllegalArgumentException(
                    "VenueDecision: routed venue must be an eligible candidate");
        }
    }

    /** Schema-ordered tree. */
    public Map<String, Object> toTree() {
        Map<String, Object> t = Trees.ordered();
        t.put("child_order_id", Trees.u64Tree(childOrderId));
        t.put("venue_id", (long) venueId);
        t.put("reason", reason);
        List<Object> cs = new ArrayList<>(candidates.size());
        for (VenueScore c : candidates) {
            cs.add(c.toTree());
        }
        t.put("candidates", cs);
        return t;
    }

    /** Strict inverse of {@link #toTree}. */
    public static VenueDecisionRec fromTree(Map<String, Object> t) {
        String p = "VenueDecision";
        Trees.checkKeys(t, KEYS, p);
        List<VenueScore> cs = new ArrayList<>();
        for (Object o : Trees.arr(t, "candidates", p)) {
            cs.add(VenueScore.fromTree(Trees.obj(o, p + ".candidates[]")));
        }
        return new VenueDecisionRec(Trees.u64(t, "child_order_id", p),
                Trees.u16(t, "venue_id", p), Trees.str(t, "reason", p), cs);
    }
}
