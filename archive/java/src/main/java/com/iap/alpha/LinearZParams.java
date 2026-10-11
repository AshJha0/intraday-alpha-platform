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

    /** Pinned z clip every params file must carry. */
    public static final double PINNED_Z_CLIP = 4.0;

    /** Pinned confidence scale every params file must carry. */
    public static final double PINNED_CONF_SCALE = 2.0;

    /**
     * True when the fit found no usable evidence: every row scores
     * {@code (0, 0)} (pinned dead-alpha rule, API_ALPHA.md section 2).
     * With {@code sigma == 0} the z denominator collapses to EPS, so every
     * row would otherwise clip to +-z_clip and report confidence 1.0.
     */
    public boolean isDead() {
        return !(sigma > 0.0) || beta == 0.0;
    }
}
