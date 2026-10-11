package com.iap.platform;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Locale;
import java.util.Map;
import java.util.TreeMap;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
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
 * <p>Authentication: one or more operator tokens. {@code $IAP_ADMIN_TOKEN} or
 * the first line of {@code $IAP_ADMIN_TOKEN_FILE} (the Kubernetes Secret
 * path) configures the single operator {@value #DEFAULT_OPERATOR};
 * {@code $IAP_ADMIN_TOKENS_FILE} adds named operators, one
 * {@code operator_id:sha256hex} line each (the file holds token HASHES, never
 * tokens). With no operator configured the service is <b>disabled</b> and the
 * routes are not registered at all (404) — an unauthenticated kill endpoint
 * is worse than none. A presented token is hashed and compared in constant
 * time against every operator and is never logged; the audit records the
 * operator id, the remote address and the token's SHA-256.
 *
 * <p>Threading: the risk engine is single-threaded by contract, and §12.4
 * forbids the HTTP path from taking a lock the trading thread can hold. An
 * accepted request therefore enqueues a command and blocks (bounded) until
 * the trading thread applies it in {@link #drain()} — at an event boundary,
 * before every pre-trade check, and while the loop idles on a quiet feed —
 * so the resulting {@code RiskEvent} carries the current <b>event</b> time.
 *
 * <p><b>A kill latches immediately and is never dropped.</b> Accepting a
 * {@code kill} raises {@link #killPending()} before the request waits, and
 * the order path refuses every new order while it is raised. If the trading
 * thread has not applied the kill when the wait expires, the command STAYS
 * queued and the response is {@code 202} ("latched") — it is applied at the
 * next opportunity and a second audit line records that. The non-halting
 * verbs (clear / override / roll) are withdrawn on timeout and answer
 * {@code 503} only when it is certain they will not be applied.
 *
 * <p>Abuse bounds: after {@value #MAX_AUTH_FAILURES} failed authentications
 * within one window (from any source) further failures answer {@code 429}
 * and are not individually audited; rejected requests of any kind write at
 * most {@value #MAX_REJECT_AUDIT_LINES} audit lines per window; one summary
 * line per window records how many were suppressed. A request with a valid
 * token is never rate-limited — a flood of bad tokens must not lock the
 * operators out of the kill switch.
 *
 * <p>Every accepted request, and every rejected one within those bounds,
 * appends one sorted-key JSON line to {@code <state-dir>/admin_audit.jsonl}
 * and increments {@code admin_requests_total{action="..."}}.
 */
public final class AdminService implements MetricsServer.AdminHandler {
    /** Default wait of an admin request for the trading thread. */
    private static final long APPLY_TIMEOUT_MS = 5_000;
    /** Longest accepted reason/approval reference. */
    private static final int MAX_REASON = 256;
    /** Operator id of the single-token configuration. */
    public static final String DEFAULT_OPERATOR = "admin";
    /** Environment variable naming the multi-operator token-hash file. */
    public static final String TOKENS_FILE_ENV = "IAP_ADMIN_TOKENS_FILE";
    /** Failed authentications tolerated per window before 429. */
    public static final int MAX_AUTH_FAILURES = 10;
    /** Rejected requests individually audited per window. */
    public static final int MAX_REJECT_AUDIT_LINES = 100;
    /** Length of one rate-limit / audit-cap window. */
    public static final long WINDOW_NS = 60_000_000_000L;

    /**
     * Called on the trading thread right after a kill was handed to the
     * risk engine, to cancel the session's working child orders.
     */
    @FunctionalInterface
    public interface KillListener {
        /**
         * @return a short note for the admin audit saying what happened to
         *         the resting child orders
         */
        String onKillApplied(Scope scope, String scopeId, long ts);
    }

    private static final int PENDING = 0;
    private static final int CLAIMED = 1;
    private static final int TIMED_OUT = 2;

    private final RiskEngine risk;
    private final SessionStore store;
    private final MetricsRegistry reg;
    /** operator id -> lower-case sha256 hex of the operator's token. */
    private final TreeMap<String, String> operators = new TreeMap<>();
    private final LongSupplier eventTime;
    private final BooleanSupplier running;
    private final ConcurrentLinkedQueue<Command> queue =
            new ConcurrentLinkedQueue<>();
    /** Accepted kills the risk engine has not recorded yet. */
    private final AtomicInteger pendingKills = new AtomicInteger();
    private volatile KillListener killListener;
    private volatile LongSupplier nanoClock = System::nanoTime;
    private volatile long applyTimeoutMs = APPLY_TIMEOUT_MS;

    // Rate-limit / audit-cap window (guarded by this).
    private boolean windowOpen;
    private long windowStartNs;
    private int authFailures;
    private int rejectLines;
    private long suppressed;

    private static final class Command {
        final String action;
        final Scope scope;
        final String scopeId;
        final String reason;
        final double limit;
        final String operator;
        final String tokenSha256;
        final String remote;
        final CountDownLatch done = new CountDownLatch(1);
        final AtomicInteger state = new AtomicInteger(PENDING);
        final AtomicReference<String> error = new AtomicReference<>();
        final AtomicReference<String> note = new AtomicReference<>("");
        volatile boolean sessionEnded;

        Command(String action, Scope scope, String scopeId, String reason,
                double limit, String operator, String tokenSha256,
                String remote) {
            this.action = action;
            this.scope = scope;
            this.scopeId = scopeId;
            this.reason = reason;
            this.limit = limit;
            this.operator = operator;
            this.tokenSha256 = tokenSha256;
            this.remote = remote;
        }

        boolean isKill() {
            return "kill".equals(action);
        }

        String scopeLabel() {
            return scope.name() + (scopeId.isEmpty() ? "" : ":" + scopeId);
        }
    }

    /**
     * Single-operator service.
     *
     * @param token       the configured admin token, or {@code null}/empty to
     *                    disable the API
     * @param eventTime   supplier of the current event time (ns)
     * @param running     true while a session is draining commands
     */
    public AdminService(RiskEngine risk, SessionStore store, MetricsRegistry reg,
            String token, LongSupplier eventTime, BooleanSupplier running) {
        this(risk, store, reg, singleOperator(token), eventTime, running);
    }

    /**
     * Multi-operator service.
     *
     * @param operators operator id to lower-case sha256 hex of that
     *                  operator's token; empty disables the API
     */
    public AdminService(RiskEngine risk, SessionStore store, MetricsRegistry reg,
            Map<String, String> operators, LongSupplier eventTime,
            BooleanSupplier running) {
        this.risk = risk;
        this.store = store;
        this.reg = reg;
        this.eventTime = eventTime;
        this.running = running;
        this.operators.putAll(operators);
    }

    /** The operator table of a single shared token (empty = disabled). */
    public static Map<String, String> singleOperator(String token) {
        TreeMap<String, String> out = new TreeMap<>();
        if (token != null && !token.isEmpty()) {
            out.put(DEFAULT_OPERATOR,
                    Sha256.hex(token.getBytes(StandardCharsets.UTF_8)));
        }
        return out;
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

    /**
     * Every configured operator: the single token of {@link #tokenFromEnv}
     * (as {@value #DEFAULT_OPERATOR}) plus the {@code operator_id:sha256hex}
     * lines of {@code $IAP_ADMIN_TOKENS_FILE} (blank lines and {@code #}
     * comments ignored). A named-but-unreadable file, a malformed line, a
     * duplicate operator id or a file without any operator is a
     * configuration error (fail fast, the variable named).
     */
    public static Map<String, String> operatorsFromEnv(Map<String, String> env) {
        TreeMap<String, String> out = new TreeMap<>(
                singleOperator(tokenFromEnv(env)));
        String file = env.get(TOKENS_FILE_ENV);
        if (file == null || file.isBlank()) {
            return out;
        }
        Path p = Path.of(file.trim());
        java.util.List<String> lines;
        try {
            lines = Files.readAllLines(p, StandardCharsets.UTF_8);
        } catch (IOException e) {
            throw new IllegalArgumentException(
                    TOKENS_FILE_ENV + " " + p + " is not readable", e);
        }
        int entries = 0;
        for (int i = 0; i < lines.size(); i++) {
            String line = lines.get(i).trim();
            if (line.isEmpty() || line.startsWith("#")) {
                continue;
            }
            int colon = line.indexOf(':');
            String id = colon < 0 ? "" : line.substring(0, colon).trim();
            String hex = colon < 0 ? ""
                    : line.substring(colon + 1).trim().toLowerCase(Locale.ROOT);
            if (!id.matches("[A-Za-z0-9._-]{1,64}")
                    || !hex.matches("[0-9a-f]{64}")) {
                throw new IllegalArgumentException(TOKENS_FILE_ENV + " " + p
                        + " line " + (i + 1)
                        + ": expected operator_id:sha256hex");
            }
            if (out.put(id, hex) != null) {
                throw new IllegalArgumentException(TOKENS_FILE_ENV + " " + p
                        + " line " + (i + 1) + ": duplicate operator id " + id);
            }
            entries++;
        }
        if (entries == 0) {
            throw new IllegalArgumentException(
                    TOKENS_FILE_ENV + " " + p + " names no operator");
        }
        return out;
    }

    /** True when an operator is configured and the routes should be served. */
    public boolean enabled() {
        return !operators.isEmpty();
    }

    /**
     * True from the moment a kill is accepted until the trading thread has
     * handed it to the risk engine. The order path consults this before
     * every order: while it is raised, nothing new is sent.
     */
    public boolean killPending() {
        return pendingKills.get() > 0;
    }

    /** Install the callback that cancels working child orders on a kill. */
    public AdminService onKill(KillListener listener) {
        this.killListener = listener;
        return this;
    }

    /** Replace the monotonic clock of the rate-limit window (tests). */
    public AdminService withClock(LongSupplier nanoClock) {
        this.nanoClock = nanoClock;
        return this;
    }

    /** Replace how long a request waits for the trading thread (tests). */
    public AdminService withApplyTimeoutMs(long timeoutMs) {
        this.applyTimeoutMs = timeoutMs;
        return this;
    }

    /** Constant-time comparison of two hex digests (no early exit). */
    private static boolean sameDigest(String a, String b) {
        int diff = a.length() ^ b.length();
        for (int i = 0; i < Math.max(a.length(), b.length()); i++) {
            char x = i < a.length() ? a.charAt(i) : 0;
            char y = i < b.length() ? b.charAt(i) : 0;
            diff |= x ^ y;
        }
        return diff == 0;
    }

    /** The operator whose token hashes to {@code tokenSha256}, or null. */
    private String operatorOf(String tokenSha256) {
        String match = null;
        for (Map.Entry<String, String> e : operators.entrySet()) {
            if (sameDigest(e.getValue(), tokenSha256)) {
                match = e.getKey();
            }
        }
        return match;
    }

    @Override
    public MetricsServer.HttpResult handle(String action, String presented,
            Map<String, String> params) {
        return handle(action, presented, params, "");
    }

    @Override
    public MetricsServer.HttpResult handle(String action, String presented,
            Map<String, String> params, String remoteAddr) {
        reg.counter(MetricsRegistry.labeled("admin_requests_total", "action",
                action)).inc();
        String remote = remoteAddr == null ? "" : remoteAddr;
        if (!enabled()) {
            return rejected(action, 404, "", "", "admin API disabled", "", "",
                    remote);
        }
        if (presented == null || presented.isEmpty()) {
            return authFailure(action, 401, "no token presented", remote);
        }
        String sha = Sha256.hex(presented.getBytes(StandardCharsets.UTF_8));
        String operator = operatorOf(sha);
        if (operator == null) {
            return authFailure(action, 403, "token mismatch", remote);
        }
        String reason = params.getOrDefault("reason", "").trim();
        if (reason.isEmpty() || reason.length() > MAX_REASON) {
            return rejected(action, 400, "", reason,
                    "reason is required (1.." + MAX_REASON + " chars)",
                    operator, sha, remote);
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
            return rejected(action, 400, String.valueOf(params.get("scope")),
                    reason, e.getMessage(), operator, sha, remote);
        }
        if (!running.getAsBoolean()) {
            return rejected(action, 503, scope.name(), reason,
                    "no session is running: risk state is not mutable",
                    operator, sha, remote);
        }
        Command cmd = new Command(action, scope, scopeId, reason, limit,
                operator, sha, remote);
        if (cmd.isKill()) {
            // Latch BEFORE waiting: from here on the order path sends nothing.
            pendingKills.incrementAndGet();
        }
        queue.add(cmd);
        long timeoutMs = applyTimeoutMs;
        boolean applied;
        try {
            applied = cmd.done.await(timeoutMs, TimeUnit.MILLISECONDS);
            if (!applied && !cmd.state.compareAndSet(PENDING, TIMED_OUT)) {
                // The trading thread claimed it as the wait expired: it is
                // being applied right now, so report what actually happens.
                applied = cmd.done.await(timeoutMs, TimeUnit.MILLISECONDS);
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return unapplied(cmd, "interrupted while waiting for the trading"
                    + " thread");
        }
        if (!applied) {
            return unapplied(cmd, "trading thread did not apply the command in "
                    + timeoutMs + "ms");
        }
        String err = cmd.error.get();
        if (err != null) {
            return rejected(action, cmd.sessionEnded ? 503 : 400, scope.name(),
                    reason, err, operator, sha, remote);
        }
        return accepted(cmd, 200, "applied" + cmd.note.get());
    }

    /**
     * The response for a command the trading thread has not applied within
     * the wait. A kill stays queued and latched (202) unless the session is
     * gone; any other verb is withdrawn, and answers 503 only if the
     * withdrawal succeeded — otherwise it was applied after all (202).
     */
    private MetricsServer.HttpResult unapplied(Command cmd, String why) {
        cmd.state.compareAndSet(PENDING, TIMED_OUT);
        boolean timedOut = cmd.state.get() == TIMED_OUT;
        String withdrawn = why + "; it was withdrawn and will not be applied";
        if (!cmd.isKill()) {
            if (timedOut) {
                // drain() skips a timed-out non-kill command, so whether or
                // not it is still queued it is never applied.
                queue.remove(cmd);
                return rejected(cmd.action, 503, cmd.scope.name(), cmd.reason,
                        withdrawn, cmd.operator, cmd.tokenSha256, cmd.remote);
            }
            return accepted(cmd, 202, why + "; it is being applied now");
        }
        if (timedOut && !running.getAsBoolean() && queue.remove(cmd)) {
            pendingKills.decrementAndGet();
            return rejected(cmd.action, 503, cmd.scope.name(), cmd.reason,
                    withdrawn + " (the session is no longer running)",
                    cmd.operator, cmd.tokenSha256, cmd.remote);
        }
        if (cmd.sessionEnded) {
            return rejected(cmd.action, 503, cmd.scope.name(), cmd.reason,
                    String.valueOf(cmd.error.get()), cmd.operator,
                    cmd.tokenSha256, cmd.remote);
        }
        return accepted(cmd, 202, "kill latched: no new order is sent from"
                + " now on; the risk engine records it at the next event"
                + " boundary and a second audit line confirms it (" + why + ")");
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
     * at the current event time. A kill is applied even when its request
     * already returned 202 (a second audit line records the late apply); a
     * withdrawn non-kill command is skipped. Returns the number applied.
     */
    public int drain() {
        int n = 0;
        Command cmd;
        while ((cmd = queue.poll()) != null) {
            boolean kill = cmd.isKill();
            boolean claimed = cmd.state.compareAndSet(PENDING, CLAIMED);
            if (!claimed && !kill) {
                cmd.done.countDown();
                continue;
            }
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
            if (kill) {
                cmd.note.set("; " + cancelWorkingOrders(cmd, ts));
                pendingKills.decrementAndGet();
            }
            cmd.done.countDown();
            if (!claimed) {
                String err = cmd.error.get();
                accepted(cmd, 200, "applied after the request returned 202"
                        + (err == null ? "" : " (risk engine: " + err + ")")
                        + cmd.note.get());
            }
            n++;
        }
        return n;
    }

    /** Run the kill listener; never throws (the kill itself already holds). */
    private String cancelWorkingOrders(Command cmd, long ts) {
        KillListener l = killListener;
        if (l == null) {
            return "resting child orders were NOT cancelled (no cancel path"
                    + " is wired); new orders are stopped";
        }
        try {
            return l.onKillApplied(cmd.scope, cmd.scopeId, ts);
        } catch (RuntimeException e) {
            return "resting child orders were NOT cancelled ("
                    + e.getMessage() + "); new orders are stopped";
        }
    }

    /** Fail every queued command (session ended without draining). */
    public void shutdown() {
        Command cmd;
        while ((cmd = queue.poll()) != null) {
            cmd.sessionEnded = true;
            cmd.error.set("session ended before the command was applied");
            if (cmd.isKill()) {
                pendingKills.decrementAndGet();
                if (cmd.state.get() == TIMED_OUT) {
                    // its request already answered 202: close the audit trail
                    accepted(cmd, 503, "session ended before the latched kill"
                            + " reached the risk engine");
                }
            }
            cmd.done.countDown();
        }
        synchronized (this) {
            flushSummary();
        }
    }

    // ------------------------------------------------------------- audit --

    /** Open a new window when the current one has expired (holds this). */
    private void rollWindow() {
        long now = nanoClock.getAsLong();
        if (!windowOpen || now - windowStartNs >= WINDOW_NS) {
            flushSummary();
            windowOpen = true;
            windowStartNs = now;
            authFailures = 0;
            rejectLines = 0;
        }
    }

    /** The single per-window summary of what was not audited (holds this). */
    private void flushSummary() {
        if (suppressed > 0) {
            writeLine("audit_summary", 429, "", "", suppressed
                    + " rejected admin requests were not individually audited"
                    + " in the last window (failed authentications answer 429"
                    + " after " + MAX_AUTH_FAILURES + ", rejected requests are"
                    + " audited up to " + MAX_REJECT_AUDIT_LINES
                    + " per window)", "", "", "");
            suppressed = 0;
        }
    }

    private void suppress() {
        suppressed++;
        reg.counter("admin_audit_suppressed_total").inc();
    }

    /** A failed authentication: audited and answered until the limit, then 429. */
    private synchronized MetricsServer.HttpResult authFailure(String action,
            int code, String message, String remote) {
        rollWindow();
        authFailures++;
        if (authFailures > MAX_AUTH_FAILURES) {
            suppress();
            reg.counter("admin_auth_rate_limited_total").inc();
            return response(action, 429,
                    "too many failed authentications; retry later");
        }
        return rejectedInWindow(action, code, "", "", message, "", "", remote);
    }

    /** A rejected request: answered always, audited within the window cap. */
    private synchronized MetricsServer.HttpResult rejected(String action,
            int code, String scope, String reason, String message,
            String operator, String tokenSha256, String remote) {
        rollWindow();
        return rejectedInWindow(action, code, scope, reason, message, operator,
                tokenSha256, remote);
    }

    private MetricsServer.HttpResult rejectedInWindow(String action, int code,
            String scope, String reason, String message, String operator,
            String tokenSha256, String remote) {
        if (rejectLines >= MAX_REJECT_AUDIT_LINES) {
            suppress();
        } else {
            rejectLines++;
            writeLine(action, code, scope, reason, message, operator,
                    tokenSha256, remote);
        }
        return response(action, code, message);
    }

    /** An accepted command (200 applied / 202 latched): always audited. */
    private synchronized MetricsServer.HttpResult accepted(Command cmd,
            int code, String message) {
        writeLine(cmd.action, code, cmd.scopeLabel(), cmd.reason, message,
                cmd.operator, cmd.tokenSha256, cmd.remote);
        return response(cmd.action, code, message);
    }

    private static MetricsServer.HttpResult response(String action, int code,
            String message) {
        return new MetricsServer.HttpResult(code,
                "{\"action\":\"" + esc(action) + "\",\"message\":\""
                        + esc(message) + "\",\"ok\":"
                        + (code == 200 || code == 202) + "}");
    }

    private static String clip(String s, int max) {
        return s.length() <= max ? s : s.substring(0, max);
    }

    /**
     * Append one audit line (holds this). The token itself is never
     * written: {@code operator} names the actor, {@code actor_token_sha256}
     * the credential used and {@code remote} where the request came from.
     * Free-text fields are clipped so one request cannot write an unbounded
     * line.
     */
    private void writeLine(String action, int code, String scope,
            String reason, String message, String operator,
            String tokenSha256, String remote) {
        String line = "{\"action\":\"" + esc(clip(action, 32))
                + "\",\"actor_token_sha256\":\"" + esc(tokenSha256)
                + "\",\"code\":" + code
                + ",\"message\":\"" + esc(clip(message, 4 * MAX_REASON))
                + "\",\"operator\":\"" + esc(operator)
                + "\",\"reason\":\"" + esc(clip(reason, MAX_REASON))
                + "\",\"remote\":\"" + esc(clip(remote, 64))
                + "\",\"scope\":\"" + esc(clip(scope, 96))
                + "\",\"ts_wallclock_ns\":"
                + (System.currentTimeMillis() * 1_000_000L) + "}\n";
        store.appendJsonl(SessionStore.ADMIN_AUDIT, line);
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
