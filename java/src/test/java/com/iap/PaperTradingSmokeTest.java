package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.Proxy;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.Map;

import org.junit.Test;

import com.iap.platform.PaperTrading;

/**
 * Paper-trading smoke: a short as-fast-as-possible session over the golden
 * EQ vector is deterministic (same fills / orders / P&amp;L twice), writes
 * a parseable session report, and serves a scrapeable /metrics endpoint
 * with the pinned metric names while running.
 */
public class PaperTradingSmokeTest {
    private static PaperTrading.Options shortSession() {
        PaperTrading.Options opts = new PaperTrading.Options();
        opts.configsDir = Paths.get("..", "configs");
        opts.eventsFile = Paths.get("..", "tests", "golden",
                "events_eq_mbo.jsonl");
        opts.maxEvents = 1500;
        return opts;
    }

    @Test
    public void shortRunIsDeterministicAndTrades() throws IOException {
        PaperTrading.Result a = PaperTrading.run(shortSession());
        PaperTrading.Result b = PaperTrading.run(shortSession());
        assertEquals(1500, a.eventsProcessed);
        assertTrue("session submits orders", a.ordersSubmitted > 0);
        assertTrue("session fills", a.fillCount > 0);
        assertTrue("risk engine saw the orders", a.riskDecisions > 0);
        // event-time pipeline => identical trading outcomes across runs
        assertEquals(a.eventsProcessed, b.eventsProcessed);
        assertEquals(a.ordersSubmitted, b.ordersSubmitted);
        assertEquals(a.fillCount, b.fillCount);
        assertEquals(a.riskDecisions, b.riskDecisions);
        assertEquals(a.riskAllowed, b.riskAllowed);
        assertEquals(a.riskRejected, b.riskRejected);
        assertEquals(a.totalPnl, b.totalPnl, 0.0);
        assertEquals(a.grossPnl, b.grossPnl, 0.0);
        // metrics agree with the run summary
        assertEquals(a.eventsProcessed,
                a.metrics.counterValue("md_events_total"));
        assertEquals(a.riskDecisions,
                a.metrics.counterValue("risk_decisions_total"));
        assertTrue(a.metrics.counterValue("alpha_signals_total") > 0);
        // the portfolio sizing layer actually solved (>= 22 one-minute bars)
        assertTrue("portfolio optimizer ran in the pipeline",
                a.metrics.counterValue("portfolio_solves_total") > 0);
    }

    @Test
    public void reportIsWrittenAndParses() throws IOException {
        PaperTrading.Options opts = shortSession();
        Path report = Files.createTempDirectory("iap-paper")
                .resolve("session_report.json");
        opts.reportPath = report;
        PaperTrading.Result res = PaperTrading.run(opts);
        assertTrue(Files.exists(report));
        Map<String, Object> doc = Json.object(Json.parse(
                new String(Files.readAllBytes(report), StandardCharsets.UTF_8)));
        assertEquals(res.eventsProcessed, Json.asLong(doc.get("events_processed")));
        assertEquals(res.fillCount, Json.asLong(doc.get("fills")));
        assertEquals(res.ordersSubmitted, Json.asLong(doc.get("orders_submitted")));
        assertEquals("asap", doc.get("mode"));
        assertEquals(Boolean.FALSE, doc.get("kill_switch_engaged"));
        Map<String, Object> risk = Json.object(doc.get("risk"));
        assertEquals(res.riskAllowed, Json.asLong(risk.get("allowed")));
        assertEquals(res.riskRejected, Json.asLong(risk.get("rejected")));
        Map<String, Object> pnl = Json.object(doc.get("pnl"));
        assertEquals(res.totalPnl, Json.asDouble(pnl.get("total")), 0.0);
        Map<String, Object> lat = Json.object(doc.get("latency_ns"));
        Map<String, Object> book = Json.object(lat.get("book_update"));
        assertTrue(Json.asLong(book.get("count")) > 0);
        assertTrue(Json.asLong(book.get("p50")) <= Json.asLong(book.get("p99")));
        assertTrue(Json.asLong(book.get("p99")) <= Json.asLong(book.get("p999")));
    }

    @Test
    public void metricsEndpointServesThePinnedNamesWhileRunning()
            throws IOException {
        PaperTrading.Options opts = shortSession();
        opts.maxEvents = 800;
        opts.port = 0; // ephemeral
        PaperTrading.Result res = PaperTrading.run(opts);
        // server stops at session end; scrape the final registry directly
        String text;
        synchronized (res.metrics) {
            text = res.metrics.toPrometheus();
        }
        for (String name : new String[] {"md_events_total",
                "md_last_event_unixtime", "decode_latency_ns",
                "book_update_latency_ns", "alpha_signals_total",
                "alpha_rolling_ic", "alpha_lifecycle_state",
                "risk_decisions_total", "risk_kill_switch_engaged",
                "portfolio_gross_notional", "portfolio_net_notional",
                "portfolio_drawdown", "jvm_gc_pause_ns"}) {
            assertTrue("exposes " + name, text.contains("# TYPE " + name));
        }
        assertTrue("bound an ephemeral port", res.httpPort > 0);
        // and a live scrape works while a server is up on the same registry
        com.iap.api.MetricsServer server = new com.iap.api.MetricsServer(
                res.metrics, 0, () -> "{\"status\":\"ok\"}");
        server.start();
        try {
            HttpURLConnection conn = (HttpURLConnection) URI.create(
                    "http://127.0.0.1:" + server.port() + "/metrics").toURL()
                    .openConnection(Proxy.NO_PROXY);
            conn.setConnectTimeout(5000);
            conn.setReadTimeout(5000);
            assertEquals(200, conn.getResponseCode());
            String scraped;
            try (InputStream in = conn.getInputStream()) {
                scraped = new String(in.readAllBytes(), StandardCharsets.UTF_8);
            }
            assertEquals(text, scraped);
            for (String line : scraped.split("\n")) {
                assertTrue(line, line.startsWith("# TYPE ")
                        || line.split(" ").length == 2);
            }
        } finally {
            server.stop();
        }
    }

    /** Deterministic EQ01 baseline dir (API_ADAPTIVE.md raw-values form). */
    private static Path baselineDir() throws IOException {
        Path dir = Files.createTempDirectory("iap-baselines");
        StringBuilder sb = new StringBuilder(
                "{\"alpha_id\":\"EQ01\",\"values\":[");
        com.iap.core.SplitMix64 rng = new com.iap.core.SplitMix64(7L);
        for (int i = 0; i < 512; i++) {
            // roughly the live signal's scale (beta ~ 1.26e-6, |z| <= 4)
            sb.append(i == 0 ? "" : ",").append(rng.normal() * 1.26e-6);
        }
        sb.append("]}");
        Files.write(dir.resolve("signal_eq01.json"),
                sb.toString().getBytes(StandardCharsets.UTF_8));
        return dir;
    }

    @Test
    public void adaptiveMetricsAreLiveInMetricsAndReport() throws IOException {
        PaperTrading.Options opts = shortSession();
        opts.baselinesDir = baselineDir();
        PaperTrading.Result a = PaperTrading.run(opts);
        PaperTrading.Result b = PaperTrading.run(opts);

        // the three adaptability gauges are on /metrics with the alpha label
        String text;
        synchronized (a.metrics) {
            text = a.metrics.toPrometheus();
        }
        for (String name : new String[] {"alpha_live_vs_backtest_drift",
                "alpha_rolling_ic", "alpha_lifecycle_state"}) {
            assertTrue("TYPE line for " + name,
                    text.contains("# TYPE " + name + " gauge"));
            assertTrue("labeled series for " + name,
                    text.contains(name + "{alpha=\"EQ01\"} "));
        }

        // final values are real, deterministic, and mirrored in Result
        assertTrue("PSI computed", !Double.isNaN(a.driftPsi));
        assertTrue("PSI is non-negative", a.driftPsi >= 0.0);
        assertTrue("rolling IC computed", !Double.isNaN(a.rollingIc));
        assertTrue("IC is a correlation",
                a.rollingIc >= -1.0 && a.rollingIc <= 1.0);
        assertEquals(a.driftPsi, b.driftPsi, 0.0);
        assertEquals(a.rollingIc, b.rollingIc, 0.0);
        assertEquals(a.lifecycle, b.lifecycle);
        assertEquals(a.driftPsi, a.metrics.gaugeValue(
                "alpha_live_vs_backtest_drift{alpha=\"EQ01\"}"), 0.0);
        assertEquals(a.rollingIc, a.metrics.gaugeValue(
                "alpha_rolling_ic{alpha=\"EQ01\"}"), 0.0);
        assertEquals((double) a.lifecycle.code(), a.metrics.gaugeValue(
                "alpha_lifecycle_state{alpha=\"EQ01\"}"), 0.0);

        // and in the session report's adaptive section
        Map<String, Object> doc = Json.object(Json.parse(a.reportJson));
        Map<String, Object> adaptive = Json.object(doc.get("adaptive"));
        assertEquals(a.driftPsi, Json.asDouble(adaptive.get("drift_psi")),
                0.0);
        assertEquals(a.rollingIc, Json.asDouble(adaptive.get("rolling_ic")),
                0.0);
        assertEquals(a.lifecycle.name(), adaptive.get("lifecycle"));
        assertEquals(a.lifecycle.code(),
                Json.asLong(adaptive.get("lifecycle_code")));
    }

    @Test
    public void missingBaselineDisarmsDriftButKeepsIcAndLifecycle()
            throws IOException {
        PaperTrading.Options opts = shortSession();
        opts.maxEvents = 800;
        opts.baselinesDir = Paths.get("no", "such", "baselines");
        PaperTrading.Result res = PaperTrading.run(opts);
        String text;
        synchronized (res.metrics) {
            text = res.metrics.toPrometheus();
        }
        assertTrue("no drift series without a baseline",
                !text.contains("alpha_live_vs_backtest_drift"));
        assertTrue(text.contains("alpha_rolling_ic{alpha=\"EQ01\"} "));
        assertTrue(text.contains("alpha_lifecycle_state{alpha=\"EQ01\"} "));
        assertTrue(Double.isNaN(res.driftPsi));
        // report carries null for the unarmed statistic
        Map<String, Object> adaptive = Json.object(Json.object(
                Json.parse(res.reportJson)).get("adaptive"));
        assertEquals(null, adaptive.get("drift_psi"));
        assertTrue(!Double.isNaN(Json.asDouble(adaptive.get("rolling_ic"))));
    }
}
