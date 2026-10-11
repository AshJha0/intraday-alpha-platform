package com.iap.monitoring;

import java.util.concurrent.atomic.AtomicLong;

/**
 * Monotone counter (mirrors rust/telemetry {@code Counter}). Lock-free:
 * the trading thread increments and the HTTP exposition thread reads
 * without ever sharing a monitor, so a slow scraper can never stall the
 * hot path (PLATFORM_CONVENTIONS.md §12.4).
 */
public final class Counter {
    private final AtomicLong value = new AtomicLong();

    /** Add one. */
    public void inc() {
        value.incrementAndGet();
    }

    /** Add {@code n} (must be &gt;= 0: counters are monotone). */
    public void add(long n) {
        if (n < 0) {
            throw new IllegalArgumentException("counter add must be >= 0: " + n);
        }
        value.addAndGet(n);
    }

    /** Current value. */
    public long get() {
        return value.get();
    }
}
