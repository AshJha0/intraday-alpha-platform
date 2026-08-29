package com.iap.alpha;

import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

import com.iap.config.Json;
import com.iap.features.FeatureVector;
import com.iap.features.Features;

/**
 * Production alpha scoring — the 6 golden flagship alphas (API_ALPHA.md).
 *
 * <p>Model {@code linear_z_v1} (all 6): with params {mu, sigma, beta,
 * z_clip, conf_scale} from configs/strategies/alpha_params.json:
 *
 * <pre>
 *   z    = clip((raw - mu) / (sigma + EPS), -z_clip, +z_clip)
 *   er   = beta * z
 *   conf = min(1, |z| / conf_scale)
 *   invalid raw (NaN)  =&gt;  er = 0.0, conf = 0.0 exactly
 * </pre>
 *
 * <p>Raw signals (API_ALPHA.md section 4; inputs are registry features from
 * the native feature engine — a NaN/invalid input makes raw NaN):
 * EQ01/FX01 {@code micro_mid_dev_bps_v1}; EQ03
 * {@code 0.5*ofi_norm_l1_w1s + 0.3*ofi_norm_l5_w1s + 0.2*ofi_norm_l5_w5s}
 * (weights pinned); EQ06 {@code ret_vol_adj_10s_v1}; FX09
 * {@code -ret_vol_adj_10s_v1 * vol_regime_ratio_v1}; FX05 is cross-pair
 * (see {@link Fx05}). Honest-reporting rule (spec section 32): params ship
 * with the sign the fit produced — this port never "fixes" coefficients.
 */
public final class Alphas {
    public static final double EPS = 1e-12;

    /** The 6 production alpha ids, pinned order. */
    public static final String[] GOLDEN_ALPHA_IDS = {
        "EQ01", "EQ03", "EQ06", "FX01", "FX05", "FX09",
    };

    private Alphas() {
    }

    /**
     * Load configs/strategies/alpha_params.json. Throws
     * IllegalStateException on an unreadable file and
     * IllegalArgumentException on missing alphas / unsupported models.
     */
    public static TreeMap<String, LinearZParams> loadParams(Path path) {
        Map<String, Object> root = Json.object(Json.parseFile(path));
        Map<String, Object> params = Json.object(root.get("params"));
        TreeMap<String, LinearZParams> out = new TreeMap<>();
        for (String aid : GOLDEN_ALPHA_IDS) {
            Object entry = params.get(aid);
            if (entry == null) {
                throw new IllegalArgumentException("alpha_params.json lacks " + aid);
            }
            Map<String, Object> p = Json.object(entry);
            if (!"linear_z_v1".equals(p.get("model"))) {
                throw new IllegalArgumentException(
                        aid + ": unsupported model " + p.get("model"));
            }
            List<String> feats = new ArrayList<>();
            for (Object f : Json.array(p.get("features"))) {
                feats.add((String) f);
            }
            out.put(aid, new LinearZParams(
                    (String) p.get("alpha_id"),
                    (String) p.get("horizon"),
                    Json.asDouble(p.get("mu")),
                    Json.asDouble(p.get("sigma")),
                    Json.asDouble(p.get("beta")),
                    Json.asDouble(p.get("z_clip")),
                    Json.asDouble(p.get("conf_scale")),
                    List.copyOf(feats)));
        }
        return out;
    }

    /**
     * Core scoring: raw to {expected_return, confidence}. NaN raw scores
     * (0, 0) exactly. Returns {er, conf}.
     */
    public static double[] scoreLinearZ(double raw, LinearZParams p) {
        if (!Double.isFinite(raw)) {
            return new double[] {0.0, 0.0};
        }
        double z = (raw - p.mu()) / (p.sigma() + EPS);
        if (z > p.zClip()) {
            z = p.zClip();
        }
        if (z < -p.zClip()) {
            z = -p.zClip();
        }
        return new double[] {p.beta() * z, Math.min(1.0, Math.abs(z) / p.confScale())};
    }

    /**
     * Raw signal of a single-frame alpha (EQ01/EQ03/EQ06/FX01/FX09) from a
     * native feature vector; NaN when any input feature is invalid. Throws
     * for FX05 (cross-pair; use {@link Fx05#rawSignals}).
     */
    public static double rawSignal(String alphaId, FeatureVector vec) {
        switch (alphaId) {
            case "EQ01":
            case "FX01":
                return vec.valueOrNaN(Features.MICRO_MID_DEV_BPS);
            case "EQ03":
                // pinned weights 0.5 / 0.3 / 0.2 (API_ALPHA.md section 4)
                return 0.5 * vec.valueOrNaN(Features.OFI_NORM_L1_W1S)
                        + 0.3 * vec.valueOrNaN(Features.OFI_NORM_L5_W1S)
                        + 0.2 * vec.valueOrNaN(Features.OFI_NORM_L5_W5S);
            case "EQ06":
                return vec.valueOrNaN(Features.RET_VOL_ADJ_10S);
            case "FX09":
                return -vec.valueOrNaN(Features.RET_VOL_ADJ_10S)
                        * vec.valueOrNaN(Features.VOL_REGIME_RATIO);
            default:
                throw new IllegalArgumentException(
                        "rawSignal: not a single-frame alpha: " + alphaId);
        }
    }

    /** Score one instrument row of a single-frame alpha. */
    public static AlphaSignal scoreRow(LinearZParams p, FeatureVector vec) {
        double[] ec = scoreLinearZ(rawSignal(p.alphaId(), vec), p);
        return new AlphaSignal(p.alphaId(), vec.instrumentId, vec.timestamp,
                ec[0], ec[1], p.horizon());
    }
}
