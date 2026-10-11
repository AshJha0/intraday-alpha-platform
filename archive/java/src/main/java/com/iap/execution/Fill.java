package com.iap.execution;

/**
 * One simulated fill. Aggressive fills are stamped with the child's
 * arrival_ts; passive fills with the triggering event's exchange_ts
 * (pinned rules 2/4 in {@link ExecutionSimulator}).
 */
public record Fill(
        long fillId,
        long orderId,
        long parentId,
        long instrumentId, // u32
        int venueId,       // u16
        int side,          // side of OUR order (0 buy / 1 sell)
        long priceTicks,
        long qty,
        long ts,           // exchange_ts of the fill
        Liquidity liquidity,
        double fee,        // > 0 cost, < 0 rebate
        double impactCost) { // linear impact charge (taker fills only)
}
