package com.iap.backtest;

import java.nio.file.Path;
import java.util.Map;

import com.iap.config.Json;

/**
 * Research-backtester cost model (spec section 18; configs/execution/execution.json
 * {@code cost_model}) — the LEGACY LINEAR-IMPACT rule of
 * {@code iap.backtest.costs} ({@code impact_model = "linear"}), and only
 * that rule.
 *
 * <p><b>Legacy (v1.5.0).</b> Since v1.5.0 the default impact model of the
 * Python research cost model is the square root
 * ({@code impact_model = "sqrt"}, the value {@code execution.json} names).
 * This class was not given that rule: it exists for the cross-language
 * backtest vector {@code tests/golden/expected_backtest.json}, whose
 * {@code config} names {@code "impact_model": "linear"}, and nothing on the
 * paper path prices a research backtest. {@link #IMPACT_MODEL} states what
 * it computes and {@link #loadLegacyLinear} reads the linear coefficient
 * whatever model the config names — a caller that wants the default research
 * costs must use the Python reference.
 *
 * <p>Pinned per-execution cost of trading {@code q} units at a row with mid
 * {@code m} and half-spread {@code hs} (price units of the instrument):
 *
 * <pre>
 *   unit        = lot_size for FX, 1 for EQUITY/ETF
 *   spread_cost = |q| * unit * hs
 *   fee_cost    = |q| * fee_per_share                       (EQUITY/ETF)
 *               = notional * commission_per_million / 1e6   (FX)
 *   impact_bps  = impact_coeff_bps_per_pct_adv * (|q| * unit / adv * 100)
 *   impact_cost = impact_bps * 1e-4 * |q| * unit * m
 * </pre>
 *
 * A {@code multiplier} scales the TOTAL cost (stress grid {0.5, 1, 2}).
 */
public record CostModel(
        double impactCoeffBpsPerPctAdv,
        double equityTakerFeePerShare,
        double fxCommissionPerMillion,
        double multiplier) {

    /** The one impact model this class implements ({@code iap.backtest.costs}). */
    public static final String IMPACT_MODEL = "linear";

    /**
     * The LEGACY linear model from the cost_model block of
     * configs/execution/execution.json: the linear coefficient and the fees.
     * The block's {@code impact_model} ("sqrt" since v1.5.0) is not applied.
     */
    public static CostModel loadLegacyLinear(Path executionConfigPath,
            double multiplier) {
        Map<String, Object> root = Json.object(Json.parseFile(executionConfigPath));
        Object cm = root.get("cost_model");
        if (cm == null) {
            throw new IllegalArgumentException(
                    executionConfigPath + ": missing 'cost_model' section");
        }
        Map<String, Object> m = Json.object(cm);
        return new CostModel(
                Json.asDouble(m.get("impact_coeff_bps_per_pct_adv")),
                Json.asDouble(m.get("equity_taker_fee_per_share")),
                Json.asDouble(m.get("fx_commission_per_million")),
                multiplier);
    }

    /**
     * {spread, fee, impact} for one signed execution of {@code qty} units
     * (multiplier applied to each component).
     */
    public double[] costComponents(double qty, double mid, double halfSpread,
            String assetClass, double adv, double lotSize) {
        if (adv <= 0) {
            throw new IllegalArgumentException("adv must be positive");
        }
        double aq = Math.abs(qty);
        double unit;
        double fee;
        if (assetClass.equals("EQUITY") || assetClass.equals("ETF")) {
            unit = 1.0;
            fee = aq * equityTakerFeePerShare;
        } else if (assetClass.equals("FX")) {
            unit = lotSize;
            double notional = aq * unit * mid;
            fee = notional * fxCommissionPerMillion / 1e6;
        } else {
            throw new IllegalArgumentException("unknown asset class " + assetClass);
        }
        double spread = aq * unit * halfSpread;
        double impactBps = impactCoeffBpsPerPctAdv * (aq * unit / adv * 100.0);
        double impact = impactBps * 1e-4 * aq * unit * mid;
        return new double[] {multiplier * spread, multiplier * fee, multiplier * impact};
    }
}
