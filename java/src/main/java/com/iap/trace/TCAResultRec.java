package com.iap.trace;

import java.util.Map;
import java.util.TreeMap;

import com.iap.contracts.CanonicalJson;
import com.iap.contracts.Trees;

/**
 * {@code schemas/tca/tca_result.schema.json} (x-version 1): the per-parent
 * TCA record (API_PORTFOLIO_TCA.md §2). Prices in ticks; costs positive
 * when execution was worse than the benchmark; the Perold identity
 * {@code IS = delay + trading + opportunity} and the split
 * {@code trading = spread + impact + timing} hold to 1e-9;
 * {@code venueContributionBps} is keyed by decimal venue id.
 */
public record TCAResultRec(long parentOrderId, long instrumentId, int side,
        long qty, long filledQty, double fillRate, long arrivalPriceTicks,
        double avgFillPrice, double intervalVwap, double intervalTwap,
        double implementationShortfallBps, double delayCostBps,
        double tradingCostBps, double opportunityCostBps, double spreadCostBps,
        double impactBps, double feesBps, double timingCostBps,
        double slippageBps, double participationRate, long nFills,
        Map<String, Double> venueContributionBps, String algo,
        LatencyStats latencyNs) {
    private static final String[] KEYS = {"parent_order_id", "instrument_id",
        "side", "qty", "filled_qty", "fill_rate", "arrival_price_ticks",
        "avg_fill_price", "interval_vwap", "interval_twap",
        "implementation_shortfall_bps", "delay_cost_bps", "trading_cost_bps",
        "opportunity_cost_bps", "spread_cost_bps", "impact_bps", "fees_bps",
        "timing_cost_bps", "slippage_bps", "participation_rate", "n_fills",
        "venue_contribution_bps", "algo", "latency_ns"};

    /**
     * {@code $defs/LatencyStats}: submit-to-acknowledge latency over an
     * order's children, ns; quantiles nearest-rank;
     * {@code min <= p50 <= p99 <= max}.
     */
    public record LatencyStats(long min, double mean, long max, long p50, long p99) {
        private static final String[] LAT_KEYS = {"min", "mean", "max", "p50", "p99"};

        public LatencyStats {
            if (min < 0 || max < 0 || p50 < 0 || p99 < 0) {
                throw new IllegalArgumentException("LatencyStats: negative latency");
            }
            Trees.finite(mean, "LatencyStats.mean");
            if (mean < 0.0) {
                throw new IllegalArgumentException("LatencyStats.mean < 0");
            }
            if (!(min <= p50 && p50 <= p99 && p99 <= max)) {
                throw new IllegalArgumentException(
                        "LatencyStats: need min <= p50 <= p99 <= max");
            }
        }

        /** Schema-ordered tree. */
        public Map<String, Object> toTree() {
            Map<String, Object> t = Trees.ordered();
            t.put("min", min);
            t.put("mean", mean);
            t.put("max", max);
            t.put("p50", p50);
            t.put("p99", p99);
            return t;
        }

        /** Strict inverse of {@link #toTree}. */
        public static LatencyStats fromTree(Map<String, Object> t) {
            String p = "LatencyStats";
            Trees.checkKeys(t, LAT_KEYS, p);
            return new LatencyStats(Trees.ranged(t, "min", p, 0, Long.MAX_VALUE),
                    Trees.num(t, "mean", p),
                    Trees.ranged(t, "max", p, 0, Long.MAX_VALUE),
                    Trees.ranged(t, "p50", p, 0, Long.MAX_VALUE),
                    Trees.ranged(t, "p99", p, 0, Long.MAX_VALUE));
        }
    }

    public TCAResultRec {
        if (side != 0 && side != 1) {
            throw new IllegalArgumentException("TCAResult.side must be 0 or 1");
        }
        if (qty < 1) {
            throw new IllegalArgumentException("TCAResult.qty must be >= 1");
        }
        if (filledQty < 0 || filledQty > qty) {
            throw new IllegalArgumentException("TCAResult: filled_qty outside [0, qty]");
        }
        if (arrivalPriceTicks < 0) {
            throw new IllegalArgumentException("TCAResult.arrival_price_ticks < 0");
        }
        if (nFills < 0 || nFills > 0xFFFFFFFFL) {
            throw new IllegalArgumentException("TCAResult.n_fills outside u32");
        }
        for (double v : new double[] {fillRate, avgFillPrice, intervalVwap,
                intervalTwap, implementationShortfallBps, delayCostBps,
                tradingCostBps, opportunityCostBps, spreadCostBps, impactBps,
                feesBps, timingCostBps, slippageBps, participationRate}) {
            Trees.finite(v, "TCAResult");
        }
        if (fillRate < 0.0 || fillRate > 1.0 || participationRate < 0.0
                || participationRate > 1.0) {
            throw new IllegalArgumentException(
                    "TCAResult: fill_rate/participation_rate outside [0, 1]");
        }
        if (avgFillPrice < 0.0 || intervalVwap < 0.0 || intervalTwap < 0.0) {
            throw new IllegalArgumentException("TCAResult: negative price benchmark");
        }
        if (Math.abs(fillRate - (double) filledQty / (double) qty) > 1e-9) {
            throw new IllegalArgumentException("TCAResult: fill_rate != filled_qty / qty");
        }
        double perold = delayCostBps + tradingCostBps + opportunityCostBps;
        if (Math.abs(perold - implementationShortfallBps) > 1e-9) {
            throw new IllegalArgumentException("TCAResult: Perold identity violated");
        }
        double split = spreadCostBps + impactBps + timingCostBps;
        if (Math.abs(split - tradingCostBps) > 1e-9) {
            throw new IllegalArgumentException(
                    "TCAResult: trading != spread + impact + timing");
        }
        ParentOrderRec.checkAlgo(algo, "TCAResult.algo");
        if (latencyNs == null) {
            throw new IllegalArgumentException("TCAResult.latency_ns is null");
        }
        TreeMap<String, Double> copy = new TreeMap<>(CanonicalJson.KEY_ORDER);
        for (Map.Entry<String, Double> e : venueContributionBps.entrySet()) {
            String k = e.getKey();
            if (!k.matches("0|[1-9][0-9]*")) {
                throw new IllegalArgumentException(
                        "TCAResult.venue_contribution_bps: key '" + k
                                + "' is not a decimal id");
            }
            copy.put(k, Trees.finite(e.getValue(),
                    "TCAResult.venue_contribution_bps." + k));
        }
        venueContributionBps = java.util.Collections.unmodifiableMap(copy);
    }

    /** Schema-ordered tree. */
    public Map<String, Object> toTree() {
        Map<String, Object> t = Trees.ordered();
        t.put("parent_order_id", Trees.u64Tree(parentOrderId));
        t.put("instrument_id", instrumentId);
        t.put("side", (long) side);
        t.put("qty", qty);
        t.put("filled_qty", filledQty);
        t.put("fill_rate", fillRate);
        t.put("arrival_price_ticks", arrivalPriceTicks);
        t.put("avg_fill_price", avgFillPrice);
        t.put("interval_vwap", intervalVwap);
        t.put("interval_twap", intervalTwap);
        t.put("implementation_shortfall_bps", implementationShortfallBps);
        t.put("delay_cost_bps", delayCostBps);
        t.put("trading_cost_bps", tradingCostBps);
        t.put("opportunity_cost_bps", opportunityCostBps);
        t.put("spread_cost_bps", spreadCostBps);
        t.put("impact_bps", impactBps);
        t.put("fees_bps", feesBps);
        t.put("timing_cost_bps", timingCostBps);
        t.put("slippage_bps", slippageBps);
        t.put("participation_rate", participationRate);
        t.put("n_fills", nFills);
        t.put("venue_contribution_bps", new TreeMap<String, Object>(venueContributionBps));
        t.put("algo", algo);
        t.put("latency_ns", latencyNs.toTree());
        return t;
    }

    /** Strict inverse of {@link #toTree}. */
    public static TCAResultRec fromTree(Map<String, Object> t) {
        String p = "TCAResult";
        Trees.checkKeys(t, KEYS, p);
        Map<String, Object> raw = Trees.obj(t, "venue_contribution_bps", p);
        TreeMap<String, Double> venues = new TreeMap<>();
        for (String k : raw.keySet()) {
            venues.put(k, Trees.num(raw, k, p + ".venue_contribution_bps"));
        }
        return new TCAResultRec(Trees.u64(t, "parent_order_id", p),
                Trees.u32(t, "instrument_id", p),
                (int) Trees.ranged(t, "side", p, 0, 1),
                Trees.ranged(t, "qty", p, 1, Long.MAX_VALUE),
                Trees.ranged(t, "filled_qty", p, 0, Long.MAX_VALUE),
                Trees.num(t, "fill_rate", p),
                Trees.ranged(t, "arrival_price_ticks", p, 0, Long.MAX_VALUE),
                Trees.num(t, "avg_fill_price", p), Trees.num(t, "interval_vwap", p),
                Trees.num(t, "interval_twap", p),
                Trees.num(t, "implementation_shortfall_bps", p),
                Trees.num(t, "delay_cost_bps", p), Trees.num(t, "trading_cost_bps", p),
                Trees.num(t, "opportunity_cost_bps", p),
                Trees.num(t, "spread_cost_bps", p), Trees.num(t, "impact_bps", p),
                Trees.num(t, "fees_bps", p), Trees.num(t, "timing_cost_bps", p),
                Trees.num(t, "slippage_bps", p), Trees.num(t, "participation_rate", p),
                Trees.u32(t, "n_fills", p), venues, Trees.str(t, "algo", p),
                LatencyStats.fromTree(Trees.obj(t, "latency_ns", p)));
    }
}
