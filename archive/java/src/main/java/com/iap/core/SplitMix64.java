package com.iap.core;

/**
 * Pinned deterministic RNG: SplitMix64 (PLATFORM_CONVENTIONS.md section 3).
 * Mirrors {@code iap/core/rng.py} bit-for-bit; the golden vectors depend on it.
 *
 * <pre>
 * state  = state + 0x9E3779B97F4A7C15          (mod 2^64, natural long wrap)
 * z      = (state ^ (state &gt;&gt;&gt; 30)) * 0xBF58476D1CE4E5B9
 * z      = (z ^ (z &gt;&gt;&gt; 27)) * 0x94D049BB133111EB
 * output = z ^ (z &gt;&gt;&gt; 31)
 * uniform = (output &gt;&gt;&gt; 11) * 2^-53            (double in [0, 1))
 * </pre>
 */
public final class SplitMix64 {
    private static final long GOLDEN_GAMMA = 0x9E3779B97F4A7C15L;
    private static final long MIX1 = 0xBF58476D1CE4E5B9L;
    private static final long MIX2 = 0x94D049BB133111EBL;
    private static final double TWO_POW_NEG53 = 0x1.0p-53;

    private long state;

    /** Seed the generator; the seed is the u64 bit pattern of {@code seed}. */
    public SplitMix64(long seed) {
        this.state = seed;
    }

    /**
     * Current generator state (u64 bit pattern). Feeding it back into the
     * constructor reproduces the stream exactly — used by deterministic
     * checkpoint/restart paths.
     */
    public long state() {
        return state;
    }

    /** Return the next raw 64-bit output (u64 bit pattern in a long). */
    public long nextU64() {
        state += GOLDEN_GAMMA;
        long z = state;
        z = (z ^ (z >>> 30)) * MIX1;
        z = (z ^ (z >>> 27)) * MIX2;
        return z ^ (z >>> 31);
    }

    /** Return a double in [0, 1): {@code (nextU64() >>> 11) * 2^-53}. */
    public double uniform() {
        return (nextU64() >>> 11) * TWO_POW_NEG53;
    }

    // -- convenience draws (all derived ONLY from uniform(), hence pinned) ----

    /** Return an int in [0, n). Derived from one uniform() draw. */
    public long below(long n) {
        if (n <= 0) {
            throw new IllegalArgumentException("below(n) requires n > 0, got " + n);
        }
        long v = (long) (uniform() * n);
        return v >= n ? n - 1 : v;
    }

    /** Return an int in [lo, hi] inclusive. */
    public long randint(long lo, long hi) {
        if (hi < lo) {
            throw new IllegalArgumentException(
                    "randint requires lo <= hi, got [" + lo + ", " + hi + "]");
        }
        return lo + below(hi - lo + 1);
    }

    /** Exponential inter-arrival draw with the given rate (&gt; 0). */
    public double exponential(double rate) {
        if (!(rate > 0.0)) {
            throw new IllegalArgumentException("exponential rate must be > 0, got " + rate);
        }
        double u = uniform();
        return -Math.log(1.0 - u) / rate;
    }

    /** Standard normal via Box-Muller (two uniform() draws, as the reference). */
    public double normal() {
        double u1 = uniform();
        double u2 = uniform();
        while (u1 == 0.0) {
            u1 = uniform();
        }
        return Math.sqrt(-2.0 * Math.log(u1)) * Math.cos(2.0 * Math.PI * u2);
    }

    /** Derive an independent child stream (seeded by nextU64). */
    public SplitMix64 split() {
        return new SplitMix64(nextU64());
    }
}
