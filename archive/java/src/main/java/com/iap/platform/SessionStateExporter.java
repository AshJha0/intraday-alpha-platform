package com.iap.platform;

import java.io.IOException;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

import com.iap.api.MetricsServer;
import com.iap.monitoring.MetricsRegistry;

/**
 * Serves the LAST PERSISTED session state as Prometheus metrics, from a
 * process that outlives the trading JVM (PLATFORM_CONVENTIONS.md §12.3).
 *
 * <p>The paper-trading process sets {@code platform_session_state} to 4
 * (STOPPED) and exits right after the checkpoint, so a scraper almost never
 * sees the value. The trading thread therefore also writes
 * {@link SessionStore#SESSION_EXIT} when a session starts and ends; this
 * exporter (a sidecar container in Kubernetes, a second service in compose,
 * the same image with a different entrypoint) reads that file and serves:
 * <ul>
 *   <li>{@value #STATE} — the {@code platform_session_state} code the last
 *       session left behind ({@code 1} RUNNING, {@code 2} FINISHED,
 *       {@code 3} FAILED, {@code 4} STOPPED); {@value #NO_MARKER} when no
 *       session has run here, {@value #UNREADABLE} when the file is
 *       malformed;</li>
 *   <li>{@value #UNIXTIME} — when the marker was written;</li>
 *   <li>{@value #CURSOR} — events processed at that time.</li>
 * </ul>
 * It never writes, never touches the trading state, and holds no lock the
 * trading thread could wait on, so it cannot weaken the shutdown guarantees.
 * A crash leaves {@code RUNNING} (1) in the file, so it is distinguishable
 * from a clean stop (4).
 */
public final class SessionStateExporter {
    /** Persisted terminal state gauge. */
    public static final String STATE = "platform_persisted_session_state";
    /** Wall-clock time of the persisted marker. */
    public static final String UNIXTIME = "platform_persisted_session_unixtime";
    /** Events processed when the marker was written. */
    public static final String CURSOR = "platform_persisted_session_event_cursor";
    /** {@link #STATE} value when no marker exists. */
    public static final long NO_MARKER = -1;
    /** {@link #STATE} value when the marker cannot be parsed. */
    public static final long UNREADABLE = -2;
    /** Default listener port (the trading JVM is on 8080). */
    public static final int DEFAULT_PORT = 9102;
    /** Seconds between re-reads of the marker. */
    private static final long REFRESH_SECONDS = 5;

    private SessionStateExporter() {
    }

    /** Re-read the marker in {@code dir} into {@code reg}. */
    public static void refresh(Path dir, MetricsRegistry reg) {
        long state;
        long time = 0;
        long cursor = 0;
        try {
            SessionStore.SessionExit m = SessionStore.readSessionExit(dir);
            if (m == null) {
                state = NO_MARKER;
            } else {
                state = m.code();
                time = m.unixSeconds();
                cursor = m.eventCursor();
            }
        } catch (IllegalStateException e) {
            state = UNREADABLE;
        }
        reg.gauge(STATE).set(state);
        reg.gauge(UNIXTIME).set(time);
        reg.gauge(CURSOR).set(cursor);
    }

    /**
     * Entry point: {@code --state-dir DIR} (else {@code $IAP_STATE_DIR}) and
     * {@code --port N} (default {@value #DEFAULT_PORT}); binds
     * {@code $IAP_BIND_ADDR} like the trading server.
     */
    public static void main(String[] args) throws IOException {
        Path dir = null;
        int port = DEFAULT_PORT;
        for (int i = 0; i < args.length; i++) {
            if (i + 1 >= args.length) {
                throw new IllegalArgumentException(args[i] + " needs a value");
            }
            switch (args[i]) {
                case "--state-dir" -> dir = Paths.get(args[++i]);
                case "--port" -> port = Integer.parseInt(args[++i]);
                default -> throw new IllegalArgumentException(
                        "unknown argument " + args[i]);
            }
        }
        if (dir == null) {
            String env = System.getenv("IAP_STATE_DIR");
            dir = Paths.get(env == null || env.isBlank() ? "/data/state"
                    : env.trim());
        }
        final Path stateDir = dir;
        MetricsRegistry reg = new MetricsRegistry();
        refresh(stateDir, reg);
        ScheduledExecutorService timer =
                Executors.newSingleThreadScheduledExecutor(r -> {
                    Thread t = new Thread(r, "iap-state-exporter");
                    t.setDaemon(false);
                    return t;
                });
        timer.scheduleWithFixedDelay(() -> refresh(stateDir, reg),
                REFRESH_SECONDS, REFRESH_SECONDS, TimeUnit.SECONDS);
        MetricsServer server = new MetricsServer(reg, port,
                () -> "{\"exporter\":\"session_state\",\"state_dir\":\""
                        + stateDir.toString().replace("\\", "\\\\")
                                .replace("\"", "\\\"") + "\"}");
        server.start();
        System.out.println("session state exporter: dir=" + stateDir
                + " port=" + server.port());
    }
}
