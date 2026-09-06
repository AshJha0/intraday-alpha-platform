package com.iap.risk;

/**
 * A fill notification (from the venue layer / drop copy).
 * {@code orderId == 0} means an external adjustment with no open order.
 * Validation happens in {@link RiskEngine#onFill}: a malformed fill is
 * audited as {@code MALFORMED_FILL} and dropped (mirrors the Rust
 * reference), never thrown.
 */
public record RiskFill(long ts, String strategyId, long instrumentId,
        long orderId, int side, long qty, long priceTicks) {
}
