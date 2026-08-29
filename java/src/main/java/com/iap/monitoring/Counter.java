package com.iap.monitoring;

/** Monotone counter (mirrors rust/telemetry {@code Counter}). */
public final class Counter {
    private long value;

    /** Add one. */
    public void inc() {
        value++;
    }

    /** Add {@code n} (must be &gt;= 0: counters are monotone). */
    public void add(long n) {
        if (n < 0) {
            throw new IllegalArgumentException("counter add must be >= 0: " + n);
        }
        value += n;
    }

    /** Current value. */
    public long get() {
        return value;
    }
}
