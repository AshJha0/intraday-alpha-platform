package com.iap.trace;

import java.util.Map;

import com.iap.contracts.Trees;

/**
 * {@code schemas/alpha/alpha_signal.schema.json} (x-version 1) — the trace
 * mirror of an alpha model output: {@code expectedReturn} is a dimensionless
 * forward return over {@code horizonNs} ({@code 1e-4} = 1 bp),
 * {@code confidence} in [0, 1] with a zero confidence carrying a zero
 * expected return, {@code direction} in {-1, 0, 1}.
 */
public record AlphaSignalRec(long timestamp, long instrumentId,
        double expectedReturn, double confidence, long horizonNs,
        int direction, String modelVersion) {
    private static final String[] KEYS = {"timestamp", "instrument_id",
        "expected_return", "confidence", "horizon_ns", "direction",
        "model_version"};

    public AlphaSignalRec {
        Trees.finite(expectedReturn, "AlphaSignal.expected_return");
        Trees.finite(confidence, "AlphaSignal.confidence");
        if (confidence < 0.0 || confidence > 1.0) {
            throw new IllegalArgumentException(
                    "AlphaSignal.confidence outside [0, 1]: " + confidence);
        }
        if (direction < -1 || direction > 1) {
            throw new IllegalArgumentException(
                    "AlphaSignal.direction must be -1, 0 or 1: " + direction);
        }
        if (confidence == 0.0 && expectedReturn != 0.0) {
            throw new IllegalArgumentException(
                    "AlphaSignal: expected_return must be 0 when confidence is 0");
        }
        if (modelVersion == null) {
            throw new IllegalArgumentException("AlphaSignal.model_version is null");
        }
    }

    /** Schema-ordered tree. */
    public Map<String, Object> toTree() {
        Map<String, Object> t = Trees.ordered();
        t.put("timestamp", timestamp);
        t.put("instrument_id", instrumentId);
        t.put("expected_return", expectedReturn);
        t.put("confidence", confidence);
        t.put("horizon_ns", horizonNs);
        t.put("direction", (long) direction);
        t.put("model_version", modelVersion);
        return t;
    }

    /** Strict inverse of {@link #toTree}. */
    public static AlphaSignalRec fromTree(Map<String, Object> t) {
        String p = "AlphaSignal";
        Trees.checkKeys(t, KEYS, p);
        return new AlphaSignalRec(Trees.i64(t, "timestamp", p),
                Trees.u32(t, "instrument_id", p),
                Trees.num(t, "expected_return", p),
                Trees.num(t, "confidence", p),
                Trees.i64(t, "horizon_ns", p),
                (int) Trees.ranged(t, "direction", p, -1, 1),
                Trees.str(t, "model_version", p));
    }
}
