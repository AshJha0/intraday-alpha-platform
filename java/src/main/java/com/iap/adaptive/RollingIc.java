package com.iap.adaptive;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.List;

/**
 * Rolling realized IC (the {@code alpha_rolling_ic} gauge) — the Java
 * mirror of the pinned Python semantics (API_ADAPTIVE.md section 4;
 * {@code iap.validation.metrics.bucket_ics}): at evaluation time
 * {@code T}, take the MATURED rows ({@code ts + horizon_ns <= T}) with
 * {@code ts} in {@code [T - ic_window_ns, T)}, compute one Pearson IC of
 * signal vs realized forward mid return per fixed event-time bucket
 * ({@code ts / bucket_ns}; a bucket needs {@code >= 8} pairs and
 * nondegenerate variance to count), and report the MEAN of the bucket
 * ICs — or {@code NaN} with fewer than {@code min_ic_buckets} buckets
 * (a monitor with no data must not report health).
 *
 * <p>Strictly event-time and lookahead-free: a signal observed at
 * {@code ts} with mid {@code m0} stays pending until the first later
 * observation at {@code ts' >= ts + horizon_ns}, whose mid {@code m1}
 * realizes the forward return {@code m1/m0 - 1}; the pair enters the
 * matured set only at {@code ts'}. Rows with {@code confidence <= 0} are
 * excluded by the caller (invalid signal rows, pinned).
 */
public final class RollingIc {
    /** Pinned per-bucket minimum pair count (metrics.bucket_ics min_obs). */
    public static final int MIN_BUCKET_PAIRS = 8;

    /** Pinned degenerate-variance guard (population std, ddof 0). */
    public static final double STD_EPS = 1e-12;

    private static final class Row {
        final long ts;
        final double signal;
        final double value; // pending: mid at signal time; matured: return

        Row(long ts, double signal, double value) {
            this.ts = ts;
            this.signal = signal;
            this.value = value;
        }
    }

    private final long horizonNs;
    private final long windowNs;
    private final long bucketNs;
    private final int minBuckets;
    private final ArrayDeque<Row> pending = new ArrayDeque<>();
    private final ArrayDeque<Row> matured = new ArrayDeque<>();

    /**
     * @param horizonNs alpha horizon (label maturity)
     * @param windowNs rolling evaluation window
     *     (configs/strategies.json adaptive.ic_window_ns)
     * @param bucketNs event-time IC bucket (adaptive.ic_bucket_ns)
     * @param minBuckets minimum live buckets (adaptive.min_ic_buckets)
     */
    public RollingIc(long horizonNs, long windowNs, long bucketNs,
            int minBuckets) {
        if (horizonNs <= 0 || windowNs <= 0 || bucketNs <= 0
                || minBuckets < 1) {
            throw new IllegalArgumentException("horizonNs, windowNs and "
                    + "bucketNs must be > 0 and minBuckets >= 1");
        }
        this.horizonNs = horizonNs;
        this.windowNs = windowNs;
        this.bucketNs = bucketNs;
        this.minBuckets = minBuckets;
    }

    /**
     * Parse an alpha-params horizon string ({@code "500ms"}, {@code "1s"},
     * {@code "5m"}, ...) to nanoseconds.
     */
    public static long parseHorizonNs(String horizon) {
        String h = horizon.trim();
        long unit;
        String num;
        if (h.endsWith("ms")) {
            unit = 1_000_000L;
            num = h.substring(0, h.length() - 2);
        } else if (h.endsWith("s")) {
            unit = 1_000_000_000L;
            num = h.substring(0, h.length() - 1);
        } else if (h.endsWith("m")) {
            unit = 60_000_000_000L;
            num = h.substring(0, h.length() - 1);
        } else if (h.endsWith("h")) {
            unit = 3_600_000_000_000L;
            num = h.substring(0, h.length() - 1);
        } else {
            throw new IllegalArgumentException("unparseable horizon " + horizon);
        }
        return Long.parseLong(num) * unit;
    }

    /**
     * One observation: first realizes every pending signal whose horizon
     * has elapsed by {@code ts} (forward return measured to the current
     * mid), then enqueues this signal. Timestamps must be non-decreasing
     * (event time). Rows that can no longer enter any future evaluation
     * window are evicted.
     */
    public void onObservation(long ts, double signal, double mid) {
        while (!pending.isEmpty()
                && ts - pending.peekFirst().ts >= horizonNs) {
            Row p = pending.pollFirst();
            matured.addLast(new Row(p.ts, p.signal, mid / p.value - 1.0));
        }
        while (!matured.isEmpty() && matured.peekFirst().ts < ts - windowNs) {
            matured.pollFirst();
        }
        pending.addLast(new Row(ts, signal, mid));
    }

    /** Matured pairs currently retained (diagnostics/tests). */
    public int pairs() {
        return matured.size();
    }

    /**
     * Rolling realized IC at evaluation time {@code nowTs}: mean Pearson
     * bucket IC over matured rows with {@code ts} in
     * {@code [nowTs - windowNs, nowTs)}; {@code NaN} with fewer than
     * {@code minBuckets} valid buckets.
     */
    public double ic(long nowTs) {
        // group the in-window rows by event-time bucket (rows are in
        // non-decreasing ts order, so buckets are contiguous runs)
        double icSum = 0.0;
        int icCount = 0;
        long bucket = Long.MIN_VALUE;
        List<Row> run = new ArrayList<>();
        for (Row r : matured) {
            if (r.ts < nowTs - windowNs || r.ts >= nowTs) {
                continue;
            }
            long b = Math.floorDiv(r.ts, bucketNs);
            if (b != bucket && !run.isEmpty()) {
                double v = bucketIc(run);
                if (!Double.isNaN(v)) {
                    icSum += v;
                    icCount++;
                }
                run.clear();
            }
            bucket = b;
            run.add(r);
        }
        if (!run.isEmpty()) {
            double v = bucketIc(run);
            if (!Double.isNaN(v)) {
                icSum += v;
                icCount++;
            }
        }
        if (icCount < minBuckets) {
            return Double.NaN;
        }
        return icSum / icCount;
    }

    /**
     * Pearson IC of one bucket ({@code NaN} = bucket does not count:
     * fewer than {@link #MIN_BUCKET_PAIRS} pairs or a population std
     * {@code <=} {@link #STD_EPS} on either marginal — pinned
     * {@code bucket_ics} semantics).
     */
    private static double bucketIc(List<Row> rows) {
        int n = rows.size();
        if (n < MIN_BUCKET_PAIRS) {
            return Double.NaN;
        }
        double ms = 0.0;
        double mr = 0.0;
        for (Row r : rows) {
            ms += r.signal;
            mr += r.value;
        }
        ms /= n;
        mr /= n;
        double css = 0.0;
        double crr = 0.0;
        double csr = 0.0;
        for (Row r : rows) {
            double ds = r.signal - ms;
            double dr = r.value - mr;
            css += ds * ds;
            crr += dr * dr;
            csr += ds * dr;
        }
        if (Math.sqrt(css / n) <= STD_EPS || Math.sqrt(crr / n) <= STD_EPS) {
            return Double.NaN;
        }
        return csr / Math.sqrt(css * crr);
    }
}
