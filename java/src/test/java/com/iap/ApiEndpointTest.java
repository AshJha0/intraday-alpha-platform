package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

import java.io.IOException;
import java.util.Map;

import org.junit.Test;

import com.iap.api.MetricsServer;
import com.iap.monitoring.MetricsRegistry;
import com.iap.platform.PaperTrading;

/**
 * HTTP endpoint semantics (PLATFORM_CONVENTIONS.md §12.5). Round-3 findings
 * SEV-2 "{@code /status} reports events_processed: 0 for the whole session",
 * SEV-2 "probes that cannot fail" and SEV-3 "path handling"; proposed tests
 * 8 (StatusReportsProgress) and 9 (HealthReflectsStall).
 */
public class ApiEndpointTest {
    /** Exact-path routing, GET-only, no-store — SEV-3 path handling. */
    @Test
    public void routingIsExactPathAndGetOnly() throws IOException {
        MetricsRegistry reg = new MetricsRegistry();
        reg.counter("md_events_total").add(3);
        MetricsServer server = new MetricsServer(reg, 0,
                () -> "{\"component\":\"test\"}");
        server.start();
        try {
            int p = server.port();
            assertEquals(200, PaperFixtures.get(p, "/health")[0]);
            // prefix matches used to serve 200 for these
            assertEquals(404, PaperFixtures.get(p, "/healthz")[0]);
            assertEquals(404, PaperFixtures.get(p, "/health/x")[0]);
            assertEquals(404, PaperFixtures.get(p, "/metricsx")[0]);
            assertEquals(404, PaperFixtures.get(p, "/nope")[0]);
            // POST on a read-only endpoint
            Object[] post = PaperFixtures.request(p, "/metrics", "POST", null,
                    Map.of("a", "b"));
            assertEquals(405, post[0]);
            // Cache-Control: no-store on every response
            Object[] ok = PaperFixtures.request(p, "/metrics", "GET", null, null);
            assertEquals(200, ok[0]);
            assertEquals("no-store", ok[2]);
            // /admin/* is absent when no admin handler is installed
            assertEquals(404, PaperFixtures.request(p, "/admin/kill", "POST",
                    "t", Map.of("scope", "global", "reason", "x"))[0]);
        } finally {
            server.stop();
        }
    }

    /**
     * Test 9 — the probes reflect real conditions: a failed session and a
     * wedged trading loop are 503 on /health; a not-yet-started, finished or
     * (in realtime mode) feed-stalled session is 503 on /ready; and a LATCHED
     * KILL SWITCH is neither — halted is not dead.
     */
    @Test
    public void probesReflectStallHaltAndSessionState() {
        PaperTrading.Options opts = new PaperTrading.Options();
        opts.realtime = true;
        PaperTrading.Result res = new PaperTrading.Result();
        long staleNs = 5_000_000_000L;

        // STARTING: alive but not ready
        assertEquals(200, PaperTrading.health(res).code());
        assertEquals(503, PaperTrading.ready(res, opts, staleNs).code());

        // RUNNING and fresh: both green
        res.state = PaperTrading.SessionState.RUNNING;
        res.eventsProcessed = 100;
        res.eventsPending = 900;
        res.lastEventWallNs = System.nanoTime();
        assertEquals(200, PaperTrading.health(res).code());
        assertEquals(200, PaperTrading.ready(res, opts, staleNs).code());

        // feed stall in realtime mode: NOT ready, still alive
        res.lastEventWallNs = System.nanoTime() - 2 * staleNs;
        MetricsServer.HttpResult notReady = PaperTrading.ready(res, opts, staleNs);
        assertEquals(503, notReady.code());
        assertTrue(notReady.json(), notReady.json().contains("stale_feed"));
        assertEquals(200, PaperTrading.health(res).code());

        // an asap (replay) session is not judged by wall clock
        PaperTrading.Options asap = new PaperTrading.Options();
        assertEquals(200, PaperTrading.ready(res, asap, staleNs).code());

        // wedged loop: no progress for longer than the liveness budget
        res.lastEventWallNs = System.nanoTime() - 2 * PaperTrading.LIVENESS_STALL_NS;
        MetricsServer.HttpResult dead = PaperTrading.health(res);
        assertEquals(503, dead.code());
        assertTrue(dead.json(), dead.json().contains("stalled"));

        // ... but not when there is nothing left to process
        res.eventsPending = 0;
        assertEquals(200, PaperTrading.health(res).code());

        // halted (kill switch latched) is healthy AND ready, and says so
        res.lastEventWallNs = System.nanoTime();
        res.eventsPending = 900;
        res.halted = true;
        MetricsServer.HttpResult halted = PaperTrading.health(res);
        assertEquals(200, halted.code());
        assertTrue(halted.json(), halted.json().contains("\"trading\":\"halted\""));
        assertTrue(PaperTrading.ready(res, asap, staleNs).json()
                .contains("\"trading\":\"halted\""));

        // FINISHED: alive (the process is exiting cleanly) but not ready
        res.state = PaperTrading.SessionState.FINISHED;
        assertEquals(200, PaperTrading.health(res).code());
        assertEquals(503, PaperTrading.ready(res, asap, staleNs).code());

        // FAILED: not alive, and the reason is reported
        res.state = PaperTrading.SessionState.FAILED;
        res.failureReason = "config load failed";
        MetricsServer.HttpResult failed = PaperTrading.health(res);
        assertEquals(503, failed.code());
        assertTrue(failed.json(), failed.json().contains("config load failed"));
    }

    /**
     * Test 8 — /status reports LIVE progress during a session, not zero
     * until the end, and carries the fields the runbooks point operators at.
     */
    @Test
    public void statusReportsProgressDuringTheSession() throws Exception {
        PaperTrading.Options opts = PaperFixtures.session(600);
        opts.realtime = true;
        opts.speed = 1000.0;   // ~1.9s of event time / 1000 => ~2s wall
        opts.port = 0;
        java.util.concurrent.atomic.AtomicInteger port =
                new java.util.concurrent.atomic.AtomicInteger(-1);
        opts.onServerStarted = r -> port.set(r.httpPort);
        PaperTrading.Result[] out = new PaperTrading.Result[1];
        Throwable[] err = new Throwable[1];
        Thread session = new Thread(() -> {
            try {
                out[0] = PaperTrading.run(opts);
            } catch (Throwable t) {
                err[0] = t;
            }
        }, "paper-session");
        session.setDaemon(true);
        session.start();
        Map<String, Object> mid = null;
        long seen = 0;
        try {
            long deadline = System.nanoTime() + 25_000_000_000L;
            while (System.nanoTime() < deadline && session.isAlive()) {
                if (port.get() < 0) {
                    Thread.sleep(10);
                    continue;
                }
                Object[] r = PaperFixtures.get(port.get(), "/status");
                assertEquals(200, r[0]);
                Map<String, Object> doc = PaperFixtures.json(r[1]);
                long n = Json.asLong(doc.get("events_processed"));
                if (n > 0 && n < 600) {
                    seen = n;
                    mid = doc;
                    // probes are green during a healthy paced session
                    assertEquals(200, PaperFixtures.get(port.get(), "/health")[0]);
                    assertEquals(200, PaperFixtures.get(port.get(), "/ready")[0]);
                    break;
                }
                Thread.sleep(5);
            }
        } finally {
            session.join(60_000);
        }
        if (err[0] != null) {
            throw new AssertionError(err[0]);
        }
        assertNotNull("scraped /status mid-session", mid);
        assertTrue("live progress: " + seen, seen > 0 && seen < 600);
        assertEquals("paper_trading", mid.get("component"));
        assertEquals("running", mid.get("status"));
        assertEquals("realtime", mid.get("mode"));
        assertEquals("EQ01", mid.get("alpha_id"));
        assertEquals(Boolean.FALSE, mid.get("kill_switch_engaged"));
        assertTrue("last_event_ts set",
                Json.asLong(mid.get("last_event_ts")) > 0);
        assertEquals(64, String.valueOf(mid.get("config_sha256")).length());
        assertEquals(0L, Json.asLong(mid.get("restarts")));
        assertEquals(600, out[0].eventsProcessed);
        assertEquals(PaperTrading.SessionState.FINISHED, out[0].state);
        // the report's status matches the terminal state (exit 0 + FINISHED)
        assertEquals("finished",
                PaperFixtures.json(out[0].reportJson).get("status"));
    }
}
