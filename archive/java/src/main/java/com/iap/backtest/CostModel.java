package com.iap.backtest;

import java.nio.file.Path;
import java.util.Map;

import com.iap.config.Json;

/**
 * Research-backtester cost model (spec section 18; configs/execution/execution.json
 * {@code cost_model}) — the port of {@code iap.backtest.costs.CostModel},
 * both impact rules, selected by the same names.
 *
 * <p>Pinned per-execution cost of trading {@code q} units at a row with mid
 * {@code m} and half-spread {@code hs} (price units of the instrument):
 *
 * <pre>
 *   unit        = lot_size for FX, 1 for EQUITY/ETF
 *   spread_cost = |q| * unit * hs
 *   fee_cost    = |q| * fee_per_share                       (EQUITY/ETF)
 *               = notional * commission_per_million / 1e6   (FX)
 *   impact_cost = impact_bps * 1e-4 * |q| * unit * m
 * </pre>
 *
 * <p><b>Impact model</b> (pinned; the default changed in v1.5.0):
 * <ul>
 *   <li>{@code impact_model = "sqrt"} — the default
 *       ({@link #DEFAULT_IMPACT_MODEL}):
 *       {@code impact_bps = sqrt_impact_coeff_bps * sqrt(|q| * unit / adv)},
 *       with the coefficient the impact in bps of trading one full ADV
 *       ({@link #DEFAULT_SQRT_IMPACT_COEFF_BPS} = 100);</li>
 *   <li>{@code impact_model = "linear"} — the LEGACY rule
 *       ({@link #LEGACY_IMPACT_MODEL}), the default up to v1.4.0:
 *       {@code impact_bps = impact_coeff_bps_per_pct_adv * (|q| * unit / adv * 100)}.
 *       {@link #withLinearImpact()} names it.</li>
 * </ul>
 *
 * A {@code multiplier} scales the TOTAL cost (stress grid {0.5, 1, 2}).
 *
 * <p>{@link #load} requires the config block to NAME its
 * {@code impact_model} (the document is x-version 2 since v1.5.0), exactly
 * as the Python loader does: a block written for v1.4.0 is rejected rather
 * than priced under the other rule. Every expression keeps the operation
 * order of the Python reference; {@code tests/golden/expected_backtest.json}
 * pins both rules at 1e-9. The execution simulator
 * ({@code com.iap.execution}) has its own linear impact rule 6 and does not
 * use this class.
 */
public record CostModel(
        double impactCoeffBpsPerPctAdv,
        double equityTakerFeePerShare,
        double fxCommissionPerMillion,
        double multiplier,
        String impactModel,
        double sqrtImpactCoeffBps) {

    /** The default impact model ({@code iap.backtest.costs.DEFAULT_IMPACT_MODEL}). */
    public static final String DEFAULT_IMPACT_MODEL = "sqrt";

    /** The rule that was the default up to v1.4.0. */
    public static final String LEGACY_IMPACT_MODEL = "linear";

    /** Pinned square-root coefficient: bps of impact for trading one full ADV. */
    public static final double DEFAULT_SQRT_IMPACT_COEFF_BPS = 100.0;

    public CostModel {
        if (!DEFAULT_IMPACT_MODEL.equals(impactModel)
                && !LEGACY_IMPACT_MODEL.equals(impactModel)) {
            throw new IllegalArgumentException("unknown impact_model '" + impactModel
                    + "'; known: sqrt, linear");
        }
        if (sqrtImpactCoeffBps < 0.0) {
            throw new IllegalArgumentException("sqrt_impact_coeff_bps must be >= 0");
        }
    }

    /**
     * The default rules: square-root impact at
     * {@link #DEFAULT_SQRT_IMPACT_COEFF_BPS} (the Python dataclass defaults).
     */
    public CostModel(double impactCoeffBpsPerPctAdv, double equityTakerFeePerShare,
            double fxCommissionPerMillion, double multiplier) {
        this(impactCoeffBpsPerPctAdv, equityTakerFeePerShare, fxCommissionPerMillion,
                multiplier, DEFAULT_IMPACT_MODEL, DEFAULT_SQRT_IMPACT_COEFF_BPS);
    }

    /**
     * The cost model the {@code cost_model} block of
     * configs/execution/execution.json names. The block must name its
     * {@code impact_model}; {@code "sqrt"} also needs
     * {@code sqrt_impact_coeff_bps}.
     */
    public static CostModel load(Path executionConfigPath, double multiplier) {
        Map<String, Object> root = Json.object(Json.parseFile(executionConfigPath));
        Object cm = root.get("cost_model");
        if (cm == null) {
            throw new IllegalArgumentException(
                    executionConfigPath + ": missing 'cost_model' section");
        }
        Map<String, Object> m = Json.object(cm);
        Object named = m.get("impact_model");
        if (named == null) {
            throw new IllegalArgumentException(executionConfigPath
                    + ": cost_model names no 'impact_model'. Since v1.5.0 the default is '"
                    + DEFAULT_IMPACT_MODEL + "' (with 'sqrt_impact_coeff_bps'); a document"
                    + " written for v1.4.0 must say '" + LEGACY_IMPACT_MODEL
                    + "' to keep the rule it was written for");
        }
        String model = String.valueOf(named);
        Object coeff = m.get("sqrt_impact_coeff_bps");
        if (model.equals("sqrt") && coeff == null) {
            throw new IllegalArgumentException(executionConfigPath
                    + ": cost_model.impact_model 'sqrt' needs 'sqrt_impact_coeff_bps'");
        }
        return new CostModel(
                Json.asDouble(m.get("impact_coeff_bps_per_pct_adv")),
                Json.asDouble(m.get("equity_taker_fee_per_share")),
                Json.asDouble(m.get("fx_commission_per_million")),
                multiplier,
                model,
                coeff == null ? DEFAULT_SQRT_IMPACT_COEFF_BPS : Json.asDouble(coeff));
    }

    /**
     * {@link #load} under the LEGACY linear impact rule, whatever model the
     * block names ({@code load(...).withLinearImpact()}).
     */
    public static CostModel loadLegacyLinear(Path executionConfigPath,
            double multiplier) {
        return load(executionConfigPath, multiplier).withLinearImpact();
    }

    /** This model at another cost multiplier. */
    public CostModel withMultiplier(double newMultiplier) {
        return new CostModel(impactCoeffBpsPerPctAdv, equityTakerFeePerShare,
                fxCommissionPerMillion, newMultiplier, impactModel, sqrtImpactCoeffBps);
    }

    /** This model under the LEGACY linear impact rule. */
    public CostModel withLinearImpact() {
        return new CostModel(impactCoeffBpsPerPctAdv, equityTakerFeePerShare,
                fxCommissionPerMillion, multiplier, LEGACY_IMPACT_MODEL,
                sqrtImpactCoeffBps);
    }

    /** This model with square-root impact of {@code coeffBps} at one ADV. */
    public CostModel withSqrtImpact(double coeffBps) {
        return new CostModel(impactCoeffBpsPerPctAdv, equityTakerFeePerShare,
                fxCommissionPerMillion, multiplier, DEFAULT_IMPACT_MODEL, coeffBps);
    }

    private boolean sqrtImpact() {
        return impactModel.equals(DEFAULT_IMPACT_MODEL);
    }

    /**
     * Impact in bps (before the multiplier) of trading {@code participation}
     * = size / ADV (a fraction, not a percentage) under the active model.
     */
    public double impactBps(double participation) {
        if (sqrtImpact()) {
            return sqrtImpactCoeffBps * Math.sqrt(participation);
        }
        return impactCoeffBpsPerPctAdv * (participation * 100.0);
    }

    /**
     * {spread, fee, impact} for one signed execution of {@code qty} units
     * (multiplier applied to each component).
     */
    public double[] costComponents(double qty, double mid, double halfSpread,
            String assetClass, double adv, double lotSize) {
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
        if (adv <= 0) {
            throw new IllegalArgumentException("adv must be positive");
        }
        double spread = aq * unit * halfSpread;
        double impactBps;
        if (sqrtImpact()) {
            impactBps = sqrtImpactCoeffBps * Math.sqrt(aq * unit / adv);
        } else {
            impactBps = impactCoeffBpsPerPctAdv * (aq * unit / adv * 100.0);
        }
        double impact = impactBps * 1e-4 * aq * unit * mid;
        return new double[] {multiplier * spread, multiplier * fee, multiplier * impact};
    }

    /**
     * Spread + fee of opening AND closing one unit, as a return:
     * {@code multiplier * (2 * half_spread + 2 * fee_per_unit) / mid}, the
     * per-unit fee being {@code equity_taker_fee_per_share} for EQUITY/ETF
     * and {@code mid * fx_commission_per_million / 1e6} for FX. Impact is
     * left out: it scales with the order size and this is the size-free
     * hurdle of the {@code cost_aware} position policy. NaN when
     * {@code mid} is not a positive finite number or the half-spread is
     * invalid.
     */
    public double roundTripCostReturn(double mid, double halfSpread,
            String assetClass) {
        double fee;
        if (assetClass.equals("EQUITY") || assetClass.equals("ETF")) {
            fee = equityTakerFeePerShare;
        } else if (assetClass.equals("FX")) {
            fee = mid * fxCommissionPerMillion / 1e6;
        } else {
            throw new IllegalArgumentException("unknown asset class " + assetClass);
        }
        boolean ok = Double.isFinite(mid) && mid > 0.0
                && Double.isFinite(halfSpread) && halfSpread >= 0.0;
        if (!ok) {
            return Double.NaN;
        }
        return multiplier * (2.0 * halfSpread + 2.0 * fee) / mid;
    }

    /**
     * Order size (qty units) at which the edge per trade equals its cost:
     * {@code edge_return = round_trip_cost_return + 2 * multiplier *
     * impact_bps(q * unit / adv) * 1e-4} solved for {@code q} under the
     * active impact model. 0 when the edge does not cover spread + fee at
     * zero size; positive infinity when the model charges no impact.
     */
    public double breakevenSize(double edgeReturn, double mid, double halfSpread,
            String assetClass, double adv, double lotSize) {
        if (!(Double.isFinite(edgeReturn) && adv > 0)) {
            throw new IllegalArgumentException(
                    "edge_return must be finite and adv positive");
        }
        double fixed = roundTripCostReturn(mid, halfSpread, assetClass);
        if (!Double.isFinite(fixed)) {
            throw new IllegalArgumentException(
                    "mid / half_spread do not price a round trip");
        }
        double room = edgeReturn - fixed;
        if (room <= 0.0) {
            return 0.0;
        }
        double unit = assetClass.equals("FX") ? lotSize : 1.0;
        double perLegBps = multiplier > 0
                ? room / (2.0 * multiplier * 1e-4) : Double.POSITIVE_INFINITY;
        double participation;
        if (sqrtImpact()) {
            if (sqrtImpactCoeffBps <= 0.0 || !Double.isFinite(perLegBps)) {
                return Double.POSITIVE_INFINITY;
            }
            double ratio = perLegBps / sqrtImpactCoeffBps;
            participation = ratio * ratio;
        } else {
            if (impactCoeffBpsPerPctAdv <= 0.0 || !Double.isFinite(perLegBps)) {
                return Double.POSITIVE_INFINITY;
            }
            participation = perLegBps / (impactCoeffBpsPerPctAdv * 100.0);
        }
        return participation * adv / unit;
    }

    /**
     * Edge-based capacity ({@code iap.validation.metrics.capacity_breakeven}):
     * {units, notional, participation} — the {@link #breakevenSize} in qty
     * units, in quote currency and as a fraction of ADV.
     */
    public double[] capacityBreakeven(double edgeReturn, double mid,
            double halfSpread, String assetClass, double adv, double lotSize) {
        double units = breakevenSize(edgeReturn, mid, halfSpread, assetClass, adv,
                lotSize);
        double unit = assetClass.equals("FX") ? lotSize : 1.0;
        return new double[] {units, units * unit * mid, units * unit / adv};
    }
}
