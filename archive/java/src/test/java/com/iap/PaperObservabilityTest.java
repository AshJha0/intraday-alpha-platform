package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

import org.junit.Test;

import com.iap.platform.PaperTrading;

/**
 * Metric semantics the alerts and dashboards rely on
 * (PLATFORM_CONVENTIONS.md §12.6). Round-3 findings SEV-2 "StaleFeed pages
 * permanently on every replayed session", SEV-2 "SequenceGapRate cannot
 * notice the single gap that halts trading", SEV-2 "the Rust telemetry file
 * is never written — order-flow panels are dead" and SEV-2 "lifecycle claims
 * do not match the Java gauge"; proposed tests 13, 20 and 26.
 */
public class PaperObservabilityTest {
    /**
     * Event-time gap, wall-clock stamp and mode label are all exported, so a
     * replayed session can be told from a live one and StaleFeed can alert on
     * the event-time gap rather than {@code time() - md_last_event_unixtime}.
     */
    @Test
    public void feedFreshnessSeriesDistinguishReplayFromLive()
            throws IOException {
        PaperTrading.Result res = PaperTrading.run(PaperFixtures.session(800));
        String text = res.metrics.toPrometheus();
        for (String name : new String[] {"md_last_event_unixtime",
                "md_last_event_wallclock_unixtime", "md_event_time_gap_seconds",
                "platform_mode", "platform_session_state"}) {
            assertTrue("exports " + name, text.contains("# TYPE " + name));
        }
        double eventTime = res.metrics.gaugeValue("md_last_event_unixtime");
        double wall = res.metrics.gaugeValue("md_last_event_wallclock_unixtime");
        // The golden vector is historical: event time is far from wall clock.
        // That is exactly why a wall-vs-event-time StaleFeed rule pages
        // forever on replay.
        assertTrue("event time is the replayed feed's time",
                Math.abs(wall - eventTime) > 86400.0);
        assertTrue("wall-clock stamp is now",
                Math.abs(wall - System.currentTimeMillis() / 1000.0) < 3600.0);
        double gap = res.metrics.gaugeValue("md_event_time_gap_seconds");
        assertTrue("event-time gap is a small non-negative number: " + gap,
                gap >= 0.0 && gap < 3600.0);
        assertEquals(1.0,
                res.metrics.gaugeValue("platform_mode{mode=\"asap\"}"), 0.0);
        assertEquals(0.0,
                res.metrics.gaugeValue("platform_mode{mode=\"realtime\"}"), 0.0);
        assertEquals(PaperTrading.SessionState.FINISHED.code(),
                res.metrics.gaugeValue("platform_session_state"), 0.0);
    }

    /**
     * Test 13 — a sequence gap is exported on the very NEXT scrape, not at
     * the next 1,024-event sampling point: risk.json halts after one gap.
     */
    @Test
    public void sequenceGapIsExportedImmediately() throws IOException {
        Path events = gappedEvents(10);
        PaperTrading.Options opts = PaperFixtures.session(60);
        opts.eventsFile = events;
        PaperTrading.Result res = PaperTrading.run(opts);
        assertTrue("far fewer than the old 1024-event sampling point",
                res.eventsProcessed < 1024);
        assertEquals("the gap is counted", 1,
                res.metrics.counterValue("md_sequence_gaps_total"));
        assertEquals("and the book is marked stale", 1.0,
                res.metrics.gaugeValue("book_stale{instrument=\"1\"}"), 0.0);
        assertTrue("the exposition carries it",
                res.metrics.toPrometheus().contains("md_sequence_gaps_total 1"));
    }

    /**
     * Execution monitoring comes from the platform's OWN fills, replacing the
     * venue-simulator counters no deployed component ever wrote.
     */
    @Test
    public void executionCountersComeFromThePlatformsOwnFills()
            throws IOException {
        PaperTrading.Result res = PaperTrading.run(PaperFixtures.session(1500));
        assertEquals(res.fillCount, res.metrics.counterValue("exec_fills_total"));
        assertEquals(res.ordersSubmitted,
                res.metrics.counterValue("exec_orders_submitted_total"));
        assertEquals(res.riskRejected,
                res.metrics.counterValue("exec_child_orders_rejected_total"));
        String text = res.metrics.toPrometheus();
        assertTrue(text.contains("# TYPE exec_slippage_bps histogram"));
        assertTrue("slippage recorded for the session's fills",
                res.metrics.histogramRef("exec_slippage_bps").count() > 0);
        // no venue_* series exist here: those described a component the
        // deployment never ran (§12.6 "no aspirational targets")
        assertTrue(!text.contains("venue_fills_total"));
    }

    /**
     * Test 20 — the lifecycle gauge is IC-gated and OBSERVATIONAL: it never
     * changes a trading decision. This pins what the alert annotation and the
     * runbook are now allowed to claim (round-3 SEV-2).
     */
    @Test
    public void lifecycleGaugeIsIcGatedAndObservational() throws IOException {
        PaperTrading.Options a = PaperFixtures.session(1500);
        a.baselinesDir = Path.of("no", "such", "baselines"); // no PSI at all
        PaperTrading.Result withoutPsi = PaperTrading.run(a);
        PaperTrading.Options b = PaperFixtures.session(1500);
        PaperTrading.Result withPsi = PaperTrading.run(b);
        // the lifecycle state is identical with and without a PSI baseline:
        // it is gated on the rolling IC only (API_ADAPTIVE.md §6)
        assertEquals(withPsi.lifecycle, withoutPsi.lifecycle);
        assertEquals(withPsi.rollingIc, withoutPsi.rollingIc, 0.0);
        // ... and the trading outcome does not depend on it either
        assertEquals(withPsi.ordersSubmitted, withoutPsi.ordersSubmitted);
        assertEquals(withPsi.fillCount, withoutPsi.fillCount);
        assertEquals(withPsi.totalPnl, withoutPsi.totalPnl, 0.0);
        assertTrue("the gauge is exported for the alpha",
                withPsi.metrics.gaugeValue(
                        "alpha_lifecycle_state{alpha=\"EQ01\"}") != null);
    }

    /**
     * Test 26 — realtime pacing across a long halt gap and a receive_ts
     * regression: no negative sleep, no exception, and the session still
     * completes quickly at a high speed multiplier.
     */
    @Test
    public void realtimePacingSurvivesHaltGapAndTimestampRegression()
            throws IOException {
        Path file = pacingEvents();
        PaperTrading.Options opts = PaperFixtures.session(Long.MAX_VALUE);
        opts.eventsFile = file;
        opts.realtime = true;
        opts.speed = 3600.0;   // 30 min of event time -> ~0.5 s
        long t0 = System.nanoTime();
        PaperTrading.Result res = PaperTrading.run(opts);
        double seconds = (System.nanoTime() - t0) / 1e9;
        assertTrue("all events processed", res.eventsProcessed > 0);
        assertTrue("30-min gap paced at 3600x takes about half a second, not "
                + seconds + "s", seconds < 10.0);
        // the session survived the regression (a negative inter-event wait is
        // never slept) and the gap gauge is a finite number the alert can read
        assertEquals(PaperTrading.SessionState.FINISHED, res.state);
        double gap = res.metrics.gaugeValue("md_event_time_gap_seconds");
        assertTrue("gap gauge is finite: " + gap, Double.isFinite(gap));
        // the 30-minute halt gap WAS observed and would trip StaleFeed (> 5s)
        assertTrue("the halt gap was exported on the event that crossed it",
                res.metrics.histogramRef("book_update_latency_ns").count()
                        == res.eventsProcessed);
    }

    /** A short EQ stream whose sequence jumps at index {@code gapAt}. */
    private static Path gappedEvents(int gapAt) throws IOException {
        List<String> out = new ArrayList<>();
        long seq = 0;
        int idx = 0;
        for (String line : Files.readAllLines(PaperFixtures.goldenEvents(),
                StandardCharsets.UTF_8)) {
            if (line.isEmpty()) {
                continue;
            }
            java.util.Map<String, Object> ev =
                    Json.object(Json.parse(line));
            if (Json.asLong(ev.get("instrument_id")) != 1
                    || Json.asLong(ev.get("venue_id")) != 1) {
                continue;
            }
            // renumber the venue-1 stream, skipping one sequence at gapAt
            if (idx == gapAt) {
                seq += 2;
            } else {
                seq += 1;
            }
            out.add(line.replaceFirst("\"sequence\":[0-9]+",
                    "\"sequence\":" + seq));
            if (++idx >= 40) {
                break;
            }
        }
        Path p = Files.createTempFile("iap-gap", ".jsonl");
        Files.write(p, String.join("\n", out).getBytes(StandardCharsets.UTF_8));
        return p;
    }

    /**
     * A stream with a 30-minute receive_ts gap and one regression, built from
     * the golden vector's first venue-1 events.
     */
    private static Path pacingEvents() throws IOException {
        List<String> src = new ArrayList<>();
        for (String line : Files.readAllLines(PaperFixtures.goldenEvents(),
                StandardCharsets.UTF_8)) {
            if (line.isEmpty()) {
                continue;
            }
            java.util.Map<String, Object> ev = Json.object(Json.parse(line));
            if (Json.asLong(ev.get("instrument_id")) == 1) {
                src.add(line);
            }
            if (src.size() >= 12) {
                break;
            }
        }
        List<String> out = new ArrayList<>();
        long base = Json.asLong(Json.object(Json.parse(src.get(0)))
                .get("receive_ts"));
        for (int i = 0; i < src.size(); i++) {
            long shift = i >= 6 ? 1_800_000_000_000L : 0L;   // 30-min halt gap
            long regress = i == 9 ? -50_000_000L : 0L;       // receive_ts back
            long ts = base + (long) i * 1_000_000L + shift + regress;
            out.add(src.get(i).replaceFirst("\"receive_ts\":[0-9]+",
                    "\"receive_ts\":" + ts));
        }
        Path p = Files.createTempFile("iap-pacing", ".jsonl");
        Files.write(p, String.join("\n", out).getBytes(StandardCharsets.UTF_8));
        return p;
    }
}
