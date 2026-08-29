package com.iap.risk;

/**
 * One audit-log record (schemas/risk_event.schema.json — exact field set
 * and enum codes). Serialized as one JSONL line with sorted keys
 * (decision, reason, rule_id, scope, scope_id, severity, timestamp) —
 * identical input sequences produce byte-identical audit logs.
 */
public record RiskEvent(long timestamp, Scope scope, String scopeId,
        String ruleId, int severity, int decision, String reason) {
    public RiskEvent {
        if (severity < 1 || severity > 3 || decision < 1 || decision > 3) {
            throw new IllegalArgumentException(
                    "RiskEvent enum out of domain: severity " + severity
                            + " decision " + decision);
        }
    }

    /**
     * Minimal JSON string escaping (quotes, backslashes, control chars) —
     * byte-identical to serde_json in the Rust reference: the two-character
     * escapes for quote, backslash, backspace, form feed, newline, carriage
     * return and tab, and a u00xx escape (lowercase hex) for every other
     * control character.
     */
    static String esc(String s) {
        StringBuilder sb = new StringBuilder(s.length() + 8);
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"' -> sb.append("\\\"");
                case '\\' -> sb.append("\\\\");
                case '\b' -> sb.append("\\b");
                case '\f' -> sb.append("\\f");
                case '\n' -> sb.append("\\n");
                case '\r' -> sb.append("\\r");
                case '\t' -> sb.append("\\t");
                default -> {
                    if (c < 0x20) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
                }
            }
        }
        return sb.toString();
    }

    /** Serialize as one JSONL line (sorted schema keys, no newline). */
    public String toJsonLine() {
        return "{\"decision\":" + decision
                + ",\"reason\":\"" + esc(reason)
                + "\",\"rule_id\":\"" + esc(ruleId)
                + "\",\"scope\":\"" + scope.name()
                + "\",\"scope_id\":\"" + esc(scopeId)
                + "\",\"severity\":" + severity
                + ",\"timestamp\":" + timestamp + "}";
    }
}
