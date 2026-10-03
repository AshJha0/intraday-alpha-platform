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

    /** The ledger threshold the helper evidence carries (t 4.0 clears it). */
    private static final double THRESHOLD = 3.5;
    /** One 15-minute block of a 2-hour rolling-IC window. */
    private static final double BLOCK_FRACTION = 0.125;

    private static Evidence ev(ExperimentResultRec r, Double capacity) {
        return ev(r, capacity, THRESHOLD);
    }

    private static Evidence ev(ExperimentResultRec r, Double capacity, Double threshold) {
        return new Evidence(r, capacity, threshold, null, null, null);
    }

    private static Evidence live(Double ic, boolean informative) {
        return new Evidence(null, null, null, null, null,
                new Evidence.Live(ic, 8, 1, informative, BLOCK_FRACTION));
    }

    /** {@code cfg} under the rules up to v1.4.0 (fixed threshold, consecutive breaches). */
    private static PolicyConfig legacy(PolicyConfig cfg) {
        PolicyConfig.Live live = cfg.live();
        return new PolicyConfig(cfg.policy(), cfg.gates(), cfg.maxConsecutiveFailures(),
                PolicyConfig.Live.legacyConsecutive(live.watchIcGate(),
                        live.reactivateIcGate(), live.retireBreachEvals(),
                        live.reactivateEvals()),
                PolicyConfig.TSTAT_FIXED);
    }

    private static AlphaLifecycle machineAt(LifecycleState target, String id) {
        return machineAt(target, id, config());
    }

    private static AlphaLifecycle machineAt(LifecycleState target, String id,
            PolicyConfig cfg) {
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
            assertNotNull(m.advance(id, 4L, new Evidence(null, null, null,
                    new Evidence.Validation(0.018, 0.02, true, true), null, null)));
        }
        if (target.index() >= LifecycleState.ACTIVE.index()) {
            assertNotNull(m.advance(id, 5L, new Evidence(null, null, null, null,
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
        // significance (v1.5.0, ledger policy): the threshold is
        // max(min_nw_tstat, the evidence's), inclusive ...
        double floor = cfg.gates().minNwTstat();
        assertEquals(PolicyConfig.TSTAT_LEDGER, cfg.tstatThreshold());
        GateResult atFloor = Gates.STATISTICAL_SIGNIFICANCE.evaluate(
                ev(atGate, null, floor), cfg);
        assertTrue(atFloor.passed());
        assertEquals(floor, atFloor.threshold(), 0.0);
        // ... an evidence threshold below the floor is replaced by the floor ...
        assertEquals(floor, Gates.STATISTICAL_SIGNIFICANCE.evaluate(
                ev(atGate, null, floor - 1.0), cfg).threshold(), 0.0);
        // ... one above it applies and fails a t at the floor ...
        GateResult above = Gates.STATISTICAL_SIGNIFICANCE.evaluate(
                ev(atGate, null, floor + 0.5), cfg);
        assertFalse(above.passed());
        assertEquals(floor + 0.5, above.threshold(), 0.0);
        assertEquals(floor, above.value(), 0.0);
        // ... and evidence without one fails with threshold null, value kept
        GateResult none = Gates.STATISTICAL_SIGNIFICANCE.evaluate(
                ev(atGate, null, null), cfg);
        assertFalse(none.passed());
        assertNull(none.threshold());
        assertEquals(floor, none.value(), 0.0);
        // the legacy fixed policy reads the config alone
        GateResult fixed = Gates.STATISTICAL_SIGNIFICANCE.evaluate(
                ev(atGate, null, null), legacy(cfg));
        assertTrue(fixed.passed());
        assertEquals(floor, fixed.threshold(), 0.0);
        assertEquals(floor, Gates.STATISTICAL_SIGNIFICANCE.evaluate(
                ev(atGate, null, floor + 5.0), legacy(cfg)).threshold(), 0.0);
        // no research block: value null; the threshold is what the evidence gives
        GateResult noResearch = Gates.STATISTICAL_SIGNIFICANCE.evaluate(
                Evidence.empty(), cfg);
        assertFalse(noResearch.passed());
        assertNull(noResearch.value());
        assertNull(noResearch.threshold());
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
        Evidence bad = new Evidence(null, null, null,
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
        // the default rule is the CUSUM: no breach counter, the statistic is
        // new_fraction * (gate - ic - k) and survives the entry into WATCH
        assertEquals(com.iap.adaptive.LifecycleGauge.BreachRule.CUSUM,
                cfg.live().breachRule());
        assertEquals(0, m.record("LV").breachCount());
        double first = BLOCK_FRACTION
                * (cfg.live().watchIcGate() - breach - cfg.live().cusumK());
        assertEquals(first, m.record("LV").cusum(), 0.0);
        assertTrue(first > 0.0 && first < cfg.live().cusumH());
        assertFalse(toWatch.gates().get("rolling_ic").passed());
        assertEquals(cfg.live().watchIcGate(),
                toWatch.gates().get("rolling_ic").threshold(), 0.0);
        // resume: a new machine over the same registry continues the
        // statistic exactly — a second breach adds to the stored value
        AlphaLifecycle resumedOnce = new AlphaLifecycle(cfg, m.registry());
        assertNull(resumedOnce.advance("LV", 11L, live(breach, true)));
        assertEquals(first + BLOCK_FRACTION
                * (cfg.live().watchIcGate() - breach - cfg.live().cusumK()),
                m.record("LV").cusum(), 0.0);
        // ... and the counters
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
        assertEquals("re-activation resets the statistic", 0.0,
                resumed.record("LV").cusum(), 0.0);
    }

    @Test
    public void cusumRetiresOnABreachOnceTheStatisticReachesItsThreshold() {
        AlphaLifecycle m = machineAt(LifecycleState.ACTIVE, "CU");
        PolicyConfig cfg = m.config();
        // deep enough to pass the threshold in ONE reading: still only WATCH
        double deep = cfg.live().watchIcGate() - 0.09;
        LifecycleTransition toWatch = m.advance("CU", 10L, live(deep, true));
        assertNotNull(toWatch);
        assertEquals(LifecycleState.WATCH, toWatch.toState());
        assertTrue(m.record("CU").cusum() >= cfg.live().cusumH());
        // a reading above the gate does not retire, whatever the statistic
        assertNull(m.advance("CU", 11L, live(cfg.live().watchIcGate() + 0.004, true)));
        assertEquals(LifecycleState.WATCH, m.state("CU"));
        assertTrue(m.record("CU").cusum() >= cfg.live().cusumH());
        // the next breach does, and says why
        LifecycleTransition retired = m.advance("CU", 12L,
                live(cfg.live().watchIcGate() - 0.003, true));
        assertNotNull(retired);
        assertEquals(LifecycleState.RETIRED, retired.toState());
        assertEquals("persistent breach: CUSUM 0.010187 >= 0.01 (slack 0.0025) "
                + "below watch gate 0.0", retired.reason());
        assertFalse(retired.gates().get("rolling_ic").passed());
        assertEquals(0.0, m.record("CU").cusum(), 0.0);
    }

    @Test
    public void theLegacyConsecutiveRuleStaysSelectableByName() {
        PolicyConfig cfg = legacy(config());
        AlphaLifecycle m = machineAt(LifecycleState.ACTIVE, "LG", cfg);
        double breach = cfg.live().watchIcGate() - 0.001;
        LifecycleTransition toWatch = m.advance("LG", 10L, live(breach, true));
        assertNotNull(toWatch);
        assertEquals(1, m.record("LG").breachCount());
        assertEquals(0.0, m.record("LG").cusum(), 0.0);
        LifecycleTransition retired = null;
        for (int i = 2; i <= cfg.live().retireBreachEvals(); i++) {
            assertNull("breach " + (i - 1) + " does not retire yet", retired);
            retired = m.advance("LG", 10L + i, live(breach, true));
        }
        assertNotNull(retired);
        assertEquals(LifecycleState.RETIRED, retired.toState());
        assertEquals("persistent breach: " + cfg.live().retireBreachEvals()
                + " consecutive evals below watch gate 0.0", retired.reason());
        // the same shallow breaches never retire under the default rule
        AlphaLifecycle d = machineAt(LifecycleState.ACTIVE, "LG");
        for (int i = 1; i <= 2 * cfg.live().retireBreachEvals(); i++) {
            d.advance("LG", 10L + i, live(breach, true));
        }
        assertEquals(LifecycleState.WATCH, d.state("LG"));
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
