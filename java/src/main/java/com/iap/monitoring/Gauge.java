package com.iap.monitoring;

/** Last-value gauge (mirrors rust/telemetry {@code Gauge}). */
public final class Gauge {
    private double value;

    /** Set the gauge. */
    public void set(double v) {
        value = v;
    }

    /** Current value. */
    public double get() {
        return value;
    }
}
