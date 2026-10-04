package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeSet;

import org.junit.Test;

import com.iap.config.ConfigService;
import com.iap.contracts.CanonicalJson;
import com.iap.lifecycle.AlphaLifecycle;
import com.iap.lifecycle.AlphaRecord;
import com.iap.lifecycle.AlphaRegistry;
import com.iap.lifecycle.Evidence;
import com.iap.lifecycle.GateEvaluation;
import com.iap.lifecycle.Gates;
import com.iap.lifecycle.LifecycleState;
import com.iap.lifecycle.LifecycleTransition;
import com.iap.lifecycle.PolicyConfig;

/**
 * Alpha promotion lifecycle parity with the Python reference
 * ({@code iap.lifecycle}) against tests/golden/expected_lifecycle.json:
 * every step of LC01 .. LC06 replayed from the embedded default config
 * (ledger significance threshold, CUSUM retirement) and of LG01 from the
 * embedded legacy config (fixed threshold, consecutive breaches) — state,
 * outcome, every gate result (value and order), the counters, the CUSUM
 * statistic and the canonical transition JSON, all exact — plus the
 * transition table, the pinned config and a byte-identical load/save of
 * research/alpha_registry.json.
 */
public class LifecycleGoldenTest {
    private static Map<String, Object> golden() {
        return Golden.json("expected_lifecycle.json");
    }

    private static Map<String, Object> obj(Object v) {
        return Json.object(v);
    }

    private static PolicyConfig config() {
        return PolicyConfig.fromTree(obj(golden().get("config")));
    }

    @Test
    public void statesTransitionTableAndConfigArePinned() {
        Map<String, Object> g = golden();
        assertEquals(3L, Json.asLong(g.get("x-version")));
        Map<String, Object> states = Json.object(g.get("states"));
        assertEquals(7, states.size());
        for (LifecycleState s : LifecycleState.values()) {
            assertEquals(s.name(), s.index(), Json.asLong(states.get(s.name())));
        }
        assertEquals(CanonicalJson.serialize(g.get("transition_table")),
                CanonicalJson.serialize(AlphaLifecycle.transitionTable()));
        assertEquals(17, AlphaLifecycle.ALLOWED_TRANSITIONS.size());
        // the embedded config round-trips and equals the committed policy files
        PolicyConfig cfg = config();
        assertEquals(CanonicalJson.serialize(g.get("config")),
                CanonicalJson.serialize(cfg.toTree()));
        PolicyConfig fromFiles = new ConfigService(Paths.get("..", "configs"))
                .lifecyclePolicy();
        assertEquals(CanonicalJson.serialize(cfg.toTree()),
                CanonicalJson.serialize(fromFiles.toTree()));
        // the committed policy is the v1.5.0 default; the legacy section
        // names the rules up to v1.4.0 and round-trips too
        assertEquals(PolicyConfig.TSTAT_LEDGER, cfg.tstatThreshold());
        assertEquals(com.iap.adaptive.LifecycleGauge.BreachRule.CUSUM,
                cfg.live().breachRule());
        Object legacyTree = obj(g.get("legacy")).get("config");
        PolicyConfig legacy = PolicyConfig.fromTree(obj(legacyTree));
        assertEquals(CanonicalJson.serialize(legacyTree),
                CanonicalJson.serialize(legacy.toTree()));
        assertEquals(PolicyConfig.TSTAT_FIXED, legacy.tstatThreshold());
        assertEquals(com.iap.adaptive.LifecycleGauge.BreachRule.CONSECUTIVE,
                legacy.live().breachRule());
        assertEquals(CanonicalJson.serialize(cfg.gates().toTree()),
                CanonicalJson.serialize(legacy.gates().toTree()));
        // the correlation gate is configured the same under either policy and
        // is the last of the eleven CANDIDATE -> VALIDATING gates
        assertEquals(LifecycleState.VALIDATING, cfg.crossAlphaMinState());
        assertEquals(LifecycleState.VALIDATING, legacy.crossAlphaMinState());
        assertEquals(0.7, cfg.gates().maxCrossAlphaCorrelation(), 0.0);
        List<Gates> candidateGates = AlphaLifecycle
                .promotionEdge(LifecycleState.CANDIDATE).gates();
        assertEquals(11, candidateGates.size());
        assertEquals(Gates.CROSS_ALPHA_CORRELATION, candidateGates.get(10));
        // the bootstrap gate is evaluated by the default policy and left out
        // by the legacy one; the edge lists it under either
        assertEquals(PolicyConfig.NET_PNL_CI_REQUIRED, cfg.netPnlCiGate());
        assertEquals(PolicyConfig.NET_PNL_CI_ABSENT, legacy.netPnlCiGate());
        assertEquals(0.0, cfg.gates().minNetPnlCiLow(), 0.0);
        assertEquals(0.95, cfg.gates().netPnlCiLevel(), 0.0);
        assertEquals(Gates.NET_PNL_BOOTSTRAP_CI, candidateGates.get(7));
    }

    @Test
    public void everyScenarioStepReplaysExactly() {
        Map<String, Object> g = golden();
        Map<String, Object> scenarios = Json.object(g.get("scenarios"));
        assertEquals(new TreeSet<>(List.of("LC01", "LC02", "LC03", "LC04", "LC05",
                "LC06")), new TreeSet<>(scenarios.keySet()));
        assertEquals("the six default scripts carry 52 transitions", 52,
                replay(g, config(), scenarios));
    }

    @Test
    public void lc06PinsTheNetPnlBootstrapGate() {
        Map<String, Object> g = golden();
        Map<String, Object> scenarios = Json.object(g.get("scenarios"));
        Map<String, Object> only = new LinkedHashMap<>();
        only.put("LC06", scenarios.get("LC06"));
        assertEquals("LC06 carries 2 transitions", 2, replay(g, config(), only));
        List<Object> steps = Json.array(scenarios.get("LC06"));
        assertEquals(8, steps.size());
        // steps 1..6 hold on the gate; the threshold is the configured 0.0
        for (int k = 1; k <= 6; k++) {
            Map<String, Object> step = obj(steps.get(k));
            assertEquals("step " + k, "HOLD", obj(step.get("expected")).get("outcome"));
            Map<String, Object> gate = bootstrapGate(step);
            assertEquals("step " + k, Boolean.FALSE, gate.get("passed"));
            assertEquals("step " + k, 0.0, Json.asDouble(gate.get("threshold")), 0.0);
        }
        // no block, no trade, no bounds, another level: value null
        assertNull(obj(obj(steps.get(1)).get("evidence")).get("pnl_bootstrap"));
        for (int k : new int[] {1, 3, 4, 5}) {
            assertNull("step " + k, bootstrapGate(obj(steps.get(k))).get("value"));
        }
        // an interval spanning zero fails at its lower bound; exactly 0.0
        // fails (strict); 12.5 passes
        assertEquals(-35.5, Json.asDouble(bootstrapGate(obj(steps.get(2))).get("value")),
                0.0);
        assertEquals(0.0, Json.asDouble(bootstrapGate(obj(steps.get(6))).get("value")),
                0.0);
        Map<String, Object> last = obj(steps.get(7));
        assertEquals("VALIDATING", obj(last.get("expected")).get("state"));
        assertEquals(12.5, Json.asDouble(bootstrapGate(last).get("value")), 0.0);
        assertEquals(Boolean.TRUE, bootstrapGate(last).get("passed"));
        assertEquals("all 11 gates passed: CANDIDATE -> VALIDATING",
                obj(obj(last.get("expected")).get("transition")).get("reason"));
    }

    /** The golden's {@code net_pnl_bootstrap_ci} result of one step. */
    private static Map<String, Object> bootstrapGate(Map<String, Object> step) {
        Map<String, Object> gates = obj(obj(step.get("expected")).get("gates"));
        return obj(gates.get(Gates.NET_PNL_BOOTSTRAP_CI.gateName()));
    }

    @Test
    public void lc05PinsTheCrossAlphaCorrelationGate() {
        Map<String, Object> g = golden();
        Map<String, Object> scenarios = Json.object(g.get("scenarios"));
        Map<String, Object> only = new LinkedHashMap<>();
        only.put("LC05", scenarios.get("LC05"));
        assertEquals("LC05 carries 14 transitions", 14, replay(g, config(), only));
        List<Object> steps = Json.array(scenarios.get("LC05"));
        Map<String, Object> last = obj(obj(steps.get(steps.size() - 1)).get("expected"));
        assertEquals("VALIDATING", last.get("state"));
        // step 1: no cross_alpha block — the gate fails closed, value null
        Map<String, Object> missing = obj(steps.get(1));
        assertTrue(obj(missing.get("evidence")).containsKey("cross_alpha"));
        assertNull(obj(missing.get("evidence")).get("cross_alpha"));
        assertEquals("HOLD", obj(missing.get("expected")).get("outcome"));
        Map<String, Object> failed = correlationGate(missing);
        assertNull(failed.get("value"));
        assertEquals(Boolean.FALSE, failed.get("passed"));
        assertEquals(0.7, Json.asDouble(failed.get("threshold")), 0.0);
        // step 8: two eligible peers exactly at the threshold — the tie passes
        Map<String, Object> tie = obj(steps.get(8));
        assertEquals("VALIDATING", obj(tie.get("expected")).get("state"));
        Map<String, Object> passed = correlationGate(tie);
        assertEquals(0.7, Json.asDouble(passed.get("value")), 0.0);
        assertEquals(Boolean.TRUE, passed.get("passed"));
        assertEquals(0.7, Json.asDouble(passed.get("threshold")), 0.0);
    }

    /** The golden's {@code cross_alpha_correlation} result of one step. */
    private static Map<String, Object> correlationGate(Map<String, Object> step) {
        Map<String, Object> gates = obj(obj(step.get("expected")).get("gates"));
        return obj(gates.get(Gates.CROSS_ALPHA_CORRELATION.gateName()));
    }

    @Test
    public void everyLegacyScenarioStepReplaysExactly() {
        Map<String, Object> g = golden();
        Map<String, Object> legacy = obj(g.get("legacy"));
        Map<String, Object> scenarios = Json.object(legacy.get("scenarios"));
        assertEquals(new TreeSet<>(List.of("LG01")), new TreeSet<>(scenarios.keySet()));
        assertEquals("the legacy script carries 9 transitions", 9,
                replay(g, PolicyConfig.fromTree(obj(legacy.get("config"))), scenarios));
        // the legacy policy leaves the bootstrap gate out: LG01 is promoted on
        // ten gates with no bootstrap block in its evidence
        Map<String, Object> promotion = obj(Json.array(scenarios.get("LG01")).get(1));
        assertNull(obj(promotion.get("evidence")).get("pnl_bootstrap"));
        Map<String, Object> expected = obj(promotion.get("expected"));
        assertEquals(10, obj(expected.get("gates")).size());
        assertTrue(!obj(expected.get("gates"))
                .containsKey(Gates.NET_PNL_BOOTSTRAP_CI.gateName()));
        assertEquals("all 10 gates passed: CANDIDATE -> VALIDATING",
                obj(expected.get("transition")).get("reason"));
    }

    /** Replay {@code scenarios} under {@code cfg}; returns the transitions seen. */
    private static int replay(Map<String, Object> g, PolicyConfig cfg,
            Map<String, Object> scenarios) {
        long t0 = Json.asLong(g.get("t0"));
        long stepNs = Json.asLong(g.get("step_ns"));
        int transitionsSeen = 0;
        for (String alphaId : new TreeSet<>(scenarios.keySet())) {
            AlphaRegistry registry = new AlphaRegistry(cfg.policy());
            AlphaLifecycle machine = new AlphaLifecycle(cfg, registry);
            machine.register(alphaId, t0);
            List<Object> steps = Json.array(scenarios.get(alphaId));
            for (int k = 0; k < steps.size(); k++) {
                Map<String, Object> step = Json.object(steps.get(k));
                String where = alphaId + " step " + k + " (" + step.get("note") + ")";
                assertEquals(where, k, Json.asLong(step.get("step")));
                long eventTs = Json.asLong(step.get("event_ts"));
                assertEquals(where, t0 + (k + 1) * stepNs, eventTs);
                String action = (String) step.get("action");
                Map<String, Object> expected = Json.object(step.get("expected"));
                LifecycleTransition transition;
                String outcome;
                Map<String, Object> gates;
                switch (action) {
                    case "advance" -> {
                        Evidence ev = Evidence.fromTree(obj(step.get("evidence")));
                        // the evidence document round-trips exactly
                        assertEquals(where, CanonicalJson.serialize(step.get("evidence")),
                                CanonicalJson.serialize(ev.toTree()));
                        transition = machine.advance(alphaId, eventTs, ev);
                        GateEvaluation eval = machine.evaluations()
                                .get(machine.evaluations().size() - 1);
                        outcome = eval.outcome().name();
                        gates = eval.toTree();
                        // evaluation order == golden key order (edge order)
                        assertEquals(where, new ArrayList<>(
                                Json.object(expected.get("gates")).keySet()),
                                new ArrayList<>(eval.gates().keySet()));
                        assertEquals(where, CanonicalJson.serialize(expected.get("gates")),
                                CanonicalJson.serialize(gates.get("gates")));
                        assertEquals(where, eval, registry.get(alphaId).lastEvaluation());
                    }
                    case "retire" -> {
                        transition = machine.retire(alphaId, eventTs,
                                (String) step.get("reason"),
                                LifecycleTransition.Actor.HUMAN);
                        outcome = "TRANSITION";
                        assertEquals(where, "{}",
                                CanonicalJson.serialize(expected.get("gates")));
                    }
                    case "reset" -> {
                        transition = machine.resetToResearch(alphaId, eventTs,
                                (String) step.get("reason"),
                                LifecycleTransition.Actor.HUMAN);
                        outcome = "TRANSITION";
                        assertEquals(where, "{}",
                                CanonicalJson.serialize(expected.get("gates")));
                    }
                    default -> throw new AssertionError("unknown action " + action);
                }
                AlphaRecord rec = registry.get(alphaId);
                assertEquals(where, expected.get("state"), rec.state().name());
                assertEquals(where, Json.asLong(expected.get("state_index")),
                        rec.state().index());
                assertEquals(where, expected.get("outcome"), outcome);
                assertEquals(where, Json.asLong(expected.get("consecutive_failures")),
                        rec.consecutiveFailures());
                assertEquals(where, Json.asLong(expected.get("breach_count")),
                        rec.breachCount());
                assertEquals(where, Json.asLong(expected.get("recovery_count")),
                        rec.recoveryCount());
                assertEquals(where, Json.asDouble(expected.get("cusum")), rec.cusum(), 0.0);
                Object want = expected.get("transition");
                if (want == null) {
                    assertNull(where, transition);
                } else {
                    assertNotNull(where, transition);
                    transitionsSeen++;
                    assertEquals(where, CanonicalJson.serialize(want), transition.toLine());
                    assertEquals(where, transition, rec.lastTransition());
                    assertEquals(where, eventTs, rec.sinceTs());
                    // the canonical line parses back to the same record
                    assertEquals(where, transition,
                            LifecycleTransition.fromTree(
                                    Json.object(com.iap.config.Json.parse(
                                            transition.toLine(), true))));
                }
            }
        }
        return transitionsSeen;
    }

    @Test
    public void registryLoadThenSaveIsByteIdentical() throws Exception {
        Path path = Paths.get("..", "research", "alpha_registry.json");
        String onDisk = new String(Files.readAllBytes(path), StandardCharsets.UTF_8);
        AlphaRegistry registry = AlphaRegistry.load(path);
        assertEquals("the bundled registry holds the 24 alphas", 24, registry.size());
        assertEquals(onDisk, registry.render());
        // and the parsed records are live state the machine can continue from
        PolicyConfig cfg = new ConfigService(Paths.get("..", "configs")).lifecyclePolicy();
        assertEquals(cfg.policy(), registry.policy());
        AlphaLifecycle machine = new AlphaLifecycle(cfg, registry);
        for (String id : registry.alphaIds()) {
            AlphaRecord rec = registry.get(id);
            assertEquals(id, LifecycleState.CANDIDATE, rec.state());
            assertTrue(id, rec.lastEvaluation().failedGates()
                    .contains("net_pnl_after_costs"));
        }
        // silence moves nothing (state and counters unchanged)
        for (String id : registry.alphaIds()) {
            AlphaRecord rec = registry.get(id);
            assertNull(machine.advance(id, rec.sinceTs() + 1, Evidence.empty()));
            assertEquals(id, LifecycleState.CANDIDATE, rec.state());
            assertEquals(id, 0, rec.consecutiveFailures());
        }
        assertEquals(24, machine.evaluations().size());
        for (GateEvaluation ev : machine.evaluations()) {
            assertEquals(GateEvaluation.Outcome.NO_EVIDENCE, ev.outcome());
        }
    }
}
