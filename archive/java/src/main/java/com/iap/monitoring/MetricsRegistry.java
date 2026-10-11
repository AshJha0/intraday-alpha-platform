package com.iap.monitoring;

import java.util.Map;
import java.util.concurrent.ConcurrentSkipListMap;

/**
 * Named metrics with deterministic (sorted) iteration and a Prometheus text
 * exposition writer — the Java mirror of rust/telemetry {@code Registry}.
 * Metric names follow the pinned contract (deployment/grafana/README.md):
 * snake_case, counters end {@code _total}, latency histograms end
 * {@code _ns}, gauges are bare nouns. Histograms are exposed as cumulative
 * {@code <name>_bucket{le="2^i - 1"}} series plus {@code _sum}/{@code _count}.
 *
 * <p>Concurrency (PLATFORM_CONVENTIONS.md §12.4): the registry and every
 * metric are lock-free — sorted concurrent maps of atomic values — so the
 * HTTP exposition thread renders while the trading thread records, and
 * neither ever waits for the other. A scrape therefore observes each
 * series at a slightly different instant (never a torn value); callers
 * that need a consistent multi-series snapshot take their own lock.
 */
public final class MetricsRegistry {
    private final ConcurrentSkipListMap<String, Counter> counters =
            new ConcurrentSkipListMap<>();
    private final ConcurrentSkipListMap<String, Gauge> gauges =
            new ConcurrentSkipListMap<>();
    private final ConcurrentSkipListMap<String, Histogram> histograms =
            new ConcurrentSkipListMap<>();

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

    /** Number of distinct series keys (counters + gauges + histograms). */
    public int seriesCount() {
        return counters.size() + gauges.size() + histograms.size();
    }

    /** Format a numeric value the way rust's f64 {@code Display} does. */
    public static String num(double v) {
        if (v == Math.rint(v) && Math.abs(v) < 1e15 && !Double.isNaN(v)) {
            return Long.toString((long) v);
        }
        return Double.toString(v);
    }

    /**
     * Registry key for a labeled series, {@code name{label="value"}}, with
     * the value escaped per the Prometheus text exposition format
     * (backslash, double quote and newline). Label names must be
     * {@code [a-zA-Z_][a-zA-Z0-9_]*}.
     */
    public static String labeled(String name, String label, String value) {
        if (!label.matches("[a-zA-Z_][a-zA-Z0-9_]*")) {
            throw new IllegalArgumentException("invalid label name: " + label);
        }
        StringBuilder sb = new StringBuilder(name.length() + label.length()
                + value.length() + 8);
        sb.append(name).append('{').append(label).append("=\"");
        for (int i = 0; i < value.length(); i++) {
            char c = value.charAt(i);
            switch (c) {
                case '\\' -> sb.append("\\\\");
                case '"' -> sb.append("\\\"");
                case '\n' -> sb.append("\\n");
                default -> sb.append(c);
            }
        }
        return sb.append("\"}").toString();
    }

    /**
     * Metric name without any {@code {label="..."}} suffix. Registry keys
     * may carry a label set (e.g.
     * {@code alpha_live_vs_backtest_drift{alpha="EQ01"}}); the exposition's
     * {@code # TYPE} line must name the bare metric, emitted once per
     * family (labeled series of one family sort adjacently in a sorted map).
     */
    private static String baseName(String key) {
        int brace = key.indexOf('{');
        return brace < 0 ? key : key.substring(0, brace);
    }

    /**
     * The label pairs of a registry key without the enclosing braces
     * ({@code alpha="EQ01"}), or {@code null} when the key is unlabeled.
     * Histogram child series have to splice {@code le} INTO that set —
     * {@code name{alpha="EQ01"}_bucket{le="7"}} is not valid exposition
     * syntax, {@code name_bucket{alpha="EQ01",le="7"}} is.
     */
    private static String labelsOf(String key) {
        int brace = key.indexOf('{');
        if (brace < 0 || !key.endsWith("}")) {
            return null;
        }
        String inner = key.substring(brace + 1, key.length() - 1);
        return inner.isEmpty() ? null : inner;
    }

    /**
     * Render one histogram child series name: {@code <base><suffix>} with the
     * family's labels and, for buckets, the {@code le} bound appended last
     * (Prometheus requires {@code le} to be part of the bucket's label set).
     */
    private static void appendChild(StringBuilder out, String base, String labels,
                                    String suffix, String le) {
        out.append(base).append(suffix);
        if (labels == null && le == null) {
            return;
        }
        out.append('{');
        if (labels != null) {
            out.append(labels);
            if (le != null) {
                out.append(',');
            }
        }
        if (le != null) {
            out.append("le=\"").append(le).append('"');
        }
        out.append('}');
    }

    /**
     * Prometheus text exposition (metrics sorted by name; histogram buckets
     * cumulative, empty buckets skipped, {@code +Inf} always present and
     * equal to {@code _count} — both derived from one read of the buckets).
     */
    public String toPrometheus() {
        StringBuilder out = new StringBuilder(1024);
        String typed = null;
        for (Map.Entry<String, Counter> e : counters.entrySet()) {
            String base = baseName(e.getKey());
            if (!base.equals(typed)) {
                out.append("# TYPE ").append(base).append(" counter\n");
                typed = base;
            }
            out.append(e.getKey()).append(' ').append(e.getValue().get()).append('\n');
        }
        typed = null;
        for (Map.Entry<String, Gauge> e : gauges.entrySet()) {
            String base = baseName(e.getKey());
            if (!base.equals(typed)) {
                out.append("# TYPE ").append(base).append(" gauge\n");
                typed = base;
            }
            out.append(e.getKey()).append(' ').append(num(e.getValue().get())).append('\n');
        }
        typed = null;
        for (Map.Entry<String, Histogram> e : histograms.entrySet()) {
            String key = e.getKey();
            String base = baseName(key);
            String labels = labelsOf(key);
            Histogram h = e.getValue();
            if (!base.equals(typed)) {
                out.append("# TYPE ").append(base).append(" histogram\n");
                typed = base;
            }
            long cum = 0;
            long[] counts = h.buckets();
            for (int i = 0; i < counts.length; i++) {
                if (counts[i] == 0) {
                    continue;
                }
                cum += counts[i];
                appendChild(out, base, labels, "_bucket", Long.toString(Histogram.bucketUpper(i)));
                out.append(' ').append(cum).append('\n');
            }
            appendChild(out, base, labels, "_bucket", "+Inf");
            out.append(' ').append(cum).append('\n');
            appendChild(out, base, labels, "_sum", null);
            out.append(' ').append(num(h.sum())).append('\n');
            appendChild(out, base, labels, "_count", null);
            out.append(' ').append(cum).append('\n');
        }
        return out.toString();
    }
}
