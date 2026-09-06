package com.iap.adaptive;

import java.util.ArrayDeque;
import java.util.Map;
import java.util.TreeMap;

/**
 * Live-vs-backtest signal drift (the {@code alpha_live_vs_backtest_drift}
 * gauge): keeps a rolling window of live alpha signal values per alpha and,
 * every {@code everyN} observations once the window holds enough samples,
 * recomputes the pinned {@link Psi} of the window against the loaded
 * {@link BaselineLoader.Baseline} for that alpha.
 *
 * <p><b>The window is EVENT TIME</b> (round-3 fix): research computes PSI
 * over {@code adaptive.monitor_window_ns} (1 h) with at least
 * {@code adaptive.min_psi_samples} (200) samples — see API_ADAPTIVE.md
 * section 2 and {@code configs/strategies.json}. The gauge used a fixed ring
 * of 256 signals, which is ~13 minutes on equities and hours on a sparse FX
 * stream: it was simply not the pinned statistic, so the live number and the
 * research number were never comparable. Feed
 * {@link #onSignal(String, long, double)} with the signal's
 * {@code exchange_ts}.
 *
 * <p>Purely observational (never feeds back into trading) and event-driven
 * (no wall clock), so a paper session stays deterministic. Alphas without a
 * loaded baseline are recorded but never produce a PSI ({@code NaN}).
 */
public final class DriftMonitor {
    /** Pinned rolling window in event time (configs/strategies.json). */
    public static final long DEFAULT_WINDOW_NS = 3_600_000_000_000L;

    /** Pinned minimum sample count before a PSI is reported. */
    public static final int DEFAULT_MIN_SAMPLES = 200;

    /** Pinned recompute cadence (observations between PSI updates). */
    public static final int DEFAULT_EVERY_N = 32;

    /** Legacy count-window size (only the deprecated count API uses it). */
    public static final int DEFAULT_WINDOW = 256;

    private static final class Sample {
        final long ts;
        final double value;

        Sample(long ts, double value) {
            this.ts = ts;
            this.value = value;
        }
    }

    private final class State {
        final ArrayDeque<Sample> window = new ArrayDeque<>();
        long sinceRecompute;
        long lastTs = Long.MIN_VALUE;
        double psi = Double.NaN;

        void observe(BaselineLoader.Baseline baseline, long ts, double value) {
            if (ts < lastTs) {
                return; // event-time regression: dropped (fail closed)
            }
            lastTs = ts;
            window.addLast(new Sample(ts, value));
            while (!window.isEmpty() && window.peekFirst().ts <= ts - windowNs) {
                window.pollFirst();
            }
            if (baseline == null || window.size() < minSamples) {
                psi = Double.NaN;
                return;
            }
            if (++sinceRecompute >= everyN || Double.isNaN(psi)) {
                sinceRecompute = 0;
                double[] live = new double[window.size()];
                int i = 0;
                for (Sample s : window) {
                    live[i++] = s.value;
                }
                psi = Psi.psi(baseline.fractions(),
                        Psi.fractions(live, 0, live.length, baseline.edges()));
            }
        }
    }

    private final Map<String, BaselineLoader.Baseline> baselines;
    private final Map<String, State> states = new TreeMap<>();
    private final long windowNs;
    private final int minSamples;
    private final int everyN;

    /** Monitor with the pinned event-time window / minimum samples. */
    public DriftMonitor(Map<String, BaselineLoader.Baseline> baselines) {
        this(baselines, DEFAULT_WINDOW_NS, DEFAULT_MIN_SAMPLES,
                DEFAULT_EVERY_N);
    }

    /**
     * @param baselines per-alpha PSI baselines (may be empty)
     * @param windowNs rolling window in event time
     *     (configs/strategies.json {@code adaptive.monitor_window_ns})
     * @param minSamples minimum samples in the window before a PSI is
     *     reported ({@code adaptive.min_psi_samples})
     * @param everyN observations between PSI recomputations
     */
    public DriftMonitor(Map<String, BaselineLoader.Baseline> baselines,
            long windowNs, int minSamples, int everyN) {
        if (windowNs <= 0 || minSamples < Psi.BUCKETS || everyN <= 0) {
            throw new IllegalArgumentException("windowNs > 0, minSamples >= "
                    + Psi.BUCKETS + " and everyN > 0 required");
        }
        this.baselines = baselines;
        this.windowNs = windowNs;
        this.minSamples = minSamples;
        this.everyN = everyN;
    }

    /** Whether a baseline is loaded for this alpha (drift is armed). */
    public boolean armed(String alphaId) {
        return baselines.containsKey(alphaId);
    }

    /** Samples currently inside the event-time window (diagnostics). */
    public int windowSize(String alphaId) {
        State st = states.get(alphaId);
        return st == null ? 0 : st.window.size();
    }

    /**
     * Record one live signal value at its {@code exchange_ts}; returns the
     * current PSI for the alpha ({@code NaN} until the event-time window
     * holds {@code minSamples} values against a loaded baseline).
     */
    public double onSignal(String alphaId, long exchangeTs, double value) {
        State st = states.computeIfAbsent(alphaId, k -> new State());
        st.observe(baselines.get(alphaId), exchangeTs, value);
        return st.psi;
    }

    /**
     * Legacy count-window entry point for callers that do not (yet) pass an
     * event timestamp: samples are stamped with a synthetic monotone counter
     * and the window degenerates to the last {@link #DEFAULT_WINDOW}
     * observations. The PSI it produces is NOT the pinned statistic — pass
     * the signal's {@code exchange_ts} to
     * {@link #onSignal(String, long, double)} instead. Kept only so a caller
     * that has no timestamp at hand still compiles; it is scheduled for
     * removal once every caller passes event time.
     */
    public double onSignal(String alphaId, double value) {
        State st = states.computeIfAbsent(alphaId, k -> new State());
        // synthetic step sized so the window holds DEFAULT_WINDOW samples:
        // the legacy path stays BOUNDED (no unbounded growth, conventions).
        long step = Math.max(1L, windowNs / DEFAULT_WINDOW);
        long ts = st.lastTs == Long.MIN_VALUE ? 0L : st.lastTs + step;
        st.observe(baselines.get(alphaId), ts, value);
        return st.psi;
    }

    /** Current PSI for an alpha ({@code NaN} while not yet computed). */
    public double psi(String alphaId) {
        State st = states.get(alphaId);
        return st == null ? Double.NaN : st.psi;
    }
}
