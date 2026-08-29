package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import java.util.List;
import java.util.Map;

import org.junit.Test;

import com.iap.core.SplitMix64;

/** Golden known-answer + behavior tests for the pinned SplitMix64 RNG. */
public class SplitMix64Test {

    @Test
    public void goldenFirstFiveU64() {
        Map<String, Object> g = Golden.json("splitmix64.json");
        long seed = Json.asLong(g.get("seed"));
        List<Object> expected = Json.array(g.get("first_5_u64"));
        SplitMix64 rng = new SplitMix64(seed);
        for (int i = 0; i < expected.size(); i++) {
            assertEquals("u64 #" + i, Json.asLong(expected.get(i)), rng.nextU64());
        }
    }

    @Test
    public void goldenFirstFiveUniformsBitExact() {
        Map<String, Object> g = Golden.json("splitmix64.json");
        long seed = Json.asLong(g.get("seed"));
        List<Object> expected = Json.array(g.get("first_5_uniform"));
        SplitMix64 rng = new SplitMix64(seed);
        for (int i = 0; i < expected.size(); i++) {
            double want = ((Double) expected.get(i)).doubleValue();
            double got = rng.uniform();
            assertEquals("uniform #" + i + " bits",
                    Double.doubleToLongBits(want), Double.doubleToLongBits(got));
        }
    }

    @Test
    public void sameSeedSameSequence() {
        SplitMix64 a = new SplitMix64(123456789L);
        SplitMix64 b = new SplitMix64(123456789L);
        for (int i = 0; i < 1000; i++) {
            assertEquals(a.nextU64(), b.nextU64());
        }
    }

    @Test
    public void uniformInHalfOpenUnitInterval() {
        SplitMix64 rng = new SplitMix64(7);
        for (int i = 0; i < 10000; i++) {
            double u = rng.uniform();
            assertTrue("u >= 0", u >= 0.0);
            assertTrue("u < 1", u < 1.0);
        }
    }

    @Test
    public void belowMatchesUniformDerivation() {
        SplitMix64 a = new SplitMix64(42);
        SplitMix64 b = new SplitMix64(42);
        for (int i = 0; i < 1000; i++) {
            long n = 1 + (i % 97);
            long viaUniform = (long) (b.uniform() * n);
            if (viaUniform >= n) {
                viaUniform = n - 1;
            }
            assertEquals(viaUniform, a.below(n));
        }
    }

    @Test(expected = IllegalArgumentException.class)
    public void belowRejectsNonPositive() {
        new SplitMix64(1).below(0);
    }

    @Test
    public void randintInclusiveBounds() {
        SplitMix64 rng = new SplitMix64(99);
        for (int i = 0; i < 5000; i++) {
            long v = rng.randint(-3, 3);
            assertTrue(v >= -3 && v <= 3);
        }
    }

    @Test(expected = IllegalArgumentException.class)
    public void randintRejectsReversedBounds() {
        new SplitMix64(1).randint(5, 4);
    }

    @Test(expected = IllegalArgumentException.class)
    public void exponentialRejectsNonPositiveRate() {
        new SplitMix64(1).exponential(0.0);
    }

    @Test
    public void splitIsDeterministic() {
        SplitMix64 a = new SplitMix64(42);
        SplitMix64 b = new SplitMix64(42);
        SplitMix64 ca = a.split();
        SplitMix64 cb = b.split();
        for (int i = 0; i < 100; i++) {
            assertEquals(ca.nextU64(), cb.nextU64());
        }
        assertEquals(a.nextU64(), b.nextU64());
    }

    @Test
    public void goldenSeedsRecordedForVectors() {
        Map<String, Object> g = Golden.json("splitmix64.json");
        assertEquals(4242424242L, Json.asLong(g.get("golden_eq_seed")));
        assertEquals(8484848484L, Json.asLong(g.get("golden_fx_seed")));
    }
}
