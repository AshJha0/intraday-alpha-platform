//! Pinned deterministic RNG: SplitMix64 (PLATFORM_CONVENTIONS.md §3).
//!
//! This is the ONLY RNG permitted on any shared/deterministic path. Every
//! language implements it identically; the golden vectors depend on it
//! bit-for-bit.
//!
//! Algorithm (state u64):
//!
//! ```text
//! state  = state + 0x9E3779B97F4A7C15          (wrapping)
//! z      = (state ^ (state >> 30)) * 0xBF58476D1CE4E5B9
//! z      = (z ^ (z >> 27)) * 0x94D049BB133111EB
//! output = z ^ (z >> 31)
//! uniform = (output >> 11) * 2^-53             # double in [0, 1)
//! ```

use crate::error::IapError;

const GOLDEN_GAMMA: u64 = 0x9E37_79B9_7F4A_7C15;
const MIX1: u64 = 0xBF58_476D_1CE4_E5B9;
const MIX2: u64 = 0x94D0_49BB_1331_11EB;
const TWO_POW_NEG53: f64 = 1.0 / 9007199254740992.0; // 2^-53, exact

/// Pinned SplitMix64 generator. Same seed => same sequence, forever.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SplitMix64 {
    state: u64,
}

impl SplitMix64 {
    /// Create a generator from a seed.
    pub const fn new(seed: u64) -> SplitMix64 {
        SplitMix64 { state: seed }
    }

    /// Current internal state (for checkpointing).
    pub const fn state(&self) -> u64 {
        self.state
    }

    /// Return the next raw 64-bit output.
    pub fn next_u64(&mut self) -> u64 {
        self.state = self.state.wrapping_add(GOLDEN_GAMMA);
        let mut z = self.state;
        z = (z ^ (z >> 30)).wrapping_mul(MIX1);
        z = (z ^ (z >> 27)).wrapping_mul(MIX2);
        z ^ (z >> 31)
    }

    /// Return a double in [0, 1): `(next_u64() >> 11) * 2^-53`.
    pub fn uniform(&mut self) -> f64 {
        (self.next_u64() >> 11) as f64 * TWO_POW_NEG53
    }

    // -- convenience draws (all derived ONLY from uniform(), hence pinned) ---

    /// Return an int in [0, n). Derived from one `uniform()` draw.
    pub fn below(&mut self, n: i64) -> Result<i64, IapError> {
        if n <= 0 {
            return Err(IapError::InvalidArgument(format!(
                "below(n) requires n > 0, got {n}"
            )));
        }
        let v = (self.uniform() * n as f64) as i64;
        Ok(if v >= n { n - 1 } else { v })
    }

    /// Return an int in [lo, hi] inclusive.
    pub fn randint(&mut self, lo: i64, hi: i64) -> Result<i64, IapError> {
        if hi < lo {
            return Err(IapError::InvalidArgument(format!(
                "randint requires lo <= hi, got [{lo}, {hi}]"
            )));
        }
        Ok(lo + self.below(hi - lo + 1)?)
    }

    /// Exponential inter-arrival draw with the given rate (> 0).
    pub fn exponential(&mut self, rate: f64) -> Result<f64, IapError> {
        if rate <= 0.0 || !rate.is_finite() {
            return Err(IapError::InvalidArgument(format!(
                "exponential rate must be > 0, got {rate}"
            )));
        }
        let u = self.uniform();
        Ok(-(1.0 - u).ln() / rate)
    }

    /// Standard normal via Box-Muller (two `uniform()` draws).
    pub fn normal(&mut self) -> f64 {
        let mut u1 = self.uniform();
        let u2 = self.uniform();
        // Guard u1 == 0 exactly (probability 2^-53 per draw).
        while u1 == 0.0 {
            u1 = self.uniform();
        }
        (-2.0 * u1.ln()).sqrt() * (2.0 * std::f64::consts::PI * u2).cos()
    }

    /// Derive an independent child stream (seeded by `next_u64`).
    pub fn split(&mut self) -> SplitMix64 {
        SplitMix64::new(self.next_u64())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn same_seed_same_sequence() {
        let mut a = SplitMix64::new(123456789);
        let mut b = SplitMix64::new(123456789);
        for _ in 0..100 {
            assert_eq!(a.next_u64(), b.next_u64());
        }
    }

    #[test]
    fn uniform_in_unit_interval() {
        let mut rng = SplitMix64::new(7);
        for _ in 0..1000 {
            let u = rng.uniform();
            assert!((0.0..1.0).contains(&u));
        }
    }

    #[test]
    fn below_and_randint_ranges() {
        let mut rng = SplitMix64::new(99);
        for _ in 0..1000 {
            let v = rng.below(10).expect("n > 0");
            assert!((0..10).contains(&v));
            let r = rng.randint(-5, 5).expect("lo <= hi");
            assert!((-5..=5).contains(&r));
        }
        assert!(rng.below(0).is_err());
        assert!(rng.randint(3, 2).is_err());
    }

    #[test]
    fn exponential_and_normal_are_deterministic() {
        let mut a = SplitMix64::new(11);
        let mut b = SplitMix64::new(11);
        for _ in 0..50 {
            let ea = a.exponential(2.5).expect("rate > 0");
            let eb = b.exponential(2.5).expect("rate > 0");
            assert_eq!(ea.to_bits(), eb.to_bits());
            assert!(ea >= 0.0);
            assert_eq!(a.normal().to_bits(), b.normal().to_bits());
        }
        assert!(a.exponential(0.0).is_err());
        assert!(a.exponential(-1.0).is_err());
    }

    #[test]
    fn split_derives_child_from_next_u64() {
        let mut parent = SplitMix64::new(42);
        let mut probe = SplitMix64::new(42);
        let child_seed = probe.next_u64();
        let child = parent.split();
        assert_eq!(child, SplitMix64::new(child_seed));
        // Parent stream advanced by exactly one draw.
        assert_eq!(parent.next_u64(), probe.next_u64());
    }
}
