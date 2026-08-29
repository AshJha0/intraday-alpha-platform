// SplitMix64 unit + golden known-answer tests (conventions section 3).

#include <gtest/gtest.h>

#include <cstdint>
#include <stdexcept>

#include "golden_util.hpp"
#include "iap/marketdata/rng.hpp"

using iap::SplitMix64;
using iap_test::load_golden_json;

TEST(SplitMix64Golden, KnownAnswerU64) {
    auto g = load_golden_json("splitmix64.json");
    SplitMix64 rng(g["seed"].u64());
    const auto& expected = g["first_5_u64"].a();
    ASSERT_EQ(expected.size(), 5u);
    for (const auto& e : expected) {
        EXPECT_EQ(rng.next_u64(), e.u64());
    }
}

TEST(SplitMix64Golden, KnownAnswerUniformBitExact) {
    auto g = load_golden_json("splitmix64.json");
    SplitMix64 rng(g["seed"].u64());
    const auto& expected = g["first_5_uniform"].a();
    ASSERT_EQ(expected.size(), 5u);
    for (const auto& e : expected) {
        double got = rng.uniform();
        // Bit-exact double equality (goldens are exact decimal doubles).
        EXPECT_EQ(got, e.num());
    }
}

TEST(SplitMix64, DeterministicSameSeed) {
    SplitMix64 a(12345), b(12345);
    for (int i = 0; i < 100; ++i) {
        EXPECT_EQ(a.next_u64(), b.next_u64());
    }
}

TEST(SplitMix64, UniformInHalfOpenUnitInterval) {
    SplitMix64 rng(7);
    for (int i = 0; i < 1000; ++i) {
        double u = rng.uniform();
        EXPECT_GE(u, 0.0);
        EXPECT_LT(u, 1.0);
    }
}

TEST(SplitMix64, UniformDerivedFromNextU64) {
    SplitMix64 a(99), b(99);
    for (int i = 0; i < 20; ++i) {
        double expected =
            static_cast<double>(a.next_u64() >> 11) * (1.0 / 9007199254740992.0);
        EXPECT_EQ(b.uniform(), expected);
    }
}

TEST(SplitMix64, BelowInRangeAndErrors) {
    SplitMix64 rng(1);
    for (int i = 0; i < 500; ++i) {
        std::int64_t v = rng.below(17);
        EXPECT_GE(v, 0);
        EXPECT_LT(v, 17);
    }
    EXPECT_THROW(rng.below(0), std::invalid_argument);
    EXPECT_THROW(rng.below(-3), std::invalid_argument);
}

TEST(SplitMix64, RandintInclusiveAndErrors) {
    SplitMix64 rng(2);
    for (int i = 0; i < 500; ++i) {
        std::int64_t v = rng.randint(-5, 5);
        EXPECT_GE(v, -5);
        EXPECT_LE(v, 5);
    }
    EXPECT_EQ(rng.randint(3, 3), 3);
    EXPECT_THROW(rng.randint(4, 3), std::invalid_argument);
}

TEST(SplitMix64, ExponentialPositiveAndErrors) {
    SplitMix64 rng(3);
    for (int i = 0; i < 100; ++i) {
        EXPECT_GE(rng.exponential(2.5), 0.0);
    }
    EXPECT_THROW(rng.exponential(0.0), std::invalid_argument);
    EXPECT_THROW(rng.exponential(-1.0), std::invalid_argument);
}

TEST(SplitMix64, SplitIsDeterministic) {
    SplitMix64 a(42), b(42);
    SplitMix64 ca = a.split();
    SplitMix64 cb = b.split();
    for (int i = 0; i < 50; ++i) {
        EXPECT_EQ(ca.next_u64(), cb.next_u64());
    }
    // Parent streams stay in lockstep after the split.
    EXPECT_EQ(a.next_u64(), b.next_u64());
}
