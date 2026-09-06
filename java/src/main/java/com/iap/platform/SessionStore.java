package com.iap.platform;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.channels.FileChannel;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import java.util.List;
import java.util.Map;

import com.iap.config.Json;

/**
 * Durable platform state for the paper-trading vertical
 * (PLATFORM_CONVENTIONS.md §12.3). The store owns four files under one
 * state directory:
 *
 * <ul>
 *   <li>{@code risk_snapshot.json} — {@code RiskEngine.snapshot()}: positions,
 *       lots, open orders, kill latches, loss overrides, throttles;</li>
 *   <li>{@code session_state.json} — the platform's own accounting: the event
 *       cursor, realized/gross P&amp;L, equity peak, order-id sequence and the
 *       restart count;</li>
 *   <li>{@code risk_audit.jsonl} — every {@code RiskEvent} of the session,
 *       appended and flushed at every checkpoint and at shutdown;</li>
 *   <li>{@code config_audit.jsonl} — {@code ConfigService.auditJsonl()}, written
 *       once at startup.</li>
 * </ul>
 *
 * <p>Both JSON documents are written atomically: content to
 * {@code <name>.tmp}, {@code force(true)} (fsync), then an
 * {@code ATOMIC_MOVE} rename onto the target, so a crash mid-write leaves the
 * previous checkpoint intact rather than a truncated file. Appends to the
 * JSONL logs are flushed and fsynced at the same points.
 *
 * <p>Reads are strict and fail closed: a malformed or unreadable file under
 * {@code --resume} raises {@link IllegalStateException} naming the file — the
 * platform must never silently start flat and un-latched.
 */
public final class SessionStore {
    /** Schema version of {@code session_state.json}. */
    public static final long STATE_VERSION = 1;

    /** The platform's own recoverable accounting (schema {@code x-version 1}). */
    public static final class State {
        /** Number of events of the configured stream already processed. */
        public long eventCursor;
        /** Instrument the session trades (guards a mismatched resume). */
        public long instrumentId;
        /** Alpha id of the session (guards a mismatched resume). */
        public String alphaId = "";
        /** SHA-256 over the loaded config hashes (guards a config change). */
        public String configSha256 = "";
        /** Reporting-currency P&amp;L booked so far. */
        public double totalPnl;
        /** Mark-to-market price-move P&amp;L booked so far. */
        public double grossPnl;
        /** Peak equity seen, for the drawdown gauge. */
        public double equityPeak;
        /** Risk-side order-id sequence (never reused across a restart). */
        public long riskOrderSeq;
        /** Fills booked so far. */
        public long fillCount;
        /** Child orders submitted so far. */
        public long ordersSubmitted;
        /** Number of {@code --resume} restarts of this session. */
        public long restarts;
        /** Number of audit lines already persisted (append cursor). */
        public long auditLines;

        /** Sorted-key JSON document (round-trips exactly). */
        public String toJson() {
            StringBuilder sb = new StringBuilder(320);
            sb.append("{\"alpha_id\":\"").append(esc(alphaId))
                    .append("\",\"audit_lines\":").append(auditLines)
                    .append(",\"config_sha256\":\"").append(esc(configSha256))
                    .append("\",\"equity_peak\":").append(num(equityPeak))
                    .append(",\"event_cursor\":").append(eventCursor)
                    .append(",\"fill_count\":").append(fillCount)
                    .append(",\"gross_pnl\":").append(num(grossPnl))
                    .append(",\"instrument_id\":").append(instrumentId)
                    .append(",\"orders_submitted\":").append(ordersSubmitted)
                    .append(",\"restarts\":").append(restarts)
                    .append(",\"risk_order_seq\":").append(riskOrderSeq)
                    .append(",\"total_pnl\":").append(num(totalPnl))
                    .append(",\"x-version\":").append(STATE_VERSION)
                    .append('}');
            return sb.toString();
        }

        private static String num(double v) {
            if (!Double.isFinite(v)) {
                throw new IllegalStateException(
                        "non-finite value in session state: " + v);
            }
            return Double.toString(v);
        }

        private static String esc(String s) {
            return s.replace("\\", "\\\\").replace("\"", "\\\"");
        }

        /** Parse a document written by {@link #toJson} (strict). */
        public static State fromJson(Map<String, Object> doc, Path where) {
            State s = new State();
            if (asLong(doc, "x-version", where) != STATE_VERSION) {
                throw bad(where, "x-version");
            }
            s.eventCursor = asLong(doc, "event_cursor", where);
            s.instrumentId = asLong(doc, "instrument_id", where);
            s.alphaId = asStr(doc, "alpha_id", where);
            s.configSha256 = asStr(doc, "config_sha256", where);
            s.totalPnl = asNum(doc, "total_pnl", where);
            s.grossPnl = asNum(doc, "gross_pnl", where);
            s.equityPeak = asNum(doc, "equity_peak", where);
            s.riskOrderSeq = asLong(doc, "risk_order_seq", where);
            s.fillCount = asLong(doc, "fill_count", where);
            s.ordersSubmitted = asLong(doc, "orders_submitted", where);
            s.restarts = asLong(doc, "restarts", where);
            s.auditLines = asLong(doc, "audit_lines", where);
            if (s.eventCursor < 0 || s.riskOrderSeq < 0 || s.restarts < 0
                    || s.auditLines < 0) {
                throw bad(where, "negative counter");
            }
            return s;
        }
    }

    private static IllegalStateException bad(Path where, String what) {
        return new IllegalStateException(
                "corrupt platform state " + where + ": " + what);
    }

    private static long asLong(Map<String, Object> doc, String key, Path where) {
        Object v = doc.get(key);
        if (!(v instanceof Long)) {
            throw bad(where, "missing/non-integer " + key);
        }
        return (Long) v;
    }

    private static double asNum(Map<String, Object> doc, String key, Path where) {
        Object v = doc.get(key);
        if (!(v instanceof Long) && !(v instanceof Double)) {
            throw bad(where, "missing/non-numeric " + key);
        }
        double d = Json.asDouble(v);
        if (!Double.isFinite(d)) {
            throw bad(where, "non-finite " + key);
        }
        return d;
    }

    private static String asStr(Map<String, Object> doc, String key, Path where) {
        Object v = doc.get(key);
        if (!(v instanceof String)) {
            throw bad(where, "missing/non-string " + key);
        }
        return (String) v;
    }

    /** File names, relative to the state directory. */
    public static final String RISK_SNAPSHOT = "risk_snapshot.json";
    /** Platform accounting document. */
    public static final String SESSION_STATE = "session_state.json";
    /** Per-session risk audit log. */
    public static final String RISK_AUDIT = "risk_audit.jsonl";
    /** Config load/change audit log. */
    public static final String CONFIG_AUDIT = "config_audit.jsonl";
    /** Admin (kill-switch API) audit log. */
    public static final String ADMIN_AUDIT = "admin_audit.jsonl";

    private final Path dir;

    /** Open (creating) a state directory. */
    public SessionStore(Path dir) {
        this.dir = dir;
        try {
            Files.createDirectories(dir);
        } catch (IOException e) {
            throw new UncheckedIOException("cannot create state dir " + dir, e);
        }
    }

    /** The state directory. */
    public Path dir() {
        return dir;
    }

    /** Absolute path of one state file. */
    public Path file(String name) {
        return dir.resolve(name);
    }

    /** True when a previous session left a resumable checkpoint here. */
    public boolean hasCheckpoint() {
        return Files.isReadable(file(SESSION_STATE))
                && Files.isReadable(file(RISK_SNAPSHOT));
    }

    /** Write {@code text} to {@code name} atomically (temp + fsync + rename). */
    public void writeAtomic(String name, String text) {
        Path target = file(name);
        Path tmp = file(name + ".tmp");
        byte[] bytes = text.getBytes(StandardCharsets.UTF_8);
        try {
            try (FileChannel ch = FileChannel.open(tmp,
                    StandardOpenOption.CREATE, StandardOpenOption.WRITE,
                    StandardOpenOption.TRUNCATE_EXISTING)) {
                ch.write(java.nio.ByteBuffer.wrap(bytes));
                ch.force(true);
            }
            try {
                Files.move(tmp, target, StandardCopyOption.ATOMIC_MOVE,
                        StandardCopyOption.REPLACE_EXISTING);
            } catch (java.nio.file.AtomicMoveNotSupportedException e) {
                Files.move(tmp, target, StandardCopyOption.REPLACE_EXISTING);
            }
        } catch (IOException e) {
            throw new UncheckedIOException("cannot write state " + target, e);
        }
    }

    /**
     * Append complete lines to a JSONL file and fsync. {@code lines} must
     * already be newline-terminated (or empty, in which case nothing is
     * written and the file is still created).
     */
    public void appendJsonl(String name, String lines) {
        Path target = file(name);
        try {
            try (FileChannel ch = FileChannel.open(target,
                    StandardOpenOption.CREATE, StandardOpenOption.WRITE,
                    StandardOpenOption.APPEND)) {
                if (!lines.isEmpty()) {
                    ch.write(java.nio.ByteBuffer.wrap(
                            lines.getBytes(StandardCharsets.UTF_8)));
                    ch.force(true);
                }
            }
        } catch (IOException e) {
            throw new UncheckedIOException("cannot append state " + target, e);
        }
    }

    /** Truncate (or create) a JSONL file — used when a session starts fresh. */
    public void truncate(String name) {
        try {
            Files.write(file(name), new byte[0]);
        } catch (IOException e) {
            throw new UncheckedIOException(
                    "cannot truncate state " + file(name), e);
        }
    }

    /** Read a state file as text (fails closed with the path named). */
    public String read(String name) {
        Path p = file(name);
        try {
            return new String(Files.readAllBytes(p), StandardCharsets.UTF_8);
        } catch (IOException e) {
            throw new IllegalStateException(
                    "cannot read platform state " + p, e);
        }
    }

    /** Read + parse {@code session_state.json} (strict). */
    public State readState() {
        Path p = file(SESSION_STATE);
        Object parsed;
        try {
            parsed = Json.parse(read(SESSION_STATE));
        } catch (RuntimeException e) {
            throw new IllegalStateException(
                    "corrupt platform state " + p + ": malformed JSON", e);
        }
        if (!(parsed instanceof Map)) {
            throw bad(p, "top level must be an object");
        }
        return State.fromJson(Json.object(parsed), p);
    }

    /** Read + parse {@code risk_snapshot.json} (strict). */
    public Map<String, Object> readRiskSnapshot() {
        Path p = file(RISK_SNAPSHOT);
        Object parsed;
        try {
            parsed = Json.parse(read(RISK_SNAPSHOT));
        } catch (RuntimeException e) {
            throw new IllegalStateException(
                    "corrupt risk snapshot " + p + ": malformed JSON", e);
        }
        if (!(parsed instanceof Map)) {
            throw bad(p, "top level must be an object");
        }
        return Json.object(parsed);
    }

    /** Lines currently in a JSONL file (empty list when absent). */
    public List<String> readLines(String name) {
        Path p = file(name);
        if (!Files.exists(p)) {
            return List.of();
        }
        try {
            return Files.readAllLines(p, StandardCharsets.UTF_8).stream()
                    .filter(s -> !s.isEmpty()).toList();
        } catch (IOException e) {
            throw new IllegalStateException("cannot read " + p, e);
        }
    }
}
