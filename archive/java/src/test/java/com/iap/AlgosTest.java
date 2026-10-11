package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

import com.iap.execution.AlgoType;
import com.iap.execution.Algos;
import com.iap.execution.ParentOrder;

/**
 * Parent-algo schedule tests (spec section 17; Algos javadoc pins the
 * semantics): TWAP equal weights, the pinned VWAP U-curve, IS exponential
 * front-loading, largest-remainder apportionment (exact sums, earlier-slice
 * tie-break) and integer-division slice times. The golden replay-fills
 * scenario quantities ({129,71,71,129} VWAP, {304,184,112} IS) are pinned
 * here independently of the execution simulator.
 */
public class AlgosTest {
    private static final long SEC = 1_000_000_000L;

    private static ParentOrder parent(AlgoType algo, long qty, int slices,
            double riskAversion) {
        ParentOrder p = new ParentOrder();
        p.parentId = 1;
        p.instrumentId = 1;
        p.venueId = 1;
        p.side = 0;
        p.qty = qty;
        p.algo = algo;
        p.startTs = 1000 * SEC;
        p.endTs = 2000 * SEC;
        p.slices = slices;
        p.riskAversion = riskAversion;
        return p;
    }

    @Test
    public void twapEqualWeights() {
        double[] w = Algos.sliceWeights(parent(AlgoType.TWAP, 100, 5, 0.0));
        assertEquals(5, w.length);
        for (double x : w) {
            assertEquals(1.0, x, 0.0);
        }
    }

    @Test
    public void vwapUCurvePinned() {
        // w_i = 1 + x_i^2, x_i = (2i - (N-1)) / (N-1): symmetric U with
        // endpoints exactly 2 and (odd N) midpoint exactly 1.
        double[] w = Algos.sliceWeights(parent(AlgoType.VWAP, 100, 5, 0.0));
        assertEquals(2.0, w[0], 0.0);
        assertEquals(2.0, w[4], 0.0);
        assertEquals(1.0, w[2], 0.0);
        for (int i = 0; i < w.length; i++) {
            double x = (2.0 * i - 4.0) / 4.0;
            assertEquals(1.0 + x * x, w[i], 0.0);
            assertEquals(w[i], w[w.length - 1 - i], 0.0); // symmetry
        }
        // Monotone: decreasing into the middle, increasing out.
        assertTrue(w[0] > w[1] && w[1] > w[2]);
        assertTrue(w[2] < w[3] && w[3] < w[4]);
    }

    @Test
    public void isFrontLoadedExponentialDecay() {
        ParentOrder p = parent(AlgoType.IS, 100, 4, 1.5);
        double[] w = Algos.sliceWeights(p);
        for (int i = 0; i < 4; i++) {
            assertEquals(Math.exp(-1.5 * i / 3.0), w[i], 0.0);
        }
        for (int i = 1; i < 4; i++) {
            assertTrue("strictly decreasing", w[i] < w[i - 1]);
        }
    }

    @Test
    public void singleSliceTakesAll() {
        for (AlgoType algo : new AlgoType[] {AlgoType.TWAP, AlgoType.VWAP,
                AlgoType.IS}) {
            ParentOrder p = parent(algo, 777, 1, 1.0);
            double[] w = Algos.sliceWeights(p);
            assertEquals(1, w.length);
            assertEquals(1.0, w[0], 0.0);
            long[] q = Algos.sliceQuantities(p);
            assertEquals(777, q[0]);
        }
    }

    @Test
    public void largestRemainderSumsExactly() {
        // Apportionment must conserve the parent qty exactly for every
        // algo/qty/slice combination, incl. awkward ones.
        long[][] cases = {{1, 7}, {5, 7}, {97, 13}, {400, 4}, {600, 3},
                {999_983, 11}, {10, 4}};
        for (AlgoType algo : new AlgoType[] {AlgoType.TWAP, AlgoType.VWAP,
                AlgoType.IS}) {
            for (long[] c : cases) {
                ParentOrder p = parent(algo, c[0], (int) c[1], 1.0);
                long[] q = Algos.sliceQuantities(p);
                long sum = 0;
                for (long x : q) {
                    assertTrue("non-negative slice", x >= 0);
                    sum += x;
                }
                assertEquals(algo + " qty " + c[0] + "/" + c[1], c[0], sum);
            }
        }
    }

    @Test
    public void largestRemainderStaysWithinOneShare() {
        // Each integer slice differs from its real-valued target by < 1.
        ParentOrder p = parent(AlgoType.VWAP, 12_345, 9, 0.0);
        double[] w = Algos.sliceWeights(p);
        long[] q = Algos.sliceQuantities(p);
        double wsum = 0.0;
        for (double x : w) {
            wsum += x;
        }
        for (int i = 0; i < q.length; i++) {
            double target = p.qty * w[i] / wsum;
            assertTrue("slice " + i, Math.abs(q[i] - target) < 1.0);
        }
    }

    @Test
    public void remainderTiesGoToEarlierSlice() {
        // TWAP 10 over 4: targets 2.5 each, all fractional parts tie at .5;
        // the 2 leftover shares must land on slices 0 and 1 (pinned).
        long[] q = Algos.sliceQuantities(parent(AlgoType.TWAP, 10, 4, 0.0));
        assertEquals(3, q[0]);
        assertEquals(3, q[1]);
        assertEquals(2, q[2]);
        assertEquals(2, q[3]);
    }

    @Test
    public void goldenVwapQuantities() {
        // The replay-fills golden parent: VWAP 400 over 4 slices.
        long[] q = Algos.sliceQuantities(parent(AlgoType.VWAP, 400, 4, 0.0));
        assertEquals(129, q[0]);
        assertEquals(71, q[1]);
        assertEquals(71, q[2]);
        assertEquals(129, q[3]);
    }

    @Test
    public void goldenIsQuantities() {
        // The replay-fills golden parent: IS 600 over 3, risk_aversion 1.0.
        long[] q = Algos.sliceQuantities(parent(AlgoType.IS, 600, 3, 1.0));
        assertEquals(304, q[0]);
        assertEquals(184, q[1]);
        assertEquals(112, q[2]);
    }

    @Test
    public void sliceTimesUseIntegerDivision() {
        ParentOrder p = parent(AlgoType.TWAP, 100, 3, 0.0);
        p.startTs = 1000;
        p.endTs = 2000; // span 1000, 3 slices: 0, 333, 666 past start
        long[] due = Algos.sliceTimes(p);
        assertEquals(1000, due[0]);
        assertEquals(1333, due[1]); // floor(1*1000/3), NOT 1333.33 rounded
        assertEquals(1666, due[2]); // floor(2*1000/3)
        for (int i = 1; i < due.length; i++) {
            assertTrue(due[i] > due[i - 1]);
        }
        assertTrue(due[due.length - 1] < p.endTs);
    }

    @Test
    public void povHasNoPrecomputedSchedule() {
        try {
            Algos.sliceWeights(parent(AlgoType.POV, 100, 4, 0.0));
            throw new AssertionError("POV sliceWeights must throw");
        } catch (IllegalArgumentException expected) {
            // pinned: POV is event-driven
        }
        try {
            Algos.sliceQuantities(parent(AlgoType.POV, 100, 4, 0.0));
            throw new AssertionError("POV sliceQuantities must throw");
        } catch (IllegalArgumentException expected) {
            // pinned
        }
    }

    @Test
    public void invalidParentsRejected() {
        try {
            Algos.sliceWeights(parent(AlgoType.TWAP, 100, 0, 0.0));
            throw new AssertionError("0 slices must throw");
        } catch (IllegalArgumentException expected) {
            // pinned
        }
        try {
            Algos.sliceQuantities(parent(AlgoType.TWAP, 0, 4, 0.0));
            throw new AssertionError("0 qty must throw");
        } catch (IllegalArgumentException expected) {
            // pinned
        }
        ParentOrder p = parent(AlgoType.TWAP, 100, 4, 0.0);
        p.endTs = p.startTs; // empty window
        try {
            Algos.sliceTimes(p);
            throw new AssertionError("empty window must throw");
        } catch (IllegalArgumentException expected) {
            // pinned
        }
    }
}
