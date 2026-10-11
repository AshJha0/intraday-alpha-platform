package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.Proxy;
import java.net.Socket;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;

import org.junit.Test;

import com.iap.api.MetricsServer;
import com.iap.monitoring.Counter;
import com.iap.monitoring.Histogram;
import com.iap.monitoring.MetricsRegistry;

/**
 * PLATFORM_CONVENTIONS.md §12.4 — monitoring is lock-free and never on the
 * trading hot path's lock. Round-3 finding SEV-2 "observability holds the
 * trading hot path's lock; a slow scraper stalls trading" and proposed test
 * 12 (MetricsScrapeDoesNotBlockTrading).
 */
public class MetricsConcurrencyTest {
    /**
     * Test 12 — a client that connects, sends a request and then stalls
     * without reading must not stop the "trading" thread from recording
     * metrics. Under the previous {@code synchronized (reg)} design the
     * writer blocked behind the exposition render.
     */
    @Test
    public void scrapeDoesNotBlockTrading() throws Exception {
        MetricsRegistry reg = new MetricsRegistry();
        // a large exposition so a stalled reader cannot be absorbed by the
        // socket buffer alone
        for (int i = 0; i < 400; i++) {
            reg.counter(String.format("filler_%03d_total", i)).add(i);
        }
        Counter events = reg.counter("md_events_total");
        Histogram book = reg.histogram("book_update_latency_ns");
        MetricsServer server = new MetricsServer(reg, 0,
                () -> "{\"status\":\"ok\"}");
        server.start();
        AtomicBoolean stop = new AtomicBoolean();
        AtomicLong processed = new AtomicLong();
        Thread trading = new Thread(() -> {
            while (!stop.get()) {
                events.inc();
                book.record(processed.incrementAndGet() & 0xFFFF);
            }
        }, "fake-trading");
        trading.setDaemon(true);
        try {
            int port = server.port();
            trading.start();
            // Four stalled clients: send a request, never read the body.
            Socket[] stalled = new Socket[4];
            for (int i = 0; i < stalled.length; i++) {
                stalled[i] = new Socket("127.0.0.1", port);
                stalled[i].getOutputStream().write(
                        ("GET /metrics HTTP/1.1\r\nHost: localhost\r\n\r\n")
                                .getBytes(StandardCharsets.UTF_8));
                stalled[i].getOutputStream().flush();
            }
            long before = processed.get();
            Thread.sleep(500);
            long after = processed.get();
            for (Socket s : stalled) {
                s.close();
            }
            assertTrue("trading advanced while scrapers stalled: "
                    + (after - before), after - before > 1000);
            // and a normal scrape still succeeds afterwards
            assertTrue(get(port, "/metrics").contains("md_events_total"));
        } finally {
            stop.set(true);
            trading.join(2000);
            server.stop();
        }
    }

    /**
     * A histogram rendered while it is being written stays internally
     * consistent: {@code +Inf} equals {@code _count} and the buckets are
     * cumulative, because both are derived from one read of the array.
     */
    @Test
    public void expositionIsConsistentWhileRecording() throws Exception {
        MetricsRegistry reg = new MetricsRegistry();
        Histogram h = reg.histogram("order_path_latency_ns");
        AtomicBoolean stop = new AtomicBoolean();
        Thread writer = new Thread(() -> {
            long i = 0;
            while (!stop.get()) {
                h.record((i++ * 7919) & 0xFFFFF);
            }
        }, "hist-writer");
        writer.setDaemon(true);
        writer.start();
        try {
            for (int round = 0; round < 200; round++) {
                String text = reg.toPrometheus();
                long inf = -1;
                long count = -2;
                long prev = -1;
                for (String line : text.split("\n")) {
                    if (line.startsWith("order_path_latency_ns_bucket{le=\"+Inf\"} ")) {
                        inf = Long.parseLong(line.substring(
                                line.lastIndexOf(' ') + 1));
                    } else if (line.startsWith("order_path_latency_ns_bucket{")) {
                        long v = Long.parseLong(line.substring(
                                line.lastIndexOf(' ') + 1));
                        assertTrue("cumulative", v >= prev);
                        prev = v;
                    } else if (line.startsWith("order_path_latency_ns_count ")) {
                        count = Long.parseLong(line.substring(
                                line.lastIndexOf(' ') + 1));
                    }
                }
                assertEquals("+Inf == _count in every scrape", inf, count);
            }
        } finally {
            stop.set(true);
            writer.join(2000);
        }
    }

    /** Concurrent counter/histogram writers lose no update (atomics). */
    @Test
    public void concurrentWritersLoseNoUpdates() throws Exception {
        MetricsRegistry reg = new MetricsRegistry();
        Counter c = reg.counter("md_events_total");
        Histogram h = reg.histogram("decode_latency_ns");
        int threads = 4;
        int perThread = 20_000;
        Thread[] ts = new Thread[threads];
        for (int t = 0; t < threads; t++) {
            ts[t] = new Thread(() -> {
                for (int i = 0; i < perThread; i++) {
                    c.inc();
                    h.record(i & 1023);
                }
            });
            ts[t].start();
        }
        for (Thread t : ts) {
            t.join();
        }
        assertEquals((long) threads * perThread, c.get());
        assertEquals((long) threads * perThread, h.count());
    }

    private static String get(int port, String path) throws IOException {
        HttpURLConnection conn = (HttpURLConnection) URI.create(
                "http://127.0.0.1:" + port + path).toURL()
                .openConnection(Proxy.NO_PROXY);
        conn.setConnectTimeout(5000);
        conn.setReadTimeout(10000);
        try (InputStream in = conn.getInputStream()) {
            return new String(in.readAllBytes(), StandardCharsets.UTF_8);
        }
    }
}
