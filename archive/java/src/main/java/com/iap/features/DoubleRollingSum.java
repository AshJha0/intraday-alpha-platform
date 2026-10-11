package com.iap.features;

/**
 * Rolling sum of a single double over a half-open event-time window
 * {@code (t - w, t]} (see {@link LongRollingSum}). Float samples are added
 * and subtracted in the same FIFO order as the C++/Python references so
 * incremental drift stays well inside the 1e-9 golden tolerance.
 */
final class DoubleRollingSum {
    private final long windowNs;
    private long[] ts;
    private double[] vals;
    private int head;
    private int count;
    private double sum;

    DoubleRollingSum(long windowNs) {
        this.windowNs = windowNs;
        this.ts = new long[16];
        this.vals = new double[16];
    }

    private DoubleRollingSum(DoubleRollingSum o) {
        this.windowNs = o.windowNs;
        this.ts = o.ts.clone();
        this.vals = o.vals.clone();
        this.head = o.head;
        this.count = o.count;
        this.sum = o.sum;
    }

    /** Deep copy (checkpoint support). */
    DoubleRollingSum copy() {
        return new DoubleRollingSum(this);
    }

    /** Append a sample, then evict expired samples. */
    void add(long t, double v) {
        if (count == ts.length) {
            grow();
        }
        int slot = (head + count) & (ts.length - 1);
        ts[slot] = t;
        vals[slot] = v;
        sum += v;
        count++;
        trim(t);
    }

    /** Evict samples with {@code ts <= now - window}. */
    void trim(long now) {
        long cutoff = now - windowNs;
        while (count > 0 && ts[head] <= cutoff) {
            sum -= vals[head];
            head = (head + 1) & (ts.length - 1);
            count--;
        }
    }

    double sum() {
        return sum;
    }

    int count() {
        return count;
    }

    private void grow() {
        int cap = ts.length * 2;
        long[] nts = new long[cap];
        double[] nvals = new double[cap];
        for (int i = 0; i < count; i++) {
            int slot = (head + i) & (ts.length - 1);
            nts[i] = ts[slot];
            nvals[i] = vals[slot];
        }
        ts = nts;
        vals = nvals;
        head = 0;
    }
}
