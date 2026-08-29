package com.iap.backtest;

import java.nio.file.Path;
import java.util.Map;

import com.iap.config.Json;

/**
 * Research-backtester cost model (spec section 18; configs/execution.json
 * {@code cost_model}), mirroring {@code iap.backtest.costs} exactly.
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

    /** Load the cost_model block of configs/execution.json. */
    public static CostModel load(Path executionConfigPath, double multiplier) {
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
