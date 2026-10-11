package com.iap.features;

/**
 * Rolling sums of an N-tuple of longs over a half-open event-time window
 * {@code (t - w, t]} on exchange_ts (API_FEATURES.md section 2). Mirrors the
 * C++ {@code RollingSum<int64,N>} / Python {@code rolling.RollingSum}
 * semantics exactly: {@link #add} appends a sample and evicts expired ones;
 * {@link #trim} evicts samples with {@code ts <= now - w}. Integer sums stay
 * exact.
 *
 * <p>Hot-path design: power-of-two ring buffers over primitive arrays.
 * Capacity doubles while a window is still filling; once the session's peak
 * window population has been seen no further allocation occurs.
 */
final class LongRollingSum {
    private final int n;
    private final long windowNs;
    private long[] ts;
    private long[] vals; // flattened: sample i occupies [i*n, i*n + n)
    private int head;
    private int count;
    private final long[] sums;

    LongRollingSum(int n, long windowNs) {
        this.n = n;
        this.windowNs = windowNs;
        this.ts = new long[16];
        this.vals = new long[16 * n];
        this.sums = new long[n];
    }

    private LongRollingSum(LongRollingSum o) {
        this.n = o.n;
        this.windowNs = o.windowNs;
        this.ts = o.ts.clone();
        this.vals = o.vals.clone();
        this.head = o.head;
        this.count = o.count;
        this.sums = o.sums.clone();
    }

    /** Deep copy (checkpoint support). */
    LongRollingSum copy() {
        return new LongRollingSum(this);
    }

    /** Append a sample (vals length must be n), then evict expired samples. */
    void add(long t, long[] sample) {
        if (count == ts.length) {
            grow();
        }
        int slot = (head + count) & (ts.length - 1);
        ts[slot] = t;
        for (int i = 0; i < n; i++) {
            vals[slot * n + i] = sample[i];
            sums[i] += sample[i];
        }
        count++;
        trim(t);
    }

    /** Evict samples with {@code ts <= now - window}. */
    void trim(long now) {
        long cutoff = now - windowNs;
        while (count > 0 && ts[head] <= cutoff) {
            for (int i = 0; i < n; i++) {
                sums[i] -= vals[head * n + i];
            }
            head = (head + 1) & (ts.length - 1);
            count--;
        }
    }

    long sum(int i) {
        return sums[i];
    }

    int count() {
        return count;
    }

    private void grow() {
        int cap = ts.length * 2;
        long[] nts = new long[cap];
        long[] nvals = new long[cap * n];
        for (int i = 0; i < count; i++) {
            int slot = (head + i) & (ts.length - 1);
            nts[i] = ts[slot];
            System.arraycopy(vals, slot * n, nvals, i * n, n);
        }
        ts = nts;
        vals = nvals;
        head = 0;
    }
}
