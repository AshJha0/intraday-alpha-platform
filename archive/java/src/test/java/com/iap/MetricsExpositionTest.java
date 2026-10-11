package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import java.util.HashSet;
import java.util.Set;

import org.junit.Test;

import com.iap.monitoring.Histogram;
import com.iap.monitoring.MetricsRegistry;

/**
 * Exposition-format contract (PLATFORM_CONVENTIONS.md §12.6): label values
 * are escaped, {@code le} labels parse as finite numbers and are cumulative
 * and monotone with {@code _count == +Inf}, and the series count of a whole
 * session is bounded (no cardinality growth with events or orders).
 *
 * <p>Round-3 findings SEV-3 "metric label built by string concatenation" and
 * proposed tests 21 / 22 / 24.
 */
public class MetricsExpositionTest {
    /** Test 21 — a label value with quotes/backslashes/newlines is escaped. */
    @Test
    public void expositionLabelValuesAreEscaped() {
        MetricsRegistry reg = new MetricsRegistry();
        String key = MetricsRegistry.labeled("alpha_rolling_ic", "alpha",
                "EQ\"01\\x\ny");
        reg.gauge(key).set(0.5);
        String text = reg.toPrometheus();
        // exactly the three pinned escapes, nothing raw
        assertTrue(text, text.contains(
                "alpha_rolling_ic{alpha=\"EQ\\\"01\\\\x\\ny\"} 0.5"));
        // the exposition stays line-oriented: one series, one TYPE line
        String[] lines = text.split("\n");
        assertEquals(2, lines.length);
        assertEquals("# TYPE alpha_rolling_ic gauge", lines[0]);
        // and the TYPE line names the BARE metric, once per family
        reg.gauge(MetricsRegistry.labeled("alpha_rolling_ic", "alpha", "EQ02"))
                .set(0.25);
        long typeLines = reg.toPrometheus().lines()
                .filter(l -> l.startsWith("# TYPE alpha_rolling_ic")).count();
        assertEquals(1, typeLines);
    }

    /** An invalid label NAME is rejected rather than silently emitted. */
    @Test
    public void invalidLabelNameIsRejected() {
        try {
            MetricsRegistry.labeled("x", "not a label", "v");
            fail("invalid label name accepted");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("invalid label name"));
        }
    }

    /**
     * Test 24 — every {@code le} is a finite integer or {@code +Inf}, the
     * bucket series is cumulative and monotone, and {@code _count} equals the
     * {@code +Inf} bucket (both derived from ONE read of the bucket array).
     */
    @Test
    public void histogramLeLabelsParseAsFloatAndAreCumulative() {
        MetricsRegistry reg = new MetricsRegistry();
        Histogram h = reg.histogram("book_update_latency_ns");
        com.iap.core.SplitMix64 rng = new com.iap.core.SplitMix64(11L);
        for (int i = 0; i < 5000; i++) {
            h.record(rng.below(1_000_000L));
        }
        String text = reg.toPrometheus();
        long prev = -1;
        long inf = -1;
        long count = -1;
        double prevLe = -1.0;
        for (String line : text.split("\n")) {
            if (line.startsWith("# TYPE ")) {
                continue;
            }
            if (line.startsWith("book_update_latency_ns_bucket{le=\"")) {
                String le = line.substring(
                        "book_update_latency_ns_bucket{le=\"".length(),
                        line.indexOf("\"}"));
                long v = Long.parseLong(line.substring(line.indexOf("} ") + 2));
                if ("+Inf".equals(le)) {
                    inf = v;
                } else {
                    double parsed = Double.parseDouble(le);
                    assertTrue(le, Double.isFinite(parsed));
                    assertEquals(le, Math.rint(parsed), parsed, 0.0);
                    assertTrue("le strictly increasing: " + le,
                            parsed > prevLe);
                    prevLe = parsed;
                }
                assertTrue("cumulative: " + line, v >= prev);
                prev = v;
            } else if (line.startsWith("book_update_latency_ns_count ")) {
                count = Long.parseLong(line.substring(
                        "book_update_latency_ns_count ".length()));
            }
        }
        assertEquals(5000, inf);
        assertEquals(inf, count);
    }

    /**
     * A LABELED histogram family renders as valid exposition: {@code # TYPE}
     * once for the family (not once per label value), {@code le} spliced INTO
     * the family's label set, and {@code _sum}/{@code _count} carrying the
     * same labels. {@code name{alpha="EQ01"}_bucket{le="7"}} — what a
     * per-key renderer produces — is a syntax error every Prometheus parser
     * rejects, so it would take the whole scrape down with it.
     */
    @Test
    public void labeledHistogramFamilyRendersOneTypeLineAndSplicesLe() {
        MetricsRegistry reg = new MetricsRegistry();
        reg.histogram(MetricsRegistry.labeled("exec_slippage_bps", "venue", "XLON"))
                .record(3);
        reg.histogram(MetricsRegistry.labeled("exec_slippage_bps", "venue", "XPAR"))
                .record(0);
        String text = reg.toPrometheus();
        assertEquals(text, 1, text.lines()
                .filter(l -> l.equals("# TYPE exec_slippage_bps histogram")).count());
        assertTrue(text, text.contains(
                "exec_slippage_bps_bucket{venue=\"XLON\",le=\"3\"} 1"));
        assertTrue(text, text.contains(
                "exec_slippage_bps_bucket{venue=\"XLON\",le=\"+Inf\"} 1"));
        assertTrue(text, text.contains("exec_slippage_bps_sum{venue=\"XLON\"} 3"));
        assertTrue(text, text.contains("exec_slippage_bps_count{venue=\"XLON\"} 1"));
        assertTrue(text, text.contains(
                "exec_slippage_bps_bucket{venue=\"XPAR\",le=\"0\"} 1"));
        // no key ever renders a brace before the child-series suffix
        for (String line : text.split("\n")) {
            int brace = line.indexOf('{');
            assertTrue(line, brace < 0 || !line.substring(brace).contains("}_"));
        }
    }

    /**
     * Test 22 — a full paper session's exposition stays under the pinned
     * series bound and does not grow with events, orders or fills.
     */
    @Test
    public void cardinalityIsBoundedAndDoesNotGrowWithEvents()
            throws java.io.IOException {
        com.iap.platform.PaperTrading.Options small = PaperFixtures.session(400);
        com.iap.platform.PaperTrading.Options big = PaperFixtures.session(1500);
        com.iap.platform.PaperTrading.Result a =
                com.iap.platform.PaperTrading.run(small);
        com.iap.platform.PaperTrading.Result b =
                com.iap.platform.PaperTrading.run(big);
        assertTrue("more events processed", b.eventsProcessed > a.eventsProcessed);
        Set<String> seriesA = seriesOf(a.metrics.toPrometheus());
        Set<String> seriesB = seriesOf(b.metrics.toPrometheus());
        // The catalogue is fixed: the longer session only ADDS series that
        // are created on first use (the first risk reject, the first
        // portfolio solve), never one series per event, order or fill.
        assertTrue("series are a fixed catalogue, not per-event: "
                        + seriesB.size() + " vs " + seriesA.size(),
                seriesB.containsAll(seriesA)
                        && seriesB.size() - seriesA.size() <= 4);
        assertTrue("series count bound (<= 60): " + seriesB.size(),
                seriesB.size() <= 60);
        // and the only label values in the whole exposition are the pinned
        // enumerations: one alpha id, the mode/limit names, the instrument
        for (String name : seriesB) {
            int brace = name.indexOf('{');
            if (brace < 0) {
                continue;
            }
            assertTrue("pinned label set: " + name,
                    name.contains("{alpha=\"EQ01\"}")
                            || name.startsWith("platform_mode{mode=")
                            || name.startsWith("risk_limit{limit=")
                            || name.startsWith("book_stale{instrument="));
        }
    }

    /** Distinct series names (histogram buckets collapse to the family). */
    private static Set<String> seriesOf(String exposition) {
        Set<String> out = new HashSet<>();
        for (String line : exposition.split("\n")) {
            if (line.startsWith("# TYPE ")) {
                continue;
            }
            String name = line.substring(0, line.lastIndexOf(' '));
            int bucket = name.indexOf("_bucket{le=");
            out.add(bucket < 0 ? name : name.substring(0, bucket) + "_bucket");
        }
        return out;
    }
}
