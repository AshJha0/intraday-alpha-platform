"""Pinned deterministic RNG: SplitMix64 (PLATFORM_CONVENTIONS.md section 3).

This is the ONLY RNG permitted on any shared/deterministic path (generator,
fill models, simulations). Every language implements it identically; the golden
vectors in ``tests/golden/`` depend on it bit-for-bit.

Algorithm (state u64)::

    state  = (state + 0x9E3779B97F4A7C15) mod 2^64
    z      = state
    z      = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) mod 2^64
    z      = ((z ^ (z >> 27)) * 0x94D049BB133111EB) mod 2^64
    output = z ^ (z >> 31)

    uniform = (output >> 11) * 2^-53      # double in [0, 1)
"""

from __future__ import annotations

import math

_MASK64 = (1 << 64) - 1
_GOLDEN_GAMMA = 0x9E3779B97F4A7C15
_MIX1 = 0xBF58476D1CE4E5B9
_MIX2 = 0x94D049BB133111EB
_TWO_POW_NEG53 = 2.0 ** -53


class SplitMix64:
    """Pinned SplitMix64 generator. Same seed => same sequence, forever."""

    __slots__ = ("state",)

    def __init__(self, seed: int) -> None:
        if not isinstance(seed, int):
            raise ValueError(f"SplitMix64 seed must be int, got {type(seed).__name__}")
        self.state: int = seed & _MASK64

    def next_u64(self) -> int:
        """Return the next raw 64-bit output."""
        self.state = (self.state + _GOLDEN_GAMMA) & _MASK64
        z = self.state
        z = ((z ^ (z >> 30)) * _MIX1) & _MASK64
        z = ((z ^ (z >> 27)) * _MIX2) & _MASK64
        return (z ^ (z >> 31)) & _MASK64

    def uniform(self) -> float:
        """Return a double in [0, 1): (next_u64() >> 11) * 2^-53."""
        return (self.next_u64() >> 11) * _TWO_POW_NEG53

    # -- convenience draws (all derived ONLY from uniform(), hence pinned) ----

    def below(self, n: int) -> int:
        """Return an int in [0, n). Derived from one uniform() draw."""
        if n <= 0:
            raise ValueError(f"below(n) requires n > 0, got {n}")
        v = int(self.uniform() * n)
        return n - 1 if v >= n else v

    def randint(self, lo: int, hi: int) -> int:
        """Return an int in [lo, hi] inclusive."""
        if hi < lo:
            raise ValueError(f"randint requires lo <= hi, got [{lo}, {hi}]")
        return lo + self.below(hi - lo + 1)

    def exponential(self, rate: float) -> float:
        """Exponential inter-arrival draw with the given rate (> 0)."""
        if rate <= 0.0:
            raise ValueError(f"exponential rate must be > 0, got {rate}")
        u = self.uniform()
        return -math.log(1.0 - u) / rate

    def normal(self) -> float:
        """Standard normal via Box-Muller (two uniform() draws)."""
        u1 = self.uniform()
        u2 = self.uniform()
        # Guard u1 == 0 exactly (probability 2^-53 per draw).
        while u1 == 0.0:
            u1 = self.uniform()
        return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)

    def split(self) -> "SplitMix64":
        """Derive an independent child stream (seeded by next_u64)."""
        return SplitMix64(self.next_u64())
