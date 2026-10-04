package com.iap.execution;

/**
 * The pure rules of the {@link ExecPolicy#PASSIVE} execution policy, a
 * mirror of the C++ reference ({@code cpp/include/iap/execution/algos.hpp},
 * "EXECUTION POLICIES"); {@code tests/golden/expected_replay_fills_passive.json}
 * pins the three ports.
 *
 * <p>State machine, per posted child, evaluated by {@link ExecutionReplay}
 * after every market event against the post-event books:
 * <ul>
 *   <li><b>POST</b> — the schedule step (split at max_child_qty) is posted
 *       as a LIMIT at {@link #postPrice}: the same-side best of the routed
 *       venue, improved by one tick when the spread is at least
 *       {@code improveMinSpreadTicks}, never at or through the opposite
 *       touch. With no same-side quote, zero patience, or at or after
 *       {@code end_ts - endMarginNs}, the child is a MARKET order.</li>
 *   <li><b>REST</b> — until {@code min(decision_ts + patience, end_ts -
 *       endMarginNs)} or until the schedule is behind:
 *       {@code scheduled(t) - filled(t) - q_cur > floor(maxBehindFraction *
 *       qty)}, with {@code q_cur} the most recent slice (0 for POV).</li>
 *   <li><b>REPRICE</b> — at the deadline while reprices remain: an unchanged
 *       post price keeps the queue position and renews the deadline;
 *       otherwise cancel and, once the cancel has taken effect, re-post the
 *       unfilled remainder.</li>
 *   <li><b>CROSS</b> — at the deadline with no reprice left, or as soon as
 *       the schedule is behind: cancel and, once the cancel has taken
 *       effect, send the unfilled remainder as a MARKET order.</li>
 * </ul>
 * Only the quantity actually cancelled is re-sent, and nothing is re-sent
 * at or after end_ts.
 */
public final class PassivePolicy {
    private PassivePolicy() {
    }

    /**
     * Rest time of one posted child:
     * {@code floor(maxRestNs * (1 - urgency) * k)}, urgency clamped to
     * [0, 1], {@code k = exp(-riskAversion)} for IS and 1 otherwise.
     */
    public static long patienceNs(PassiveParams params, double urgency,
            boolean isAlgo, double riskAversion) {
        double u = Math.min(Math.max(urgency, 0.0), 1.0);
        double x = (double) params.maxRestNs() * (1.0 - u);
        if (isAlgo) {
            x *= Math.exp(-riskAversion);
        }
        return (long) Math.floor(x);
    }

    /**
     * Limit price of a posted child from the routed venue's touches
     * ({@code {price_ticks, qty}} or null) at decision time; 0 when it
     * cannot be posted (no same-side quote). Never at or through the
     * opposite touch.
     */
    public static long postPrice(int side, long[] bestBid, long[] bestAsk,
            long improveMinSpreadTicks) {
        long[] own = side == 0 ? bestBid : bestAsk;
        if (own == null) {
            return 0;
        }
        long[] opp = side == 0 ? bestAsk : bestBid;
        long price = own[0];
        if (opp != null) {
            long spread = side == 0 ? opp[0] - own[0] : own[0] - opp[0];
            if (improveMinSpreadTicks > 0 && spread >= improveMinSpreadTicks) {
                price += side == 0 ? 1 : -1;
            }
            if (side == 0 && price >= opp[0]) {
                price = opp[0] - 1;
            } else if (side == 1 && price <= opp[0]) {
                price = opp[0] + 1;
            }
        }
        return price > 0 ? price : 0;
    }

    /** {@code floor(maxBehindFraction * qty)}: the BEHIND tolerance. */
    public static long maxBehindQty(PassiveParams params, long parentQty) {
        return (long) Math.floor(params.maxBehindFraction() * (double) parentQty);
    }
}
