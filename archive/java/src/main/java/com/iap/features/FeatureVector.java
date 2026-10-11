package com.iap.features;

import java.util.Arrays;

/**
 * FeatureVector contract over the documented native sub-vector
 * (API_FEATURES.md section 1): {@code values[i]} is finite whenever
 * {@code valid[i]}; invalid slots carry NaN. Golden comparisons are by
 * registry feature name via {@link Features#index}.
 */
public final class FeatureVector {
    public long instrumentId; // u32
    public long timestamp;    // exchange_ts of the emission event (ns)
    public final double[] values = new double[Features.COUNT];
    public final boolean[] valid = new boolean[Features.COUNT];

    public FeatureVector() {
        Arrays.fill(values, Double.NaN);
    }

    /** Reset to the all-invalid state (values NaN). */
    public void clear() {
        Arrays.fill(values, Double.NaN);
        Arrays.fill(valid, false);
    }

    /** Deep copy. */
    public FeatureVector copy() {
        FeatureVector out = new FeatureVector();
        out.instrumentId = instrumentId;
        out.timestamp = timestamp;
        System.arraycopy(values, 0, out.values, 0, values.length);
        System.arraycopy(valid, 0, out.valid, 0, valid.length);
        return out;
    }

    /** Value of a slot, or NaN when the slot is invalid (alpha-input rule). */
    public double valueOrNaN(int slot) {
        return valid[slot] ? values[slot] : Double.NaN;
    }
}
