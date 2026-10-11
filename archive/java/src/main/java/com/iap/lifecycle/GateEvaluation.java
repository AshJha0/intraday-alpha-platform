package com.iap.lifecycle;

import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

import com.iap.contracts.CanonicalJson;
import com.iap.contracts.Trees;

/**
 * One {@code advance} call: the gates it evaluated (edge order) and what it
 * did ({@code iap.lifecycle.registry.GateEvaluation}). {@code gates} is
 * empty for NO_EVIDENCE / TERMINAL. On the wire {@code gate_order} carries
 * the pinned evaluation order, which the sorted-key serialisation of
 * {@code gates} would lose.
 */
public record GateEvaluation(String alphaId, long eventTs, LifecycleState state,
        Outcome outcome, Map<String, GateResult> gates, int consecutiveFailures,
        LifecycleTransition transition) {
    private static final String[] KEYS = {"alpha_id", "event_ts", "state",
        "outcome", "gate_order", "gates", "failed_gates", "consecutive_failures",
        "transition"};

    /** Outcome codes of one evaluation (strings on the wire). */
    public enum Outcome {
        TRANSITION, HOLD, NO_EVIDENCE, TERMINAL;

        static Outcome parse(String name, String where) {
            for (Outcome o : values()) {
                if (o.name().equals(name)) {
                    return o;
                }
            }
            throw new IllegalArgumentException(where + ": unknown outcome '" + name + "'");
        }
    }

    public GateEvaluation {
        if (alphaId == null || state == null || outcome == null) {
            throw new IllegalArgumentException("GateEvaluation: null field");
        }
        if ((outcome == Outcome.TRANSITION) != (transition != null)) {
            throw new IllegalArgumentException(
                    "GateEvaluation: TRANSITION outcome iff a transition");
        }
        if (consecutiveFailures < 0) {
            throw new IllegalArgumentException(
                    "GateEvaluation.consecutive_failures must be >= 0");
        }
        gates = Collections.unmodifiableMap(new LinkedHashMap<>(gates));
    }

    /** Names of the failed gates in evaluation order. */
    public List<String> failedGates() {
        List<String> out = new ArrayList<>();
        for (Map.Entry<String, GateResult> e : gates.entrySet()) {
            if (!e.getValue().passed()) {
                out.add(e.getKey());
            }
        }
        return out;
    }

    /** JSON-ready tree (gate order carried explicitly). */
    public Map<String, Object> toTree() {
        Map<String, Object> t = Trees.ordered();
        t.put("alpha_id", alphaId);
        t.put("event_ts", eventTs);
        t.put("state", state.name());
        t.put("outcome", outcome.name());
        t.put("gate_order", new ArrayList<Object>(gates.keySet()));
        TreeMap<String, Object> g = new TreeMap<>(CanonicalJson.KEY_ORDER);
        for (Map.Entry<String, GateResult> e : gates.entrySet()) {
            g.put(e.getKey(), e.getValue().toTree());
        }
        t.put("gates", g);
        t.put("failed_gates", new ArrayList<Object>(failedGates()));
        t.put("consecutive_failures", (long) consecutiveFailures);
        t.put("transition", transition == null ? null : transition.toTree());
        return t;
    }

    /** Strict inverse of {@link #toTree}. */
    public static GateEvaluation fromTree(Map<String, Object> t) {
        String p = "GateEvaluation";
        Trees.checkKeys(t, KEYS, p);
        Map<String, Object> raw = Trees.obj(t, "gates", p);
        LinkedHashMap<String, GateResult> gates = new LinkedHashMap<>();
        for (Object o : Trees.arr(t, "gate_order", p)) {
            if (!(o instanceof String name) || !raw.containsKey(name)
                    || gates.containsKey(name)) {
                throw new IllegalArgumentException(
                        p + ": gate_order does not match gates");
            }
            gates.put(name, GateResult.fromTree(Trees.obj(raw, name, p + ".gates")));
        }
        if (gates.size() != raw.size()) {
            throw new IllegalArgumentException(p + ": gate_order does not match gates");
        }
        Object tr = t.get("transition");
        GateEvaluation ev = new GateEvaluation(Trees.str(t, "alpha_id", p),
                Trees.i64(t, "event_ts", p),
                LifecycleState.parse(Trees.str(t, "state", p), p + ".state"),
                Outcome.parse(Trees.str(t, "outcome", p), p + ".outcome"), gates,
                (int) Trees.ranged(t, "consecutive_failures", p, 0, Integer.MAX_VALUE),
                tr == null ? null
                        : LifecycleTransition.fromTree(Trees.obj(tr, p + ".transition")));
        List<Object> failed = Trees.arr(t, "failed_gates", p);
        if (!failed.equals(new ArrayList<Object>(ev.failedGates()))) {
            throw new IllegalArgumentException(p + ": failed_gates does not match gates");
        }
        return ev;
    }
}
