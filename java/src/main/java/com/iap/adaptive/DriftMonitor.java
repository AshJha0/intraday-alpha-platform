package com.iap.adaptive;

import java.util.Map;
import java.util.TreeMap;

/**
 * Live-vs-backtest signal drift (the {@code alpha_live_vs_backtest_drift}
 * gauge): keeps a rolling window of live alpha signal values per alpha and,
 * every {@code everyN} observations once the window is full, recomputes the
 * pinned {@link Psi} of the window against the loaded
 * {@link BaselineLoader.Baseline} for that alpha.
 *
 * <p>Purely observational (never feeds back into trading) and event-driven
 * (no wall clock), so a paper session stays deterministic. Alphas without a
 * loaded baseline are recorded but never produce a PSI ({@code NaN}).
 */
public final class DriftMonitor {
    /** Pinned rolling-window size (live sample per alpha). */
    public static final int DEFAULT_WINDOW = 256;

    /** Pinned recompute cadence (observations between PSI updates). */
    public static final int DEFAULT_EVERY_N = 32;

    private final class State {
        final double[] ring = new double[window];
        int size;
        int next;
        long sinceRecompute;
        double psi = Double.NaN;

        void observe(BaselineLoader.Baseline baseline, double value) {
            ring[next] = value;
            next = (next + 1) % ring.length;
            if (size < ring.length) {
                size++;
            }
            if (baseline == null || size < ring.length) {
                return;
            }
            if (++sinceRecompute >= everyN || Double.isNaN(psi)) {
                sinceRecompute = 0;
                // window = the last `size` values ending just before `next`
                double[] live = Psi.fractions(ring, next, size,
                        baseline.edges());
                psi = Psi.psi(baseline.fractions(), live);
            }
        }
    }

    private final Map<String, BaselineLoader.Baseline> baselines;
    private final Map<String, State> states = new TreeMap<>();
    private final int window;
    private final int everyN;

    /** Monitor with the pinned window / cadence. */
    public DriftMonitor(Map<String, BaselineLoader.Baseline> baselines) {
        this(baselines, DEFAULT_WINDOW, DEFAULT_EVERY_N);
    }

    public DriftMonitor(Map<String, BaselineLoader.Baseline> baselines,
            int window, int everyN) {
        if (window < Psi.BUCKETS || everyN <= 0) {
            throw new IllegalArgumentException(
                    "window must be >= " + Psi.BUCKETS + " and everyN > 0");
        }
        this.baselines = baselines;
        this.window = window;
        this.everyN = everyN;
    }

    /** Whether a baseline is loaded for this alpha (drift is armed). */
    public boolean armed(String alphaId) {
        return baselines.containsKey(alphaId);
    }

    /**
     * Record one live signal value; returns the current PSI for the alpha
     * ({@code NaN} until the window has filled against a loaded baseline).
     */
    public double onSignal(String alphaId, double value) {
        State st = states.computeIfAbsent(alphaId, k -> new State());
        st.observe(baselines.get(alphaId), value);
        return st.psi;
    }

    /** Current PSI for an alpha ({@code NaN} while not yet computed). */
    public double psi(String alphaId) {
        State st = states.get(alphaId);
        return st == null ? Double.NaN : st.psi;
    }
}
