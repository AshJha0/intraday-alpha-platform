package com.iap.backtest;

import java.util.Arrays;

/**
 * Row-driven research backtester (spec section 18, research engine): the
 * port of {@code iap.backtest.engine.Backtester} in rows-mode latency, for
 * one instrument quoted in the reporting currency. The golden
 * {@code tests/golden/expected_backtest.json} pins it under both rule sets.
 *
 * <p><b>Rules (v1.5.0).</b> {@link Config#defaults} is the Python default —
 * the cost-aware position policy, fills capped at the displayed L1 size and
 * decisions only on the rows the IC scores — priced by the default
 * square-root {@link CostModel}. {@link Config#legacy} names the v1.4.0
 * rules ({@code BacktestConfig.legacy()}): the sign policy, uncapped fills,
 * every row traded; pair it with {@link CostModel#withLinearImpact()}.
 * Nothing on the paper path uses this class.
 *
 * <p>Pinned semantics:
 * <ul>
 *   <li><b>Decision at t, execution at t + latency</b>: the signal at row i
 *       produces a target position; the trade toward that target executes at
 *       row {@code i + latency_rows} at that row's mid, with spread/fee/
 *       impact charged as explicit costs ({@link CostModel}). Rows whose
 *       mid/half-spread are invalid (NaN, or hs &lt; 0) cannot execute; the
 *       previous position carries (the stale target is NOT queued).</li>
 *   <li><b>Position rule</b> {@code "cost_aware"} (default): the entry
 *       threshold at a row is {@link CostModel#roundTripCostReturn}; enter
 *       from flat only when confidence &gt;= conf_min and
 *       |expected_return| exceeds it; hold until {@code horizon_ns} of event
 *       time has elapsed since entry, or until an opposite signal that
 *       itself clears the threshold flips the position and restarts the
 *       clock; at expiry a same-direction signal clearing
 *       {@code hysteresis * threshold} renews the hold without trading,
 *       otherwise the position is closed. The policy needs
 *       {@code horizon_ns} ({@link Config#forHorizon}); a run without it is
 *       an error, never a silent fall-back.</li>
 *   <li><b>Position rule</b> {@code "sign"} (legacy): target =
 *       sign(expected_return) * max_pos_qty when confidence &gt;= conf_min,
 *       else flat, re-decided on every row.</li>
 *   <li><b>Fill cap</b> ({@code cap_fills_at_l1}): the quantity traded at a
 *       row is capped at the displayed L1 size on the side it takes (ask
 *       size for a buy, bid size for a sell; a non-finite or non-positive
 *       size fills nothing). The unfilled remainder is not queued.</li>
 *   <li><b>Blocked rows</b> ({@code block_rows}): a row whose
 *       {@code allowed} flag is false produces no decision (the position
 *       carries). Python's default {@code block_rows_column = "auto"}
 *       derives that mask from the labels
 *       ({@code iap.labels.frames.scored_rows}); Java has no label engine,
 *       so the mask is an INPUT of {@link #run} and the golden embeds the
 *       one the reference computed.</li>
 *   <li><b>Accounting identity</b> (tested):
 *       {@code total_pnl = gross_pnl - total_costs} with gross the
 *       mark-to-market price-move P&amp;L and costs the explicit component
 *       sums; open terminal positions stay marked at the final mid.</li>
 * </ul>
 *
 * <p>Not ported (Python only, none of them a default): TIME-mode latency
 * ({@code latency_ns}), {@code max_decision_age_ns},
 * {@code flatten_at_session_end}, and the per-row currency conversion.
 */
public final class ResearchBacktester {
    /** The default position policy ({@code iap.backtest.engine}). */
    public static final String DEFAULT_POSITION_POLICY = "cost_aware";

    /** The rule that was the default up to v1.4.0. */
    public static final String LEGACY_POSITION_POLICY = "sign";

    /** Default fraction of the entry threshold that renews an expired hold. */
    public static final double DEFAULT_HYSTERESIS = 0.5;

    /** {@link Config#horizonNs()} value meaning "not set". */
    public static final long NO_HORIZON = 0L;

    /**
     * Research backtest configuration (mirrors
     * {@code iap.backtest.engine.BacktestConfig}; rows-mode latency).
     *
     * @param positionPolicy {@code "cost_aware"} or {@code "sign"}
     * @param horizonNs label horizon the expected return is over
     *     ({@link #NO_HORIZON} = unset; the cost-aware policy needs it)
     * @param hysteresis fraction of the entry threshold a same-direction
     *     signal must clear to renew an expired hold, in [0, 1]
     * @param capFillsAtL1 cap each fill at the displayed L1 size
     * @param blockRows only rows flagged {@code allowed} make a decision
     */
    public record Config(long maxPosQty, double confMin, int latencyRows,
            String positionPolicy, long horizonNs, double hysteresis,
            boolean capFillsAtL1, boolean blockRows) {
        public Config {
            if (!DEFAULT_POSITION_POLICY.equals(positionPolicy)
                    && !LEGACY_POSITION_POLICY.equals(positionPolicy)) {
                throw new IllegalArgumentException("unknown position_policy '"
                        + positionPolicy + "'; known: cost_aware, sign");
            }
            if (horizonNs < 0) {
                throw new IllegalArgumentException("horizon_ns must be positive");
            }
            if (!(hysteresis >= 0.0 && hysteresis <= 1.0)) {
                throw new IllegalArgumentException("hysteresis must be in [0, 1]");
            }
            if (latencyRows < 0) {
                throw new IllegalArgumentException("latency_rows must be >= 0");
            }
            if (maxPosQty <= 0) {
                throw new IllegalArgumentException("max_pos_qty must be positive");
            }
        }

        /**
         * The v1.5.0 default rules: cost-aware positions, fills capped at
         * the displayed size, scored rows only. The horizon is still unset
         * ({@link #forHorizon}).
         */
        public static Config defaults(long maxPosQty, double confMin,
                int latencyRows) {
            return new Config(maxPosQty, confMin, latencyRows,
                    DEFAULT_POSITION_POLICY, NO_HORIZON, DEFAULT_HYSTERESIS, true,
                    true);
        }

        /**
         * The v1.4.0 rules, named: {@code position_policy = "sign"}, fills
         * not capped at displayed size, every row traded.
         */
        public static Config legacy(long maxPosQty, double confMin,
                int latencyRows) {
            return new Config(maxPosQty, confMin, latencyRows,
                    LEGACY_POSITION_POLICY, NO_HORIZON, DEFAULT_HYSTERESIS, false,
                    false);
        }

        /** This configuration with the label horizon set (ns, positive). */
        public Config forHorizon(long newHorizonNs) {
            if (newHorizonNs <= 0) {
                throw new IllegalArgumentException("horizon_ns must be positive");
            }
            return new Config(maxPosQty, confMin, latencyRows, positionPolicy,
                    newHorizonNs, hysteresis, capFillsAtL1, blockRows);
        }

        /** This configuration with another hysteresis fraction. */
        public Config withHysteresis(double newHysteresis) {
            return new Config(maxPosQty, confMin, latencyRows, positionPolicy,
                    horizonNs, newHysteresis, capFillsAtL1, blockRows);
        }
    }

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
    private final Config config;

    public ResearchBacktester(CostModel costModel, Config config) {
        this.costModel = costModel;
        this.config = config;
    }

    public Config config() {
        return config;
    }

    /**
     * Desired position sign (-1 / 0 / +1) per decision row under the
     * {@code cost_aware} policy ({@code iap.backtest.engine.cost_aware_targets}).
     *
     * <p>{@code entryThreshold} is the per-row round-trip cost as a return;
     * a non-finite entry means the row cannot open or flip a position. A
     * row whose expected return is non-finite or whose confidence is below
     * {@code confMin} carries no signal: it neither enters nor renews, and
     * an expired hold is closed on it.
     */
    public static double[] costAwareTargets(long[] ts, double[] expectedReturn,
            double[] confidence, double[] entryThreshold, double confMin,
            long horizonNs, double hysteresis) {
        int n = ts.length;
        double[] out = new double[n];
        double pos = 0.0;
        long entryTs = 0;
        for (int i = 0; i < n; i++) {
            double e = expectedReturn[i];
            double thr = entryThreshold[i];
            boolean hasSignal = Double.isFinite(e) && confidence[i] >= confMin;
            double sign = hasSignal ? Math.signum(e) : 0.0;
            boolean clearsEntry = hasSignal && Double.isFinite(thr)
                    && Math.abs(e) > thr;
            if (pos == 0.0) {
                if (clearsEntry && sign != 0.0) {
                    pos = sign;
                    entryTs = ts[i];
                }
            } else if (clearsEntry && sign == -pos) {
                pos = sign;
                entryTs = ts[i];
            } else if (ts[i] - entryTs >= horizonNs) {
                boolean renews = hasSignal && sign == pos && Double.isFinite(thr)
                        && Math.abs(e) > hysteresis * thr;
                if (renews) {
                    entryTs = ts[i];
                } else {
                    pos = 0.0;
                }
            }
            out[i] = pos;
        }
        return out;
    }

    /**
     * Run one instrument without displayed sizes or a row mask: valid only
     * for a configuration that neither caps fills nor blocks rows (the
     * legacy rules).
     */
    public Result run(long instrumentId, long[] ts, double[] mid,
            double[] halfSpread, double[] er, double[] conf,
            String assetClass, double adv, double lotSize) {
        return run(instrumentId, ts, mid, halfSpread, er, conf, null, null, null,
                assetClass, adv, lotSize);
    }

    /**
     * Run one instrument. {@code mid} and {@code halfSpread} carry NaN on
     * invalid rows; {@code er}/{@code conf} are the alpha scores.
     *
     * @param bidSize displayed L1 bid size per row ({@code depth_bid_l1_v1};
     *     NaN = none); required when the configuration caps fills
     * @param askSize displayed L1 ask size per row ({@code depth_ask_l1_v1})
     * @param allowed rows that may make a decision (the rows the IC scores);
     *     required when the configuration blocks rows
     * @param assetClass "EQUITY" / "ETF" / "FX"
     * @param lotSize FX qty-unit size (1 for equities)
     */
    public Result run(long instrumentId, long[] ts, double[] mid,
            double[] halfSpread, double[] er, double[] conf,
            double[] bidSize, double[] askSize, boolean[] allowed,
            String assetClass, double adv, double lotSize) {
        int n = ts.length;
        if (mid.length != n || halfSpread.length != n || er.length != n
                || conf.length != n) {
            throw new IllegalArgumentException("row arrays must align");
        }
        if (config.capFillsAtL1()) {
            if (bidSize == null || askSize == null) {
                throw new IllegalArgumentException("instrument " + instrumentId
                        + ": cap_fills_at_l1 needs the displayed L1 sizes");
            }
            if (bidSize.length != n || askSize.length != n) {
                throw new IllegalArgumentException("row arrays must align");
            }
        }
        if (config.blockRows()) {
            if (allowed == null) {
                throw new IllegalArgumentException("instrument " + instrumentId
                        + ": block_rows needs the allowed-row mask");
            }
            if (allowed.length != n) {
                throw new IllegalArgumentException("row arrays must align");
            }
        }
        double unit = assetClass.equals("FX") ? lotSize : 1.0;
        int latencyRows = config.latencyRows();
        long maxPosQty = config.maxPosQty();

        // decision at i -> desired target at execution row i + latency
        double[] target = new double[n];
        if (config.positionPolicy().equals(DEFAULT_POSITION_POLICY)) {
            if (config.horizonNs() == NO_HORIZON) {
                throw new IllegalArgumentException(
                        "position_policy 'cost_aware' needs horizon_ns: use "
                        + "Config.forHorizon(<label horizon>), or name the legacy "
                        + "rule with Config.legacy()");
            }
            double[] threshold = new double[n];
            for (int i = 0; i < n; i++) {
                threshold[i] = costModel.roundTripCostReturn(mid[i], halfSpread[i],
                        assetClass);
            }
            double[] signs = costAwareTargets(ts, er, conf, threshold,
                    config.confMin(), config.horizonNs(), config.hysteresis());
            for (int i = 0; i < n; i++) {
                target[i] = signs[i] * maxPosQty;
            }
        } else {
            for (int i = 0; i < n; i++) {
                double sign = Double.isFinite(er[i]) ? Math.signum(er[i]) : 0.0;
                target[i] = conf[i] >= config.confMin() ? sign * maxPosQty : 0.0;
            }
        }
        // Blocked at the DECISION row: no target is produced there.
        double[] execTarget = new double[n];
        Arrays.fill(execTarget, Double.NaN);
        if (latencyRows < n) {
            for (int i = 0; i + latencyRows < n; i++) {
                boolean decides = !config.blockRows() || allowed[i];
                execTarget[i + latencyRows] = decides ? target[i] : Double.NaN;
            }
        }
        for (int i = 0; i < n; i++) {
            boolean executable = Double.isFinite(mid[i])
                    && Double.isFinite(halfSpread[i]) && halfSpread[i] >= 0.0;
            if (!executable) {
                execTarget[i] = Double.NaN; // cannot trade here; carry position
            }
        }

        double[] pos = new double[n];
        double cur = 0.0;
        for (int i = 0; i < n; i++) {
            double tgt = execTarget[i];
            if (config.capFillsAtL1()) {
                if (Double.isFinite(tgt) && tgt != cur) {
                    double want = tgt - cur;
                    double size = want > 0 ? askSize[i] : bidSize[i];
                    double cap = Double.isFinite(size) && size > 0.0 ? size : 0.0;
                    cur += Math.signum(want) * Math.min(Math.abs(want), cap);
                }
            } else if (!Double.isNaN(tgt)) {
                cur = tgt;
            }
            pos[i] = cur;
        }

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

        Result r = new Result();
        r.instrumentId = instrumentId;
        r.nRows = n;
        r.equity = new double[n];
        r.positions = pos;
        double cash = 0.0;
        double before = 0.0;
        for (int i = 0; i < n; i++) {
            double trade = pos[i] - before;
            before = pos[i];
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
            r.equity[i] = cash + pos[i] * unit * mark[i];
        }
        r.totalCosts = r.spreadCost + r.feeCost + r.impactCost;

        double gross = 0.0;
        for (int i = 0; i + 1 < n; i++) {
            gross += pos[i] * unit * (mark[i + 1] - mark[i]);
        }
        r.grossPnl = gross;
        r.totalPnl = n > 0 ? r.equity[n - 1] : 0.0;
        return r;
    }
}
