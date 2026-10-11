package com.iap.risk;

/** Decision codes (schema: ALLOW=1 REJECT=2 KILL=3). */
public enum Decision {
    /** Order may proceed. */
    ALLOW(1),
    /** Order rejected. */
    REJECT(2),
    /** A kill switch engaged / trading stopped in scope. */
    KILL(3);

    private final int code;

    Decision(int code) {
        this.code = code;
    }

    /** Wire value. */
    public int code() {
        return code;
    }
}
