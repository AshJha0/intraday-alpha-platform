package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import java.nio.file.Paths;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

import org.junit.Test;

import com.iap.risk.Decision;
import com.iap.risk.OrderRequest;
import com.iap.risk.RiskDecision;
import com.iap.risk.RiskEngine;
import com.iap.risk.RiskEvent;
import com.iap.risk.RiskFill;
import com.iap.risk.Scope;

/**
 * Golden replay of tests/golden/expected_risk_decisions.json (the Rust
 * engine is the reference; the Java engine must reproduce decision +
 * rule_id + severity for every order step exactly, emit the pinned
 * KILL/notification events in order, and replay the audit log
 * byte-identically).
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

    /** Build the engine exactly as the reference harness does. */
    static RiskEngine buildEngine(Map<String, Object> golden) {
        Map<String, Object> config = Json.object(com.iap.config.Json.parseFile(
                Paths.get("..").resolve((String) golden.get("config"))));
        TreeMap<Long, Double> ticks = new TreeMap<>();
        for (Map.Entry<String, Object> e
                : Json.object(golden.get("instruments")).entrySet()) {
            ticks.put(Long.parseLong(e.getKey()),
                    Json.asDouble(Json.object(e.getValue()).get("tick_size")));
        }
        return RiskEngine.fromConfig(config, ticks);
    }

    /** Replay the golden step script; assert per-order expectations when
     *  {@code check}. Returns the audit JSONL. */
    static String replay(Map<String, Object> golden, boolean check) {
        RiskEngine eng = buildEngine(golden);
        List<Object> steps = Json.array(golden.get("steps"));
        for (int i = 0; i < steps.size(); i++) {
            Map<String, Object> step = Json.object(steps.get(i));
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
        return eng.auditJsonl();
    }

    private static long countOrders(Map<String, Object> golden) {
        return Json.array(golden.get("steps")).stream()
                .filter(s -> "order".equals(Json.object(s).get("type")))
                .count();
    }

    @Test
    public void goldenDecisionsMatchExactly() {
        Map<String, Object> golden = Golden.json("expected_risk_decisions.json");
        assertTrue("golden vector must pin at least 25 orders",
                countOrders(golden) >= 25);
        replay(golden, true);
    }

    @Test
    public void goldenKillEventsInOrder() {
        Map<String, Object> golden = Golden.json("expected_risk_decisions.json");
        RiskEngine eng = buildEngine(golden);
        // drive without assertions, then inspect the audit events
        replayInto(golden, eng);
        List<RiskEvent> notif = eng.audit().stream()
                .filter(e -> switch (e.ruleId()) {
                    case "VENUE_DISCONNECT", "VENUE_RECONNECT",
                            "KILL_SWITCH_ENGAGED", "KILL_SWITCH_CLEARED",
                            "STRATEGY_LOSS", "DAILY_LOSS" -> true;
                    default -> false;
                })
                .filter(e -> !e.reason().isEmpty())
                .filter(e -> e.decision() != Decision.REJECT.code())
                .toList();
        List<Object> expected = Json.array(golden.get("expected_kill_events"));
        assertEquals("kill/notification event count", expected.size(),
                notif.size());
        for (int i = 0; i < expected.size(); i++) {
            Map<String, Object> want = Json.object(expected.get(i));
            RiskEvent got = notif.get(i);
            assertEquals(want.get("rule_id"), got.ruleId());
            assertEquals(Scope.parse((String) want.get("scope")), got.scope());
            assertEquals(want.get("scope_id"), got.scopeId());
            assertEquals(Json.asLong(want.get("decision")), got.decision());
        }
    }

    private static void replayInto(Map<String, Object> golden, RiskEngine eng) {
        // Reuse replay() for the driving logic by replaying on a fresh
        // engine — audit equality below only needs the JSONL, but this
        // variant hands back the engine for event inspection.
        List<Object> steps = Json.array(golden.get("steps"));
        for (Object o : steps) {
            Map<String, Object> step = Json.object(o);
            switch ((String) step.get("type")) {
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
                case "order" ->
                        eng.checkOrder(parseOrder(Json.object(step.get("order"))));
                default -> throw new IllegalStateException("unknown step");
            }
        }
    }

    @Test
    public void auditLogReplaysByteIdentically() {
        Map<String, Object> golden = Golden.json("expected_risk_decisions.json");
        String a = replay(golden, false);
        String b = replay(golden, false);
        assertEquals(a, b);
        long nOrders = countOrders(golden);
        long nNotif = Json.array(golden.get("expected_kill_events")).size();
        assertEquals(nOrders + nNotif, a.lines().count());
        // every line is a schema-shaped sorted-key JSON object
        a.lines().forEach(line -> {
            Map<String, Object> ev = Json.object(Json.parse(line));
            assertEquals(List.of("decision", "reason", "rule_id", "scope",
                    "scope_id", "severity", "timestamp"),
                    List.copyOf(ev.keySet()));
            long sev = Json.asLong(ev.get("severity"));
            long dec = Json.asLong(ev.get("decision"));
            assertTrue(sev >= 1 && sev <= 3);
            assertTrue(dec >= 1 && dec <= 3);
        });
    }
}
