package com.iap.api;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.function.Supplier;

import com.iap.monitoring.MetricsRegistry;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

/**
 * Prometheus/ops HTTP endpoint (spec §25; PLATFORM_CONVENTIONS.md §12.5).
 * Exact-path routing, {@code GET}-only except the admin verbs, with
 * {@code Cache-Control: no-store} on every response:
 * <ul>
 *   <li>{@code GET /metrics} — Prometheus text exposition of the shared
 *       {@link MetricsRegistry};</li>
 *   <li>{@code GET /health} — liveness: 200 while the process can make
 *       progress, 503 (with reasons) when it cannot;</li>
 *   <li>{@code GET /ready} — readiness: 503 while no session is running or
 *       the feed is stale;</li>
 *   <li>{@code GET /status} — component status JSON from the supplier;</li>
 *   <li>{@code POST /admin/{kill,clear,override,roll}} — the runbook's
 *       manual kill-switch path, present only when an {@link AdminHandler}
 *       is installed (otherwise 404).</li>
 * </ul>
 *
 * <p>Concurrency (PLATFORM_CONVENTIONS.md §12.4): the registry is lock-free,
 * so rendering an exposition never blocks the trading thread. Handlers run
 * on a small bounded pool rather than the dispatcher thread, and
 * {@code sun.net.httpserver.maxReqTime}/{@code maxRspTime} bound a client
 * that connects and then stalls.
 */
public final class MetricsServer {
    /** Bounded handler pool: enough for scrape + probes, never unbounded. */
    private static final int HANDLER_THREADS = 4;
    private static final int HANDLER_QUEUE = 32;
    /** Request/response deadlines for a stalled client (seconds). */
    private static final String MAX_REQ_TIME = "20";
    private static final String MAX_RSP_TIME = "20";
    /** Hard cap on an admin request body (form encoded; 8 KiB is ample). */
    private static final int MAX_BODY_BYTES = 8192;

    /** An HTTP status plus a JSON body. */
    public record HttpResult(int code, String json) {
        public HttpResult {
            if (code < 100 || code > 599 || json == null) {
                throw new IllegalArgumentException("bad HttpResult " + code);
            }
        }
    }

    /** A liveness/readiness probe: 200 when healthy, 503 when not. */
    @FunctionalInterface
    public interface Probe {
        /** Evaluate the probe now. */
        HttpResult get();
    }

    /**
     * Handler for {@code POST /admin/<action>}. The server has already
     * checked the method and parsed the parameters; the handler owns
     * authentication (constant-time token comparison), auditing and the
     * mapping onto the risk API, and returns the status to send.
     *
     * @param action one of {@code kill}, {@code clear}, {@code override},
     *               {@code roll}
     * @param token  the presented bearer token, or {@code null}
     * @param params decoded query/form parameters
     */
    @FunctionalInterface
    public interface AdminHandler {
        /** Apply one admin action. */
        HttpResult handle(String action, String token, Map<String, String> params);
    }

    /** The admin actions the server routes (PLATFORM_CONVENTIONS.md §12.5). */
    private static final List<String> ADMIN_ACTIONS =
            List.of("kill", "clear", "override", "roll");

    private final HttpServer server;
    private final MetricsRegistry registry;
    private final Supplier<String> status;
    private final ExecutorService pool;

    /** Bind with default (always-200) probes and no admin API. */
    public MetricsServer(MetricsRegistry registry, int port,
            Supplier<String> statusJson) throws IOException {
        this(registry, port, statusJson,
                () -> new HttpResult(200, "{\"status\":\"ok\"}"),
                () -> new HttpResult(200, "{\"status\":\"ok\"}"), null);
    }

    /**
     * Bind (port 0 = ephemeral) without starting; call {@link #start}.
     *
     * @param statusJson supplier of the /status body (a JSON object)
     * @param health     liveness probe (/health)
     * @param ready      readiness probe (/ready)
     * @param admin      admin handler, or {@code null} to disable /admin/*
     */
    public MetricsServer(MetricsRegistry registry, int port,
            Supplier<String> statusJson, Probe health, Probe ready,
            AdminHandler admin) throws IOException {
        this.registry = registry;
        this.status = statusJson;
        // Bound how long a stalled peer may hold a handler thread. These are
        // read by the JDK http server at construction time.
        System.setProperty("sun.net.httpserver.maxReqTime", MAX_REQ_TIME);
        System.setProperty("sun.net.httpserver.maxRspTime", MAX_RSP_TIME);
        this.server = HttpServer.create(new InetSocketAddress(port), 0);
        ThreadPoolExecutor tp = new ThreadPoolExecutor(HANDLER_THREADS,
                HANDLER_THREADS, 60L, TimeUnit.SECONDS,
                new ArrayBlockingQueue<>(HANDLER_QUEUE), r -> {
                    Thread t = new Thread(r, "iap-http");
                    t.setDaemon(true);
                    return t;
                }, new ThreadPoolExecutor.AbortPolicy());
        this.pool = tp;
        server.setExecutor(tp);

        server.createContext("/metrics", ex -> {
            if (!exactGet(ex, "/metrics")) {
                return;
            }
            respond(ex, 200, "text/plain; version=0.0.4; charset=utf-8",
                    this.registry.toPrometheus());
        });
        server.createContext("/health", ex -> {
            if (!exactGet(ex, "/health")) {
                return;
            }
            HttpResult r = health.get();
            respond(ex, r.code(), "application/json", r.json());
        });
        server.createContext("/ready", ex -> {
            if (!exactGet(ex, "/ready")) {
                return;
            }
            HttpResult r = ready.get();
            respond(ex, r.code(), "application/json", r.json());
        });
        server.createContext("/status", ex -> {
            if (!exactGet(ex, "/status")) {
                return;
            }
            respond(ex, 200, "application/json", this.status.get());
        });
        if (admin != null) {
            for (String action : ADMIN_ACTIONS) {
                String path = "/admin/" + action;
                server.createContext(path, ex -> handleAdmin(ex, path, action, admin));
            }
        }
    }

    private static void handleAdmin(HttpExchange ex, String path, String action,
            AdminHandler admin) throws IOException {
        if (!ex.getRequestURI().getPath().equals(path)) {
            respond(ex, 404, "application/json",
                    "{\"error\":\"not_found\"}");
            return;
        }
        if (!"POST".equals(ex.getRequestMethod())) {
            ex.getResponseHeaders().set("Allow", "POST");
            respond(ex, 405, "application/json",
                    "{\"error\":\"method_not_allowed\"}");
            return;
        }
        Map<String, String> params =
                parseQuery(ex.getRequestURI().getRawQuery());
        byte[] body = ex.getRequestBody().readNBytes(MAX_BODY_BYTES + 1);
        if (body.length > MAX_BODY_BYTES) {
            respond(ex, 413, "application/json",
                    "{\"error\":\"body_too_large\"}");
            return;
        }
        params.putAll(parseQuery(new String(body, StandardCharsets.UTF_8)));
        HttpResult r = admin.handle(action, bearerToken(ex), params);
        respond(ex, r.code(), "application/json", r.json());
    }

    /** The presented admin token, or {@code null} when absent. */
    private static String bearerToken(HttpExchange ex) {
        String auth = ex.getRequestHeaders().getFirst("Authorization");
        if (auth != null) {
            String prefix = "Bearer ";
            if (auth.regionMatches(true, 0, prefix, 0, prefix.length())) {
                return auth.substring(prefix.length()).trim();
            }
            return null;
        }
        String header = ex.getRequestHeaders().getFirst("X-IAP-Admin-Token");
        return header == null ? null : header.trim();
    }

    /**
     * Decode an {@code application/x-www-form-urlencoded} query or body into
     * a sorted map. Malformed pairs are ignored (the handler validates what
     * it needs and rejects with 400).
     */
    static Map<String, String> parseQuery(String raw) {
        TreeMap<String, String> out = new TreeMap<>();
        if (raw == null || raw.isEmpty()) {
            return out;
        }
        for (String pair : raw.split("&")) {
            if (pair.isEmpty()) {
                continue;
            }
            int eq = pair.indexOf('=');
            if (eq < 0) {
                continue;
            }
            String k = URLDecoder.decode(pair.substring(0, eq),
                    StandardCharsets.UTF_8);
            String v = URLDecoder.decode(pair.substring(eq + 1),
                    StandardCharsets.UTF_8);
            if (!k.isEmpty()) {
                out.put(k, v);
            }
        }
        return out;
    }

    /**
     * Enforce exact-path + GET semantics ({@code createContext} matches by
     * prefix, so {@code /healthz} and {@code /health/x} would otherwise hit
     * the {@code /health} handler).
     *
     * @return true when the request may be served
     */
    private static boolean exactGet(HttpExchange ex, String path)
            throws IOException {
        if (!ex.getRequestURI().getPath().equals(path)) {
            respond(ex, 404, "application/json", "{\"error\":\"not_found\"}");
            return false;
        }
        String method = ex.getRequestMethod();
        if (!"GET".equals(method) && !"HEAD".equals(method)) {
            ex.getResponseHeaders().set("Allow", "GET");
            respond(ex, 405, "application/json",
                    "{\"error\":\"method_not_allowed\"}");
            return false;
        }
        return true;
    }

    private static void respond(HttpExchange ex, int code, String contentType,
            String body) throws IOException {
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", contentType);
        ex.getResponseHeaders().set("Cache-Control", "no-store");
        boolean head = "HEAD".equals(ex.getRequestMethod());
        ex.sendResponseHeaders(code, head ? -1 : bytes.length);
        if (!head) {
            try (OutputStream os = ex.getResponseBody()) {
                os.write(bytes);
            }
        } else {
            ex.close();
        }
    }

    /** Start serving (bounded background pool). */
    public void start() {
        server.start();
    }

    /** The actual bound port (useful with port 0). */
    public int port() {
        return server.getAddress().getPort();
    }

    /** Stop the server and its handler pool (immediately). */
    public void stop() {
        server.stop(0);
        pool.shutdownNow();
    }
}
