package com.iap.lifecycle;

import java.util.Map;

import com.iap.contracts.Trees;

/**
 * {@code lifecycle_transition.schema.json#/$defs/GateResult}: one gate
 * evaluated for a transition — {@code value} compared with
 * {@code threshold}; both are {@code null} for a boolean gate, and
 * {@code value} is {@code null} when the metric was absent (a missing
 * number never passes).
 */
public record GateResult(boolean passed, Double value, Double threshold) {
    private static final String[] KEYS = {"passed", "value", "threshold"};

    public GateResult {
        if (value != null) {
            Trees.finite(value, "GateResult.value");
        }
        if (threshold != null) {
            Trees.finite(threshold, "GateResult.threshold");
        }
    }

    /** Schema-ordered tree. */
    public Map<String, Object> toTree() {
        Map<String, Object> t = Trees.ordered();
        t.put("passed", passed);
        t.put("value", value);
        t.put("threshold", threshold);
        return t;
    }

    /** Strict inverse of {@link #toTree}. */
    public static GateResult fromTree(Map<String, Object> t) {
        String p = "GateResult";
        Trees.checkKeys(t, KEYS, p);
        return new GateResult(Trees.bool(t, "passed", p), Trees.optNum(t, "value", p),
                Trees.optNum(t, "threshold", p));
    }
}
