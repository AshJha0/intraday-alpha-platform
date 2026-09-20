package com.iap.lifecycle;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

import com.iap.adaptive.LifecycleGauge;
import com.iap.contracts.CanonicalJson;
import com.iap.contracts.PyFormat;
import com.iap.lifecycle.GateEvaluation.Outcome;
import com.iap.lifecycle.LifecycleTransition.Actor;

/**
 * The alpha promotion state machine ({@code iap.lifecycle.machine.AlphaLifecycle}):
 * <pre>
 * RESEARCH(0) -> CANDIDATE(1) -> VALIDATING(2) -> PAPER(3) -> ACTIVE(4)
 *                                                  ACTIVE(4) &lt;-&gt; WATCH(5) -> RETIRED(6)
 * </pre>
 * The transition table {@link #ALLOWED_TRANSITIONS} is data: one {@link Edge}
 * per allowed move with its kind, the actor that may take it and the gates it
 * evaluates, in pinned order. {@link #advance} looks up the SYSTEM edge
 * leaving the current state, evaluates its gates against the {@link Evidence}
 * and applies the outcome:
 * <ul>
 *   <li><b>promotion</b> (RESEARCH..PAPER): every gate passes ⇒ one state up,
 *       failure counter reset;</li>
 *   <li><b>demotion</b>: at CANDIDATE a failed {@code leakage_clean} demotes
 *       to RESEARCH at once; at VALIDATING / PAPER each failed evaluation
 *       increments {@code consecutive_failures} and the
 *       {@code max_consecutive_failures}-th one demotes to CANDIDATE;</li>
 *   <li><b>silence is not evidence</b>: an evaluation whose evidence block
 *       for the edge is absent ({@code research} at CANDIDATE,
 *       {@code validation} at VALIDATING, {@code paper} at PAPER,
 *       {@code live} / null or uninformative rolling IC at ACTIVE / WATCH)
 *       evaluates nothing and moves nothing — not even the failure counter
 *       ({@code NO_EVIDENCE}). RESEARCH is the exception by construction:
 *       its {@code ledger_entry_exists} gate IS the presence check;</li>
 *   <li><b>live</b> (ACTIVE / WATCH): delegated unchanged to the pinned
 *       {@link LifecycleGauge} rules (hysteresis, entering breach counts);
 *       its transition is wrapped into a {@link LifecycleTransition} with
 *       {@code gates = {"rolling_ic": ...}} and the policy name;</li>
 *   <li><b>RETIRED is terminal for SYSTEM</b>: a SYSTEM {@code advance} on a
 *       RETIRED alpha records {@code TERMINAL} and returns {@code null};
 *       re-entry is the HUMAN {@link #resetToResearch}, which re-runs the
 *       whole evidence chain.</li>
 * </ul>
 * Manual edges ({@link #retire}, {@link #resetToResearch}) require
 * {@link Actor#HUMAN} and a non-empty reason. Every call is a pure function
 * of the registry state and its arguments — no wall clock, no RNG, sorted
 * iteration only.
 */
public final class AlphaLifecycle {
    /** Why an edge exists. */
    public enum EdgeKind {
        PROMOTION, DEMOTION, LIVE, MANUAL
    }

    /** One allowed transition: who may take it and which gates it evaluates. */
    public record Edge(LifecycleState fromState, LifecycleState toState, EdgeKind kind,
            Actor actor, List<Gates> gates) {
        public Edge {
            if (fromState == toState) {
                throw new IllegalArgumentException("Edge: from_state == to_state");
            }
            gates = List.copyOf(gates);
        }

        /** JSON-ready row ({@code transition_table} in the golden). */
        public Map<String, Object> toTree() {
            Map<String, Object> t = new LinkedHashMap<>();
            t.put("from_state", fromState.name());
            t.put("to_state", toState.name());
            t.put("kind", kind.name());
            t.put("actor", actor.name());
            List<Object> names = new ArrayList<>(gates.size());
            for (Gates g : gates) {
                names.add(g.gateName());
            }
            t.put("gates", names);
            return t;
        }
    }

    private static Edge edge(LifecycleState from, LifecycleState to, EdgeKind kind,
            Actor actor, Gates... gates) {
        return new Edge(from, to, kind, actor, List.of(gates));
    }

    /** The transition table (pinned order; 17 edges). */
    public static final List<Edge> ALLOWED_TRANSITIONS = List.of(
            edge(LifecycleState.RESEARCH, LifecycleState.CANDIDATE, EdgeKind.PROMOTION,
                    Actor.SYSTEM, Gates.LEDGER_ENTRY_EXISTS, Gates.LEAKAGE_CLEAN),
            edge(LifecycleState.CANDIDATE, LifecycleState.VALIDATING, EdgeKind.PROMOTION,
                    Actor.SYSTEM, Gates.LEAKAGE_CLEAN, Gates.OOS_IC,
                    Gates.STATISTICAL_SIGNIFICANCE, Gates.FOLD_CONSISTENCY,
                    Gates.FOLD_COUNT, Gates.HYPOTHESIS_SIGN, Gates.NET_PNL_AFTER_COSTS,
                    Gates.CAPACITY, Gates.STABILITY),
            edge(LifecycleState.CANDIDATE, LifecycleState.RESEARCH, EdgeKind.DEMOTION,
                    Actor.SYSTEM, Gates.LEAKAGE_CLEAN),
            edge(LifecycleState.VALIDATING, LifecycleState.PAPER, EdgeKind.PROMOTION,
                    Actor.SYSTEM, Gates.HOLDOUT_IC_TRACKS_RESEARCH,
                    Gates.REPLAY_REPRODUCIBLE, Gates.CROSS_LANGUAGE_PARITY),
            edge(LifecycleState.VALIDATING, LifecycleState.CANDIDATE, EdgeKind.DEMOTION,
                    Actor.SYSTEM),
            edge(LifecycleState.PAPER, LifecycleState.ACTIVE, EdgeKind.PROMOTION,
                    Actor.SYSTEM, Gates.PAPER_MIN_SESSIONS, Gates.PAPER_IC_TRACKING,
                    Gates.PAPER_NET_PNL, Gates.NO_KILL_EVENTS),
            edge(LifecycleState.PAPER, LifecycleState.CANDIDATE, EdgeKind.DEMOTION,
                    Actor.SYSTEM),
            edge(LifecycleState.ACTIVE, LifecycleState.WATCH, EdgeKind.LIVE, Actor.SYSTEM,
                    Gates.ROLLING_IC),
            edge(LifecycleState.WATCH, LifecycleState.ACTIVE, EdgeKind.LIVE, Actor.SYSTEM,
                    Gates.ROLLING_IC),
            edge(LifecycleState.WATCH, LifecycleState.RETIRED, EdgeKind.LIVE, Actor.SYSTEM,
                    Gates.ROLLING_IC),
            edge(LifecycleState.RESEARCH, LifecycleState.RETIRED, EdgeKind.MANUAL, Actor.HUMAN),
            edge(LifecycleState.CANDIDATE, LifecycleState.RETIRED, EdgeKind.MANUAL, Actor.HUMAN),
            edge(LifecycleState.VALIDATING, LifecycleState.RETIRED, EdgeKind.MANUAL, Actor.HUMAN),
            edge(LifecycleState.PAPER, LifecycleState.RETIRED, EdgeKind.MANUAL, Actor.HUMAN),
            edge(LifecycleState.ACTIVE, LifecycleState.RETIRED, EdgeKind.MANUAL, Actor.HUMAN),
            edge(LifecycleState.WATCH, LifecycleState.RETIRED, EdgeKind.MANUAL, Actor.HUMAN),
            edge(LifecycleState.RETIRED, LifecycleState.RESEARCH, EdgeKind.MANUAL, Actor.HUMAN));

    /** The table entry for {@code (from, to, kind)}; none is an {@link IllegalArgumentException}. */
    public static Edge edgeFor(LifecycleState from, LifecycleState to, EdgeKind kind) {
        for (Edge e : ALLOWED_TRANSITIONS) {
            if (e.fromState() == from && e.toState() == to && e.kind() == kind) {
                return e;
            }
        }
        throw new IllegalArgumentException("no " + kind + " edge " + from + " -> " + to);
    }

    /** The SYSTEM promotion edge leaving a pre-live state. */
    public static Edge promotionEdge(LifecycleState from) {
        for (Edge e : ALLOWED_TRANSITIONS) {
            if (e.fromState() == from && e.kind() == EdgeKind.PROMOTION) {
                return e;
            }
        }
        throw new IllegalArgumentException("no promotion edge leaves " + from);
    }

    /** JSON-ready copy of {@link #ALLOWED_TRANSITIONS}. */
    public static List<Object> transitionTable() {
        List<Object> out = new ArrayList<>(ALLOWED_TRANSITIONS.size());
        for (Edge e : ALLOWED_TRANSITIONS) {
            out.add(e.toTree());
        }
        return out;
    }

    private final PolicyConfig config;
    private final AlphaRegistry registry;
    private final LifecycleTransitionLog log;
    private final List<GateEvaluation> evaluations = new ArrayList<>();
    private final List<LifecycleTransition> transitions = new ArrayList<>();
    private final TreeMap<String, LifecycleGauge> gauges =
            new TreeMap<>(CanonicalJson.KEY_ORDER);

    /** The machine over a registry; {@code log} (nullable) receives every transition. */
    public AlphaLifecycle(PolicyConfig config, AlphaRegistry registry,
            LifecycleTransitionLog log) {
        if (!registry.policy().equals(config.policy())) {
            throw new IllegalArgumentException("registry policy '" + registry.policy()
                    + "' != config policy '" + config.policy() + "'");
        }
        this.config = config;
        this.registry = registry;
        this.log = log;
    }

    public AlphaLifecycle(PolicyConfig config, AlphaRegistry registry) {
        this(config, registry, null);
    }

    public PolicyConfig config() {
        return config;
    }

    public AlphaRegistry registry() {
        return registry;
    }

    /** Every evaluation of this instance, in call order. */
    public List<GateEvaluation> evaluations() {
        return evaluations;
    }

    /** Every transition of this instance, in call order. */
    public List<LifecycleTransition> transitions() {
        return transitions;
    }

    // -- registration / queries ---------------------------------------------

    /** Enter {@code alphaId} at RESEARCH as of {@code eventTs}. */
    public AlphaRecord register(String alphaId, long eventTs) {
        return registry.add(AlphaRecord.fresh(alphaId, eventTs, null, null, null, null));
    }

    /** Enter {@code alphaId} at RESEARCH with the research versions it came from. */
    public AlphaRecord register(String alphaId, long eventTs, String experimentId,
            String dataVersion, String featureVersion, String modelVersion) {
        return registry.add(AlphaRecord.fresh(alphaId, eventTs, experimentId,
                dataVersion, featureVersion, modelVersion));
    }

    public LifecycleState state(String alphaId) {
        return registry.get(alphaId).state();
    }

    public AlphaRecord record(String alphaId) {
        return registry.get(alphaId);
    }

    // -- transitions ----------------------------------------------------------

    private LifecycleTransition apply(AlphaRecord rec, LifecycleTransition transition) {
        if (log != null) {
            log.append(transition);
        }
        transitions.add(transition);
        rec.state = transition.toState();
        rec.sinceTs = transition.eventTs();
        rec.lastTransition = transition;
        rec.consecutiveFailures = 0;
        rec.breachCount = 0;
        rec.recoveryCount = 0;
        gauges.remove(rec.alphaId());
        return transition;
    }

    private void recordEvaluation(AlphaRecord rec, GateEvaluation evaluation) {
        evaluations.add(evaluation);
        rec.lastEvaluation = evaluation;
    }

    private LifecycleTransition transition(AlphaRecord rec, Edge edge, long eventTs,
            String reason, Map<String, GateResult> gates, Actor actor) {
        return new LifecycleTransition(rec.alphaId(), edge.fromState(), edge.toState(),
                eventTs, reason, gates, config.policy(), actor);
    }

    /**
     * Evaluate the SYSTEM edge leaving the alpha's current state; returns the
     * transition made or {@code null} (a {@link GateEvaluation} is recorded
     * either way).
     */
    public LifecycleTransition advance(String alphaId, long eventTs, Evidence evidence) {
        if (evidence == null) {
            throw new IllegalArgumentException("advance: evidence is null");
        }
        AlphaRecord rec = registry.get(alphaId);
        LifecycleState state = rec.state();
        if (state == LifecycleState.RETIRED) {
            recordEvaluation(rec, new GateEvaluation(alphaId, eventTs, state,
                    Outcome.TERMINAL, Map.of(), rec.consecutiveFailures, null));
            return null;
        }
        if (state == LifecycleState.ACTIVE || state == LifecycleState.WATCH) {
            return advanceLive(rec, eventTs, evidence);
        }
        return advancePromotion(rec, eventTs, evidence);
    }

    private static boolean blockPresent(LifecycleState state, Evidence ev) {
        return switch (state) {
            case RESEARCH -> true;
            case CANDIDATE -> ev.research() != null;
            case VALIDATING -> ev.validation() != null;
            case PAPER -> ev.paper() != null;
            default -> throw new IllegalStateException("not a promotion state: " + state);
        };
    }

    private LifecycleTransition advancePromotion(AlphaRecord rec, long eventTs,
            Evidence evidence) {
        LifecycleState state = rec.state();
        Edge edge = promotionEdge(state);
        if (!blockPresent(state, evidence)) {
            recordEvaluation(rec, new GateEvaluation(rec.alphaId(), eventTs, state,
                    Outcome.NO_EVIDENCE, Map.of(), rec.consecutiveFailures, null));
            return null;
        }
        Map<String, GateResult> results = new LinkedHashMap<>();
        List<String> failed = new ArrayList<>();
        for (Gates g : edge.gates()) {
            GateResult r = g.evaluate(evidence, config);
            results.put(g.gateName(), r);
            if (!r.passed()) {
                failed.add(g.gateName());
            }
        }
        if (failed.isEmpty()) {
            LifecycleTransition t = transition(rec, edge, eventTs,
                    "all " + edge.gates().size() + " gates passed: " + state.name()
                            + " -> " + edge.toState().name(), results, Actor.SYSTEM);
            apply(rec, t);
            recordEvaluation(rec, new GateEvaluation(rec.alphaId(), eventTs, state,
                    Outcome.TRANSITION, results, 0, t));
            return t;
        }
        if (state == LifecycleState.CANDIDATE
                && failed.contains(Gates.LEAKAGE_CLEAN.gateName())) {
            Edge demote = edgeFor(LifecycleState.CANDIDATE, LifecycleState.RESEARCH,
                    EdgeKind.DEMOTION);
            LifecycleTransition t = transition(rec, demote, eventTs,
                    "leakage_clean failed: a leaking alpha is not a candidate", results,
                    Actor.SYSTEM);
            apply(rec, t);
            recordEvaluation(rec, new GateEvaluation(rec.alphaId(), eventTs, state,
                    Outcome.TRANSITION, results, 0, t));
            return t;
        }
        if (state == LifecycleState.VALIDATING || state == LifecycleState.PAPER) {
            rec.consecutiveFailures++;
            if (rec.consecutiveFailures >= config.maxConsecutiveFailures()) {
                Edge demote = edgeFor(state, LifecycleState.CANDIDATE, EdgeKind.DEMOTION);
                LifecycleTransition t = transition(rec, demote, eventTs,
                        rec.consecutiveFailures + " consecutive failed evaluations (max "
                                + config.maxConsecutiveFailures() + "); failed gates: "
                                + String.join(", ", failed), results, Actor.SYSTEM);
                apply(rec, t);
                recordEvaluation(rec, new GateEvaluation(rec.alphaId(), eventTs, state,
                        Outcome.TRANSITION, results, 0, t));
                return t;
            }
        }
        recordEvaluation(rec, new GateEvaluation(rec.alphaId(), eventTs, state,
                Outcome.HOLD, results, rec.consecutiveFailures, null));
        return null;
    }

    private static LifecycleGauge.State gaugeState(LifecycleState s) {
        return switch (s) {
            case ACTIVE -> LifecycleGauge.State.ACTIVE;
            case WATCH -> LifecycleGauge.State.WATCH;
            case RETIRED -> LifecycleGauge.State.RETIRED;
            default -> throw new IllegalStateException("not a live state: " + s);
        };
    }

    private static LifecycleState liveState(LifecycleGauge.State s) {
        return switch (s) {
            case ACTIVE -> LifecycleState.ACTIVE;
            case WATCH -> LifecycleState.WATCH;
            case RETIRED -> LifecycleState.RETIRED;
        };
    }

    private LifecycleGauge gauge(AlphaRecord rec) {
        LifecycleGauge g = gauges.get(rec.alphaId());
        if (g == null) {
            PolicyConfig.Live live = config.live();
            g = LifecycleGauge.restore(live.watchIcGate(), live.reactivateIcGate(),
                    live.retireBreachEvals(), live.reactivateEvals(),
                    gaugeState(rec.state()), rec.breachCount, rec.recoveryCount);
            gauges.put(rec.alphaId(), g);
        }
        return g;
    }

    /**
     * The tracker's transition reason, verbatim
     * ({@code iap.adaptive.lifecycle.LifecycleTracker}).
     */
    private String liveReason(LifecycleState to, double rollingIc) {
        PolicyConfig.Live live = config.live();
        return switch (to) {
            case WATCH -> "rolling_ic " + PyFormat.fixed(rollingIc, 6) + " < watch gate "
                    + CanonicalJson.floatRepr(live.watchIcGate());
            case RETIRED -> "persistent breach: " + live.retireBreachEvals()
                    + " consecutive evals below watch gate "
                    + CanonicalJson.floatRepr(live.watchIcGate());
            case ACTIVE -> "re-activation: " + live.reactivateEvals()
                    + " consecutive evals >= reactivate gate "
                    + CanonicalJson.floatRepr(live.reactivateIcGate());
            default -> throw new IllegalStateException("not a live target: " + to);
        };
    }

    /**
     * The {@code rolling_ic} gate as it decided a live transition: a breach
     * transition (→ WATCH, → RETIRED) failed the watch gate; a re-activation
     * (→ ACTIVE) passed the reactivate gate.
     */
    private GateResult liveGateResult(LifecycleState to, double rollingIc) {
        if (to == LifecycleState.ACTIVE) {
            return new GateResult(true, rollingIc, config.live().reactivateIcGate());
        }
        return new GateResult(false, rollingIc, config.live().watchIcGate());
    }

    private LifecycleTransition advanceLive(AlphaRecord rec, long eventTs,
            Evidence evidence) {
        LifecycleState state = rec.state();
        Evidence.Live live = evidence.live();
        if (live == null || live.rollingIc() == null || !live.informative()) {
            recordEvaluation(rec, new GateEvaluation(rec.alphaId(), eventTs, state,
                    Outcome.NO_EVIDENCE, Map.of(), rec.consecutiveFailures, null));
            return null;
        }
        LifecycleGauge gauge = gauge(rec);
        double rollingIc = live.rollingIc();
        LifecycleGauge.State before = gauge.state();
        LifecycleGauge.State after = gauge.update(rollingIc, live.informative());
        Map<String, GateResult> gate = new LinkedHashMap<>();
        gate.put(Gates.ROLLING_IC.gateName(), Gates.ROLLING_IC.evaluate(evidence, config));
        if (after == before) {
            rec.breachCount = gauge.breachCount();
            rec.recoveryCount = gauge.recoveryCount();
            recordEvaluation(rec, new GateEvaluation(rec.alphaId(), eventTs, state,
                    Outcome.HOLD, gate, rec.consecutiveFailures, null));
            return null;
        }
        LifecycleState to = liveState(after);
        Edge edge = edgeFor(state, to, EdgeKind.LIVE);
        Map<String, GateResult> decided = new LinkedHashMap<>();
        decided.put(Gates.ROLLING_IC.gateName(), liveGateResult(to, rollingIc));
        LifecycleTransition t = transition(rec, edge, eventTs, liveReason(to, rollingIc),
                decided, Actor.SYSTEM);
        apply(rec, t);
        // The gauge keeps counting across its own transition (the breach that
        // enters WATCH counts as breach #1 — pinned): keep it, and mirror its
        // counters on the record so a reload resumes exactly.
        gauges.put(rec.alphaId(), gauge);
        rec.breachCount = gauge.breachCount();
        rec.recoveryCount = gauge.recoveryCount();
        recordEvaluation(rec, new GateEvaluation(rec.alphaId(), eventTs, state,
                Outcome.TRANSITION, gate, 0, t));
        return t;
    }

    // -- manual edges ---------------------------------------------------------

    private static void checkManual(Actor actor, String reason, String what) {
        if (actor == null) {
            throw new IllegalArgumentException(what + ": actor must be given");
        }
        if (actor != Actor.HUMAN) {
            throw new IllegalArgumentException(what + ": requires Actor.HUMAN, got " + actor);
        }
        if (reason == null || reason.isBlank()) {
            throw new IllegalArgumentException(what + ": a non-empty reason is required");
        }
    }

    /** Manual retirement from any non-retired state (HUMAN only). */
    public LifecycleTransition retire(String alphaId, long eventTs, String reason,
            Actor actor) {
        checkManual(actor, reason, "retire");
        AlphaRecord rec = registry.get(alphaId);
        if (rec.state() == LifecycleState.RETIRED) {
            throw new IllegalArgumentException("retire: " + alphaId + " is already RETIRED");
        }
        Edge edge = edgeFor(rec.state(), LifecycleState.RETIRED, EdgeKind.MANUAL);
        return apply(rec, transition(rec, edge, eventTs, reason, Map.of(), actor));
    }

    /** Manual re-research: RETIRED → RESEARCH (HUMAN only). */
    public LifecycleTransition resetToResearch(String alphaId, long eventTs, String reason,
            Actor actor) {
        checkManual(actor, reason, "reset_to_research");
        AlphaRecord rec = registry.get(alphaId);
        if (rec.state() != LifecycleState.RETIRED) {
            throw new IllegalArgumentException("reset_to_research: " + alphaId + " is "
                    + rec.state() + ", not RETIRED");
        }
        Edge edge = edgeFor(LifecycleState.RETIRED, LifecycleState.RESEARCH, EdgeKind.MANUAL);
        return apply(rec, transition(rec, edge, eventTs, reason, Map.of(), actor));
    }
}
