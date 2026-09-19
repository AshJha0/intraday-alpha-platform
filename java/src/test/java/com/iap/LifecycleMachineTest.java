package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.List;
import java.util.Map;

import org.junit.Test;

import com.iap.config.ConfigService;
import com.iap.lifecycle.AlphaLifecycle;
import com.iap.lifecycle.AlphaLifecycle.Edge;
import com.iap.lifecycle.AlphaLifecycle.EdgeKind;
import com.iap.lifecycle.AlphaRecord;
import com.iap.lifecycle.AlphaRegistry;
import com.iap.lifecycle.Evidence;
import com.iap.lifecycle.ExperimentResultRec;
import com.iap.lifecycle.GateEvaluation;
import com.iap.lifecycle.GateResult;
import com.iap.lifecycle.Gates;
import com.iap.lifecycle.LifecycleState;
import com.iap.lifecycle.LifecycleTransition;
import com.iap.lifecycle.LifecycleTransition.Actor;
import com.iap.lifecycle.LifecycleTransitionLog;
import com.iap.lifecycle.PolicyConfig;

/** Unit tests of the lifecycle machine: edges, gates, actors, config guards. */
public class LifecycleMachineTest {
    private static final String SHA_A =
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
    private static final String SHA_B =
            "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210";

    private static PolicyConfig config() {
        return new ConfigService(Paths.get("..", "configs")).lifecyclePolicy();
    }

    private static ExperimentResultRec research(String alphaId, double ic, double rankIc,
            double tStat, double netBps, boolean leakagePassed, Boolean hypothesis) {
        return new ExperimentResultRec("exp-" + alphaId, alphaId, SHA_A, SHA_B, null,
                ic, rankIc, tStat, 2, 0.55, 40.0, netBps + 7.0, 7.0, netBps, 20.0, 1.5,
                1.0, 4, leakagePassed, Map.of("shift_ok", leakagePassed),
                hypothesis, leakagePassed ? "PROMOTE" : "REJECT", 10, "test",
                1700000000000000000L);
    }

    private static Evidence ev(ExperimentResultRec r, Double capacity) {
        return new Evidence(r, capacity, null, null, null);
    }

    private static Evidence live(Double ic, boolean informative) {
        return new Evidence(null, null, null, null, new Evidence.Live(ic, 8, 1, informative));
    }

    private static AlphaLifecycle machineAt(LifecycleState target, String id) {
        PolicyConfig cfg = config();
        AlphaLifecycle m = new AlphaLifecycle(cfg, new AlphaRegistry(cfg.policy()));
        m.register(id, 1L);
        ExperimentResultRec good = research(id, 0.02, 0.03, 4.0, 5.0, true, true);
        if (target.index() >= LifecycleState.CANDIDATE.index()) {
            assertNotNull(m.advance(id, 2L, ev(good, null)));
        }
        if (target.index() >= LifecycleState.VALIDATING.index()) {
            assertNotNull(m.advance(id, 3L, ev(good, 5e6)));
        }
        if (target.index() >= LifecycleState.PAPER.index()) {
            assertNotNull(m.advance(id, 4L, new Evidence(null, null,
                    new Evidence.Validation(0.018, 0.02, true, true), null, null)));
        }
        if (target.index() >= LifecycleState.ACTIVE.index()) {
            assertNotNull(m.advance(id, 5L, new Evidence(null, null, null,
                    new Evidence.Paper(5, 0.015, 0.02, 100.0, 0, 0.001), null)));
        }
        assertEquals(target, m.state(id));
        return m;
    }

    @Test
    public void transitionTableHasOneEdgePerPinnedRow() {
        List<Edge> edges = AlphaLifecycle.ALLOWED_TRANSITIONS;
        assertEquals(17, edges.size());
        int promotion = 0;
        int demotion = 0;
        int liveEdges = 0;
        int manual = 0;
        for (Edge e : edges) {
            switch (e.kind()) {
                case PROMOTION -> {
                    promotion++;
                    assertEquals(Actor.SYSTEM, e.actor());
                    assertEquals(e.fromState().index() + 1, e.toState().index());
                    assertFalse(e.gates().isEmpty());
                }
                case DEMOTION -> {
                    demotion++;
                    assertTrue(e.toState().index() < e.fromState().index());
                }
                case LIVE -> {
                    liveEdges++;
                    assertEquals(List.of(Gates.ROLLING_IC), e.gates());
                }
                case MANUAL -> {
                    manual++;
                    assertEquals(Actor.HUMAN, e.actor());
                    assertTrue(e.gates().isEmpty());
                }
            }
        }
        assertEquals(4, promotion);
        assertEquals(3, demotion);
        assertEquals(3, liveEdges);
        assertEquals(7, manual);
        // every gate of the table is used by some edge, and every name is unique
        for (Gates g : Gates.values()) {
            assertTrue(g.gateName(), edges.stream().anyMatch(e -> e.gates().contains(g)));
            assertEquals(g, Gates.byName(g.gateName()));
        }
        try {
            AlphaLifecycle.edgeFor(LifecycleState.RESEARCH, LifecycleState.PAPER,
                    EdgeKind.PROMOTION);
            fail("phantom edge");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("no PROMOTION edge"));
        }
    }

    @Test
    public void gateKindsCompareAsPinned() {
        PolicyConfig cfg = config();
        // min (inclusive): oos_ic == threshold passes
        ExperimentResultRec atGate = research("G1", cfg.gates().minOosIc(), 0.01,
                cfg.gates().minNwTstat(), 0.0, true, true);
        assertTrue(Gates.OOS_IC.evaluate(ev(atGate, null), cfg).passed());
        assertTrue(Gates.STATISTICAL_SIGNIFICANCE.evaluate(ev(atGate, null), cfg).passed());
        // gt (strict): net_pnl == 0.0 fails
        GateResult net = Gates.NET_PNL_AFTER_COSTS.evaluate(ev(atGate, null), cfg);
        assertFalse(net.passed());
        assertEquals(0.0, net.value(), 0.0);
        assertEquals(cfg.gates().minNetReturnBps(), net.threshold(), 0.0);
        // max (inclusive): stability gap exactly 1.0 passes, above fails
        ExperimentResultRec gapOne = research("G2", 0.02, 0.04, 4.0, 1.0, true, true);
        assertTrue(Gates.STABILITY.evaluate(ev(gapOne, null), cfg).passed());
        ExperimentResultRec gapBig = research("G3", 0.02, 0.0401, 4.0, 1.0, true, true);
        assertFalse(Gates.STABILITY.evaluate(ev(gapBig, null), cfg).passed());
        assertEquals(1.0, Gates.icRankGap(0.02, 0.04, 1e-12), 1e-12);
        // bool: value and threshold are null; hypothesis None is a failure
        GateResult leak = Gates.LEAKAGE_CLEAN.evaluate(ev(gapOne, null), cfg);
        assertTrue(leak.passed());
        assertNull(leak.value());
        assertNull(leak.threshold());
        ExperimentResultRec noHyp = research("G4", 0.02, 0.03, 4.0, 1.0, true, null);
        assertFalse(Gates.HYPOTHESIS_SIGN.evaluate(ev(noHyp, null), cfg).passed());
        // absent block / metric: fails with value null, threshold kept
        GateResult cap = Gates.CAPACITY.evaluate(Evidence.empty(), cfg);
        assertFalse(cap.passed());
        assertNull(cap.value());
        assertEquals(cfg.gates().minCapacityUsd(), cap.threshold(), 0.0);
        // uninformative live reading: rolling_ic metric absent
        assertNull(Gates.ROLLING_IC.evaluate(live(0.5, false), cfg).value());
        assertEquals(cfg.live().watchIcGate(),
                Gates.ROLLING_IC.evaluate(live(0.5, true), cfg).threshold(), 0.0);
    }

    @Test
    public void manualEdgesRequireAHumanAndAReason() throws Exception {
        Path dir = Files.createTempDirectory("iap-lifecycle");
        LifecycleTransitionLog log = new LifecycleTransitionLog(
                dir.resolve("lifecycle_transitions.jsonl"), true);
        PolicyConfig cfg = config();
        AlphaLifecycle m = new AlphaLifecycle(cfg, new AlphaRegistry(cfg.policy()), log);
        m.register("MX", 1L);
        try {
            m.retire("MX", 2L, "system says so", Actor.SYSTEM);
            fail("SYSTEM retired an alpha");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("requires Actor.HUMAN"));
        }
        try {
            m.retire("MX", 2L, "   ", Actor.HUMAN);
            fail("blank reason accepted");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("non-empty reason"));
        }
        try {
            m.resetToResearch("MX", 2L, "not retired yet", Actor.HUMAN);
            fail("reset of a non-retired alpha accepted");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("not RETIRED"));
        }
        LifecycleTransition t = m.retire("MX", 2L, "desk decision", Actor.HUMAN);
        assertEquals(LifecycleState.RETIRED, m.state("MX"));
        assertEquals(Actor.HUMAN, t.actor());
        assertTrue(t.gates().isEmpty());
        assertEquals(cfg.policy(), t.policy());
        try {
            m.retire("MX", 3L, "twice", Actor.HUMAN);
            fail("retired twice");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("already RETIRED"));
        }
        // SYSTEM on RETIRED is terminal: no movement, TERMINAL recorded
        assertNull(m.advance("MX", 4L, live(0.5, true)));
        assertEquals(GateEvaluation.Outcome.TERMINAL,
                m.evaluations().get(m.evaluations().size() - 1).outcome());
        LifecycleTransition back = m.resetToResearch("MX", 5L, "re-research", Actor.HUMAN);
        assertEquals(LifecycleState.RESEARCH, m.state("MX"));
        // the log holds both canonical lines and replays them
        List<LifecycleTransition> logged = log.readAll();
        assertEquals(List.of(t, back), logged);
        assertEquals(2, m.transitions().size());
    }

    @Test
    public void demotionCountersAndSilence() {
        AlphaLifecycle m = machineAt(LifecycleState.VALIDATING, "DM");
        PolicyConfig cfg = m.config();
        Evidence bad = new Evidence(null, null,
                new Evidence.Validation(0.018, 0.02, true, false), null, null);
        for (int i = 1; i < cfg.maxConsecutiveFailures(); i++) {
            assertNull(m.advance("DM", 10L + i, bad));
            assertEquals(i, m.record("DM").consecutiveFailures());
            // silence does not count
            assertNull(m.advance("DM", 100L + i, Evidence.empty()));
            assertEquals(i, m.record("DM").consecutiveFailures());
            assertEquals(GateEvaluation.Outcome.NO_EVIDENCE,
                    m.record("DM").lastEvaluation().outcome());
        }
        LifecycleTransition t = m.advance("DM", 200L, bad);
        assertNotNull(t);
        assertEquals(LifecycleState.CANDIDATE, t.toState());
        assertTrue(t.reason(), t.reason().startsWith(cfg.maxConsecutiveFailures()
                + " consecutive failed evaluations"));
        assertTrue(t.reason(), t.reason().endsWith("cross_language_parity"));
        assertEquals(0, m.record("DM").consecutiveFailures());
        // a leaking re-run at CANDIDATE demotes at once
        ExperimentResultRec leaking = research("DM", 0.02, 0.03, 4.0, 5.0, false, true);
        LifecycleTransition down = m.advance("DM", 300L, ev(leaking, 5e6));
        assertNotNull(down);
        assertEquals(LifecycleState.RESEARCH, down.toState());
        assertEquals(9, down.gates().size());
    }

    @Test
    public void liveEdgesDelegateToTheGaugeRulesAndResume() {
        AlphaLifecycle m = machineAt(LifecycleState.ACTIVE, "LV");
        PolicyConfig cfg = m.config();
        double breach = cfg.live().watchIcGate() - 0.01;
        double recover = cfg.live().reactivateIcGate() + 0.01;
        LifecycleTransition toWatch = m.advance("LV", 10L, live(breach, true));
        assertNotNull(toWatch);
        assertEquals(LifecycleState.WATCH, toWatch.toState());
        assertEquals(1, m.record("LV").breachCount());
        assertFalse(toWatch.gates().get("rolling_ic").passed());
        assertEquals(cfg.live().watchIcGate(),
                toWatch.gates().get("rolling_ic").threshold(), 0.0);
        // resume: a new machine over the same registry continues the counters
        AlphaLifecycle resumed = new AlphaLifecycle(cfg, m.registry());
        int needed = cfg.live().reactivateEvals();
        LifecycleTransition toActive = null;
        for (int i = 0; i < needed; i++) {
            toActive = resumed.advance("LV", 20L + i, live(recover, true));
        }
        assertNotNull(toActive);
        assertEquals(LifecycleState.ACTIVE, toActive.toState());
        assertTrue(toActive.gates().get("rolling_ic").passed());
        assertEquals(cfg.live().reactivateIcGate(),
                toActive.gates().get("rolling_ic").threshold(), 0.0);
        assertEquals("re-activation: " + needed
                + " consecutive evals >= reactivate gate 0.005", toActive.reason());
        // a null IC or an uninformative reading moves nothing
        assertNull(resumed.advance("LV", 40L, live(null, true)));
        assertNull(resumed.advance("LV", 41L, live(breach, false)));
        assertEquals(LifecycleState.ACTIVE, resumed.state("LV"));
        assertEquals(0, resumed.record("LV").breachCount());
    }

    @Test
    public void registryAndConfigGuards() {
        PolicyConfig cfg = config();
        try {
            new AlphaLifecycle(cfg, new AlphaRegistry("other_policy"));
            fail("policy mismatch accepted");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("policy"));
        }
        AlphaRegistry reg = new AlphaRegistry(cfg.policy());
        reg.add(AlphaRecord.fresh("A1", 1L, null, null, null, null));
        try {
            reg.add(AlphaRecord.fresh("A1", 2L, null, null, null, null));
            fail("duplicate registration accepted");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("already registered"));
        }
        try {
            reg.get("nope");
            fail("unknown alpha accepted");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("unknown alpha"));
        }
        // config: a threshold key missing from lifecycle.json is named
        Map<String, Object> lc = new java.util.LinkedHashMap<>(
                new ConfigService(Paths.get("..", "configs")).doc(ConfigService.LIFECYCLE));
        Map<String, Object> gates = new java.util.LinkedHashMap<>(
                com.iap.config.Json.object(lc.get("gates")));
        gates.remove("min_folds");
        lc.put("gates", gates);
        try {
            PolicyConfig.fromDocs(lc, ConfigService.LIFECYCLE,
                    new ConfigService(Paths.get("..", "configs")).doc(ConfigService.STRATEGIES),
                    ConfigService.STRATEGIES);
            fail("missing key accepted");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage(),
                    expected.getMessage().contains("strategies/lifecycle.json.gates"));
            assertTrue(expected.getMessage(), expected.getMessage().contains("min_folds"));
        }
    }
}
