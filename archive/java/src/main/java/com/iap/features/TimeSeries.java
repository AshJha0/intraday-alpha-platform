package com.iap.features;

/**
 * Append-only (ts, value) series with at-or-before lookup and trimming — the
 * mid-history structure behind returns/history lookups (API_FEATURES.md
 * section 2: {@code x(t - h)} = latest sample at-or-before, no
 * interpolation). Mirrors the C++/Python {@code TimeSeries} (compaction at
 * 4096). Values are stored as doubles; the integer mid2 history fits a
 * double exactly for all realistic tick counts, and the engine keeps a
 * separate long series where exactness matters.
 */
final class TimeSeries {
    private static final int COMPACT_AT = 4096;

    private long[] ts;
    private double[] vals;
    private int size;
    private int start;

    TimeSeries() {
        ts = new long[64];
        vals = new double[64];
    }

    private TimeSeries(TimeSeries o) {
        this.ts = o.ts.clone();
        this.vals = o.vals.clone();
        this.size = o.size;
        this.start = o.start;
    }

    /** Deep copy (checkpoint support). */
    TimeSeries copy() {
        return new TimeSeries(this);
    }

    void append(long t, double v) {
        if (size == ts.length) {
            long[] nts = new long[ts.length * 2];
            double[] nvals = new double[ts.length * 2];
            System.arraycopy(ts, 0, nts, 0, size);
            System.arraycopy(vals, 0, nvals, 0, size);
            ts = nts;
            vals = nvals;
        }
        ts[size] = t;
        vals[size] = v;
        size++;
    }

    /**
     * Latest value with {@code ts <= t}; NaN when no sample is that early.
     * (All stored values are finite, so NaN is unambiguous.)
     */
    double atOrBefore(long t) {
        int idx = upperBound(t, start);
        if (idx == start) {
            return Double.NaN;
        }
        return vals[idx - 1];
    }

    boolean isEmpty() {
        return size == start;
    }

    double last() {
        return vals[size - 1];
    }

    /**
     * Forget samples with {@code ts < minTs}, keeping the newest
     * at-or-before one (so {@link #atOrBefore} stays correct at the trim
     * boundary).
     */
    void trim(long minTs) {
        int i = upperBound(minTs, 0);
        if (i > 0) {
            start = Math.max(start, i - 1);
        }
        if (start >= COMPACT_AT) {
            System.arraycopy(ts, start, ts, 0, size - start);
            System.arraycopy(vals, start, vals, 0, size - start);
            size -= start;
            start = 0;
        }
    }

    /** First index in [lo, size) with ts[index] > t. */
    private int upperBound(long t, int lo) {
        int hi = size;
        while (lo < hi) {
            int mid = (lo + hi) >>> 1;
            if (ts[mid] <= t) {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        return lo;
    }
}
