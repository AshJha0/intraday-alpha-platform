package com.iap.core;

/** STATUS event payload codes, carried in the qty field (u8). */
public final class SessionStatus {
    public static final int TRADING = 1;
    public static final int HALT = 2;
    public static final int AUCTION = 3;
    public static final int CLOSE = 4;

    private SessionStatus() {
    }

    /** True when {@code code} is a known session status code. */
    public static boolean isValid(long code) {
        return code >= TRADING && code <= CLOSE;
    }
}
