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
}
