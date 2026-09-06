package com.iap.platform;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.BooleanSupplier;
import java.util.function.LongSupplier;

import com.iap.api.MetricsServer;
import com.iap.codec.Sha256;
import com.iap.monitoring.MetricsRegistry;
import com.iap.risk.RiskEngine;
import com.iap.risk.Scope;

/**
 * The runtime kill-switch admin API (PLATFORM_CONVENTIONS.md §12.5,
 * {@code RUNBOOK_incident_kill_switch.md} §2) — the path that makes the
 * runbook's manual ENGAGE/CLEAR/OVERRIDE/ROLL procedure real on a running
 * Java platform, instead of "edit a baked-in config and hope".
 *
 * <p>Authentication: a token from {@code $IAP_ADMIN_TOKEN} or the first line
 * of {@code $IAP_ADMIN_TOKEN_FILE} (the Kubernetes Secret path). With no
 * token configured the service is <b>disabled</b> and the routes are not
 * registered at all (404) — an unauthenticated kill endpoint is worse than
 * none. Presented tokens are compared in constant time and never logged;
 * the audit records the token's SHA-256 so an actor is attributable without
 * the secret leaving the operator.
 *
 * <p>Threading: the risk engine is single-threaded by contract, and §12.4
 * forbids the HTTP path from taking a lock the trading thread can hold. An
 * accepted request therefore enqueues a command and blocks (bounded) until
 * the trading thread applies it in {@link #drain()} at the next event
 * boundary — so the resulting {@code RiskEvent} carries the current
 * <b>event</b> time and sorts into the audit log exactly where a
 * programmatic call would. If no session is draining, the request fails
 * {@code 503} rather than mutating risk state from a foreign thread.
 *
 * <p>Every request — accepted, refused or rejected — appends one sorted-key
 * JSON line to {@code <state-dir>/admin_audit.jsonl} and increments
 * {@code admin_requests_total{action="..."}}.
 */
public final class AdminService implements MetricsServer.AdminHandler {
    /** How long an admin request waits for the trading thread. */
    private static final long APPLY_TIMEOUT_MS = 5_000;
    /** Longest accepted reason/approval reference. */
    private static final int MAX_REASON = 256;

    private final RiskEngine risk;
    private final SessionStore store;
    private final MetricsRegistry reg;
    private final byte[] token;
    private final String tokenSha256;
    private final LongSupplier eventTime;
    private final BooleanSupplier running;
    private final ConcurrentLinkedQueue<Command> queue =
            new ConcurrentLinkedQueue<>();

    private static final class Command {
        final String action;
        final Scope scope;
        final String scopeId;
        final String reason;
        final double limit;
        final CountDownLatch done = new CountDownLatch(1);
        final AtomicReference<String> error = new AtomicReference<>();

        Command(String action, Scope scope, String scopeId, String reason,
                double limit) {
            this.action = action;
            this.scope = scope;
            this.scopeId = scopeId;
            this.reason = reason;
            this.limit = limit;
        }
    }

    /**
     * @param token       the configured admin token, or {@code null}/empty to
     *                    disable the API
     * @param eventTime   supplier of the current event time (ns)
     * @param running     true while a session is draining commands
     */
    public AdminService(RiskEngine risk, SessionStore store, MetricsRegistry reg,
            String token, LongSupplier eventTime, BooleanSupplier running) {
        this.risk = risk;
        this.store = store;
        this.reg = reg;
        this.eventTime = eventTime;
        this.running = running;
        if (token == null || token.isEmpty()) {
            this.token = null;
            this.tokenSha256 = "";
        } else {
            this.token = token.getBytes(StandardCharsets.UTF_8);
            this.tokenSha256 = Sha256.hex(this.token);
        }
    }

    /**
     * The configured admin token: {@code $IAP_ADMIN_TOKEN}, else the trimmed
     * first line of {@code $IAP_ADMIN_TOKEN_FILE}, else {@code null}.
     * An unreadable/empty token FILE is a configuration error (fail fast):
     * naming a secret that is not there must not silently disable the halt
     * path.
     */
    public static String tokenFromEnv(Map<String, String> env) {
        String direct = env.get("IAP_ADMIN_TOKEN");
        if (direct != null && !direct.isBlank()) {
            return direct.trim();
        }
        String file = env.get("IAP_ADMIN_TOKEN_FILE");
        if (file == null || file.isBlank()) {
            return null;
        }
        Path p = Path.of(file.trim());
        String first;
        try {
            first = Files.readAllLines(p, StandardCharsets.UTF_8).stream()
                    .findFirst().orElse("").trim();
        } catch (IOException e) {
            throw new IllegalArgumentException(
                    "IAP_ADMIN_TOKEN_FILE " + p + " is not readable", e);
        }
        if (first.isEmpty()) {
            throw new IllegalArgumentException(
                    "IAP_ADMIN_TOKEN_FILE " + p + " is empty");
        }
        return first;
    }

    /** True when a token is configured and the routes should be served. */
    public boolean enabled() {
        return token != null;
    }

    /** Constant-time token comparison (no early exit on the first mismatch). */
    private boolean tokenMatches(String presented) {
        if (presented == null || token == null) {
            return false;
        }
        byte[] p = presented.getBytes(StandardCharsets.UTF_8);
        int diff = p.length ^ token.length;
        for (int i = 0; i < Math.max(p.length, token.length); i++) {
            byte a = i < p.length ? p[i] : 0;
            byte b = i < token.length ? token[i] : 0;
            diff |= a ^ b;
        }
        return diff == 0;
    }

    @Override
    public MetricsServer.HttpResult handle(String action, String presented,
            Map<String, String> params) {
        reg.counter(MetricsRegistry.labeled("admin_requests_total", "action",
                action)).inc();
        if (!enabled()) {
            return audit(action, 404, "", "", "admin API disabled");
        }
        if (presented == null || presented.isEmpty()) {
            return audit(action, 401, "", "", "no token presented");
        }
        if (!tokenMatches(presented)) {
            return audit(action, 403, "", "", "token mismatch");
        }
        String reason = params.getOrDefault("reason", "").trim();
        if (reason.isEmpty() || reason.length() > MAX_REASON) {
            return audit(action, 400, "", reason,
                    "reason is required (1.." + MAX_REASON + " chars)");
        }
        Scope scope;
        String scopeId;
        double limit = Double.NaN;
        try {
            if ("roll".equals(action)) {
                scope = Scope.GLOBAL;
                scopeId = "";
            } else {
                scope = parseScope(params.get("scope"));
                scopeId = scopeId(scope, params.get("id"));
            }
            if ("override".equals(action)) {
                limit = parseLimit(params.get("limit"));
            }
        } catch (IllegalArgumentException e) {
            return audit(action, 400, String.valueOf(params.get("scope")),
                    reason, e.getMessage());
        }
        if (!running.getAsBoolean()) {
            return audit(action, 503, scope.name(), reason,
                    "no session is running: risk state is not mutable");
        }
        Command cmd = new Command(action, scope, scopeId, reason, limit);
        queue.add(cmd);
        boolean applied;
        try {
            applied = cmd.done.await(APPLY_TIMEOUT_MS, TimeUnit.MILLISECONDS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return audit(action, 503, scope.name(), reason, "interrupted");
        }
        if (!applied) {
            queue.remove(cmd);
            return audit(action, 503, scope.name(), reason,
                    "trading thread did not apply the command in "
                            + APPLY_TIMEOUT_MS + "ms");
        }
        String err = cmd.error.get();
        if (err != null) {
            return audit(action, 400, scope.name(), reason, err);
        }
        return audit(action, 200, scope.name() + (scopeId.isEmpty() ? ""
                : ":" + scopeId), reason, "applied");
    }

    private static Scope parseScope(String s) {
        if (s == null || s.isEmpty()) {
            throw new IllegalArgumentException(
                    "scope is required (global|strategy|instrument|venue)");
        }
        return switch (s.toLowerCase(java.util.Locale.ROOT)) {
            case "global" -> Scope.GLOBAL;
            case "strategy" -> Scope.STRATEGY;
            case "instrument" -> Scope.INSTRUMENT;
            case "venue" -> Scope.VENUE;
            default -> throw new IllegalArgumentException(
                    "unknown scope " + s
                            + " (global|strategy|instrument|venue)");
        };
    }

    private static String scopeId(Scope scope, String id) {
        if (scope == Scope.GLOBAL) {
            return "";
        }
        if (id == null || id.isEmpty()) {
            throw new IllegalArgumentException(
                    "id is required for scope " + scope.name().toLowerCase(
                            java.util.Locale.ROOT));
        }
        if (scope == Scope.INSTRUMENT || scope == Scope.VENUE) {
            try {
                Long.parseLong(id);
            } catch (NumberFormatException e) {
                throw new IllegalArgumentException(
                        "id must be a decimal id for scope " + scope + ": " + id);
            }
        }
        return id;
    }

    private static double parseLimit(String s) {
        if (s == null || s.isEmpty()) {
            throw new IllegalArgumentException("limit is required for override");
        }
        double v;
        try {
            v = Double.parseDouble(s);
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException("limit is not a number: " + s);
        }
        if (!(Double.isFinite(v) && v > 0.0)) {
            throw new IllegalArgumentException(
                    "limit must be finite and > 0: " + s);
        }
        return v;
    }

    /**
     * Apply every queued command on the CALLING thread (the trading thread),
     * at the current event time. Returns the number applied.
     */
    public int drain() {
        int n = 0;
        Command cmd;
        while ((cmd = queue.poll()) != null) {
            long ts = eventTime.getAsLong();
            try {
                switch (cmd.action) {
                    case "kill" -> risk.engageKill(cmd.scope, cmd.scopeId, ts,
                            cmd.reason);
                    case "clear" -> risk.clearKill(cmd.scope, cmd.scopeId, ts,
                            cmd.reason);
                    case "override" -> risk.overrideLossLimit(cmd.scope,
                            cmd.scopeId, cmd.limit, ts, cmd.reason);
                    case "roll" -> risk.rollSession(ts, cmd.reason);
                    default -> throw new IllegalArgumentException(
                            "unknown admin action " + cmd.action);
                }
            } catch (RuntimeException e) {
                cmd.error.set(String.valueOf(e.getMessage()));
            }
            cmd.done.countDown();
            n++;
        }
        return n;
    }

    /** Fail every queued command (session ended without draining). */
    public void shutdown() {
        Command cmd;
        while ((cmd = queue.poll()) != null) {
            cmd.error.set("session ended before the command was applied");
            cmd.done.countDown();
        }
    }

    /**
     * Append the audit line and build the response. The token itself is
     * never written; {@code actor_token_sha256} identifies the actor.
     */
    private synchronized MetricsServer.HttpResult audit(String action, int code,
            String scope, String reason, String message) {
        String line = "{\"action\":\"" + esc(action)
                + "\",\"actor_token_sha256\":\"" + tokenSha256
                + "\",\"code\":" + code
                + ",\"message\":\"" + esc(message)
                + "\",\"reason\":\"" + esc(reason)
                + "\",\"scope\":\"" + esc(scope)
                + "\",\"ts_wallclock_ns\":"
                + (System.currentTimeMillis() * 1_000_000L) + "}\n";
        store.appendJsonl(SessionStore.ADMIN_AUDIT, line);
        return new MetricsServer.HttpResult(code,
                "{\"action\":\"" + esc(action) + "\",\"message\":\""
                        + esc(message) + "\",\"ok\":" + (code == 200) + "}");
    }

    private static String esc(String s) {
        StringBuilder sb = new StringBuilder(s.length() + 8);
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"' -> sb.append("\\\"");
                case '\\' -> sb.append("\\\\");
                case '\n' -> sb.append("\\n");
                case '\r' -> sb.append("\\r");
                case '\t' -> sb.append("\\t");
                default -> {
                    if (c < 0x20) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
                }
            }
        }
        return sb.toString();
    }
}
