package com.iap.lifecycle;

import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.TreeMap;

import com.iap.contracts.CanonicalJson;
import com.iap.contracts.Trees;

/**
 * {@code schemas/alpha/lifecycle_transition.schema.json} (x-version 1): an
 * alpha moving between {@link LifecycleState}s. {@code policy} names the
 * lifecycle policy whose gates were applied, {@code gates} the results keyed
 * by gate name (evaluation order preserved in memory; sorted on the wire),
 * {@code actor} SYSTEM or HUMAN. Canonical line = Python's
 * {@code canonical_json(transition.to_dict())}.
 */
public record LifecycleTransition(String alphaId, LifecycleState fromState,
        LifecycleState toState, long eventTs, String reason,
        Map<String, GateResult> gates, String policy, Actor actor) {
    private static final String[] KEYS = {"alpha_id", "from_state", "to_state",
        "event_ts", "reason", "gates", "policy", "actor"};

    /** Who took a transition. */
    public enum Actor {
        SYSTEM, HUMAN;

        static Actor parse(String name, String where) {
            for (Actor a : values()) {
                if (a.name().equals(name)) {
                    return a;
                }
            }
            throw new IllegalArgumentException(where + ": unknown actor '" + name + "'");
        }
    }

    public LifecycleTransition {
        Trees.ident(alphaId, "LifecycleTransition.alpha_id");
        if (fromState == null || toState == null || actor == null) {
            throw new IllegalArgumentException("LifecycleTransition: null state/actor");
        }
        if (fromState == toState) {
            throw new IllegalArgumentException(
                    "LifecycleTransition: from_state == to_state (" + fromState + ")");
        }
        if (reason == null) {
            throw new IllegalArgumentException("LifecycleTransition.reason is null");
        }
        if (policy == null || policy.isEmpty()) {
            throw new IllegalArgumentException("LifecycleTransition.policy is empty");
        }
        gates = Collections.unmodifiableMap(new LinkedHashMap<>(gates));
    }

    /** Schema-ordered tree ({@code gates} in code-point key order). */
    public Map<String, Object> toTree() {
        Map<String, Object> t = Trees.ordered();
        t.put("alpha_id", alphaId);
        t.put("from_state", fromState.name());
        t.put("to_state", toState.name());
        t.put("event_ts", eventTs);
        t.put("reason", reason);
        TreeMap<String, Object> g = new TreeMap<>(CanonicalJson.KEY_ORDER);
        for (Map.Entry<String, GateResult> e : gates.entrySet()) {
            g.put(e.getKey(), e.getValue().toTree());
        }
        t.put("gates", g);
        t.put("policy", policy);
        t.put("actor", actor.name());
        return t;
    }

    /** The canonical JSONL line (no newline). */
    public String toLine() {
        return CanonicalJson.serialize(toTree());
    }

    /** Strict inverse of {@link #toTree}; gates keep the tree's key order. */
    public static LifecycleTransition fromTree(Map<String, Object> t) {
        String p = "LifecycleTransition";
        Trees.checkKeys(t, KEYS, p);
        Map<String, Object> raw = Trees.obj(t, "gates", p);
        LinkedHashMap<String, GateResult> gates = new LinkedHashMap<>();
        for (String k : raw.keySet()) {
            gates.put(k, GateResult.fromTree(Trees.obj(raw, k, p + ".gates")));
        }
        return new LifecycleTransition(Trees.str(t, "alpha_id", p),
                LifecycleState.parse(Trees.str(t, "from_state", p), p + ".from_state"),
                LifecycleState.parse(Trees.str(t, "to_state", p), p + ".to_state"),
                Trees.i64(t, "event_ts", p), Trees.str(t, "reason", p), gates,
                Trees.str(t, "policy", p),
                Actor.parse(Trees.str(t, "actor", p), p + ".actor"));
    }
}
