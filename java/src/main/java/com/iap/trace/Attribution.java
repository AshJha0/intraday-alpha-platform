package com.iap.trace;

import java.util.Map;

import com.iap.contracts.Trees;

/**
 * {@code decision_trace.schema.json#/$defs/Attribution}: P&amp;L attribution
 * of one decision in bps of traded notional; signs are contributions
 * (negative = cost) and {@code totalBps} is the sum of the five (1e-9).
 *
 * <p>The pinned decomposition from a signal and a TCA record
 * ({@link #of}): {@code alpha = sign(side) * expected_return * 1e4},
 * {@code spread = -spread_cost_bps}, {@code impact = -impact_bps},
 * {@code fees = -fees_bps}, {@code timing = -timing_cost_bps}. The realized
 * P&amp;L never enters it; the residual is reported beside it, never
 * absorbed.
 */
public record Attribution(double alphaBps, double spreadBps, double impactBps,
        double feesBps, double timingBps, double totalBps) {
    private static final String[] KEYS = {"alpha_bps", "spread_bps",
        "impact_bps", "fees_bps", "timing_bps", "total_bps"};

    public Attribution {
        for (double v : new double[] {alphaBps, spreadBps, impactBps, feesBps,
                timingBps, totalBps}) {
            Trees.finite(v, "Attribution");
        }
        double sum = alphaBps + spreadBps + impactBps + feesBps + timingBps;
        if (Math.abs(sum - totalBps) > 1e-9) {
            throw new IllegalArgumentException(
                    "Attribution: total_bps != sum of components");
        }
    }

    /** The pinned decomposition (see class doc); {@code side} 0 = BID. */
    public static Attribution of(int side, double expectedReturn, TCAResultRec tca) {
        double alpha = (side == 0 ? 1.0 : -1.0) * expectedReturn * 1e4;
        double spread = -tca.spreadCostBps();
        double impact = -tca.impactBps();
        double fees = -tca.feesBps();
        double timing = -tca.timingCostBps();
        return new Attribution(alpha, spread, impact, fees, timing,
                alpha + spread + impact + fees + timing);
    }

    /** Schema-ordered tree. */
    public Map<String, Object> toTree() {
        Map<String, Object> t = Trees.ordered();
        t.put("alpha_bps", alphaBps);
        t.put("spread_bps", spreadBps);
        t.put("impact_bps", impactBps);
        t.put("fees_bps", feesBps);
        t.put("timing_bps", timingBps);
        t.put("total_bps", totalBps);
        return t;
    }

    /** Strict inverse of {@link #toTree}. */
    public static Attribution fromTree(Map<String, Object> t) {
        String p = "Attribution";
        Trees.checkKeys(t, KEYS, p);
        return new Attribution(Trees.num(t, "alpha_bps", p),
                Trees.num(t, "spread_bps", p), Trees.num(t, "impact_bps", p),
                Trees.num(t, "fees_bps", p), Trees.num(t, "timing_bps", p),
                Trees.num(t, "total_bps", p));
    }
}
