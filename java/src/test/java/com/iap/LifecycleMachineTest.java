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
import java.util.ArrayList;
import java.util.LinkedHashMap;
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

    /** A bootstrap interval that passes the gate under the default policy. */
    private static final Evidence.PnlBootstrap PASSING_BOOTSTRAP =
            new Evidence.PnlBootstrap(12.5, 90.0, 0.95, 1000, 20260829L, 9.0, 626, 40);

    /**
     * Research evidence; it carries a passing bootstrap interval and an empty
     * cross-alpha block ("there is no other alpha"), the vacuous pass of the
     * correlation gate.
     */
    private static Evidence ev(ExperimentResultRec r, Double capacity, Double threshold) {
        return new Evidence(r, capacity, threshold, PASSING_BOOTSTRAP,
                Evidence.CrossAlpha.noPeers(), null, null, null);
    }

    private static Evidence live(Double ic, boolean informative) {
        return new Evidence(null, null, null, null, null, null, null,
                new Evidence.Live(ic, 8, 1, informative, BLOCK_FRACTION));
    }

    /**
     * {@code cfg} under the rules up to v1.4.0 (fixed threshold, consecutive
     * breaches, no bootstrap gate).
     */
    private static PolicyConfig legacy(PolicyConfig cfg) {
        PolicyConfig.Live live = cfg.live();
        return new PolicyConfig(cfg.policy(), cfg.gates(), cfg.maxConsecutiveFailures(),
                PolicyConfig.Live.legacyConsecutive(live.watchIcGate(),
                        live.reactivateIcGate(), live.retireBreachEvals(),
                        live.reactivateEvals()),
                PolicyConfig.TSTAT_FIXED, cfg.crossAlphaMinState(),
                PolicyConfig.NET_PNL_CI_ABSENT);
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
            assertNotNull(m.advance(id, 4L, new Evidence(null, null, null, null, null,
                    new Evidence.Validation(0.018, 0.02, true, true), null, null)));
        }
        if (target.index() >= LifecycleState.ACTIVE.index()) {
            assertNotNull(m.advance(id, 5L, new Evidence(null, null, null, null, null,
                    null, new Evidence.Paper(5, 0.015, 0.02, 100.0, 0, 0.001), null)));
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
        Evidence bad = new Evidence(null, null, null, null, null,
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
        assertEquals(11, down.gates().size());
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

    // ------------------------------------------- the cross-alpha correlation gate

    private static final String CORRELATION_GATE = "cross_alpha_correlation";

    private static Evidence.Peer peer(String alphaId, LifecycleState state,
            double correlation) {
        return new Evidence.Peer(alphaId, state, correlation);
    }

    private static Evidence.CrossAlpha peers(Evidence.Peer... rows) {
        return new Evidence.CrossAlpha(List.of(rows));
    }

    /** CANDIDATE evidence that passes every other gate under either policy. */
    private static Evidence candidate(String id, Evidence.CrossAlpha crossAlpha) {
        return new Evidence(research(id, 0.02, 0.03, 4.0, 5.0, true, true), 5e6,
                THRESHOLD, PASSING_BOOTSTRAP, crossAlpha, null, null, null);
    }

    /**
     * Gates a CANDIDATE evaluation carries under {@code cfg}: the edge's
     * eleven, ten when the policy leaves the bootstrap gate out.
     */
    private static int candidateGateCount(PolicyConfig cfg) {
        return cfg.netPnlCiRequired() ? 11 : 10;
    }

    private static PolicyConfig withMinState(PolicyConfig cfg, LifecycleState minState) {
        return new PolicyConfig(cfg.policy(), cfg.gates(), cfg.maxConsecutiveFailures(),
                cfg.live(), cfg.tstatThreshold(), minState, cfg.netPnlCiGate());
    }

    /**
     * Evaluate {@code crossAlpha} at CANDIDATE and expect a HOLD on the
     * correlation gate alone; returns the gate's result.
     */
    private static GateResult heldByCorrelation(PolicyConfig cfg,
            Evidence.CrossAlpha crossAlpha) {
        AlphaLifecycle m = machineAt(LifecycleState.CANDIDATE, "CX", cfg);
        assertNull(m.advance("CX", 10L, candidate("CX", crossAlpha)));
        GateEvaluation eval = m.evaluations().get(m.evaluations().size() - 1);
        assertEquals(GateEvaluation.Outcome.HOLD, eval.outcome());
        assertEquals(List.of(CORRELATION_GATE), eval.failedGates());
        int count = candidateGateCount(cfg);
        assertEquals(count, eval.gates().size());
        assertEquals("evaluated last", CORRELATION_GATE,
                new ArrayList<>(eval.gates().keySet()).get(count - 1));
        assertEquals(LifecycleState.CANDIDATE, m.state("CX"));
        assertEquals(0, m.record("CX").consecutiveFailures());
        return eval.gates().get(CORRELATION_GATE);
    }

    /**
     * Evaluate {@code crossAlpha} at CANDIDATE and expect the promotion to
     * VALIDATING; returns the gate's result as the transition carries it.
     */
    private static GateResult promotedPastCorrelation(PolicyConfig cfg,
            Evidence.CrossAlpha crossAlpha) {
        AlphaLifecycle m = machineAt(LifecycleState.CANDIDATE, "CX", cfg);
        LifecycleTransition t = m.advance("CX", 10L, candidate("CX", crossAlpha));
        assertNotNull(t);
        assertEquals(LifecycleState.CANDIDATE, t.fromState());
        assertEquals(LifecycleState.VALIDATING, t.toState());
        assertEquals(candidateGateCount(cfg), t.gates().size());
        GateResult r = t.gates().get(CORRELATION_GATE);
        assertNotNull(r);
        assertTrue(r.passed());
        return r;
    }

    /** {@code action} must throw an IllegalArgumentException naming {@code needle}. */
    private static void rejects(String needle, Runnable action) {
        try {
            action.run();
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage(), expected.getMessage().contains(needle));
            return;
        }
        fail("accepted: " + needle);
    }

    @Test
    public void crossAlphaGateRowIsPinned() {
        assertEquals(20, Gates.values().length);
        Gates gate = Gates.CROSS_ALPHA_CORRELATION;
        assertEquals(Gates.STABILITY.ordinal() + 1, gate.ordinal());
        assertEquals(gate.ordinal() + 1, Gates.HOLDOUT_IC_TRACKS_RESEARCH.ordinal());
        assertEquals(CORRELATION_GATE, gate.gateName());
        assertEquals("cross_alpha", gate.block());
        assertEquals(Gates.Kind.MAX, gate.kind());
        assertEquals("max_cross_alpha_correlation", gate.thresholdKey());
        assertEquals(gate, Gates.byName(CORRELATION_GATE));
        PolicyConfig cfg = config();
        assertEquals(3L, PolicyConfig.LIFECYCLE_CONFIG_VERSION);
        assertEquals(LifecycleState.VALIDATING, cfg.crossAlphaMinState());
        assertEquals(0.7, cfg.gates().maxCrossAlphaCorrelation(), 0.0);
        assertEquals(0.7, gate.threshold(cfg), 0.0);
        // the eleven gates of the edge, the correlation gate last
        List<Gates> edge = AlphaLifecycle.promotionEdge(LifecycleState.CANDIDATE).gates();
        assertEquals(List.of(Gates.LEAKAGE_CLEAN, Gates.OOS_IC,
                Gates.STATISTICAL_SIGNIFICANCE, Gates.FOLD_CONSISTENCY, Gates.FOLD_COUNT,
                Gates.HYPOTHESIS_SIGN, Gates.NET_PNL_AFTER_COSTS,
                Gates.NET_PNL_BOOTSTRAP_CI, Gates.CAPACITY, Gates.STABILITY,
                Gates.CROSS_ALPHA_CORRELATION), edge);
        for (Edge e : AlphaLifecycle.ALLOWED_TRANSITIONS) {
            assertEquals(e.fromState() == LifecycleState.CANDIDATE
                    && e.kind() == EdgeKind.PROMOTION, e.gates().contains(gate));
        }
    }

    @Test
    public void crossAlphaMissingBlockFailsClosed() {
        for (PolicyConfig cfg : List.of(config(), legacy(config()))) {
            GateResult r = heldByCorrelation(cfg, null);
            assertFalse(r.passed());
            assertNull(r.value());
            assertEquals(0.7, r.threshold(), 0.0);
        }
        // silence at CANDIDATE still keys on the research block alone: a
        // cross-alpha block without research evaluates nothing
        AlphaLifecycle m = machineAt(LifecycleState.CANDIDATE, "CX");
        assertNull(m.advance("CX", 10L, new Evidence(null, null, null, null,
                Evidence.CrossAlpha.noPeers(), null, null, null)));
        GateEvaluation eval = m.evaluations().get(m.evaluations().size() - 1);
        assertEquals(GateEvaluation.Outcome.NO_EVIDENCE, eval.outcome());
        assertTrue(eval.gates().isEmpty());
    }

    @Test
    public void crossAlphaEligiblePeerAboveThresholdHolds() {
        GateResult r = heldByCorrelation(config(),
                peers(peer("P", LifecycleState.ACTIVE, 0.82)));
        assertFalse(r.passed());
        assertEquals(0.82, r.value(), 0.0);
        assertEquals(0.7, r.threshold(), 0.0);
        // every state from VALIDATING to WATCH counts under the default policy
        for (LifecycleState state : List.of(LifecycleState.VALIDATING,
                LifecycleState.PAPER, LifecycleState.ACTIVE, LifecycleState.WATCH)) {
            GateResult s = heldByCorrelation(config(), peers(peer("P", state, 0.75)));
            assertEquals(state.name(), 0.75, s.value(), 0.0);
        }
    }

    @Test
    public void crossAlphaAbsoluteValueIsGated() {
        GateResult r = heldByCorrelation(config(),
                peers(peer("P", LifecycleState.PAPER, -0.9),
                        peer("Q", LifecycleState.ACTIVE, 0.1)));
        assertFalse(r.passed());
        assertEquals(0.9, r.value(), 0.0);
    }

    @Test
    public void crossAlphaIneligiblePeersAreIgnored() {
        GateResult r = promotedPastCorrelation(config(),
                peers(peer("P", LifecycleState.RESEARCH, 0.99),
                        peer("Q", LifecycleState.CANDIDATE, 0.95),
                        peer("R", LifecycleState.RETIRED, -0.99)));
        assertEquals(0.0, r.value(), 0.0);
        assertEquals(0.7, r.threshold(), 0.0);
        // an eligible peer beside them is the one that counts
        GateResult s = promotedPastCorrelation(config(),
                peers(peer("P", LifecycleState.CANDIDATE, 0.95),
                        peer("Q", LifecycleState.ACTIVE, 0.25),
                        peer("R", LifecycleState.VALIDATING, -0.4)));
        assertEquals(0.4, s.value(), 0.0);
    }

    @Test
    public void crossAlphaTieAtTheThresholdPasses() {
        GateResult r = promotedPastCorrelation(config(),
                peers(peer("P", LifecycleState.PAPER, 0.7),
                        peer("Q", LifecycleState.WATCH, -0.7)));
        assertEquals(0.7, r.value(), 0.0);
        assertEquals(0.7, r.threshold(), 0.0);
    }

    @Test
    public void crossAlphaEmptyPeerListPassesVacuously() {
        for (PolicyConfig cfg : List.of(config(), legacy(config()))) {
            GateResult r = promotedPastCorrelation(cfg, Evidence.CrossAlpha.noPeers());
            assertEquals(0.0, r.value(), 0.0);
            assertEquals(0.7, r.threshold(), 0.0);
        }
    }

    @Test
    public void crossAlphaMinStateActiveIgnoresEarlierStates() {
        Evidence.CrossAlpha early = peers(peer("P", LifecycleState.VALIDATING, 0.9),
                peer("Q", LifecycleState.PAPER, -0.95));
        // counted under the default VALIDATING ...
        assertEquals(0.95, heldByCorrelation(config(), early).value(), 0.0);
        // ... and not under ACTIVE
        PolicyConfig active = withMinState(config(), LifecycleState.ACTIVE);
        assertEquals(0.0, promotedPastCorrelation(active, early).value(), 0.0);
        // ACTIVE and WATCH peers still count; RETIRED never does
        assertEquals(0.8, heldByCorrelation(active,
                peers(peer("P", LifecycleState.RETIRED, 0.99),
                        peer("Q", LifecycleState.WATCH, 0.8))).value(), 0.0);
        // PAPER as the lowest state: VALIDATING is ignored, PAPER counts
        PolicyConfig paper = withMinState(config(), LifecycleState.PAPER);
        assertEquals(0.95, heldByCorrelation(paper, early).value(), 0.0);
        assertEquals(0.0, promotedPastCorrelation(paper,
                peers(peer("P", LifecycleState.VALIDATING, 0.9))).value(), 0.0);
        // the statistic itself, without the machine
        assertEquals(0.95, early.maxAbsCorrelation(LifecycleState.VALIDATING), 0.0);
        assertEquals(0.95, early.maxAbsCorrelation(LifecycleState.PAPER), 0.0);
        assertEquals(0.0, early.maxAbsCorrelation(LifecycleState.ACTIVE), 0.0);
        assertTrue(early.peers().get(1).eligible(LifecycleState.PAPER));
        assertFalse(early.peers().get(0).eligible(LifecycleState.PAPER));
    }

    @Test
    public void crossAlphaConfigIsStrict() {
        PolicyConfig cfg = config();
        // the record refuses a state outside VALIDATING / PAPER / ACTIVE
        for (LifecycleState bad : List.of(LifecycleState.RESEARCH,
                LifecycleState.CANDIDATE, LifecycleState.WATCH, LifecycleState.RETIRED)) {
            rejects("cross_alpha_min_state", () -> withMinState(cfg, bad));
        }
        rejects("cross_alpha_min_state", () -> withMinState(cfg, null));
        for (LifecycleState ok : PolicyConfig.CROSS_ALPHA_MIN_STATES) {
            assertEquals(ok, withMinState(cfg, ok).crossAlphaMinState());
        }
        // the merged view: key order, round trip, strict parse
        Map<String, Object> tree = cfg.toTree();
        assertEquals(List.of("policy", "tstat_threshold", "cross_alpha_min_state",
                "net_pnl_ci_gate", "gates", "demotion", "live"),
                new ArrayList<>(tree.keySet()));
        List<String> gateKeys = new ArrayList<>(
                com.iap.config.Json.object(tree.get("gates")).keySet());
        assertEquals(List.of("max_kill_events", "max_cross_alpha_correlation",
                "min_net_pnl_ci_low", "net_pnl_ci_level"),
                gateKeys.subList(gateKeys.size() - 4, gateKeys.size()));
        assertEquals(tree, PolicyConfig.fromTree(tree).toTree());
        assertEquals(cfg, PolicyConfig.fromTree(tree));
        rejects("cross_alpha_min_state",
                () -> PolicyConfig.fromTree(configTree(cfg, "CANDIDATE", 0.7)));
        rejects("cross_alpha_min_state",
                () -> PolicyConfig.fromTree(configTree(cfg, "validating", 0.7)));
        rejects("cross_alpha_min_state",
                () -> PolicyConfig.fromTree(configTree(cfg, 2L, 0.7)));
        rejects("cross_alpha_min_state",
                () -> PolicyConfig.fromTree(configTree(cfg, null, 0.7)));
        rejects("max_cross_alpha_correlation",
                () -> PolicyConfig.fromTree(configTree(cfg, "VALIDATING", 1.5)));
        rejects("max_cross_alpha_correlation",
                () -> PolicyConfig.fromTree(configTree(cfg, "VALIDATING", -0.1)));
        rejects("max_cross_alpha_correlation",
                () -> PolicyConfig.fromTree(configTree(cfg, "VALIDATING", "0.7")));
        assertEquals(LifecycleState.ACTIVE, PolicyConfig.fromTree(
                configTree(cfg, "ACTIVE", 1.0)).crossAlphaMinState());
        assertEquals(0.0, PolicyConfig.fromTree(configTree(cfg, "PAPER", 0.0))
                .gates().maxCrossAlphaCorrelation(), 0.0);
        Map<String, Object> noState = configTree(cfg, "VALIDATING", 0.7);
        noState.remove("cross_alpha_min_state");
        rejects("cross_alpha_min_state", () -> PolicyConfig.fromTree(noState));
        Map<String, Object> noThreshold = configTree(cfg, "VALIDATING", 0.7);
        com.iap.config.Json.object(noThreshold.get("gates"))
                .remove("max_cross_alpha_correlation");
        rejects("max_cross_alpha_correlation", () -> PolicyConfig.fromTree(noThreshold));
        // the loader: both keys are required and an older x-version is refused
        ConfigService service = new ConfigService(Paths.get("..", "configs"));
        Map<String, Object> strategies = service.doc(ConfigService.STRATEGIES);
        Map<String, Object> badState = new LinkedHashMap<>(
                service.doc(ConfigService.LIFECYCLE));
        badState.put("cross_alpha_min_state", "CANDIDATE");
        rejects("strategies/lifecycle.json.cross_alpha_min_state",
                () -> PolicyConfig.fromDocs(badState, ConfigService.LIFECYCLE, strategies,
                        ConfigService.STRATEGIES));
        Map<String, Object> missingState = new LinkedHashMap<>(
                service.doc(ConfigService.LIFECYCLE));
        missingState.remove("cross_alpha_min_state");
        rejects("cross_alpha_min_state",
                () -> PolicyConfig.fromDocs(missingState, ConfigService.LIFECYCLE,
                        strategies, ConfigService.STRATEGIES));
        Map<String, Object> missingGate = new LinkedHashMap<>(
                service.doc(ConfigService.LIFECYCLE));
        Map<String, Object> gates = new LinkedHashMap<>(
                com.iap.config.Json.object(missingGate.get("gates")));
        gates.remove("max_cross_alpha_correlation");
        missingGate.put("gates", gates);
        rejects("max_cross_alpha_correlation",
                () -> PolicyConfig.fromDocs(missingGate, ConfigService.LIFECYCLE,
                        strategies, ConfigService.STRATEGIES));
        Map<String, Object> oldVersion = new LinkedHashMap<>(
                service.doc(ConfigService.LIFECYCLE));
        oldVersion.put("x-version", 2L);
        rejects("x-version",
                () -> PolicyConfig.fromDocs(oldVersion, ConfigService.LIFECYCLE, strategies,
                        ConfigService.STRATEGIES));
    }

    /** {@code cfg}'s merged view with the two correlation-gate values replaced. */
    private static Map<String, Object> configTree(PolicyConfig cfg, Object minState,
            Object threshold) {
        Map<String, Object> tree = cfg.toTree();
        tree.put("cross_alpha_min_state", minState);
        com.iap.config.Json.object(tree.get("gates"))
                .put("max_cross_alpha_correlation", threshold);
        return tree;
    }

    private static Map<String, Object> peerTree(Object alphaId, Object state,
            Object correlation) {
        Map<String, Object> t = new LinkedHashMap<>();
        t.put("alpha_id", alphaId);
        t.put("state", state);
        t.put("correlation", correlation);
        return t;
    }

    /** An evidence document whose {@code cross_alpha.peers} are {@code peerTrees}. */
    private static Map<String, Object> evidenceTree(List<Object> peerTrees) {
        Map<String, Object> cross = new LinkedHashMap<>();
        cross.put("peers", peerTrees);
        Map<String, Object> t = Evidence.empty().toTree();
        t.put("cross_alpha", cross);
        return t;
    }

    @Test
    public void crossAlphaEvidenceIsStrict() {
        // the key is always on the wire, after significance_threshold
        Map<String, Object> empty = Evidence.empty().toTree();
        assertEquals(List.of("research", "capacity_usd", "significance_threshold",
                "pnl_bootstrap", "cross_alpha", "validation", "paper", "live"),
                new ArrayList<>(empty.keySet()));
        assertNull(empty.get("cross_alpha"));
        assertNull(Evidence.fromTree(empty).crossAlpha());
        // a document without the key is rejected, not defaulted
        Map<String, Object> old = Evidence.empty().toTree();
        old.remove("cross_alpha");
        rejects("cross_alpha", () -> Evidence.fromTree(old));
        // round trip, peers in order
        Map<String, Object> doc = evidenceTree(List.of(
                peerTree("A1", "ACTIVE", -0.9), peerTree("A2", "CANDIDATE", 0.95)));
        Evidence ev = Evidence.fromTree(doc);
        assertEquals(peers(peer("A1", LifecycleState.ACTIVE, -0.9),
                peer("A2", LifecycleState.CANDIDATE, 0.95)), ev.crossAlpha());
        assertEquals(doc, ev.toTree());
        Map<String, Object> none = evidenceTree(List.of());
        assertEquals(Evidence.CrossAlpha.noPeers(), Evidence.fromTree(none).crossAlpha());
        assertEquals(none, Evidence.fromTree(none).toTree());
        // an integer correlation is a number; the bounds themselves are allowed
        assertEquals(1.0, Evidence.fromTree(evidenceTree(List.of(
                peerTree("A", "ACTIVE", -1.0), peerTree("B", "ACTIVE", 1L))))
                .crossAlpha().maxAbsCorrelation(LifecycleState.ACTIVE), 0.0);
        // the block: exactly the key "peers", an array
        Map<String, Object> notObject = Evidence.empty().toTree();
        notObject.put("cross_alpha", List.of());
        rejects("cross_alpha", () -> Evidence.fromTree(notObject));
        Map<String, Object> noPeersKey = Evidence.empty().toTree();
        noPeersKey.put("cross_alpha", new LinkedHashMap<String, Object>());
        rejects("peers", () -> Evidence.fromTree(noPeersKey));
        Map<String, Object> extraKey = evidenceTree(List.of());
        com.iap.config.Json.object(extraKey.get("cross_alpha")).put("x", 1L);
        rejects("unknown key 'x'", () -> Evidence.fromTree(extraKey));
        Map<String, Object> peersNotArray = evidenceTree(List.of());
        com.iap.config.Json.object(peersNotArray.get("cross_alpha"))
                .put("peers", new LinkedHashMap<String, Object>());
        rejects("cross_alpha.peers", () -> Evidence.fromTree(peersNotArray));
        rejects("cross_alpha.peers[]",
                () -> Evidence.fromTree(evidenceTree(List.of("A1"))));
        // a peer: exactly its three keys, each of its type and range
        Map<String, Object> shortPeer = peerTree("A1", "ACTIVE", 0.1);
        shortPeer.remove("correlation");
        rejects("missing key 'correlation'",
                () -> Evidence.fromTree(evidenceTree(List.of(shortPeer))));
        Map<String, Object> longPeer = peerTree("A1", "ACTIVE", 0.1);
        longPeer.put("x", 1L);
        rejects("unknown key 'x'",
                () -> Evidence.fromTree(evidenceTree(List.of(longPeer))));
        rejects("state", () -> Evidence.fromTree(evidenceTree(List.of(
                peerTree("A1", "LIVE", 0.1)))));
        rejects("state", () -> Evidence.fromTree(evidenceTree(List.of(
                peerTree("A1", 4L, 0.1)))));
        rejects("alpha_id", () -> Evidence.fromTree(evidenceTree(List.of(
                peerTree("", "ACTIVE", 0.1)))));
        rejects("alpha_id", () -> Evidence.fromTree(evidenceTree(List.of(
                peerTree(7L, "ACTIVE", 0.1)))));
        rejects("correlation", () -> Evidence.fromTree(evidenceTree(List.of(
                peerTree("A1", "ACTIVE", 1.5)))));
        rejects("correlation", () -> Evidence.fromTree(evidenceTree(List.of(
                peerTree("A1", "ACTIVE", -1.01)))));
        rejects("correlation", () -> Evidence.fromTree(evidenceTree(List.of(
                peerTree("A1", "ACTIVE", "0.1")))));
        rejects("correlation", () -> Evidence.fromTree(evidenceTree(List.of(
                peerTree("A1", "ACTIVE", Double.NaN)))));
        // strictly increasing ids: unsorted and duplicate lists are rejected
        rejects("sorted by alpha_id", () -> Evidence.fromTree(evidenceTree(List.of(
                peerTree("B", "ACTIVE", 0.1), peerTree("A", "ACTIVE", 0.1)))));
        rejects("sorted by alpha_id", () -> Evidence.fromTree(evidenceTree(List.of(
                peerTree("A", "ACTIVE", 0.1), peerTree("A", "PAPER", 0.2)))));
        // the records guard themselves too
        rejects("sorted by alpha_id", () -> peers(peer("B", LifecycleState.ACTIVE, 0.1),
                peer("A", LifecycleState.ACTIVE, 0.1)));
        rejects("alpha_id", () -> peer("", LifecycleState.ACTIVE, 0.1));
        rejects("state", () -> peer("A", null, 0.1));
        rejects("correlation", () -> peer("A", LifecycleState.ACTIVE, 1.0000001));
        rejects("correlation",
                () -> peer("A", LifecycleState.ACTIVE, Double.POSITIVE_INFINITY));
        rejects("peers", () -> new Evidence.CrossAlpha(null));
    }

    // ---------------------------------------------- the net P&L bootstrap gate

    private static final String BOOTSTRAP_GATE = "net_pnl_bootstrap_ci";

    /** An interval of 626 bars at level 0.95 with these bounds and trade count. */
    private static Evidence.PnlBootstrap bootstrap(Double ciLow, Double ciHigh,
            long nTrades) {
        return new Evidence.PnlBootstrap(ciLow, ciHigh, 0.95, 1000, 20260829L, 9.0, 626,
                nTrades);
    }

    /** CANDIDATE evidence that passes every gate but, possibly, the bootstrap one. */
    private static Evidence bootstrapEvidence(String id, Evidence.PnlBootstrap boot) {
        return new Evidence(research(id, 0.02, 0.03, 4.0, 5.0, true, true), 5e6,
                THRESHOLD, boot, Evidence.CrossAlpha.noPeers(), null, null, null);
    }

    /**
     * Evaluate {@code boot} at CANDIDATE under the default policy and expect a
     * HOLD on the bootstrap gate alone; returns the gate's result.
     */
    private static GateResult heldByBootstrap(Evidence.PnlBootstrap boot) {
        AlphaLifecycle m = machineAt(LifecycleState.CANDIDATE, "BX");
        assertNull(m.advance("BX", 10L, bootstrapEvidence("BX", boot)));
        GateEvaluation eval = m.evaluations().get(m.evaluations().size() - 1);
        assertEquals(GateEvaluation.Outcome.HOLD, eval.outcome());
        assertEquals(List.of(BOOTSTRAP_GATE), eval.failedGates());
        List<String> order = new ArrayList<>(eval.gates().keySet());
        assertEquals(11, order.size());
        assertEquals("net_pnl_after_costs", order.get(6));
        assertEquals(BOOTSTRAP_GATE, order.get(7));
        assertEquals("capacity", order.get(8));
        assertEquals(LifecycleState.CANDIDATE, m.state("BX"));
        assertEquals(0, m.record("BX").consecutiveFailures());
        GateResult r = eval.gates().get(BOOTSTRAP_GATE);
        assertFalse(r.passed());
        assertEquals(0.0, r.threshold(), 0.0);
        return r;
    }

    @Test
    public void bootstrapGateRowIsPinned() {
        Gates gate = Gates.NET_PNL_BOOTSTRAP_CI;
        assertEquals(Gates.NET_PNL_AFTER_COSTS.ordinal() + 1, gate.ordinal());
        assertEquals(gate.ordinal() + 1, Gates.CAPACITY.ordinal());
        assertEquals(BOOTSTRAP_GATE, gate.gateName());
        assertEquals("pnl_bootstrap", gate.block());
        assertEquals(Gates.Kind.GT, gate.kind());
        assertEquals("min_net_pnl_ci_low", gate.thresholdKey());
        assertEquals(gate, Gates.byName(BOOTSTRAP_GATE));
        PolicyConfig cfg = config();
        assertEquals(PolicyConfig.NET_PNL_CI_REQUIRED, cfg.netPnlCiGate());
        assertTrue(cfg.netPnlCiRequired());
        assertEquals(0.0, cfg.gates().minNetPnlCiLow(), 0.0);
        assertEquals(0.95, cfg.gates().netPnlCiLevel(), 0.0);
        assertEquals(0.0, gate.threshold(cfg), 0.0);
        // the statistic itself, without the machine
        assertEquals(12.5, PASSING_BOOTSTRAP.gateValue(0.95), 0.0);
        assertNull(PASSING_BOOTSTRAP.gateValue(0.9));
        assertNull(bootstrap(12.5, 90.0, 0).gateValue(0.95));
        assertNull(bootstrap(null, null, 40).gateValue(0.95));
    }

    @Test
    public void bootstrapMissingBlockFailsClosed() {
        assertNull(heldByBootstrap(null).value());
    }

    @Test
    public void bootstrapIntervalSpanningZeroFailsAtItsValue() {
        assertEquals(-35.5, heldByBootstrap(bootstrap(-35.5, 60.25, 40)).value(), 0.0);
    }

    @Test
    public void bootstrapUnusableIntervalIsNoEvidenceForTheGate() {
        // no trade in any fold: the degenerate [0, 0] interval is not evidence
        assertNull(heldByBootstrap(bootstrap(0.0, 0.0, 0)).value());
        // a positive interval of an untraded alpha is not evidence either
        assertNull(heldByBootstrap(bootstrap(12.5, 90.0, 0)).value());
        // too few bars: no bounds
        assertNull(heldByBootstrap(new Evidence.PnlBootstrap(null, null, 0.95, 1000,
                20260829L, 9.0, 5, 40)).value());
        // taken at another level than the policy's 0.95
        assertNull(heldByBootstrap(new Evidence.PnlBootstrap(12.5, 90.0, 0.9, 1000,
                20260829L, 9.0, 626, 40)).value());
    }

    @Test
    public void bootstrapLowerBoundIsComparedStrictly() {
        assertEquals(0.0, heldByBootstrap(bootstrap(0.0, 45.0, 40)).value(), 0.0);
        AlphaLifecycle m = machineAt(LifecycleState.CANDIDATE, "BX");
        LifecycleTransition t = m.advance("BX", 10L,
                bootstrapEvidence("BX", PASSING_BOOTSTRAP));
        assertNotNull(t);
        assertEquals(LifecycleState.VALIDATING, t.toState());
        assertEquals("all 11 gates passed: CANDIDATE -> VALIDATING", t.reason());
        assertEquals(11, t.gates().size());
        GateResult r = t.gates().get(BOOTSTRAP_GATE);
        assertTrue(r.passed());
        assertEquals(12.5, r.value(), 0.0);
        assertEquals(0.0, r.threshold(), 0.0);
    }

    @Test
    public void bootstrapGateIsNotEvaluatedUnderTheAbsentPolicy() {
        PolicyConfig absent = legacy(config());
        assertEquals(PolicyConfig.NET_PNL_CI_ABSENT, absent.netPnlCiGate());
        assertFalse(absent.netPnlCiRequired());
        // a null block, and a block that would fail, are both promoted on ten gates
        List<Evidence> candidates = List.of(bootstrapEvidence("BX", null),
                bootstrapEvidence("BX", bootstrap(-35.5, 60.25, 40)));
        for (Evidence candidate : candidates) {
            AlphaLifecycle m = machineAt(LifecycleState.CANDIDATE, "BX", absent);
            LifecycleTransition t = m.advance("BX", 10L, candidate);
            assertNotNull(t);
            assertEquals(LifecycleState.VALIDATING, t.toState());
            assertEquals("all 10 gates passed: CANDIDATE -> VALIDATING", t.reason());
            assertEquals(10, t.gates().size());
            assertFalse(t.gates().containsKey(BOOTSTRAP_GATE));
            GateEvaluation eval = m.evaluations().get(m.evaluations().size() - 1);
            List<String> order = new ArrayList<>(eval.gates().keySet());
            assertEquals(10, order.size());
            assertFalse(order.contains(BOOTSTRAP_GATE));
            assertEquals("net_pnl_after_costs", order.get(6));
            assertEquals("capacity", order.get(7));
        }
        // a failed evaluation under the absent policy does not list the gate
        AlphaLifecycle held = machineAt(LifecycleState.CANDIDATE, "BX", absent);
        assertNull(held.advance("BX", 10L, new Evidence(
                research("BX", 0.02, 0.03, 4.0, 5.0, true, true), 1.0, THRESHOLD, null,
                Evidence.CrossAlpha.noPeers(), null, null, null)));
        GateEvaluation eval = held.evaluations().get(held.evaluations().size() - 1);
        assertEquals(List.of("capacity"), eval.failedGates());
        assertEquals(10, eval.gates().size());
        // the transition table is the same under either policy: eleven gates
        Edge edge = AlphaLifecycle.promotionEdge(LifecycleState.CANDIDATE);
        assertEquals(11, edge.gates().size());
        assertEquals(Gates.NET_PNL_BOOTSTRAP_CI, edge.gates().get(7));
        AlphaLifecycle required = machineAt(LifecycleState.CANDIDATE, "BX");
        assertEquals(edge.gates(), required.edgeGates(edge));
        List<Gates> evaluated = held.edgeGates(edge);
        assertEquals(10, evaluated.size());
        assertFalse(evaluated.contains(Gates.NET_PNL_BOOTSTRAP_CI));
        // the other edges are evaluated whole under either policy
        for (Edge e : AlphaLifecycle.ALLOWED_TRANSITIONS) {
            if (e != edge) {
                assertEquals(e.gates(), held.edgeGates(e));
            }
        }
        // the legacy rules with the gate required hold the null block
        PolicyConfig strict = new PolicyConfig(absent.policy(), absent.gates(),
                absent.maxConsecutiveFailures(), absent.live(), absent.tstatThreshold(),
                absent.crossAlphaMinState(), PolicyConfig.NET_PNL_CI_REQUIRED);
        AlphaLifecycle m = machineAt(LifecycleState.CANDIDATE, "BX", strict);
        assertNull(m.advance("BX", 10L, bootstrapEvidence("BX", null)));
        assertEquals(List.of(BOOTSTRAP_GATE),
                m.evaluations().get(m.evaluations().size() - 1).failedGates());
    }

    /** {@code cfg}'s merged view with one top-level value replaced. */
    private static Map<String, Object> withKey(PolicyConfig cfg, String key, Object value) {
        Map<String, Object> tree = cfg.toTree();
        tree.put(key, value);
        return tree;
    }

    /** {@code cfg}'s merged view with one gate threshold replaced. */
    private static Map<String, Object> withGate(PolicyConfig cfg, String key, Object value) {
        Map<String, Object> tree = cfg.toTree();
        com.iap.config.Json.object(tree.get("gates")).put(key, value);
        return tree;
    }

    @Test
    public void bootstrapConfigIsStrict() {
        PolicyConfig cfg = config();
        // the record refuses an unknown policy name
        rejects("net_pnl_ci_gate", () -> new PolicyConfig(cfg.policy(), cfg.gates(),
                cfg.maxConsecutiveFailures(), cfg.live(), cfg.tstatThreshold(),
                cfg.crossAlphaMinState(), "optional"));
        rejects("net_pnl_ci_gate", () -> new PolicyConfig(cfg.policy(), cfg.gates(),
                cfg.maxConsecutiveFailures(), cfg.live(), cfg.tstatThreshold(),
                cfg.crossAlphaMinState(), null));
        // the merged view: strict parse, round trip under either name
        rejects("net_pnl_ci_gate",
                () -> PolicyConfig.fromTree(withKey(cfg, "net_pnl_ci_gate", "optional")));
        rejects("net_pnl_ci_gate",
                () -> PolicyConfig.fromTree(withKey(cfg, "net_pnl_ci_gate", "Required")));
        rejects("net_pnl_ci_gate",
                () -> PolicyConfig.fromTree(withKey(cfg, "net_pnl_ci_gate", true)));
        rejects("net_pnl_ci_gate",
                () -> PolicyConfig.fromTree(withKey(cfg, "net_pnl_ci_gate", null)));
        Map<String, Object> noPolicy = cfg.toTree();
        noPolicy.remove("net_pnl_ci_gate");
        rejects("net_pnl_ci_gate", () -> PolicyConfig.fromTree(noPolicy));
        for (String name : List.of(PolicyConfig.NET_PNL_CI_REQUIRED,
                PolicyConfig.NET_PNL_CI_ABSENT)) {
            Map<String, Object> tree = withKey(cfg, "net_pnl_ci_gate", name);
            assertEquals(name, PolicyConfig.fromTree(tree).netPnlCiGate());
            assertEquals(tree, PolicyConfig.fromTree(tree).toTree());
        }
        for (double bad : new double[] {1.0, 0.0, 1.5, -0.1}) {
            rejects("net_pnl_ci_level",
                    () -> PolicyConfig.fromTree(withGate(cfg, "net_pnl_ci_level", bad)));
        }
        rejects("net_pnl_ci_level",
                () -> PolicyConfig.fromTree(withGate(cfg, "net_pnl_ci_level", "0.95")));
        rejects("min_net_pnl_ci_low",
                () -> PolicyConfig.fromTree(withGate(cfg, "min_net_pnl_ci_low", "0.0")));
        assertEquals(-250.0, PolicyConfig.fromTree(
                withGate(cfg, "min_net_pnl_ci_low", -250.0)).gates().minNetPnlCiLow(), 0.0);
        for (String key : List.of("min_net_pnl_ci_low", "net_pnl_ci_level")) {
            Map<String, Object> tree = cfg.toTree();
            com.iap.config.Json.object(tree.get("gates")).remove(key);
            rejects(key, () -> PolicyConfig.fromTree(tree));
        }
        // the loader: the policy and both thresholds are required
        ConfigService service = new ConfigService(Paths.get("..", "configs"));
        Map<String, Object> strategies = service.doc(ConfigService.STRATEGIES);
        Map<String, Object> badPolicy = new LinkedHashMap<>(
                service.doc(ConfigService.LIFECYCLE));
        badPolicy.put("net_pnl_ci_gate", "optional");
        rejects("strategies/lifecycle.json.net_pnl_ci_gate",
                () -> PolicyConfig.fromDocs(badPolicy, ConfigService.LIFECYCLE, strategies,
                        ConfigService.STRATEGIES));
        Map<String, Object> missingPolicy = new LinkedHashMap<>(
                service.doc(ConfigService.LIFECYCLE));
        missingPolicy.remove("net_pnl_ci_gate");
        rejects("net_pnl_ci_gate",
                () -> PolicyConfig.fromDocs(missingPolicy, ConfigService.LIFECYCLE,
                        strategies, ConfigService.STRATEGIES));
        for (String key : List.of("min_net_pnl_ci_low", "net_pnl_ci_level")) {
            Map<String, Object> doc = new LinkedHashMap<>(
                    service.doc(ConfigService.LIFECYCLE));
            Map<String, Object> gates = new LinkedHashMap<>(
                    com.iap.config.Json.object(doc.get("gates")));
            gates.remove(key);
            doc.put("gates", gates);
            rejects(key, () -> PolicyConfig.fromDocs(doc, ConfigService.LIFECYCLE,
                    strategies, ConfigService.STRATEGIES));
        }
    }

    /** An evidence document whose {@code pnl_bootstrap} has one value replaced. */
    private static Map<String, Object> bootstrapTree(String key, Object value) {
        Map<String, Object> t = bootstrapEvidence("BX", PASSING_BOOTSTRAP).toTree();
        com.iap.config.Json.object(t.get("pnl_bootstrap")).put(key, value);
        return t;
    }

    @Test
    public void bootstrapEvidenceIsStrict() {
        // the key is always on the wire; null when there is no interval
        Map<String, Object> empty = Evidence.empty().toTree();
        assertTrue(empty.containsKey("pnl_bootstrap"));
        assertNull(empty.get("pnl_bootstrap"));
        assertNull(Evidence.fromTree(empty).pnlBootstrap());
        // a document without the key is rejected, not defaulted
        Map<String, Object> old = Evidence.empty().toTree();
        old.remove("pnl_bootstrap");
        rejects("pnl_bootstrap", () -> Evidence.fromTree(old));
        // round trip, keys in order
        Map<String, Object> doc = bootstrapEvidence("BX", PASSING_BOOTSTRAP).toTree();
        assertEquals(List.of("ci_low", "ci_high", "level", "n_resamples", "seed",
                "mean_block", "n_bars", "n_trades"), new ArrayList<>(
                com.iap.config.Json.object(doc.get("pnl_bootstrap")).keySet()));
        Evidence back = Evidence.fromTree(doc);
        assertEquals(PASSING_BOOTSTRAP, back.pnlBootstrap());
        assertEquals(doc, back.toTree());
        Map<String, Object> unbounded = bootstrapEvidence("BX",
                bootstrap(null, null, 40)).toTree();
        assertEquals(unbounded, Evidence.fromTree(unbounded).toTree());
        // the bounds: both numbers or both null, ordered
        rejects("both numbers or both null",
                () -> Evidence.fromTree(bootstrapTree("ci_low", null)));
        rejects("both numbers or both null",
                () -> Evidence.fromTree(bootstrapTree("ci_high", null)));
        rejects("ci_high < ci_low", () -> Evidence.fromTree(bootstrapTree("ci_high", 12.0)));
        rejects("ci_low", () -> Evidence.fromTree(bootstrapTree("ci_low", "12.5")));
        rejects("ci_low", () -> Evidence.fromTree(bootstrapTree("ci_low", Double.NaN)));
        // level in (0, 1); at least one resample; counts and block in range
        rejects("level", () -> Evidence.fromTree(bootstrapTree("level", 1.0)));
        rejects("level", () -> Evidence.fromTree(bootstrapTree("level", 0.0)));
        rejects("n_resamples", () -> Evidence.fromTree(bootstrapTree("n_resamples", 0L)));
        rejects("n_resamples",
                () -> Evidence.fromTree(bootstrapTree("n_resamples", 1000.0)));
        rejects("seed", () -> Evidence.fromTree(bootstrapTree("seed", -1L)));
        rejects("mean_block", () -> Evidence.fromTree(bootstrapTree("mean_block", 0.5)));
        rejects("n_bars", () -> Evidence.fromTree(bootstrapTree("n_bars", -1L)));
        rejects("n_trades", () -> Evidence.fromTree(bootstrapTree("n_trades", -1L)));
        // exactly its eight keys
        for (String key : List.of("ci_low", "ci_high", "level", "n_resamples", "seed",
                "mean_block", "n_bars", "n_trades")) {
            Map<String, Object> t = bootstrapEvidence("BX", PASSING_BOOTSTRAP).toTree();
            com.iap.config.Json.object(t.get("pnl_bootstrap")).remove(key);
            rejects("missing key '" + key + "'", () -> Evidence.fromTree(t));
        }
        rejects("unknown key 'x'", () -> Evidence.fromTree(bootstrapTree("x", 1L)));
        Map<String, Object> notObject = Evidence.empty().toTree();
        notObject.put("pnl_bootstrap", List.of());
        rejects("pnl_bootstrap", () -> Evidence.fromTree(notObject));
        // the record guards itself too
        rejects("both numbers or both null", () -> bootstrap(12.5, null, 40));
        rejects("ci_high < ci_low", () -> bootstrap(12.5, 12.0, 40));
        rejects("level", () -> new Evidence.PnlBootstrap(12.5, 90.0, 1.0, 1000,
                20260829L, 9.0, 626, 40));
        rejects("n_resamples", () -> new Evidence.PnlBootstrap(12.5, 90.0, 0.95, 0,
                20260829L, 9.0, 626, 40));
        rejects("mean_block", () -> new Evidence.PnlBootstrap(12.5, 90.0, 0.95, 1000,
                20260829L, 0.5, 626, 40));
        rejects("n_trades", () -> bootstrap(12.5, 90.0, -1));
    }
}
