package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.concurrent.atomic.AtomicReference;

import org.junit.Test;

import com.iap.monitoring.MetricsRegistry;
import com.iap.platform.PaperTrading;
import com.iap.platform.SessionStateExporter;
import com.iap.platform.SessionStore;

/**
 * The persisted session marker and the exporter that serves it: a clean stop
 * is observable after the trading process is gone, a resume and a finished
 * session replace it, and a crash is never mistaken for a stop.
 */
public class SessionMarkerTest {
    private static double gauge(MetricsRegistry reg, String name) {
        Double v = reg.gaugeValue(name);
        assertNotNull(name, v);
        return v;
    }

    private static PaperTrading.Result stopMidStream(PaperTrading.Options opts)
            throws Exception {
        opts.realtime = true;
        opts.speed = 700.0;
        opts.checkpointEveryEvents = 0;
        AtomicReference<PaperTrading.Result> live = new AtomicReference<>();
        opts.onServerStarted = live::set;
        PaperTrading.Result[] out = new PaperTrading.Result[1];
        Throwable[] err = new Throwable[1];
        Thread session = new Thread(() -> {
            try {
                out[0] = PaperTrading.run(opts);
            } catch (Throwable t) {
                err[0] = t;
            }
        }, "paper-marker-stop");
        session.setDaemon(true);
        session.start();
        long deadline = System.nanoTime() + 60_000_000_000L;
        while (System.nanoTime() < deadline && session.isAlive()) {
            PaperTrading.Result r = live.get();
            if (r != null && r.eventsProcessed >= 900) {
                r.requestStop();
                break;
            }
            Thread.sleep(1);
        }
        session.join(60_000);
        if (err[0] != null) {
            throw new AssertionError(err[0]);
        }
        return out[0];
    }

    @Test
    public void stopThenResumeThenFinishIsVisibleInThePersistedMarker()
            throws Exception {
        PaperTrading.Options opts = PaperFixtures.session(2000);
        PaperTrading.Result res = stopMidStream(opts);
        assertEquals(PaperTrading.SessionState.STOPPED, res.state);

        // The trading run has returned; the marker is what outlives it.
        SessionStore.SessionExit m = SessionStore.readSessionExit(res.stateDir);
        assertNotNull(m);
        assertEquals("STOPPED", m.state());
        assertEquals(4L, m.code());
        assertEquals(res.eventsProcessed, m.eventCursor());
        assertTrue(m.unixSeconds() > 1_600_000_000L);
        MetricsRegistry reg = new MetricsRegistry();
        SessionStateExporter.refresh(res.stateDir, reg);
        assertEquals(4.0, gauge(reg, SessionStateExporter.STATE), 0.0);
        assertEquals((double) m.unixSeconds(),
                gauge(reg, SessionStateExporter.UNIXTIME), 0.0);
        assertEquals((double) res.eventsProcessed,
                gauge(reg, SessionStateExporter.CURSOR), 0.0);
        assertTrue(reg.toPrometheus()
                .contains(SessionStateExporter.STATE + " 4"));

        // The checkpoint the marker describes is still the committed one.
        assertEquals(res.eventsProcessed,
                new SessionStore(res.stateDir).readState().eventCursor);

        // A resume replaces the marker with the finished one.
        PaperTrading.Options leg2 = PaperFixtures.session(2000);
        leg2.stateDir = res.stateDir;
        leg2.resume = true;
        PaperTrading.Result r2 = PaperTrading.run(leg2);
        assertEquals(PaperTrading.SessionState.FINISHED, r2.state);
        SessionStateExporter.refresh(res.stateDir, reg);
        assertEquals(2.0, gauge(reg, SessionStateExporter.STATE), 0.0);
        assertEquals("FINISHED",
                SessionStore.readSessionExit(res.stateDir).state());
    }

    @Test
    public void aFinishedSessionLeavesFinishedAndNeverStopped()
            throws Exception {
        PaperTrading.Options opts = PaperFixtures.session(300);
        PaperTrading.Result res = PaperTrading.run(opts);
        assertEquals(PaperTrading.SessionState.FINISHED, res.state);
        MetricsRegistry reg = new MetricsRegistry();
        SessionStateExporter.refresh(res.stateDir, reg);
        assertEquals(2.0, gauge(reg, SessionStateExporter.STATE), 0.0);
        assertEquals(300.0, gauge(reg, SessionStateExporter.CURSOR), 0.0);
    }

    @Test
    public void noMarkerAndAMalformedMarkerAreReportedDistinctly()
            throws IOException {
        Path dir = Files.createTempDirectory("iap-marker");
        assertNull(SessionStore.readSessionExit(dir));
        MetricsRegistry reg = new MetricsRegistry();
        SessionStateExporter.refresh(dir, reg);
        assertEquals(-1.0, gauge(reg, SessionStateExporter.STATE), 0.0);

        Files.write(dir.resolve(SessionStore.SESSION_EXIT),
                "{not json".getBytes(StandardCharsets.UTF_8));
        SessionStateExporter.refresh(dir, reg);
        assertEquals(-2.0, gauge(reg, SessionStateExporter.STATE), 0.0);

        // a crash leaves RUNNING: not 4, so it is not mistaken for a stop
        new SessionStore(dir).writeSessionExit("RUNNING", 1, 17, 1_700_000_000L);
        SessionStateExporter.refresh(dir, reg);
        assertEquals(1.0, gauge(reg, SessionStateExporter.STATE), 0.0);
        assertEquals(17.0, gauge(reg, SessionStateExporter.CURSOR), 0.0);
    }
}
