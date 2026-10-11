package com.iap.execution;

/**
 * Parent-algo schedule construction (spec section 17). PINNED SCHEDULES
 * (the C++ port is the reference for tests/golden/expected_replay_fills.json):
 *
 * <ul>
 *   <li>Slice decision times (TWAP/VWAP/IS): slice i of N is due at
 *       {@code due_i = start_ts + i * (end_ts - start_ts) / N} (integer
 *       division) and is issued while processing the first event with
 *       {@code exchange_ts >= due_i}. Slice weights map to integer child
 *       quantities by largest-remainder apportionment (floor each target,
 *       hand remaining shares to the largest fractional parts, ties to the
 *       earlier slice) — quantities sum exactly to the parent qty.</li>
 *   <li>TWAP: equal weights (w_i = 1).</li>
 *   <li>VWAP: pinned U-shaped session volume curve ("time_of_day_default"):
 *       {@code w_i = 1 + x_i^2, x_i = (2i - (N-1)) / (N-1)} (N &gt;= 2;
 *       N = 1: all).</li>
 *   <li>IS: front-loaded exponential decay
 *       {@code w_i = exp(-risk_aversion * i / max(1, N-1))}.</li>
 *   <li>POV: no precomputed slices — event-driven (see
 *       {@link ExecutionReplay}): after each in-window TRADE, target =
 *       floor(participation * volume) and the deficit against the qty
 *       COMMITTED (filled + open/in-flight children — a cancelled MARKET
 *       remainder frees its qty and is re-sent) is covered by one child
 *       (capped at max_child_qty and the parent remainder).</li>
 *   <li>Child sizing: a slice larger than max_child_qty is split into
 *       ceil(slice / max_child_qty) children of max_child_qty (the last one
 *       the remainder), all decided at the same event, in order — no
 *       quantity is ever silently dropped.</li>
 *   <li>Time-in-force: every child carries expireTs = end_ts (simulator
 *       rule 7): no child outlives its parent's window; an unfilled slice
 *       at end_ts is reported as unfilled_qty, never filled later.</li>
 * </ul>
 */
public final class Algos {
    private Algos() {
    }

    /** Slice weights for TWAP/VWAP/IS (throws for POV — event-driven). */
    public static double[] sliceWeights(ParentOrder parent) {
        if (parent.algo == AlgoType.POV) {
            throw new IllegalArgumentException("POV has no precomputed slice weights");
        }
        int n = parent.slices;
        if (n <= 0) {
            throw new IllegalArgumentException("slices must be > 0");
        }
        double[] w = new double[n];
        java.util.Arrays.fill(w, 1.0);
        if (n == 1) {
            return w;
        }
        for (int i = 0; i < n; i++) {
            switch (parent.algo) {
                case TWAP -> w[i] = 1.0;
                case VWAP -> {
                    double x = (2.0 * i - (n - 1)) / (n - 1);
                    w[i] = 1.0 + x * x;
                }
                case IS -> w[i] = Math.exp(-parent.riskAversion * i / (double) (n - 1));
                case POV -> {
                    // unreachable
                }
            }
        }
        return w;
    }

    /** Integer child quantities per slice (largest remainder; sums to qty). */
    public static long[] sliceQuantities(ParentOrder parent) {
        if (parent.qty <= 0) {
            throw new IllegalArgumentException("parent qty must be > 0");
        }
        double[] w = sliceWeights(parent);
        double wsum = 0.0;
        for (double x : w) {
            wsum += x;
        }
        int n = w.length;
        long[] q = new long[n];
        double[] frac = new double[n];
        Integer[] order = new Integer[n];
        long assigned = 0;
        for (int i = 0; i < n; i++) {
            double target = (double) parent.qty * w[i] / wsum;
            q[i] = (long) Math.floor(target);
            assigned += q[i];
            frac[i] = target - Math.floor(target);
            order[i] = i;
        }
        // Largest remainder; ties resolved toward the earlier slice.
        java.util.Arrays.sort(order, (a, b) -> {
            if (frac[a] != frac[b]) {
                return frac[a] > frac[b] ? -1 : 1;
            }
            return Integer.compare(a, b);
        });
        long left = parent.qty - assigned;
        for (int i = 0; left > 0 && i < n; i++) {
            q[order[i]]++;
            left--;
        }
        return q;
    }

    /** Due time of each slice (integer-division spacing; see class doc). */
    public static long[] sliceTimes(ParentOrder parent) {
        if (parent.endTs <= parent.startTs) {
            throw new IllegalArgumentException("parent window must have end_ts > start_ts");
        }
        int n = parent.slices;
        if (n <= 0) {
            throw new IllegalArgumentException("slices must be > 0");
        }
        long[] out = new long[n];
        long span = parent.endTs - parent.startTs;
        for (int i = 0; i < n; i++) {
            out[i] = parent.startTs + (long) i * span / n;
        }
        return out;
    }
}
