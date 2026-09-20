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
import com.iap.lifecycle.LifecycleState;
import com.iap.lifecycle.LifecycleTransition;
import com.iap.lifecycle.PolicyConfig;

/**
 * Alpha promotion lifecycle parity with the Python reference
 * ({@code iap.lifecycle}) against tests/golden/expected_lifecycle.json:
 * every step of LC01 / LC02 / LC03 replayed from the embedded config —
 * state, outcome, every gate result (value and order), the counters and the
 * canonical transition JSON, all exact — plus the transition table, the
 * pinned config and a byte-identical load/save of research/alpha_registry.json.
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
        assertEquals(1L, Json.asLong(g.get("x-version")));
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
    }

    @Test
    public void everyScenarioStepReplaysExactly() {
        Map<String, Object> g = golden();
        long t0 = Json.asLong(g.get("t0"));
        long stepNs = Json.asLong(g.get("step_ns"));
        PolicyConfig cfg = config();
        Map<String, Object> scenarios = Json.object(g.get("scenarios"));
        assertEquals(new TreeSet<>(List.of("LC01", "LC02", "LC03")),
                new TreeSet<>(scenarios.keySet()));
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
        assertEquals("the three scripts carry 16 transitions", 16, transitionsSeen);
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
