package com.iap.monitoring;

import java.lang.management.GarbageCollectorMXBean;
import java.lang.management.ManagementFactory;
import java.util.List;
import java.util.TreeMap;

/**
 * Samples JVM GC activity ({@link GarbageCollectorMXBean}) into the pinned
 * {@code jvm_gc_pause_ns} histogram (deployment/grafana/README.md). Each
 * {@link #sample} records, per collector, the pauses that completed since
 * the previous sample: the MX bean exposes cumulative collection count and
 * total time, so the per-pause estimate is the mean pause of the delta
 * window ({@code delta_time / delta_count}), recorded {@code delta_count}
 * times — the histogram's log2 buckets absorb the approximation.
 */
public final class GcMetrics {
    private final MetricsRegistry registry;
    private final TreeMap<String, long[]> last = new TreeMap<>(); // {count, ms}

    public GcMetrics(MetricsRegistry registry) {
        this.registry = registry;
    }

    /** Poll the MX beans and record new pauses; returns pauses recorded. */
    public long sample() {
        List<GarbageCollectorMXBean> beans =
                ManagementFactory.getGarbageCollectorMXBeans();
        long recorded = 0;
        Histogram h = registry.histogram("jvm_gc_pause_ns");
        for (GarbageCollectorMXBean b : beans) {
            long count = b.getCollectionCount();
            long timeMs = b.getCollectionTime();
            if (count < 0) {
                continue; // collector does not report counts
            }
            long[] prev = last.computeIfAbsent(b.getName(), k -> new long[] {0, 0});
            long dCount = count - prev[0];
            long dTimeMs = Math.max(timeMs - prev[1], 0);
            if (dCount > 0) {
                long perPauseNs = dTimeMs * 1_000_000L / dCount;
                for (long i = 0; i < dCount; i++) {
                    h.record(perPauseNs);
                }
                recorded += dCount;
            }
            prev[0] = count;
            prev[1] = timeMs;
        }
        return recorded;
    }
}
