package com.iap.alpha;

import java.util.List;

/**
 * Fitted {@code linear_z_v1} parameters for one alpha, loaded opaquely from
 * configs/strategies/alpha_params.json (API_ALPHA.md section 2 — ports
 * NEVER fit; Python research owns fitting).
 */
public record LinearZParams(
        String alphaId,
        String horizon,
        double mu,
        double sigma,
        double beta,
        double zClip,
        double confScale,
        List<String> features) {
}
