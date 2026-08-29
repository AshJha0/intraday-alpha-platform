package com.iap.monitoring;

import java.util.Map;
import java.util.TreeMap;

/**
 * Named metrics with deterministic (sorted) iteration and a Prometheus text
 * exposition writer — the Java mirror of rust/telemetry {@code Registry}.
 * Metric names follow the pinned contract (deployment/grafana/README.md):
 * snake_case, counters end {@code _total}, latency histograms end
 * {@code _ns}, gauges are bare nouns. Histograms are exposed as cumulative
 * {@code <name>_bucket{le="2^i - 1"}} series plus {@code _sum}/{@code _count}.
 *
 * <p>Single-threaded by design; callers that share a registry across
 * threads (e.g. the HTTP metrics server) synchronize on the registry
 * object itself.
 */
public final class MetricsRegistry {
    private final TreeMap<String, Counter> counters = new TreeMap<>();
    private final TreeMap<String, Gauge> gauges = new TreeMap<>();
    private final TreeMap<String, Histogram> histograms = new TreeMap<>();

    /** Named counter (created at zero on first use). */
    public Counter counter(String name) {
        return counters.computeIfAbsent(name, k -> new Counter());
    }

    /** Named gauge (created at zero on first use). */
    public Gauge gauge(String name) {
        return gauges.computeIfAbsent(name, k -> new Gauge());
    }

    /** Named histogram (created empty on first use). */
    public Histogram histogram(String name) {
        return histograms.computeIfAbsent(name, k -> new Histogram());
    }

    /** Read a counter value (0 when absent). */
    public long counterValue(String name) {
        Counter c = counters.get(name);
        return c == null ? 0 : c.get();
    }

    /** Read a gauge value ({@code null} when absent). */
    public Double gaugeValue(String name) {
        Gauge g = gauges.get(name);
        return g == null ? null : g.get();
    }

    /** Read-only named histogram ({@code null} when absent). */
    public Histogram histogramRef(String name) {
        return histograms.get(name);
    }

    /** Format a numeric value the way rust's f64 {@code Display} does. */
    public static String num(double v) {
        if (v == Math.rint(v) && Math.abs(v) < 1e15 && !Double.isNaN(v)) {
            return Long.toString((long) v);
        }
        return Double.toString(v);
    }

    /**
     * Prometheus text exposition (metrics sorted by name; histogram buckets
     * cumulative, empty buckets skipped, {@code +Inf} always present).
     */
    public String toPrometheus() {
        StringBuilder out = new StringBuilder(1024);
        for (Map.Entry<String, Counter> e : counters.entrySet()) {
            out.append("# TYPE ").append(e.getKey()).append(" counter\n");
            out.append(e.getKey()).append(' ').append(e.getValue().get()).append('\n');
        }
        for (Map.Entry<String, Gauge> e : gauges.entrySet()) {
            out.append("# TYPE ").append(e.getKey()).append(" gauge\n");
            out.append(e.getKey()).append(' ').append(num(e.getValue().get())).append('\n');
        }
        for (Map.Entry<String, Histogram> e : histograms.entrySet()) {
            String name = e.getKey();
            Histogram h = e.getValue();
            out.append("# TYPE ").append(name).append(" histogram\n");
            long cum = 0;
            long[] counts = h.buckets();
            for (int i = 0; i < counts.length; i++) {
                if (counts[i] == 0) {
                    continue;
                }
                cum += counts[i];
                out.append(name).append("_bucket{le=\"")
                        .append(Histogram.bucketUpper(i)).append("\"} ")
                        .append(cum).append('\n');
            }
            out.append(name).append("_bucket{le=\"+Inf\"} ").append(h.count()).append('\n');
            out.append(name).append("_sum ").append(num(h.sum())).append('\n');
            out.append(name).append("_count ").append(h.count()).append('\n');
        }
        return out.toString();
    }
}
