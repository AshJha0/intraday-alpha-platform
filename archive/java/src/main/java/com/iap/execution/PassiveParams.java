package com.iap.execution;

/**
 * Parameters of the {@link ExecPolicy#PASSIVE} policy (mirror of the C++
 * {@code PassiveParams}; {@link #DEFAULT} holds the pinned defaults).
 *
 * @param maxRestNs             rest time of a posted child at urgency 0
 * @param maxReprices           reprices / rest extensions before crossing
 * @param maxBehindFraction     schedule-behind tolerance, share of the parent qty
 * @param improveMinSpreadTicks post one tick inside when the spread is at
 *                              least this many ticks (0 = never)
 * @param endMarginNs           no child rests this close to the parent's end_ts
 */
public record PassiveParams(long maxRestNs, int maxReprices,
        double maxBehindFraction, long improveMinSpreadTicks, long endMarginNs) {

    /** The pinned defaults (30 s, 1, 0.1, 3 ticks, 1 s). */
    public static final PassiveParams DEFAULT =
            new PassiveParams(30_000_000_000L, 1, 0.1, 3, 1_000_000_000L);

    public PassiveParams {
        if (maxRestNs < 0 || endMarginNs < 0) {
            throw new IllegalArgumentException(
                    "max_rest_ns and end_margin_ns must be >= 0");
        }
        if (maxReprices < 0) {
            throw new IllegalArgumentException("max_reprices must be >= 0");
        }
        if (!(maxBehindFraction >= 0.0 && maxBehindFraction <= 1.0)) {
            throw new IllegalArgumentException(
                    "max_behind_fraction must be in [0, 1]");
        }
        if (improveMinSpreadTicks < 0) {
            throw new IllegalArgumentException(
                    "improve_min_spread_ticks must be >= 0");
        }
    }
}
