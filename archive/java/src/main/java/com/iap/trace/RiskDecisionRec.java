package com.iap.trace;

import java.util.Map;

import com.iap.contracts.Trees;

/**
 * {@code schemas/risk/risk_decision.schema.json} (x-version 1): one
 * pre-trade decision. {@code decision} is ALLOW=1 / REJECT=2 / KILL=3;
 * {@code ruleIndex} the pinned check index of the deciding rule
 * (PLATFORM_CONVENTIONS.md §11.1) and {@code -1} for ALLOW.
 */
public record RiskDecisionRec(long orderId, String strategyId, long instrumentId,
        long timestampNs, int decision, String ruleId, int ruleIndex,
        String reason) {
    private static final String[] KEYS = {"order_id", "strategy_id",
        "instrument_id", "timestamp_ns", "decision", "rule_id", "rule_index",
        "reason"};

    /** Wire code of ALLOW. */
    public static final int ALLOW = 1;
    /** Wire code of REJECT. */
    public static final int REJECT = 2;
    /** Wire code of KILL. */
    public static final int KILL = 3;

    public RiskDecisionRec {
        Trees.ident(strategyId, "RiskDecision.strategy_id");
        if (decision < ALLOW || decision > KILL) {
            throw new IllegalArgumentException(
                    "RiskDecision.decision must be 1..3: " + decision);
        }
        if (ruleIndex < -1 || ruleIndex > 0xFFFF) {
            throw new IllegalArgumentException(
                    "RiskDecision.rule_index outside [-1, 65535]: " + ruleIndex);
        }
        if (decision == ALLOW && ruleIndex != -1) {
            throw new IllegalArgumentException(
                    "RiskDecision: ALLOW requires rule_index == -1");
        }
        if (decision != ALLOW && ruleIndex < 0) {
            throw new IllegalArgumentException(
                    "RiskDecision: REJECT/KILL require a pinned rule_index >= 0");
        }
        if (ruleId == null || reason == null) {
            throw new IllegalArgumentException("RiskDecision: rule_id/reason null");
        }
    }

    /** Schema-ordered tree. */
    public Map<String, Object> toTree() {
        Map<String, Object> t = Trees.ordered();
        t.put("order_id", Trees.u64Tree(orderId));
        t.put("strategy_id", strategyId);
        t.put("instrument_id", instrumentId);
        t.put("timestamp_ns", timestampNs);
        t.put("decision", (long) decision);
        t.put("rule_id", ruleId);
        t.put("rule_index", (long) ruleIndex);
        t.put("reason", reason);
        return t;
    }

    /** Strict inverse of {@link #toTree}. */
    public static RiskDecisionRec fromTree(Map<String, Object> t) {
        String p = "RiskDecision";
        Trees.checkKeys(t, KEYS, p);
        return new RiskDecisionRec(Trees.u64(t, "order_id", p),
                Trees.str(t, "strategy_id", p),
                Trees.u32(t, "instrument_id", p),
                Trees.i64(t, "timestamp_ns", p),
                (int) Trees.ranged(t, "decision", p, 1, 3),
                Trees.str(t, "rule_id", p),
                (int) Trees.ranged(t, "rule_index", p, -1, 0xFFFF),
                Trees.str(t, "reason", p));
    }
}
