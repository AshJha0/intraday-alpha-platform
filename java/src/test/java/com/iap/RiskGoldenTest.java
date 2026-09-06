package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

import org.junit.Test;

import com.iap.risk.Decision;
import com.iap.risk.InstrumentRef;
import com.iap.risk.OrderRequest;
import com.iap.risk.RiskDecision;
import com.iap.risk.RiskEngine;
import com.iap.risk.RiskEvent;
import com.iap.risk.RiskFill;
import com.iap.risk.RiskLimits;
import com.iap.risk.Scope;

/**
 * Golden replay of tests/golden/expected_risk_decisions.json (the Rust
 * engine is the reference; the Java engine must reproduce decision +
 * rule_id + severity for every order step exactly, emit the pinned
 * notification events in order, reproduce expected_risk_audit.jsonl BYTE
 * FOR BYTE — the cross-language audit-parity golden, including the
 * decimal-tie formatting cases — and restore expected_risk_snapshot.json
 * into an engine that produces the identical audit tail).
 */
public class RiskGoldenTest {
    static OrderRequest parseOrder(Map<String, Object> v) {
        return new OrderRequest(
                Json.asLong(v.get("order_id")),
                Json.asLong(v.get("instrument_id")),
                (int) Json.asLong(v.get("side")),
                Json.asLong(v.get("qty")),
                Json.asLong(v.get("price_ticks")),
                (int) Json.asLong(v.get("order_type")),
                (int) Json.asLong(v.get("venue_id")),
                (String) v.get("strategy_id"),
                Json.asDouble(v.get("urgency")),
                Json.asLong(v.get("timestamp")));
    }

    static TreeMap<Long, InstrumentRef> instruments(Map<String, Object> golden) {
        TreeMap<Long, InstrumentRef> out = new TreeMap<>();
        for (Map.Entry<String, Object> e
                : Json.object(golden.get("instruments")).entrySet()) {
            Map<String, Object> spec = Json.object(e.getValue());
            out.put(Long.parseLong(e.getKey()), new InstrumentRef(
                    Json.asDouble(spec.get("tick_size")),
                    Json.asDouble(spec.get("qty_unit")),
                    (String) spec.get("quote_ccy")));
        }
        return out;
    }

    static Map<String, Object> config(Map<String, Object> golden) {
        return Json.object(com.iap.config.Json.parseFile(
                Paths.get("..").resolve((String) golden.get("config"))));
    }

    /** Build the engine exactly as the reference harness does. */
    static RiskEngine buildEngine(Map<String, Object> golden) {
        return RiskEngine.fromConfig(config(golden), instruments(golden));
    }

    /** Apply one step; assert an order step's expectation when {@code check}. */
    static void apply(RiskEngine eng, int i, Map<String, Object> step,
            boolean check) {
        String type = (String) step.get("type");
        switch (type) {
            case "market" -> eng.onMarket(
                    Json.asLong(step.get("instrument_id")),
                    Json.asLong(step.get("bid_ticks")),
                    Json.asLong(step.get("ask_ticks")),
                    Json.asLong(step.get("ts")));
            case "fill" -> eng.onFill(new RiskFill(
                    Json.asLong(step.get("ts")),
                    (String) step.get("strategy_id"),
                    Json.asLong(step.get("instrument_id")),
                    Json.asLong(step.get("order_id")),
                    (int) Json.asLong(step.get("side")),
                    Json.asLong(step.get("qty")),
                    Json.asLong(step.get("price_ticks"))));
            case "cancel" -> eng.onOrderDone(Json.asLong(step.get("order_id")));
            case "gap" -> eng.onSequenceGap(
                    Json.asLong(step.get("instrument_id")),
                    Json.asLong(step.get("ts")));
            case "recover" -> eng.onFeedRecovered(
                    Json.asLong(step.get("instrument_id")),
                    Json.asLong(step.get("ts")));
            case "venue_down" -> eng.onVenueDisconnect(
                    (int) Json.asLong(step.get("venue_id")),
                    Json.asLong(step.get("ts")));
            case "venue_up" -> eng.onVenueReconnect(
                    (int) Json.asLong(step.get("venue_id")),
                    Json.asLong(step.get("ts")));
            case "kill" -> eng.engageKill(
                    Scope.parse((String) step.get("scope")),
                    (String) step.get("scope_id"),
                    Json.asLong(step.get("ts")),
                    (String) step.getOrDefault("reason", ""));
            case "unkill" -> eng.clearKill(
                    Scope.parse((String) step.get("scope")),
                    (String) step.get("scope_id"),
                    Json.asLong(step.get("ts")),
                    (String) step.getOrDefault("reason", ""));
            case "override_loss" -> eng.overrideLossLimit(
                    Scope.parse((String) step.get("scope")),
                    (String) step.get("scope_id"),
                    Json.asDouble(step.get("new_limit")),
                    Json.asLong(step.get("ts")),
                    (String) step.getOrDefault("approver", ""));
            case "roll_session" -> eng.rollSession(Json.asLong(step.get("ts")),
                    (String) step.getOrDefault("reason", ""));
            case "order" -> {
                OrderRequest order = parseOrder(Json.object(step.get("order")));
                RiskDecision d = eng.checkOrder(order);
                if (check) {
                    Map<String, Object> exp = Json.object(step.get("expect"));
                    assertEquals("step " + i + " order " + order.orderId()
                            + ": decision (" + d.ruleId() + " / "
                            + d.reason() + ")",
                            Json.asLong(exp.get("decision")),
                            d.decision().code());
                    assertEquals("step " + i + " order " + order.orderId()
                            + ": rule (" + d.reason() + ")",
                            exp.get("rule_id"), d.ruleId());
                    assertEquals("step " + i + " order " + order.orderId()
                            + ": severity",
                            Json.asLong(exp.get("severity")),
                            d.severity().code());
                }
            }
            default -> throw new IllegalStateException(
                    "unknown step type " + type);
        }
    }

    /** Replay the golden step script into {@code eng}. */
    static void replayInto(Map<String, Object> golden, RiskEngine eng,
            boolean check) {
        List<Object> steps = Json.array(golden.get("steps"));
        for (int i = 0; i < steps.size(); i++) {
            apply(eng, i, Json.object(steps.get(i)), check);
        }
    }

    static String replay(Map<String, Object> golden, boolean check) {
        RiskEngine eng = buildEngine(golden);
        replayInto(golden, eng, check);
        return eng.auditJsonl();
    }

    private static long countOrders(Map<String, Object> golden) {
        return Json.array(golden.get("steps")).stream()
                .filter(s -> "order".equals(Json.object(s).get("type")))
                .count();
    }

    static boolean isNotification(String ruleId, int decision) {
        return switch (ruleId) {
            case "VENUE_DISCONNECT", "VENUE_RECONNECT", "KILL_SWITCH_ENGAGED",
                    "KILL_SWITCH_CLEARED", "LOSS_LIMIT_OVERRIDE",
                    "SESSION_ROLLED", "BOOTSTRAP_COMPLETE", "STATE_RESTORED",
                    "MALFORMED_FILL" -> true;
            case "STRATEGY_LOSS", "DAILY_LOSS" -> decision == Decision.KILL.code();
            default -> false;
        };
    }

    @Test
    public void goldenDecisionsMatchExactly() {
        Map<String, Object> golden = Golden.json("expected_risk_decisions.json");
        assertEquals(3L, Json.asLong(golden.get("x-version")));
        assertTrue("golden vector must pin at least 55 orders",
                countOrders(golden) >= 55);
        replay(golden, true);
    }

    @Test
    public void goldenNotificationEventsInOrder() {
        Map<String, Object> golden = Golden.json("expected_risk_decisions.json");
        RiskEngine eng = buildEngine(golden);
        replayInto(golden, eng, false);
        List<RiskEvent> notif = eng.audit().stream()
                .filter(e -> isNotification(e.ruleId(), e.decision()))
                .toList();
        List<Object> expected = Json.array(golden.get("expected_notification_events"));
        assertEquals("notification event count", expected.size(), notif.size());
        for (int i = 0; i < expected.size(); i++) {
            Map<String, Object> want = Json.object(expected.get(i));
            RiskEvent got = notif.get(i);
            assertEquals(want.get("rule_id"), got.ruleId());
            assertEquals(Scope.parse((String) want.get("scope")), got.scope());
            assertEquals(want.get("scope_id"), got.scopeId());
            assertEquals(Json.asLong(want.get("decision")), got.decision());
        }
    }

    @Test
    public void auditLogMatchesGoldenByteForByte() throws IOException {
        Map<String, Object> golden = Golden.json("expected_risk_decisions.json");
        String a = replay(golden, false);
        String b = replay(golden, false);
        assertEquals("audit must be deterministic", a, b);
        String want = new String(Files.readAllBytes(Paths.get("..", "tests",
                "golden", "expected_risk_audit.jsonl")), StandardCharsets.UTF_8);
        assertEquals("audit JSONL must be byte-identical to the Rust golden",
                want, a);
        long nOrders = countOrders(golden);
        long nNotif = Json.array(golden.get("expected_notification_events")).size();
        assertEquals(nOrders + nNotif, a.lines().count());
        a.lines().forEach(line -> {
            Map<String, Object> ev = Json.object(Json.parse(line));
            assertEquals(List.of("decision", "reason", "rule_id", "scope",
                    "scope_id", "severity", "timestamp"),
                    List.copyOf(ev.keySet()));
        });
    }

    @Test
    public void fixedFormatGoldenCasesIncludingDecimalTies() {
        Map<String, Object> golden = Golden.json("expected_risk_decisions.json");
        List<Object> cases = Json.array(golden.get("fixed_format_cases"));
        assertTrue(cases.size() >= 10);
        for (Object c : cases) {
            List<Object> row = Json.array(c);
            double v = Json.asDouble(row.get(0));
            int d = (int) Json.asLong(row.get(1));
            assertEquals("fmtFixed(" + v + ", " + d + ")", row.get(2),
                    RiskEngine.fmtFixed(v, d));
        }
    }

    @Test
    public void snapshotRestoreReproducesTheAuditTail() {
        Map<String, Object> golden = Golden.json("expected_risk_decisions.json");
        List<Object> steps = Json.array(golden.get("steps"));
        int k = (int) Json.asLong(golden.get("snapshot_after_step"));
        // 1. the engine's own snapshot after step k equals the Rust golden
        RiskEngine eng = buildEngine(golden);
        for (int i = 0; i <= k; i++) {
            apply(eng, i, Json.object(steps.get(i)), true);
        }
        Map<String, Object> want = Golden.json("expected_risk_snapshot.json");
        Map<String, Object> mine = Json.object(Json.parse(eng.snapshot()));
        assertEquals(want, mine);
        // 2. restoring the golden snapshot and replaying the rest reproduces
        //    the unbroken run's audit tail exactly
        RiskLimits limits = RiskLimits.fromJson(config(golden));
        RiskEngine restored = RiskEngine.restore(limits, instruments(golden), want, 0);
        assertEquals(1, restored.audit().size());
        assertEquals("STATE_RESTORED", restored.audit().get(0).ruleId());
        for (int i = k + 1; i < steps.size(); i++) {
            apply(restored, i, Json.object(steps.get(i)), true);
            apply(eng, i, Json.object(steps.get(i)), true);
        }
        List<String> full = eng.auditJsonl().lines().toList();
        List<String> tail = restored.auditJsonl().lines().skip(1).toList();
        assertEquals(full.subList(full.size() - tail.size(), full.size()), tail);
        assertEquals(Json.parse(eng.snapshot()), Json.parse(restored.snapshot()));
    }
}
