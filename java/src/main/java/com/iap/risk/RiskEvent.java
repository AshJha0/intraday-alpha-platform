package com.iap.risk;

/**
 * One audit-log record (schemas/risk/risk_event.schema.json x-version 2 —
 * exact field set and enum codes). Serialized as one JSONL line with sorted
 * keys (decision, prev_sha256, reason, rule_id, scope, scope_id, seq,
 * severity, timestamp) — identical input sequences produce byte-identical
 * audit logs.
 *
 * <p>Tamper evidence (G-P0-1): the engine stamps every record it emits with
 * {@code seq} (1, 2, 3, ... continued across a chained restore) and
 * {@code prevSha256}, the lowercase hex SHA-256 of the previous record's
 * JSONL line, or {@link #GENESIS_SHA256} for the first.
 */
public record RiskEvent(long timestamp, Scope scope, String scopeId,
        String ruleId, int severity, int decision, String reason, long seq,
        String prevSha256) {
    /** {@code prev_sha256} of the first record of a chain (64 zeros). */
    public static final String GENESIS_SHA256 =
            "0000000000000000000000000000000000000000000000000000000000000000";

    public RiskEvent {
        if (severity < 1 || severity > 3 || decision < 1 || decision > 3) {
            throw new IllegalArgumentException(
                    "RiskEvent enum out of domain: severity " + severity
                            + " decision " + decision);
        }
    }

    /** A record not yet emitted (the engine assigns its chain position). */
    public RiskEvent(long timestamp, Scope scope, String scopeId, String ruleId,
            int severity, int decision, String reason) {
        this(timestamp, scope, scopeId, ruleId, severity, decision, reason, 0L, "");
    }

    /** This record at chain position {@code seq} after {@code prevSha256}. */
    RiskEvent chained(long seqNo, String prev) {
        return new RiskEvent(timestamp, scope, scopeId, ruleId, severity, decision,
                reason, seqNo, prev);
    }

    /** Lowercase hex SHA-256 of one audit line (UTF-8, without newline). */
    public static String lineSha256(String line) {
        try {
            byte[] d = java.security.MessageDigest.getInstance("SHA-256")
                    .digest(line.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            StringBuilder sb = new StringBuilder(64);
            for (byte b : d) {
                sb.append(Character.forDigit((b >> 4) & 0xF, 16))
                        .append(Character.forDigit(b & 0xF, 16));
            }
            return sb.toString();
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 unavailable", e);
        }
    }

    /**
     * {@code line} without its chain fields: the record's content,
     * comparable between an unbroken run and a restored one (whose chain
     * positions are shifted by the {@code STATE_RESTORED} record).
     */
    public static String unchainedLine(String line) {
        int p = line.indexOf(",\"prev_sha256\":\"");
        String out = line;
        if (p >= 0) {
            int end = line.indexOf('"', p + 16);
            out = line.substring(0, p) + line.substring(end + 1);
        }
        int s = out.lastIndexOf(",\"seq\":");
        if (s >= 0) {
            int end = out.indexOf(',', s + 7);
            out = out.substring(0, s) + out.substring(end);
        }
        return out;
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
                + ",\"prev_sha256\":\"" + esc(prevSha256)
                + "\",\"reason\":\"" + esc(reason)
                + "\",\"rule_id\":\"" + esc(ruleId)
                + "\",\"scope\":\"" + scope.name()
                + "\",\"scope_id\":\"" + esc(scopeId)
                + "\",\"seq\":" + Long.toUnsignedString(seq)
                + ",\"severity\":" + severity
                + ",\"timestamp\":" + timestamp + "}";
    }
}
