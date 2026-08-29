// Pinned deterministic RNG: SplitMix64 (PLATFORM_CONVENTIONS.md section 3).
//
// This is the ONLY RNG permitted on any shared/deterministic path. Every
// language implements it identically; the golden vectors depend on it
// bit-for-bit. Mirrors python/src/iap/core/rng.py exactly.

#pragma once

#include <cmath>
#include <cstdint>
#include <stdexcept>

namespace iap {

class SplitMix64 {
public:
    explicit SplitMix64(std::uint64_t seed) : state_(seed) {}

    // Next raw 64-bit output.
    std::uint64_t next_u64() {
        state_ += 0x9E3779B97F4A7C15ULL;
        std::uint64_t z = state_;
        z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
        z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
        return z ^ (z >> 31);
    }

    // Double in [0, 1): (next_u64() >> 11) * 2^-53.
    double uniform() {
        return static_cast<double>(next_u64() >> 11) * kTwoPowNeg53;
    }

    // ---- convenience draws (all derived ONLY from uniform(), hence pinned) --

    // Int in [0, n). Derived from one uniform() draw (matches Python).
    std::int64_t below(std::int64_t n) {
        if (n <= 0) {
            throw std::invalid_argument("below(n) requires n > 0, got " +
                                        std::to_string(n));
        }
        auto v = static_cast<std::int64_t>(uniform() * static_cast<double>(n));
        return v >= n ? n - 1 : v;
    }

    // Int in [lo, hi] inclusive.
    std::int64_t randint(std::int64_t lo, std::int64_t hi) {
        if (hi < lo) {
            throw std::invalid_argument("randint requires lo <= hi, got [" +
                                        std::to_string(lo) + ", " +
                                        std::to_string(hi) + "]");
        }
        return lo + below(hi - lo + 1);
    }

    // Exponential inter-arrival draw with the given rate (> 0).
    double exponential(double rate) {
        if (rate <= 0.0) {
            throw std::invalid_argument("exponential rate must be > 0, got " +
                                        std::to_string(rate));
        }
        double u = uniform();
        return -std::log(1.0 - u) / rate;
    }

    // Standard normal via Box-Muller (two uniform() draws).
    double normal() {
        double u1 = uniform();
        double u2 = uniform();
        while (u1 == 0.0) {
            u1 = uniform();
        }
        return std::sqrt(-2.0 * std::log(u1)) *
               std::cos(2.0 * 3.141592653589793 * u2);
    }

    // Derive an independent child stream (seeded by next_u64).
    SplitMix64 split() { return SplitMix64(next_u64()); }

    std::uint64_t state() const { return state_; }

private:
    static constexpr double kTwoPowNeg53 = 1.0 / 9007199254740992.0;  // 2^-53
    std::uint64_t state_;
};

}  // namespace iap
