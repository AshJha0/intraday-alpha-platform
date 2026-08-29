package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.Proxy;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.util.Map;

import org.junit.Test;

import com.iap.api.MetricsServer;
import com.iap.monitoring.MetricsRegistry;

/**
 * HTTP exposition endpoint: /metrics serves valid Prometheus text of the
 * shared registry (and observes later updates), /health and /status serve
 * JSON. Bound to an ephemeral port so the suite never collides.
 */
public class ApiServerTest {
    private static String get(int port, String path) throws IOException {
        HttpURLConnection conn = (HttpURLConnection) URI.create(
                "http://127.0.0.1:" + port + path).toURL()
                .openConnection(Proxy.NO_PROXY);
        conn.setConnectTimeout(5000);
        conn.setReadTimeout(5000);
        assertEquals(200, conn.getResponseCode());
        try (InputStream in = conn.getInputStream()) {
            return new String(in.readAllBytes(), StandardCharsets.UTF_8);
        }
    }

    @Test
    public void servesMetricsHealthAndStatus() throws IOException {
        MetricsRegistry reg = new MetricsRegistry();
        synchronized (reg) {
            reg.counter("md_events_total").add(7);
            reg.gauge("risk_kill_switch_engaged").set(0.0);
            reg.histogram("book_update_latency_ns").record(123);
        }
        MetricsServer server = new MetricsServer(reg, 0,
                () -> "{\"component\":\"test\",\"status\":\"ok\"}");
        server.start();
        try {
            int port = server.port();
            assertTrue(port > 0);
            String metrics = get(port, "/metrics");
            assertTrue(metrics.contains("# TYPE md_events_total counter"));
            assertTrue(metrics.contains("md_events_total 7"));
            assertTrue(metrics.contains("risk_kill_switch_engaged 0"));
            assertTrue(metrics.contains("book_update_latency_ns_count 1"));
            assertTrue(metrics.contains(
                    "book_update_latency_ns_bucket{le=\"+Inf\"} 1"));
            // every line parses as comment or name/value
            for (String line : metrics.split("\n")) {
                assertTrue(line, line.startsWith("# TYPE ")
                        || line.split(" ").length == 2);
            }
            // live registry: an update is visible on the next scrape
            synchronized (reg) {
                reg.counter("md_events_total").add(3);
            }
            assertTrue(get(port, "/metrics").contains("md_events_total 10"));
            Map<String, Object> health = Json.object(Json.parse(
                    get(port, "/health")));
            assertEquals("ok", health.get("status"));
            Map<String, Object> status = Json.object(Json.parse(
                    get(port, "/status")));
            assertEquals("test", status.get("component"));
        } finally {
            server.stop();
        }
    }
}
