package com.iap.execution;

/**
 * Internal (decision to wire) latency legs; the venue leg comes from
 * {@link VenueSpec}. Defaults pinned by the golden replay-fills scenario.
 */
public record LatencyConfig(long decisionNs, long riskNs, long wireNs) {
    public static final LatencyConfig DEFAULT =
            new LatencyConfig(50_000, 50_000, 100_000);
}
