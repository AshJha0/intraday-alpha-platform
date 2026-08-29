package com.iap.orderbook;

import java.util.Arrays;

/**
 * Exact-integer state summary of one venue book — the golden-comparable shape
 * of {@code expected_book_states.json} entries. Depth/order-count arrays are
 * best-first {@code [price_ticks, value]} pairs.
 */
public final class BookState {
    public final long bestBidTicks;
    public final long bestBidSize;
    public final long bestAskTicks;
    public final long bestAskSize;
    public final long[][] depthBidTop5;
    public final long[][] depthAskTop5;
    public final long[][] orderCountBidTop3;
    public final long[][] orderCountAskTop3;
    public final long tradeFlow;
    public final long sequence;

    public BookState(
            long bestBidTicks, long bestBidSize, long bestAskTicks, long bestAskSize,
            long[][] depthBidTop5, long[][] depthAskTop5,
            long[][] orderCountBidTop3, long[][] orderCountAskTop3,
            long tradeFlow, long sequence) {
        this.bestBidTicks = bestBidTicks;
        this.bestBidSize = bestBidSize;
        this.bestAskTicks = bestAskTicks;
        this.bestAskSize = bestAskSize;
        this.depthBidTop5 = depthBidTop5;
        this.depthAskTop5 = depthAskTop5;
        this.orderCountBidTop3 = orderCountBidTop3;
        this.orderCountAskTop3 = orderCountAskTop3;
        this.tradeFlow = tradeFlow;
        this.sequence = sequence;
    }

    @Override
    public boolean equals(Object obj) {
        if (this == obj) {
            return true;
        }
        if (!(obj instanceof BookState o)) {
            return false;
        }
        return bestBidTicks == o.bestBidTicks
                && bestBidSize == o.bestBidSize
                && bestAskTicks == o.bestAskTicks
                && bestAskSize == o.bestAskSize
                && Arrays.deepEquals(depthBidTop5, o.depthBidTop5)
                && Arrays.deepEquals(depthAskTop5, o.depthAskTop5)
                && Arrays.deepEquals(orderCountBidTop3, o.orderCountBidTop3)
                && Arrays.deepEquals(orderCountAskTop3, o.orderCountAskTop3)
                && tradeFlow == o.tradeFlow
                && sequence == o.sequence;
    }

    @Override
    public int hashCode() {
        int h = Long.hashCode(bestBidTicks);
        h = 31 * h + Long.hashCode(bestBidSize);
        h = 31 * h + Long.hashCode(bestAskTicks);
        h = 31 * h + Long.hashCode(bestAskSize);
        h = 31 * h + Arrays.deepHashCode(depthBidTop5);
        h = 31 * h + Arrays.deepHashCode(depthAskTop5);
        h = 31 * h + Arrays.deepHashCode(orderCountBidTop3);
        h = 31 * h + Arrays.deepHashCode(orderCountAskTop3);
        h = 31 * h + Long.hashCode(tradeFlow);
        h = 31 * h + Long.hashCode(sequence);
        return h;
    }

    @Override
    public String toString() {
        return "BookState(best_bid=" + bestBidTicks + "x" + bestBidSize
                + ", best_ask=" + bestAskTicks + "x" + bestAskSize
                + ", depth_bid_top5=" + Arrays.deepToString(depthBidTop5)
                + ", depth_ask_top5=" + Arrays.deepToString(depthAskTop5)
                + ", order_count_bid_top3=" + Arrays.deepToString(orderCountBidTop3)
                + ", order_count_ask_top3=" + Arrays.deepToString(orderCountAskTop3)
                + ", trade_flow=" + tradeFlow
                + ", sequence=" + Long.toUnsignedString(sequence) + ")";
    }
}
