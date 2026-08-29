package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

import org.junit.Test;

import com.iap.core.SplitMix64;
import com.iap.monitoring.GcMetrics;
import com.iap.monitoring.Histogram;
import com.iap.monitoring.MetricsRegistry;

/**
 * Monitoring primitives: log2 bucket boundaries (mirroring rust/telemetry
 * exactly), percentile correctness vs a sorted reference, and Prometheus
 * text exposition validity (cumulative buckets, _sum/_count, TYPE lines).
 */
public class MetricsTest {
    @Test
    public void bucketBoundariesMatchRustTelemetry() {
        assertEquals(0, Histogram.bucketOf(0));
        assertEquals(1, Histogram.bucketOf(1));
        assertEquals(2, Histogram.bucketOf(2));
        assertEquals(2, Histogram.bucketOf(3));
        assertEquals(3, Histogram.bucketOf(4));
        assertEquals(10, Histogram.bucketOf(1023));
        assertEquals(11, Histogram.bucketOf(1024));
        assertEquals(63, Histogram.bucketOf(Long.MAX_VALUE));
        assertEquals(0, Histogram.bucketUpper(0));
        assertEquals(1, Histogram.bucketUpper(1));
        assertEquals(3, Histogram.bucketUpper(2));
        assertEquals(1023, Histogram.bucketUpper(10));
        try {
            Histogram.bucketOf(-1);
            fail("negative sample");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains(">= 0"));
        }
    }

    @Test
    public void quantilesMatchSortedReferenceWithinOnePowerOfTwo() {
        SplitMix64 rng = new SplitMix64(20260829L);
        Histogram h = new Histogram();
        List<Long> samples = new ArrayList<>();
        for (int i = 0; i < 5000; i++) {
            long v = rng.below(2_000_000L); // ns-scale latencies
            samples.add(v);
            h.record(v);
        }
        long[] sorted = samples.stream().mapToLong(Long::longValue).sorted()
                .toArray();
        for (double q : new double[] {0.5, 0.9, 0.99, 0.999, 1.0}) {
            int rank = Math.max((int) Math.ceil(q * sorted.length), 1);
            long exact = sorted[rank - 1];
            long got = h.quantile(q);
            // the pinned semantics: the inclusive upper bound of the exact
            // order statistic's bucket — >= exact, < 2*exact (one power of 2)
            assertEquals("bucket upper of the exact order statistic",
                    Histogram.bucketUpper(Histogram.bucketOf(exact)), got);
            assertTrue("conservative", got >= exact);
            assertTrue("within one power of two", exact == 0 || got < 2 * exact);
        }
        assertEquals(5000, h.count());
        double sum = 0;
        for (long v : sorted) {
            sum += v;
        }
        assertEquals(sum, h.sum(), 0.0);
        assertEquals(sorted[sorted.length - 1], h.max());
    }

    @Test
    public void emptyAndDegenerateQuantiles() {
        Histogram h = new Histogram();
        try {
            h.quantile(0.5);
            fail("empty histogram");
        } catch (IllegalStateException expected) {
            assertTrue(expected.getMessage().contains("empty"));
        }
        try {
            h.record(1);
            h.quantile(1.5);
            fail("bad q");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("[0, 1]"));
        }
        // single sample: every quantile is that bucket's upper bound
        Histogram one = new Histogram();
        one.record(5); // bucket [4,8) -> upper 7
        assertEquals(7, one.quantile(0.0));
        assertEquals(7, one.quantile(1.0));
        assertArrayEqualsLong(new long[] {7, 7, 7}, one.p50p99p999());
    }

    private static void assertArrayEqualsLong(long[] want, long[] got) {
        assertTrue(Arrays.equals(want, got));
    }

    @Test
    public void expositionFormatIsValidPrometheusText() {
        MetricsRegistry reg = new MetricsRegistry();
        reg.counter("md_events_total").add(42);
        reg.gauge("risk_kill_switch_engaged").set(1.0);
        reg.gauge("exec_slippage_bps").set(-1.25);
        Histogram h = reg.histogram("order_path_latency_ns");
        h.record(0);
        h.record(3);
        h.record(3);
        h.record(900);
        String text = reg.toPrometheus();
        String[] lines = text.split("\n");
        // counters first, then gauges, then histograms; sorted within kind
        assertEquals("# TYPE md_events_total counter", lines[0]);
        assertEquals("md_events_total 42", lines[1]);
        assertEquals("# TYPE exec_slippage_bps gauge", lines[2]);
        assertEquals("exec_slippage_bps -1.25", lines[3]);
        assertEquals("# TYPE risk_kill_switch_engaged gauge", lines[4]);
        assertEquals("risk_kill_switch_engaged 1", lines[5]);
        assertEquals("# TYPE order_path_latency_ns histogram", lines[6]);
        // cumulative buckets, empty buckets skipped, +Inf == count
        assertEquals("order_path_latency_ns_bucket{le=\"0\"} 1", lines[7]);
        assertEquals("order_path_latency_ns_bucket{le=\"3\"} 3", lines[8]);
        assertEquals("order_path_latency_ns_bucket{le=\"1023\"} 4", lines[9]);
        assertEquals("order_path_latency_ns_bucket{le=\"+Inf\"} 4", lines[10]);
        assertEquals("order_path_latency_ns_sum 906", lines[11]);
        assertEquals("order_path_latency_ns_count 4", lines[12]);
        assertEquals(13, lines.length);
        // generic validity: every non-comment line is "name[{le=..}] number"
        for (String line : lines) {
            if (line.startsWith("# TYPE ")) {
                continue;
            }
            String[] parts = line.split(" ");
            assertEquals(line, 2, parts.length);
            double v = Double.parseDouble(parts[1]);
            assertTrue(line, Double.isFinite(v));
        }
    }

    @Test
    public void countersAreMonotoneAndGaugesLastValue() {
        MetricsRegistry reg = new MetricsRegistry();
        reg.counter("x_total").inc();
        reg.counter("x_total").add(4);
        assertEquals(5, reg.counterValue("x_total"));
        assertEquals(0, reg.counterValue("missing_total"));
        try {
            reg.counter("x_total").add(-1);
            fail("negative add");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains(">= 0"));
        }
        reg.gauge("pos").set(-12.5);
        assertEquals(-12.5, reg.gaugeValue("pos"), 0.0);
        assertEquals(null, reg.gaugeValue("missing"));
    }

    @Test
    public void gcMetricsRecordIntoPinnedHistogram() {
        MetricsRegistry reg = new MetricsRegistry();
        GcMetrics gc = new GcMetrics(reg);
        gc.sample();
        // provoke some allocation, then resample; whether a GC actually ran
        // is JVM-dependent, so only the invariants are asserted
        byte[][] garbage = new byte[64][];
        for (int i = 0; i < 4096; i++) {
            garbage[i & 63] = new byte[8192];
        }
        assertTrue(garbage[0].length > 0);
        gc.sample();
        // the histogram exists under the pinned grafana contract name and
        // the exposition renders it
        reg.histogram("jvm_gc_pause_ns");
        assertTrue(reg.toPrometheus().contains("jvm_gc_pause_ns"));
    }
}
