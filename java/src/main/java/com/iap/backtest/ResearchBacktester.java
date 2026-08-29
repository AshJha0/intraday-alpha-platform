package com.iap.backtest;

/**
 * Row-driven research backtester (spec section 18, research engine),
 * mirroring {@code iap.backtest.engine.Backtester} exactly — the golden
 * {@code tests/golden/expected_backtest.json} pins its EQ01 run.
 *
 * <p>Pinned semantics:
 * <ul>
 *   <li><b>Decision at t, execution at t + latency</b>: the signal at row i
 *       produces a target position; the trade toward that target executes at
 *       row {@code i + latency_rows} at that row's mid, with spread/fee/
 *       impact charged as explicit costs ({@link CostModel}). Rows whose
 *       mid/half-spread are invalid (NaN, or hs &lt; 0) cannot execute; the
 *       previous position carries (the stale target is NOT queued).</li>
 *   <li><b>Position rule</b>: target = sign(expected_return) * max_pos_qty
 *       when confidence &gt;= conf_min, else flat.</li>
 *   <li><b>Accounting identity</b> (tested):
 *       {@code total_pnl = gross_pnl - total_costs} with gross the
 *       mark-to-market price-move P&amp;L and costs the explicit component
 *       sums; open terminal positions stay marked at the final mid.</li>
 * </ul>
 */
public final class ResearchBacktester {
    /** Per-instrument result (mirrors iap.backtest.engine.InstrumentResult). */
    public static final class Result {
        public long instrumentId;
        public double totalPnl;
        public double grossPnl;
        public double totalCosts;
        public double spreadCost;
        public double feeCost;
        public double impactCost;
        public int tradeCount;
        public long tradedQty;
        public int nRows;
        public double[] equity;
        public double[] positions;
    }

    private final CostModel costModel;
    private final long maxPosQty;
    private final double confMin;
    private final int latencyRows;

    public ResearchBacktester(CostModel costModel, long maxPosQty,
            double confMin, int latencyRows) {
        if (latencyRows < 0) {
            throw new IllegalArgumentException("latency_rows must be >= 0");
        }
        if (maxPosQty <= 0) {
            throw new IllegalArgumentException("max_pos_qty must be positive");
        }
        this.costModel = costModel;
        this.maxPosQty = maxPosQty;
        this.confMin = confMin;
        this.latencyRows = latencyRows;
    }

    /**
     * Run one instrument. {@code mid} and {@code halfSpread} carry NaN on
     * invalid rows; {@code er}/{@code conf} are the alpha scores (finite).
     *
     * @param assetClass "EQUITY" / "ETF" / "FX"
     * @param lotSize FX qty-unit size (1 for equities)
     */
    public Result run(long instrumentId, long[] ts, double[] mid,
            double[] halfSpread, double[] er, double[] conf,
            String assetClass, double adv, double lotSize) {
        int n = ts.length;
        if (mid.length != n || halfSpread.length != n || er.length != n
                || conf.length != n) {
            throw new IllegalArgumentException("row arrays must align");
        }
        double unit = assetClass.equals("FX") ? lotSize : 1.0;

        // decision at i -> desired target at execution row i + latency
        double[] target = new double[n];
        for (int i = 0; i < n; i++) {
            double sign = Double.isFinite(er[i]) ? Math.signum(er[i]) : 0.0;
            target[i] = conf[i] >= confMin ? sign * maxPosQty : 0.0;
        }
        double[] execTarget = new double[n];
        java.util.Arrays.fill(execTarget, Double.NaN);
        if (latencyRows == 0) {
            System.arraycopy(target, 0, execTarget, 0, n);
        } else if (latencyRows < n) {
            System.arraycopy(target, 0, execTarget, latencyRows, n - latencyRows);
        }
        for (int i = 0; i < n; i++) {
            boolean executable = Double.isFinite(mid[i])
                    && Double.isFinite(halfSpread[i]) && halfSpread[i] >= 0.0;
            if (!executable) {
                execTarget[i] = Double.NaN; // cannot trade here; carry position
            }
        }

        double[] pos = new double[n];
        double prev = 0.0;
        for (int i = 0; i < n; i++) {
            pos[i] = Double.isNaN(execTarget[i]) ? prev : execTarget[i];
            prev = pos[i];
        }

        Result r = new Result();
        r.instrumentId = instrumentId;
        r.nRows = n;
        double cash = 0.0;
        double posBefore = 0.0;
        for (int i = 0; i < n; i++) {
            double trade = pos[i] - posBefore;
            posBefore = pos[i];
            if (trade != 0.0) {
                double[] comp = costModel.costComponents(trade, mid[i],
                        halfSpread[i], assetClass, adv, lotSize);
                r.spreadCost += comp[0];
                r.feeCost += comp[1];
                r.impactCost += comp[2];
                cash += -trade * unit * mid[i] - (comp[0] + comp[1] + comp[2]);
                r.tradeCount++;
                r.tradedQty += (long) Math.abs(trade);
            }
        }
        r.totalCosts = r.spreadCost + r.feeCost + r.impactCost;

        // mark = forward-filled mid (leading gaps take the first finite mid)
        double[] mark = new double[n];
        double last = Double.NaN;
        for (int i = 0; i < n; i++) {
            if (Double.isFinite(mid[i])) {
                last = mid[i];
            }
            mark[i] = last;
        }
        if (n > 0 && !Double.isFinite(mark[0])) {
            double fill = 0.0;
            for (int i = 0; i < n; i++) {
                if (Double.isFinite(mid[i])) {
                    fill = mid[i];
                    break;
                }
            }
            for (int i = 0; i < n && !Double.isFinite(mark[i]); i++) {
                mark[i] = fill;
            }
        }

        double gross = 0.0;
        for (int i = 0; i + 1 < n; i++) {
            gross += pos[i] * unit * (mark[i + 1] - mark[i]);
        }
        r.grossPnl = gross;

        r.equity = new double[n];
        r.positions = pos;
        double cash2 = 0.0;
        double before = 0.0;
        for (int i = 0; i < n; i++) {
            double trade = pos[i] - before;
            before = pos[i];
            if (trade != 0.0) {
                double[] comp = costModel.costComponents(trade, mid[i],
                        halfSpread[i], assetClass, adv, lotSize);
                cash2 += -trade * unit * mid[i] - (comp[0] + comp[1] + comp[2]);
            }
            r.equity[i] = cash2 + pos[i] * unit * mark[i];
        }
        r.totalPnl = n > 0 ? r.equity[n - 1] : 0.0;
        return r;
    }
}
