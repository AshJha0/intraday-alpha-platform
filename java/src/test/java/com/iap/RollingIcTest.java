package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import org.junit.Test;

import com.iap.adaptive.RollingIc;

/**
 * Rolling realized IC — the pinned bucket-IC semantics (API_ADAPTIVE.md
 * section 4): event-time, lookahead-free maturation of (signal, forward
 * mid return) pairs, one Pearson IC per fixed event-time bucket
 * ({@code >= 8} pairs, nondegenerate variance), mean over buckets within
 * the rolling window, NaN below {@code min_ic_buckets}.
 */
public class RollingIcTest {
    @Test
    public void noLookaheadHandCase() {
        // horizon 10ns: a signal's return is realized only by the first
        // observation at or after ts + 10. One 1000ns bucket, min 1.
        RollingIc ic = new RollingIc(10, 1000, 1000, 1);
        // returns engineered exactly linear in the signal:
        // mid_i = 100/(1 + 0.001 i) and final mid 100
        // => fwd return_i = 0.001 * i  (perfect positive correlation)
        for (int i = 0; i <= 8; i++) {
            ic.onObservation(i, i, 100.0 / (1.0 + 0.001 * i));
            assertEquals("nothing matured yet", 0, ic.pairs());
            assertTrue("no pair may exist before the horizon elapsed",
                    Double.isNaN(ic.ic(i)));
        }
        // ts=17 realizes signals 0..7 (7+10 <= 17); signal 8 stays pending
        ic.onObservation(17, 99.0, 100.0);
        assertEquals(8, ic.pairs());
        assertEquals("8-pair bucket, perfectly correlated", 1.0, ic.ic(18),
                1e-12);
        // ts=18 realizes signal 8 as well
        ic.onObservation(18, 99.0, 100.0);
        assertEquals(9, ic.pairs());
        assertEquals(1.0, ic.ic(19), 1e-12);
    }

    @Test
    public void meanOfBucketIcsHandCase() {
        // two 100ns buckets: +1 correlation in the first, -1 in the
        // second -> mean bucket IC is exactly 0. Horizon 50 so no pair is
        // realized inside its own bucket (returns all measure to mid 100).
        RollingIc ic = new RollingIc(50, 100_000, 100, 2);
        for (int i = 0; i < 8; i++) { // bucket 0: ts 0..7
            ic.onObservation(i, i, 100.0 / (1.0 + 0.001 * i));
        }
        for (int i = 0; i < 8; i++) { // bucket 1: ts 100..107, inverted
            ic.onObservation(100 + i, -i, 100.0 / (1.0 + 0.001 * i));
        }
        ic.onObservation(300, 0.0, 100.0); // realizes everything
        assertEquals(16, ic.pairs());
        assertTrue("both buckets count (min_ic_buckets=2 met)",
                !Double.isNaN(ic.ic(301)));
        assertEquals(0.0, ic.ic(301), 1e-12);
    }

    @Test
    public void bucketAndWindowRules() {
        // buckets of 10ns, window 100ns, min 1 bucket
        RollingIc ic = new RollingIc(1, 100, 10, 1);
        // 7 pairs only -> bucket skipped -> NaN (never a fabricated 0)
        for (int i = 0; i < 7; i++) {
            ic.onObservation(i, i, 100.0 / (1.0 + 0.001 * i));
        }
        ic.onObservation(9, 0.0, 100.0);
        assertEquals(7, ic.pairs());
        assertTrue("< 8 pairs per bucket never counts",
                Double.isNaN(ic.ic(10)));

        // constant signal -> degenerate variance -> bucket skipped
        RollingIc flat = new RollingIc(1, 100, 10, 1);
        for (int i = 0; i < 8; i++) {
            flat.onObservation(i, 5.0, 100.0 + i);
        }
        flat.onObservation(9, 0.0, 200.0);
        assertEquals(8, flat.pairs());
        assertTrue(Double.isNaN(flat.ic(10)));

        // window: matured rows older than nowTs - windowNs don't count,
        // and rows are evicted once they can never re-enter a window.
        // Horizon 20: only the ts=30 observation realizes the pairs, so
        // every return measures to mid 100 (exactly linear in the signal).
        RollingIc w = new RollingIc(20, 100, 10, 1);
        for (int i = 0; i < 8; i++) {
            w.onObservation(i, i, 100.0 / (1.0 + 0.001 * i));
        }
        w.onObservation(30, 0.0, 100.0);
        assertEquals(1.0, w.ic(50), 1e-12);
        assertTrue("bucket left the [T-100, T) window",
                Double.isNaN(w.ic(150)));
        w.onObservation(200, 0.0, 100.0); // triggers eviction
        assertEquals(0, w.pairs());
    }

    @Test
    public void horizonParsingAndValidation() {
        assertEquals(500_000_000L, RollingIc.parseHorizonNs("500ms"));
        assertEquals(1_000_000_000L, RollingIc.parseHorizonNs("1s"));
        assertEquals(5_000_000_000L, RollingIc.parseHorizonNs("5s"));
        assertEquals(300_000_000_000L, RollingIc.parseHorizonNs("5m"));
        assertEquals(900_000_000_000L, RollingIc.parseHorizonNs("15m"));
        assertEquals(3_600_000_000_000L, RollingIc.parseHorizonNs("1h"));
        try {
            RollingIc.parseHorizonNs("soon");
            fail("unparseable horizon");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("horizon"));
        }
        try {
            new RollingIc(0, 100, 10, 1);
            fail("bad horizon");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("horizonNs"));
        }
        try {
            new RollingIc(1, 100, 10, 0);
            fail("bad min buckets");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("minBuckets"));
        }
    }
}
