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
 * (iap.adaptive.lifecycle, configs/strategies/strategies.json adaptive.lifecycle):
 * strict {@code <} breach at watch_ic_gate, inclusive {@code >=} recovery
 * at reactivate_ic_gate, NaN = no evidence = no movement; retirement by the
 * CUSUM rule (the default since v1.5.0) or by the legacy count of
 * consecutive breaches with hysteresis, each selected by name.
 */
public class LifecycleGaugeTest {
    /** The pinned gates under the LEGACY consecutive rule (the rule up to v1.4.0). */
    private static LifecycleGauge pinned() {
        return LifecycleGauge.legacyConsecutive(0.0, 0.005, 6, 3);
    }

    /** The pinned gates under the default CUSUM rule (strategies.json). */
    private static LifecycleGauge pinnedCusum() {
        return LifecycleGauge.cusum(0.0, 0.005, 6, 3, 0.0025, 0.01);
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
    public void gatesAndRuleLoadFromThePinnedStrategiesConfig() {
        LifecycleGauge g = LifecycleGauge.fromStrategiesConfig(
                Paths.get("..", "configs", "strategies", "strategies.json"));
        // configs/strategies/strategies.json adaptive.lifecycle: watch 0.0,
        // reactivate 0.005, reactivate after 3; breach_rule cusum with
        // cusum_k 0.0025 and cusum_h 0.01 (v1.5.0)
        assertEquals(LifecycleGauge.BreachRule.CUSUM, g.breachRule());
        assertEquals(State.ACTIVE, g.update(0.0, true, 0.125));
        assertEquals(State.WATCH, g.update(-1e-9, true, 0.125));
        assertEquals("a breach inside the slack adds nothing", 0.0, g.cusum(), 0.0);
        // 0.125 * (0.04 - 0.0025) = 0.0046875 per reading: the third retires
        assertEquals(State.WATCH, g.update(-0.04, true, 0.125));
        assertEquals(State.WATCH, g.update(-0.04, true, 0.125));
        assertEquals(State.RETIRED, g.update(-0.04, true, 0.125));
        assertEquals(0.0140625, g.cusumAtLastUpdate(), 1e-15);
        assertEquals(0.0, g.cusum(), 0.0);

        try {
            LifecycleGauge.legacyConsecutive(0.0, -0.1, 6, 3);
            fail("reactivate gate below watch gate");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("reactivate_ic_gate"));
        }
        try {
            LifecycleGauge.legacyConsecutive(0.0, 0.005, 0, 3);
            fail("bad eval count");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains(">= 1"));
        }
        try {
            LifecycleGauge.cusum(0.0, 0.005, 6, 3, 0.0025, 0.0);
            fail("cusum without a threshold");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("cusum_h"));
        }
        try {
            LifecycleGauge.cusum(0.0, 0.005, 6, 3, -0.1, 0.01);
            fail("negative slack");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("cusum_k"));
        }
        try {
            LifecycleGauge.BreachRule.parse("majority", "test");
            fail("unknown rule");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("breach_rule"));
        }
    }

    @Test
    public void cusumWeightsAReadingByItsNewInformation() {
        // the same four breaches: overlapping windows (1/8 new) do not
        // retire, disjoint windows (all new) do on the second
        LifecycleGauge overlapping = pinnedCusum();
        LifecycleGauge disjoint = pinnedCusum();
        for (int i = 0; i < 4; i++) {
            overlapping.update(-0.01, true, 0.125);
        }
        assertEquals(State.WATCH, overlapping.state());
        assertEquals(4 * 0.125 * 0.0075, overlapping.cusum(), 1e-15);
        assertEquals(State.WATCH, disjoint.update(-0.01, true, 1.0));
        assertEquals(State.RETIRED, disjoint.update(-0.01, true, 1.0));
        assertFalse(disjoint.allocatable());

        try {
            pinnedCusum().update(-0.01, true, 0.0);
            fail("new_fraction 0");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("new_fraction"));
        }
        try {
            pinnedCusum().update(-0.01, true, 1.5);
            fail("new_fraction above 1");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("new_fraction"));
        }
    }

    @Test
    public void cusumRetiresOnlyOnABreachAndNeverOnTheEnteringReading() {
        LifecycleGauge g = pinnedCusum();
        // one reading takes S past the threshold: WATCH, not RETIRED
        assertEquals(State.WATCH, g.update(-0.09, true, 0.125));
        assertEquals(0.0109375, g.cusum(), 0.0);
        // above the gate with S still over the threshold: no retirement
        assertEquals(State.WATCH, g.update(0.004, true, 0.125));
        assertTrue(g.cusum() >= 0.01);
        // the next breach retires and resets S
        assertEquals(State.RETIRED, g.update(-0.003, true, 0.125));
        assertEquals(0.0, g.cusum(), 0.0);
        assertEquals(0, g.breachCount());

        // NaN and uninformative readings move nothing, S included
        LifecycleGauge h = pinnedCusum();
        h.update(-0.02, true, 0.125);
        double before = h.cusum();
        assertEquals(State.WATCH, h.update(Double.NaN, true, 0.125));
        assertEquals(State.WATCH, h.update(-0.5, false, 0.125));
        assertEquals(before, h.cusum(), 0.0);
        // recovery drains S and three in a row re-activate, resetting it
        h.update(0.01, true, 0.125);
        h.update(0.01, true, 0.125);
        assertEquals(State.ACTIVE, h.update(0.01, true, 0.125));
        assertEquals(0.0, h.cusum(), 0.0);

        // S accumulates in ACTIVE too and survives the entry into WATCH;
        // a restored gauge continues from the persisted statistic
        LifecycleGauge r = LifecycleGauge.restore(LifecycleGauge.BreachRule.CUSUM,
                0.0, 0.005, 6, 3, 0.0025, 0.01, State.WATCH, 0, 0, 0.009);
        assertEquals(State.RETIRED, r.update(-0.02, true, 0.125));
        assertEquals(0.009 + 0.125 * (0.0 - -0.02 - 0.0025), r.cusumAtLastUpdate(), 0.0);
    }
}
