package com.iap.execution;

/**
 * How a parent order's children are sent (mirror of the C++
 * {@code ExecPolicy}, {@code cpp/include/iap/execution/algos.hpp}).
 * {@link #NATIVE} is the default and the only behaviour before v1.5.0:
 * TWAP / VWAP children join the same-side best as LIMIT orders and wait,
 * POV / IS children are MARKET orders. {@link #AGGRESSIVE} sends every
 * child as a MARKET order. {@link #PASSIVE} works every schedule step
 * through the POST, REST, REPRICE / CROSS state machine of
 * {@link PassivePolicy}.
 */
public enum ExecPolicy {
    NATIVE(0), PASSIVE(1), AGGRESSIVE(2);

    public final int code;

    ExecPolicy(int code) {
        this.code = code;
    }
}
