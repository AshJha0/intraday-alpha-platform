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
 *       (strict) &rarr; WATCH, with the entering breach counted.</li>
 *   <li>{@code WATCH} (1) — allocated, on probation.
 *       {@code retire_breach_evals} CONSECUTIVE breaches &rarr; RETIRED;
 *       {@code reactivate_evals} consecutive evals
 *       {@code >= reactivate_ic_gate} (inclusive) &rarr; ACTIVE; the
 *       neutral zone between the gates resets BOTH counters
 *       (hysteresis).</li>
 *   <li>{@code RETIRED} (2) — allocation halted.
 *       {@code reactivate_evals} consecutive recoveries &rarr; WATCH (a
 *       retired alpha re-earns ACTIVE through probation, never
 *       directly).</li>
 *   <li>{@code NaN} rolling IC (too little matured data): no transition,
 *       no counter movement — silence is not evidence.</li>
 * </ul>
 *
 * <p>The gates are pinned in {@code configs/strategies.json}
 * {@code adaptive.lifecycle} ({@code reactivate_ic_gate >= watch_ic_gate}
 * enforced). Deterministic and wall-clock-free: evaluations happen at
 * event-time block boundaries.
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

    private final double watchIcGate;
    private final double reactivateIcGate;
    private final int retireBreachEvals;
    private final int reactivateEvals;
    private State state = State.ACTIVE;
    private int breachCount;
    private int recoveryCount;

    public LifecycleGauge(double watchIcGate, double reactivateIcGate,
            int retireBreachEvals, int reactivateEvals) {
        if (retireBreachEvals < 1 || reactivateEvals < 1) {
            throw new IllegalArgumentException(
                    "lifecycle eval counts must be >= 1");
        }
        if (reactivateIcGate < watchIcGate) {
            throw new IllegalArgumentException(
                    "reactivate_ic_gate must be >= watch_ic_gate");
        }
        this.watchIcGate = watchIcGate;
        this.reactivateIcGate = reactivateIcGate;
        this.retireBreachEvals = retireBreachEvals;
        this.reactivateEvals = reactivateEvals;
    }

    /**
     * Gates from {@code configs/strategies.json} {@code adaptive.lifecycle}
     * (the pinned source of truth shared with the Python tracker).
     */
    public static LifecycleGauge fromStrategiesConfig(Path strategiesJson) {
        Map<String, Object> doc = Json.object(Json.parseFile(strategiesJson));
        Map<String, Object> lc = Json.object(
                Json.object(doc.get("adaptive")).get("lifecycle"));
        return new LifecycleGauge(Json.asDouble(lc.get("watch_ic_gate")),
                Json.asDouble(lc.get("reactivate_ic_gate")),
                (int) Json.asLong(lc.get("retire_breach_evals")),
                (int) Json.asLong(lc.get("reactivate_evals")));
    }

    /** Current state (never null; starts ACTIVE). */
    public State state() {
        return state;
    }

    private void transition(State to) {
        state = to;
        breachCount = 0;
        recoveryCount = 0;
    }

    /**
     * One evaluation with the current rolling realized IC; returns the
     * (possibly new) state. {@code NaN} = no evidence, no movement.
     */
    public State update(double rollingIc) {
        return update(rollingIc, true);
    }

    /**
     * One evaluation. {@code informative} is false when the evaluation's
     * matured set gained no new rows since the last counted evaluation
     * (pinned, API_ADAPTIVE.md section 6): re-reading a frozen IC window is
     * ONE reading, not N consecutive breaches, so it moves nothing.
     */
    public State update(double rollingIc, boolean informative) {
        if (Double.isNaN(rollingIc) || !informative) {
            return state;
        }
        boolean breach = rollingIc < watchIcGate;
        boolean recover = rollingIc >= reactivateIcGate;
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

    /** Retirement halts allocation; ACTIVE and WATCH trade. */
    public boolean allocatable() {
        return state != State.RETIRED;
    }
}
