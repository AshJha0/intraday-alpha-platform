package com.iap.tca;

import com.iap.execution.Liquidity;

/**
 * One child execution of a parent order, stamped with its reference market
 * state (mid, half-spread, displayed contra depth) — pinned §2.4: the state
 * prevailing at {@code ts} for a TAKER fill, the state strictly BEFORE
 * {@code ts} for a MAKER fill (see {@link #stamp}).
 */
public record TcaFill(long ts, double price, long qty, double midAtFill,
        double halfSpreadAtFill, long oppDepthAtFill, Liquidity liquidity) {
    public TcaFill {
        if (qty <= 0) {
            throw new IllegalArgumentException("fill qty must be > 0: " + qty);
        }
    }

    /** Taker fill (state prevailing at ts). */
    public TcaFill(long ts, double price, long qty, double midAtFill,
            double halfSpreadAtFill, long oppDepthAtFill) {
        this(ts, price, qty, midAtFill, halfSpreadAtFill, oppDepthAtFill,
                Liquidity.TAKER);
    }

    /**
     * Stamp a fill from a timeline with the pinned reference state
     * ({@code prevailing(ts)} for TAKER, {@code prevailing(ts - 1)} for
     * MAKER); throws when no state prevails (TCA never guesses).
     */
    public static TcaFill stamp(MarketTimeline timeline, long ts, double price,
            long qty, int side, Liquidity liquidity) {
        if (side < 0 || side > 1) {
            throw new IllegalArgumentException("side must be 0 or 1");
        }
        long refTs = liquidity == Liquidity.TAKER ? ts : ts - 1;
        int i = timeline.prevailing(refTs);
        if (i < 0) {
            throw new IllegalArgumentException(
                    "fill at " + ts + " precedes the first market state");
        }
        long opp = side == 0 ? timeline.askSize(i) : timeline.bidSize(i);
        return new TcaFill(ts, price, qty, timeline.mid(i), timeline.halfSpread(i),
                opp, liquidity);
    }
}
