package com.iap.core;

/** Order/quote/aggressor side (u8): BID=0, ASK=1. Int constants (hot path). */
public final class Side {
    public static final int BID = 0;
    public static final int ASK = 1;

    private Side() {
    }

    /** True when {@code side} is a valid side code. */
    public static boolean isValid(int side) {
        return side == BID || side == ASK;
    }
}
