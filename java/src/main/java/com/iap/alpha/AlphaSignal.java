package com.iap.alpha;

/**
 * AlphaSignal contract (API_ALPHA.md section 1;
 * schemas/alpha_signal.schema.json). Invariants: {@code expectedReturn} is
 * finite always; when {@code confidence == 0}, {@code expectedReturn == 0.0}
 * exactly. NaN never leaves a scorer.
 */
public record AlphaSignal(
        String alphaId,
        long instrumentId,   // u32
        long timestamp,      // exchange_ts of the scored feature row
        double expectedReturn,
        double confidence,   // in [0, 1]; 0 whenever the signal is invalid
        String horizon) {
}
