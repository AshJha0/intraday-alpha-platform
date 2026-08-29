package com.iap.api;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.function.Supplier;

import com.iap.monitoring.MetricsRegistry;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

/**
 * Prometheus/ops HTTP endpoint (spec §25; deployment/grafana/README.md
 * "Java monitoring wave"). Serves:
 * <ul>
 *   <li>{@code GET /metrics} — Prometheus text exposition of the shared
 *       {@link MetricsRegistry};</li>
 *   <li>{@code GET /health} — liveness JSON {@code {"status":"ok"}};</li>
 *   <li>{@code GET /status} — component status JSON from the supplier.</li>
 * </ul>
 * The registry is single-threaded by contract, so every render
 * synchronizes on the registry object; writers that share it with this
 * server must do the same.
 */
public final class MetricsServer {
    private final HttpServer server;
    private final MetricsRegistry registry;
    private final Supplier<String> status;

    /**
     * Bind (port 0 = ephemeral) without starting; call {@link #start}.
     *
     * @param statusJson supplier of the /status body (a JSON object)
     */
    public MetricsServer(MetricsRegistry registry, int port,
            Supplier<String> statusJson) throws IOException {
        this.registry = registry;
        this.status = statusJson;
        this.server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/metrics", ex -> {
            String body;
            synchronized (this.registry) {
                body = this.registry.toPrometheus();
            }
            respond(ex, 200, "text/plain; version=0.0.4; charset=utf-8", body);
        });
        server.createContext("/health", ex ->
                respond(ex, 200, "application/json", "{\"status\":\"ok\"}"));
        server.createContext("/status", ex ->
                respond(ex, 200, "application/json", this.status.get()));
    }

    private static void respond(HttpExchange ex, int code, String contentType,
            String body) throws IOException {
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", contentType);
        ex.sendResponseHeaders(code, bytes.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(bytes);
        }
    }

    /** Start serving (background dispatcher thread). */
    public void start() {
        server.start();
    }

    /** The actual bound port (useful with port 0). */
    public int port() {
        return server.getAddress().getPort();
    }

    /** Stop the server (immediately). */
    public void stop() {
        server.stop(0);
    }
}
