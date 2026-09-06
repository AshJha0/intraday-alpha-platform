package com.iap.tca;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * TCA metric formulas (spec §19, API_PORTFOLIO_TCA.md §2) — pure functions,
 * golden-tested against {@code tests/golden/expected_tca.json} at 1e-9.
 * Sign convention everywhere: {@code s = +1} buy, {@code -1} sell; costs
 * are POSITIVE when execution is worse than the benchmark; bps figures
 * multiply by 1e4.
 */
public final class Tca {
    /** Pinned adverse-selection markout deltas (name → ns). */
    public static final Map<String, Long> ADVERSE_DELTAS_NS;

    static {
        Map<String, Long> m = new LinkedHashMap<>();
        m.put("100ms", 100_000_000L);
        m.put("1s", 1_000_000_000L);
        m.put("10s", 10_000_000_000L);
        ADVERSE_DELTAS_NS = java.util.Collections.unmodifiableMap(m);
    }

    /**
     * Perold implementation-shortfall record (exact pinned key set;
     * {@code delay + trading + opportunity == total_is} exactly).
     */
    public record Perold(double qtyTarget, double qtyFilled, double fillRate,
            double delayCost, double tradingCost, double opportunityCost,
            double totalIs, double delayBps, double tradingBps,
            double opportunityBps, double totalIsBps) {
    }

    /** Spread / impact split of the executed cost vs the fill-time mid. */
    public record SpreadImpact(double spreadCost, double impactCost,
            double execCostVsMid) {
    }

    /** OLS impact estimate (bps per unit participation). */
    public record ImpactRegression(double slopeBpsPerParticipation,
            double interceptBps, double r2, long n) {
    }

    /** Full per-order TCA record (spec §19 metric table). */
    public record OrderTca(long orderId, long instrumentId, String side,
            double decisionMid, double arrivalMid, double endMid,
            Double fillVwap, Double arrivalSlippageBps, Double vwapSlippageBps,
            Double twapSlippageBps, Perold perold, double spreadCost,
            double impactCost, double timingCost,
            Map<String, Double> adverseSelectionBps,
            Map<String, Integer> adverseSelectionN,
            Double executionAlphaVsVwapBps, int nFills) {
    }

    private Tca() {
    }

    /**
     * Perold IS decomposition (currency units + bps of Q*decision_mid).
     * Fills are (price, qty) pairs in submission order.
     */
    public static Perold peroldDecomposition(int sideSign, long qtyTarget,
            List<double[]> fills, double decisionMid, double arrivalMid,
            double endMid) {
        if (sideSign != 1 && sideSign != -1) {
            throw new IllegalArgumentException("side_sign must be +1 or -1");
        }
        if (qtyTarget <= 0) {
            throw new IllegalArgumentException("qty_target must be > 0");
        }
        if (decisionMid <= 0.0) {
            throw new IllegalArgumentException("decision_mid must be > 0");
        }
        double qf = 0.0;
        for (double[] f : fills) {
            qf += f[1];
        }
        if (qf > (double) qtyTarget) {
            throw new IllegalArgumentException("filled more than target");
        }
        double s = sideSign;
        double delay = s * qf * (arrivalMid - decisionMid);
        double trading = 0.0;
        for (double[] f : fills) {
            trading += f[1] * (f[0] - arrivalMid);
        }
        trading *= s;
        double opportunity = s * ((double) qtyTarget - qf) * (endMid - decisionMid);
        double total = delay + trading + opportunity;
        double denom = (double) qtyTarget * decisionMid;
        return new Perold((double) qtyTarget, qf, qf / (double) qtyTarget,
                delay, trading, opportunity, total,
                1e4 * delay / denom, 1e4 * trading / denom,
                1e4 * opportunity / denom, 1e4 * total / denom);
    }

    /**
     * Signed fill-VWAP slippage vs the arrival mid, in bps (null when
     * unfilled).
     */
    public static Double arrivalSlippageBps(TcaParentOrder order,
            double arrivalMid) {
        if (order.qtyFilled() == 0) {
            return null;
        }
        if (arrivalMid <= 0.0) {
            throw new IllegalArgumentException("arrival_mid must be > 0");
        }
        return 1e4 * order.sign() * (order.fillVwap() - arrivalMid) / arrivalMid;
    }

    /** Split executed cost vs fill-time mid into spread + impact (currency). */
    public static SpreadImpact spreadAndImpactCost(TcaParentOrder order) {
        double s = order.sign();
        double spread = 0.0;
        double execVsMid = 0.0;
        for (TcaFill f : order.fills) {
            spread += (double) f.qty() * f.halfSpreadAtFill();
            execVsMid += s * (double) f.qty() * (f.price() - f.midAtFill());
        }
        return new SpreadImpact(spread, execVsMid - spread, execVsMid);
    }

    /**
     * Mean post-fill markout {@code s*(mid(t+delta) - p_f)/p_f} bps per
     * pinned delta over the fills whose markout is DEFINED (pinned §2.5:
     * the timeline reaches {@code t_f + delta} and no HALT started inside
     * {@code (t_f, t_f + delta]}); null when no fill qualifies — a stale
     * last mid is never carried past the end of the data. Negative =
     * post-fill reversion; positive = continued adverse drift.
     */
    public static Map<String, Double> adverseSelection(TcaParentOrder order,
            MarketTimeline timeline) {
        return adverseSelectionWithCounts(order, timeline, new LinkedHashMap<>());
    }

    /** {@link #adverseSelection} filling {@code counts} with n_defined per delta. */
    public static Map<String, Double> adverseSelectionWithCounts(
            TcaParentOrder order, MarketTimeline timeline,
            Map<String, Integer> counts) {
        Map<String, Double> out = new LinkedHashMap<>();
        double s = order.sign();
        for (Map.Entry<String, Long> e : ADVERSE_DELTAS_NS.entrySet()) {
            double sum = 0.0;
            int n = 0;
            for (TcaFill f : order.fills) {
                long t = f.ts() + e.getValue();
                if (f.price() > 0.0 && timeline.midDefinedAt(t, f.ts())) {
                    sum += 1e4 * s * (timeline.midAt(t) - f.price()) / f.price();
                    n++;
                }
            }
            out.put(e.getKey(), n > 0 ? sum / n : null);
            counts.put(e.getKey(), n);
        }
        return out;
    }

    /**
     * Pinned window rules: {@code decision <= arrival <= end}, end inside
     * the timeline (no fabricated end_mid), every fill inside
     * {@code [arrival_ts, end_ts]}. Throws otherwise.
     */
    public static void validateOrderWindow(TcaParentOrder order,
            MarketTimeline timeline) {
        if (!(order.decisionTs <= order.arrivalTs && order.arrivalTs <= order.endTs)) {
            throw new IllegalArgumentException(
                    "order needs decision_ts <= arrival_ts <= end_ts");
        }
        if (timeline.size() == 0 || order.endTs > timeline.lastTs()) {
            throw new IllegalArgumentException("order " + order.orderId
                    + ": end_ts " + order.endTs + " is beyond the timeline end"
                    + " (end_mid would be fabricated)");
        }
        for (TcaFill f : order.fills) {
            if (f.ts() < order.arrivalTs || f.ts() > order.endTs) {
                throw new IllegalArgumentException("order " + order.orderId
                        + ": fill at " + f.ts() + " outside [" + order.arrivalTs
                        + ", " + order.endTs + "]");
            }
        }
    }

    /**
     * OLS of per-fill signed cost bps on per-fill participation; slope is
     * the impact estimate. Requires n &gt;= 3; zero x-variance returns
     * slope 0.
     */
    public static ImpactRegression impactRegression(double[] participation,
            double[] signedCostBps) {
        int n = participation.length;
        if (n != signedCostBps.length) {
            throw new IllegalArgumentException("length mismatch");
        }
        if (n < 3) {
            throw new IllegalArgumentException(
                    "need at least 3 fills for the impact regression");
        }
        double mx = 0.0;
        double my = 0.0;
        for (int i = 0; i < n; i++) {
            mx += participation[i];
            my += signedCostBps[i];
        }
        mx /= n;
        my /= n;
        double sxx = 0.0;
        for (double x : participation) {
            sxx += (x - mx) * (x - mx);
        }
        if (sxx == 0.0) {
            return new ImpactRegression(0.0, my, 0.0, n);
        }
        double sxy = 0.0;
        double syy = 0.0;
        for (int i = 0; i < n; i++) {
            sxy += (participation[i] - mx) * (signedCostBps[i] - my);
            syy += (signedCostBps[i] - my) * (signedCostBps[i] - my);
        }
        double slope = sxy / sxx;
        double r2 = syy > 0.0 ? (sxy * sxy) / (sxx * syy) : 0.0;
        return new ImpactRegression(slope, my - slope * mx, r2, n);
    }

    /** Full per-order TCA record against a market timeline. */
    public static OrderTca orderTca(TcaParentOrder order, MarketTimeline timeline) {
        validateOrderWindow(order, timeline);
        double md = timeline.midAt(order.decisionTs);
        double ma = timeline.midAt(order.arrivalTs);
        double me = timeline.midAt(order.endTs);
        if (Double.isNaN(md) || Double.isNaN(ma) || Double.isNaN(me)) {
            throw new IllegalArgumentException(
                    "order references time before the first market state");
        }
        List<double[]> pairs = order.fills.stream()
                .map(f -> new double[] {f.price(), (double) f.qty()})
                .toList();
        Perold perold = peroldDecomposition(order.sign(), order.qtyTarget,
                pairs, md, ma, me);
        SpreadImpact split = spreadAndImpactCost(order);
        Double vwapMkt = timeline.intervalVwap(order.arrivalTs, order.endTs);
        Double twapMkt = timeline.intervalTwap(order.arrivalTs, order.endTs);
        boolean filled = order.qtyFilled() > 0;
        double fv = order.fillVwap();
        double s = order.sign();
        Double vwapSlip = filled && vwapMkt != null && vwapMkt != 0.0
                ? 1e4 * s * (fv - vwapMkt) / vwapMkt : null;
        Double twapSlip = filled && twapMkt != null && twapMkt != 0.0
                ? 1e4 * s * (fv - twapMkt) / twapMkt : null;
        Map<String, Integer> nDefined = new LinkedHashMap<>();
        Map<String, Double> markouts = adverseSelectionWithCounts(order, timeline,
                nDefined);
        return new OrderTca(order.orderId, order.instrumentId,
                order.side == 0 ? "BUY" : "SELL", md, ma, me,
                filled ? fv : null, arrivalSlippageBps(order, ma),
                vwapSlip, twapSlip, perold, split.spreadCost(),
                split.impactCost(),
                perold.tradingCost() - split.execCostVsMid(),
                markouts, nDefined,
                vwapSlip == null ? null : -vwapSlip,
                order.fills.size());
    }
}
