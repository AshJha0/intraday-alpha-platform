package com.iap.tca;

/**
 * One child execution of a parent order, stamped with the prevailing market
 * state at the fill instant (mid, half-spread, displayed contra depth).
 */
public record TcaFill(long ts, double price, long qty, double midAtFill,
        double halfSpreadAtFill, long oppDepthAtFill) {
    public TcaFill {
        if (qty <= 0) {
            throw new IllegalArgumentException("fill qty must be > 0: " + qty);
        }
    }
}
