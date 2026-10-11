package com.iap.risk;

/** Severity codes (schema: INFO=1 WARN=2 BREACH=3). */
public enum Severity {
    /** Routine (allowed orders, switch clears). */
    INFO(1),
    /** A rejected order / degraded state. */
    WARN(2),
    /** A limit breach or kill-switch action. */
    BREACH(3);

    private final int code;

    Severity(int code) {
        this.code = code;
    }

    /** Wire value. */
    public int code() {
        return code;
    }
}
