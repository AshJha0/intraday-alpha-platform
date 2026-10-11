package com.iap.monitoring;

/**
 * Last-value gauge (mirrors rust/telemetry {@code Gauge}). A volatile
 * double is written and read atomically (JLS §17.7 guarantees 64-bit
 * atomicity for volatile fields), so the exposition thread never takes a
 * lock the trading thread could be holding.
 */
public final class Gauge {
    private volatile double value;

    /** Set the gauge. */
    public void set(double v) {
        value = v;
    }

    /** Current value. */
    public double get() {
        return value;
    }
}
