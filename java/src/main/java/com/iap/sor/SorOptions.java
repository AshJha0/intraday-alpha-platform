package com.iap.sor;

/**
 * configs/execution.json {@code sor} block: rebate preference for passive
 * routing and the venue latency budget (mean latency above it makes a venue
 * ineligible).
 */
public record SorOptions(boolean preferRebate, long maxVenueLatencyNs) {
    /** Rebates preferred, no latency budget. */
    public static final SorOptions DEFAULT = new SorOptions(true, Long.MAX_VALUE);
}
