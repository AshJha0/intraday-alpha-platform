package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import java.nio.file.Paths;

import org.junit.Test;

import com.iap.adaptive.LifecycleGauge;
import com.iap.adaptive.LifecycleGauge.State;

/**
 * Lifecycle state machine — the Java mirror of the pinned Python tracker
 * (iap.adaptive.lifecycle, configs/strategies.json adaptive.lifecycle):
 * strict {@code <} breach at watch_ic_gate, inclusive {@code >=} recovery
 * at reactivate_ic_gate, consecutive-eval retirement/reactivation with
 * hysteresis, NaN = no evidence = no movement.
 */
public class LifecycleGaugeTest {
    /** The pinned production gates (configs/strategies.json). */
    private static LifecycleGauge pinned() {
        return new LifecycleGauge(0.0, 0.005, 6, 3);
    }

    @Test
    public void transitionsAtThePinnedThresholdEdges() {
        LifecycleGauge g = pinned();
        assertEquals(State.ACTIVE, g.state());
        assertEquals(0, State.ACTIVE.code());
        assertEquals(1, State.WATCH.code());
        assertEquals(2, State.RETIRED.code());

        // breach is strict <: IC exactly at the watch gate stays ACTIVE
        assertEquals(State.ACTIVE, g.update(0.0));
        assertEquals(State.ACTIVE, g.update(0.5));
        // first strict breach -> WATCH (the entering breach counts)
        assertEquals(State.WATCH, g.update(Math.nextDown(0.0)));

        // 5 total breaches: still WATCH; the 6th consecutive retires
        for (int i = 0; i < 4; i++) {
            assertEquals("breach " + (i + 2), State.WATCH, g.update(-0.1));
        }
        assertEquals(State.RETIRED, g.update(-0.1));
        assertFalse("retirement halts allocation", g.allocatable());

        // recovery is inclusive >=: exactly the reactivate gate counts;
        // a retired alpha re-earns only WATCH, never ACTIVE directly
        assertEquals(State.RETIRED, g.update(0.005));
        assertEquals(State.RETIRED, g.update(0.005));
        assertEquals(State.WATCH, g.update(0.005));
        assertTrue(g.allocatable());
        // and from WATCH, 3 consecutive recoveries re-activate
        assertEquals(State.WATCH, g.update(0.01));
        assertEquals(State.WATCH, g.update(0.01));
        assertEquals(State.ACTIVE, g.update(0.01));
    }

    @Test
    public void neutralZoneResetsBothCountersAndBreachMustBeConsecutive() {
        LifecycleGauge g = pinned();
        assertEquals(State.WATCH, g.update(-0.5)); // breach 1
        g.update(-0.5); // 2
        g.update(-0.5); // 3
        g.update(-0.5); // 4
        g.update(-0.5); // 5
        // neutral zone (watch gate <= ic < reactivate gate): both reset
        assertEquals(State.WATCH, g.update(0.001));
        // so five more breaches still don't retire...
        for (int i = 0; i < 5; i++) {
            assertEquals(State.WATCH, g.update(-0.5));
        }
        // ...the sixth consecutive one does
        assertEquals(State.RETIRED, g.update(-0.5));

        // recovery streak is also broken by the neutral zone
        LifecycleGauge h = pinned();
        assertEquals(State.WATCH, h.update(-1.0));
        h.update(0.9);
        h.update(0.9);
        assertEquals(State.WATCH, h.update(0.001)); // reset at 2 of 3
        h.update(0.9);
        h.update(0.9);
        assertEquals(State.ACTIVE, h.update(0.9));
    }

    @Test
    public void nanIsNoEvidenceAndMovesNothing() {
        LifecycleGauge g = pinned();
        assertEquals(State.ACTIVE, g.update(Double.NaN));
        assertEquals(State.WATCH, g.update(-1.0)); // breach 1
        g.update(-1.0); // 2
        // NaN in the middle: state and counters untouched
        assertEquals(State.WATCH, g.update(Double.NaN));
        g.update(-1.0); // 3
        g.update(-1.0); // 4
        g.update(-1.0); // 5
        assertEquals("6th consecutive real breach retires despite the NaN",
                State.RETIRED, g.update(-1.0));
    }

    @Test
    public void gatesLoadFromThePinnedStrategiesConfig() {
        LifecycleGauge g = LifecycleGauge.fromStrategiesConfig(
                Paths.get("..", "configs", "strategies.json"));
        // configs/strategies.json adaptive.lifecycle: watch 0.0,
        // reactivate 0.005, retire after 6, reactivate after 3
        assertEquals(State.ACTIVE, g.update(0.0));
        assertEquals(State.WATCH, g.update(-1e-9));
        for (int i = 0; i < 4; i++) {
            assertEquals(State.WATCH, g.update(-1.0));
        }
        assertEquals(State.RETIRED, g.update(-1.0));

        try {
            new LifecycleGauge(0.0, -0.1, 6, 3);
            fail("reactivate gate below watch gate");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("reactivate_ic_gate"));
        }
        try {
            new LifecycleGauge(0.0, 0.005, 0, 3);
            fail("bad eval count");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains(">= 1"));
        }
    }
}
