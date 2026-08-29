package com.iap.risk;

/**
 * A fill notification (from the venue layer / drop copy).
 * {@code orderId == 0} means an external adjustment with no resting order.
 */
public record RiskFill(long ts, String strategyId, long instrumentId,
        long orderId, int side, long qty, long priceTicks) {
    public RiskFill {
        if (qty <= 0) {
            throw new IllegalArgumentException("fill qty must be > 0: " + qty);
        }
        if (side < 0 || side > 1) {
            throw new IllegalArgumentException("fill side must be 0 or 1: " + side);
        }
    }
}
