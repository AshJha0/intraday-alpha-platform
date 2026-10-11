package com.iap.adaptive;

import java.nio.file.Path;
import java.util.Map;

import com.iap.config.Json;

/**
 * Alpha lifecycle state machine (the {@code alpha_lifecycle_state} gauge,
 * 0/1/2) — the Java mirror of the pinned Python rules
 * ({@code iap.adaptive.lifecycle.LifecycleTracker}; normative contract
 * /API_ADAPTIVE.md), evaluated once per adaptive block on the rolling
 * realized IC of the deployed signal over matured rows:
 *
 * <ul>
 *   <li>{@code ACTIVE} (0) — allocated. {@code rolling_ic < watch_ic_gate}
 *       (strict) &rarr; WATCH.</li>
 *   <li>{@code WATCH} (1) — allocated, on probation. A persistent breach
 *       &rarr; RETIRED, by the {@link BreachRule} in force;
 *       {@code reactivate_evals} consecutive evals
 *       {@code >= reactivate_ic_gate} (inclusive) &rarr; ACTIVE.</li>
 *   <li>{@code RETIRED} (2) — allocation halted.
 *       {@code reactivate_evals} consecutive recoveries &rarr; WATCH (a
 *       retired alpha re-earns ACTIVE through probation, never
 *       directly).</li>
 *   <li>{@code NaN} rolling IC (too little matured data) or an
 *       uninformative reading: no transition, no counter movement —
 *       silence is not evidence.</li>
 * </ul>
 *
 * <p><b>Retirement rule</b> (the default changed in v1.5.0; both rules are
 * implemented and each is selected by name — nothing is implied):
 *
 * <ul>
 *   <li>{@link BreachRule#CUSUM} — the default. Every counted reading in
 *       ACTIVE or WATCH updates
 *       <pre>
 *   s = S + new_fraction * (watch_ic_gate - rolling_ic - cusum_k)
 *   S = s &gt; 0 ? s : 0</pre>
 *       where {@code new_fraction} in (0, 1] is the share of the reading's
 *       window that is new since the last counted reading. In WATCH a
 *       reading that is itself a breach and leaves {@code S >= cusum_h}
 *       retires the alpha; the reading that ENTERS WATCH never does.
 *       {@code S} survives ACTIVE &rarr; WATCH and is reset by every other
 *       transition; the breach counter is unused and stays 0. The two
 *       expressions are written exactly as in the Python and Rust
 *       implementations (one multiplication, left-to-right subtraction, an
 *       explicit comparison) so {@code S} is bit-identical.</li>
 *   <li>{@link BreachRule#CONSECUTIVE} — the LEGACY rule, the default up to
 *       v1.4.0: {@code retire_breach_evals} CONSECUTIVE breaches in WATCH
 *       (the entering breach counts) retire; the neutral zone between the
 *       gates resets BOTH counters (hysteresis).</li>
 * </ul>
 *
 * <p>The gates and the rule are pinned in
 * {@code configs/strategies/strategies.json} {@code adaptive.lifecycle}
 * ({@code reactivate_ic_gate >= watch_ic_gate} enforced; the block must name
 * its {@code breach_rule}). Deterministic and wall-clock-free: evaluations
 * happen at event-time block boundaries.
 */
public final class LifecycleGauge {
    /** Lifecycle states with their pinned gauge codes. */
    public enum State {
        ACTIVE(0), WATCH(1), RETIRED(2);

        private final int code;

        State(int code) {
            this.code = code;
        }

        /** The 0/1/2 value exported on the gauge. */
        public int code() {
            return code;
        }
    }

    /** How a persistent breach is recognised (class docs). */
    public enum BreachRule {
        /** The default since v1.5.0. */
        CUSUM("cusum"),
        /** The legacy rule (the default up to v1.4.0). */
        CONSECUTIVE("consecutive");

        private final String wire;

        BreachRule(String wire) {
            this.wire = wire;
        }

        /** The config value ({@code "cusum"} / {@code "consecutive"}). */
        public String wire() {
            return wire;
        }

        /** Parse a config value; an unknown one is an {@link IllegalArgumentException}. */
        public static BreachRule parse(String value, String where) {
            for (BreachRule r : values()) {
                if (r.wire.equals(value)) {
                    return r;
                }
            }
            throw new IllegalArgumentException(where + ": unknown breach_rule '" + value
                    + "'; known: cusum, consecutive");
        }
    }

    private final double watchIcGate;
    private final double reactivateIcGate;
    private final int retireBreachEvals;
    private final int reactivateEvals;
    private final BreachRule breachRule;
    private final double cusumK;
    private final double cusumH;
    private State state = State.ACTIVE;
    private int breachCount;
    private int recoveryCount;
    private double cusum;
    private double cusumAtLastUpdate;

    private LifecycleGauge(double watchIcGate, double reactivateIcGate,
            int retireBreachEvals, int reactivateEvals, BreachRule breachRule,
            double cusumK, double cusumH) {
        if (retireBreachEvals < 1 || reactivateEvals < 1) {
            throw new IllegalArgumentException(
                    "lifecycle eval counts must be >= 1");
        }
        if (reactivateIcGate < watchIcGate) {
            throw new IllegalArgumentException(
                    "reactivate_ic_gate must be >= watch_ic_gate");
        }
        if (breachRule == null) {
            throw new IllegalArgumentException("breach_rule must be named");
        }
        if (!(cusumK >= 0.0) || Double.isInfinite(cusumK)) {
            throw new IllegalArgumentException("cusum_k must be a finite number >= 0");
        }
        if (breachRule == BreachRule.CUSUM
                && !(cusumH > 0.0 && !Double.isInfinite(cusumH))) {
            throw new IllegalArgumentException("breach_rule 'cusum' needs cusum_h > 0");
        }
        this.watchIcGate = watchIcGate;
        this.reactivateIcGate = reactivateIcGate;
        this.retireBreachEvals = retireBreachEvals;
        this.reactivateEvals = reactivateEvals;
        this.breachRule = breachRule;
        this.cusumK = cusumK;
        this.cusumH = cusumH;
    }

    /** A gauge under the named rule, starting ACTIVE. */
    public static LifecycleGauge of(BreachRule breachRule, double watchIcGate,
            double reactivateIcGate, int retireBreachEvals, int reactivateEvals,
            double cusumK, double cusumH) {
        return new LifecycleGauge(watchIcGate, reactivateIcGate, retireBreachEvals,
                reactivateEvals, breachRule, cusumK, cusumH);
    }

    /** A gauge under the default {@link BreachRule#CUSUM} rule, starting ACTIVE. */
    public static LifecycleGauge cusum(double watchIcGate, double reactivateIcGate,
            int retireBreachEvals, int reactivateEvals, double cusumK, double cusumH) {
        return of(BreachRule.CUSUM, watchIcGate, reactivateIcGate, retireBreachEvals,
                reactivateEvals, cusumK, cusumH);
    }

    /**
     * A gauge under the LEGACY {@link BreachRule#CONSECUTIVE} rule (the
     * default up to v1.4.0), starting ACTIVE.
     */
    public static LifecycleGauge legacyConsecutive(double watchIcGate,
            double reactivateIcGate, int retireBreachEvals, int reactivateEvals) {
        return of(BreachRule.CONSECUTIVE, watchIcGate, reactivateIcGate,
                retireBreachEvals, reactivateEvals, 0.0, 0.0);
    }

    /**
     * Gates and rule from {@code configs/strategies/strategies.json}
     * {@code adaptive.lifecycle} (the pinned source of truth shared with the
     * Python tracker). The block must name its {@code breach_rule};
     * {@code "cusum"} must also give {@code cusum_k} and {@code cusum_h}.
     */
    public static LifecycleGauge fromStrategiesConfig(Path strategiesJson) {
        Map<String, Object> doc = Json.object(Json.parseFile(strategiesJson));
        Map<String, Object> lc = Json.object(
                Json.object(doc.get("adaptive")).get("lifecycle"));
        String where = strategiesJson + ": adaptive.lifecycle";
        Object ruleName = lc.get("breach_rule");
        if (!(ruleName instanceof String)) {
            throw new IllegalArgumentException(where + " names no breach_rule: 'cusum' "
                    + "(the default since v1.5.0, with cusum_k and cusum_h) or "
                    + "'consecutive' (the rule up to v1.4.0)");
        }
        BreachRule rule = BreachRule.parse((String) ruleName, where);
        double k = 0.0;
        double h = 0.0;
        if (rule == BreachRule.CUSUM) {
            if (lc.get("cusum_k") == null || lc.get("cusum_h") == null) {
                throw new IllegalArgumentException(where
                        + ": breach_rule 'cusum' needs cusum_k and cusum_h");
            }
            k = Json.asDouble(lc.get("cusum_k"));
            h = Json.asDouble(lc.get("cusum_h"));
        }
        return of(rule, Json.asDouble(lc.get("watch_ic_gate")),
                Json.asDouble(lc.get("reactivate_ic_gate")),
                (int) Json.asLong(lc.get("retire_breach_evals")),
                (int) Json.asLong(lc.get("reactivate_evals")), k, h);
    }

    /**
     * A gauge resumed at a persisted point of the SAME rules: state, the
     * consecutive breach / recovery counters and the CUSUM statistic (the
     * platform lifecycle registry mirrors them after every live evaluation
     * so a reload continues exactly; see
     * {@code com.iap.lifecycle.AlphaLifecycle}).
     */
    public static LifecycleGauge restore(BreachRule breachRule, double watchIcGate,
            double reactivateIcGate, int retireBreachEvals, int reactivateEvals,
            double cusumK, double cusumH, State state, int breachCount,
            int recoveryCount, double cusum) {
        if (state == null || breachCount < 0 || recoveryCount < 0) {
            throw new IllegalArgumentException(
                    "lifecycle restore needs a state and counters >= 0");
        }
        if (!(cusum >= 0.0) || Double.isInfinite(cusum)) {
            throw new IllegalArgumentException(
                    "lifecycle restore needs a finite cusum >= 0");
        }
        LifecycleGauge g = of(breachRule, watchIcGate, reactivateIcGate,
                retireBreachEvals, reactivateEvals, cusumK, cusumH);
        g.state = state;
        g.breachCount = breachCount;
        g.recoveryCount = recoveryCount;
        g.cusum = cusum;
        g.cusumAtLastUpdate = cusum;
        return g;
    }

    /** Current state (never null; starts ACTIVE). */
    public State state() {
        return state;
    }

    /** The retirement rule this gauge applies. */
    public BreachRule breachRule() {
        return breachRule;
    }

    /** Consecutive breaches counted so far (legacy rule; the entering breach is 1). */
    public int breachCount() {
        return breachCount;
    }

    /** Consecutive recoveries counted so far (WATCH / RETIRED). */
    public int recoveryCount() {
        return recoveryCount;
    }

    /** The CUSUM statistic {@code S} (0 under the legacy rule). */
    public double cusum() {
        return cusum;
    }

    /**
     * {@code S} as the last counted reading left it, BEFORE the reset of a
     * transition that reading caused — the statistic a retirement reason
     * quotes.
     */
    public double cusumAtLastUpdate() {
        return cusumAtLastUpdate;
    }

    private void transition(State to) {
        boolean keepCusum = to == State.WATCH && state != State.RETIRED;
        state = to;
        breachCount = 0;
        recoveryCount = 0;
        if (!keepCusum) {
            cusum = 0.0; // a verdict was reached; evidence starts over
        }
    }

    /**
     * One informative evaluation over a disjoint window
     * ({@code new_fraction} 1.0); returns the (possibly new) state.
     * {@code NaN} = no evidence, no movement.
     */
    public State update(double rollingIc) {
        return update(rollingIc, true, 1.0);
    }

    /** One evaluation over a disjoint window ({@code new_fraction} 1.0). */
    public State update(double rollingIc, boolean informative) {
        return update(rollingIc, informative, 1.0);
    }

    /**
     * One evaluation. {@code informative} is false when the evaluation's
     * matured set gained no new rows since the last counted evaluation
     * (pinned, API_ADAPTIVE.md section 6): re-reading a frozen IC window is
     * ONE reading, not N consecutive breaches, so it moves nothing.
     * {@code newFraction} in (0, 1] is the share of the reading's window that
     * is new since the last counted one; the CUSUM rule weights the reading
     * by it, the legacy rule does not read it.
     */
    public State update(double rollingIc, boolean informative, double newFraction) {
        if (Double.isNaN(rollingIc) || !informative) {
            return state;
        }
        boolean breach = rollingIc < watchIcGate;
        boolean recover = rollingIc >= reactivateIcGate;
        if (breachRule == BreachRule.CUSUM) {
            return updateCusum(rollingIc, breach, recover, newFraction);
        }
        switch (state) {
            case ACTIVE -> {
                if (breach) {
                    transition(State.WATCH);
                    breachCount = 1; // the entering breach counts (pinned)
                }
            }
            case WATCH -> {
                if (breach) {
                    breachCount++;
                    recoveryCount = 0;
                    if (breachCount >= retireBreachEvals) {
                        transition(State.RETIRED);
                    }
                } else if (recover) {
                    recoveryCount++;
                    breachCount = 0;
                    if (recoveryCount >= reactivateEvals) {
                        transition(State.ACTIVE);
                    }
                } else {
                    // neutral zone: hysteresis resets both counters
                    breachCount = 0;
                    recoveryCount = 0;
                }
            }
            case RETIRED -> {
                if (recover) {
                    recoveryCount++;
                    if (recoveryCount >= reactivateEvals) {
                        transition(State.WATCH);
                    }
                } else {
                    recoveryCount = 0;
                }
            }
        }
        return state;
    }

    private State updateCusum(double rollingIc, boolean breach, boolean recover,
            double newFraction) {
        if (!(newFraction > 0.0 && newFraction <= 1.0)) {
            throw new IllegalArgumentException("new_fraction must be in (0, 1]");
        }
        if (state != State.RETIRED) {
            // Exactly these two expressions in every port (class docs).
            double s = cusum + newFraction * (watchIcGate - rollingIc - cusumK);
            cusum = s > 0.0 ? s : 0.0;
        }
        cusumAtLastUpdate = cusum;
        switch (state) {
            case ACTIVE -> {
                if (breach) {
                    transition(State.WATCH);
                }
            }
            case WATCH -> {
                if (breach && cusum >= cusumH) {
                    transition(State.RETIRED);
                } else if (recover) {
                    recoveryCount++;
                    if (recoveryCount >= reactivateEvals) {
                        transition(State.ACTIVE);
                    }
                } else {
                    recoveryCount = 0;
                }
            }
            case RETIRED -> {
                if (recover) {
                    recoveryCount++;
                    if (recoveryCount >= reactivateEvals) {
                        transition(State.WATCH);
                    }
                } else {
                    recoveryCount = 0;
                }
            }
        }
        return state;
    }

    /** Retirement halts allocation; ACTIVE and WATCH trade. */
    public boolean allocatable() {
        return state != State.RETIRED;
    }
}
