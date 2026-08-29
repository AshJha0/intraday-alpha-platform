package com.iap.risk;

/** The outcome of one pre-trade check. */
public record RiskDecision(Decision decision, String ruleId,
        Severity severity, String reason) {
    /** True when the order may proceed. */
    public boolean allowed() {
        return decision == Decision.ALLOW;
    }
}
