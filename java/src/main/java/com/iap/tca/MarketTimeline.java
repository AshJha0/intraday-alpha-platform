package com.iap.tca;

import java.util.ArrayList;
import java.util.List;

/**
 * Event-time market state series with prevailing-state lookup (spec §19).
 * Prices are research doubles (already converted from ticks); timestamps
 * int ns. {@code prevailing(t)} returns the index of the latest state with
 * {@code ts <= t} (or -1 before the first state) — exactly the
 * label-alignment convention (no lookahead).
 */
public final class MarketTimeline {
    private final List<Long> ts = new ArrayList<>();
    private final List<Double> bid = new ArrayList<>();
    private final List<Double> ask = new ArrayList<>();
    private final List<Long> bidSz = new ArrayList<>();
    private final List<Long> askSz = new ArrayList<>();
    private final List<long[]> tradeTsQty = new ArrayList<>();
    private final List<Double> tradePx = new ArrayList<>();

    /** Append one BBO state (non-decreasing ts, uncrossed). */
    public void append(long t, double bidPx, double askPx, long bidSize,
            long askSize) {
        if (!ts.isEmpty() && t < ts.get(ts.size() - 1)) {
            throw new IllegalArgumentException(
                    "timeline timestamps must be non-decreasing");
        }
        if (askPx < bidPx) {
            throw new IllegalArgumentException("crossed timeline state");
        }
        ts.add(t);
        bid.add(bidPx);
        ask.add(askPx);
        bidSz.add(bidSize);
        askSz.add(askSize);
    }

    /** Record one market trade print. */
    public void addTrade(long t, double price, long qty) {
        tradeTsQty.add(new long[] {t, qty});
        tradePx.add(price);
    }

    /** Number of BBO states. */
    public int size() {
        return ts.size();
    }

    /** Number of trade prints. */
    public int tradeCount() {
        return tradePx.size();
    }

    /** Timestamp of state i. */
    public long ts(int i) {
        return ts.get(i);
    }

    /** Mid of state i. */
    public double mid(int i) {
        return 0.5 * (bid.get(i) + ask.get(i));
    }

    /** Half-spread of state i. */
    public double halfSpread(int i) {
        return 0.5 * (ask.get(i) - bid.get(i));
    }

    /** Displayed bid size of state i. */
    public long bidSize(int i) {
        return bidSz.get(i);
    }

    /** Displayed ask size of state i. */
    public long askSize(int i) {
        return askSz.get(i);
    }

    /** Index of the latest state with ts &lt;= t, or -1. */
    public int prevailing(long t) {
        int lo = 0;
        int hi = ts.size(); // first index with ts > t
        while (lo < hi) {
            int m = (lo + hi) >>> 1;
            if (ts.get(m) <= t) {
                lo = m + 1;
            } else {
                hi = m;
            }
        }
        return lo - 1;
    }

    /** Prevailing mid at time t (NaN before the first state). */
    public double midAt(long t) {
        int i = prevailing(t);
        return i < 0 ? Double.NaN : mid(i);
    }

    /** Prevailing half-spread at time t (NaN before the first state). */
    public double halfSpreadAt(long t) {
        int i = prevailing(t);
        return i < 0 ? Double.NaN : halfSpread(i);
    }

    /** Market VWAP of trades in {@code [startTs, endTs]} (null if none). */
    public Double intervalVwap(long startTs, long endTs) {
        double num = 0.0;
        long den = 0;
        for (int i = 0; i < tradePx.size(); i++) {
            long t = tradeTsQty.get(i)[0];
            if (startTs <= t && t <= endTs) {
                num += tradePx.get(i) * (double) tradeTsQty.get(i)[1];
                den += tradeTsQty.get(i)[1];
            }
        }
        return den > 0 ? num / den : null;
    }

    /**
     * Time-weighted prevailing mid over {@code [startTs, endTs)} (null when
     * no state prevails at startTs).
     */
    public Double intervalTwap(long startTs, long endTs) {
        if (endTs <= startTs) {
            throw new IllegalArgumentException("end_ts must exceed start_ts");
        }
        int i = prevailing(startTs);
        if (i < 0) {
            return null;
        }
        double total = 0.0;
        long t = startTs;
        while (i + 1 < ts.size() && ts.get(i + 1) < endTs) {
            long nxt = Math.max(ts.get(i + 1), startTs);
            total += mid(i) * (double) (nxt - t);
            t = nxt;
            i++;
        }
        total += mid(i) * (double) (endTs - t);
        return total / (double) (endTs - startTs);
    }
}
