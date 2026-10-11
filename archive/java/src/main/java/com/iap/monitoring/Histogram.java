package com.iap.monitoring;

import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicLongArray;
import java.util.concurrent.atomic.DoubleAdder;

/**
 * Fixed-bucket log2 histogram of non-negative long samples (latency in ns,
 * sizes, ...), mirroring rust/telemetry semantics exactly: 65 buckets,
 * bucket 0 holds the value 0, bucket {@code i >= 1} holds values in
 * {@code [2^(i-1), 2^i)}. Quantiles (p50/p99/p999) are answered from the
 * cumulative counts as the <b>inclusive upper bound</b> of the bucket
 * containing the rank-{@code ceil(q*count)} sample (1-based) — conservative
 * for latency: the reported value is always &gt;= the exact order statistic
 * and within one power of two of it.
 *
 * <p>Lock-free (PLATFORM_CONVENTIONS.md §12.4): every field is an atomic so
 * the exposition thread reads while the trading thread records. A reader
 * observes each field atomically but the set of fields is not a single
 * snapshot; the exposition writer therefore derives {@code +Inf} and
 * {@code _count} from one read of the bucket array so the cumulative series
 * is always internally consistent.
 */
public final class Histogram {
    /** Number of buckets: value 0 plus one bucket per power of two. */
    public static final int BUCKETS = 65;

    private final AtomicLongArray counts = new AtomicLongArray(BUCKETS);
    private final AtomicLong total = new AtomicLong();
    private final DoubleAdder sum = new DoubleAdder();
    private final AtomicLong max = new AtomicLong();

    /** Bucket index for a value: 0 for 0, else {@code 64 - nlz(value)}. */
    public static int bucketOf(long value) {
        if (value < 0) {
            throw new IllegalArgumentException("histogram samples must be >= 0: " + value);
        }
        return value == 0 ? 0 : 64 - Long.numberOfLeadingZeros(value);
    }

    /** Inclusive upper bound of a bucket ({@code 2^i - 1}; 0 for bucket 0). */
    public static long bucketUpper(int index) {
        if (index < 0 || index >= BUCKETS) {
            throw new IllegalArgumentException("bucket index out of range: " + index);
        }
        if (index == 0) {
            return 0;
        }
        if (index == 64) {
            return Long.MAX_VALUE; // saturating top bucket
        }
        return (1L << index) - 1;
    }

    /** Record one sample (&gt;= 0). */
    public void record(long value) {
        counts.incrementAndGet(bucketOf(value));
        total.incrementAndGet();
        sum.add((double) value);
        max.accumulateAndGet(value, Math::max);
    }

    /** Number of recorded samples. */
    public long count() {
        return total.get();
    }

    /** Sum of recorded samples (double; exact for realistic ns totals). */
    public double sum() {
        return sum.sum();
    }

    /** Largest recorded sample (0 when empty). */
    public long max() {
        return max.get();
    }

    /** Copy of the bucket counts (a consistent-enough snapshot for rendering). */
    public long[] buckets() {
        long[] out = new long[BUCKETS];
        for (int i = 0; i < BUCKETS; i++) {
            out[i] = counts.get(i);
        }
        return out;
    }

    /**
     * Quantile estimate for {@code q} in [0, 1]: the inclusive upper bound
     * of the bucket holding the rank-{@code ceil(q*count)} sample.
     *
     * @throws IllegalStateException when the histogram is empty
     * @throws IllegalArgumentException when q is not a finite value in [0, 1]
     */
    public long quantile(double q) {
        if (!(Double.isFinite(q) && q >= 0.0 && q <= 1.0)) {
            throw new IllegalArgumentException("quantile must be in [0, 1]: " + q);
        }
        long[] c = buckets();
        long n = 0;
        for (long v : c) {
            n += v;
        }
        if (n == 0) {
            throw new IllegalStateException("empty histogram has no quantiles");
        }
        long rank = Math.max((long) Math.ceil(q * (double) n), 1L);
        long cum = 0;
        for (int i = 0; i < BUCKETS; i++) {
            cum += c[i];
            if (cum >= rank) {
                return bucketUpper(i);
            }
        }
        return bucketUpper(BUCKETS - 1);
    }

    /** {p50, p99, p999} convenience triple. */
    public long[] p50p99p999() {
        return new long[] {quantile(0.50), quantile(0.99), quantile(0.999)};
    }
}
