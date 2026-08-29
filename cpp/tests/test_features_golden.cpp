// Feature-engine golden parity (API_FEATURES.md section 5) + engine
// behavior: cadence, warmup, validity rules.
//
// Golden comparisons are BY FEATURE NAME: every checkpoint feature that this
// port implements natively (the pinned native 40 plus the alpha-input
// extras) must match tests/golden/expected_features.json at abs 1e-9 /
// rel 1e-9, with exact validity agreement.

#include <gtest/gtest.h>

#include <cmath>
#include <map>
#include <set>
#include <string>
#include <vector>

#include "golden_util.hpp"
#include "iap/features/feature_engine.hpp"
#include "iap/marketdata/codec.hpp"

namespace {

using iap::FeatureEngine;
using iap::FeatureVector;
using iap::MarketEvent;

const std::map<std::uint32_t, double> kTicks = {{1u, 0.01}, {101u, 1e-05}};

std::vector<MarketEvent> load_events(const std::string& name) {
    return iap::read_jsonl(iap_test::golden_path(name));
}

// Run a golden vector at cadence 0 and return the vector emitted after each
// 1-based event index.
std::vector<FeatureVector> run_vector(const std::string& name) {
    FeatureEngine engine(kTicks, 0);
    std::vector<FeatureVector> rows;
    engine.run(load_events(name), &rows);
    return rows;
}

// Compare one golden checkpoint (all names this port implements natively).
void check_checkpoint(const std::string& market, const std::string& cp_key,
                      const std::string& vector_file) {
    const auto golden = iap_test::load_golden_json("expected_features.json");
    const auto& cp = golden[market]["checkpoints"][cp_key];
    const auto rows = run_vector(vector_file);
    const std::size_t idx = static_cast<std::size_t>(std::stoul(cp_key)) - 1;
    ASSERT_LT(idx, rows.size());
    const FeatureVector& vec = rows[idx];
    EXPECT_EQ(vec.timestamp, cp["timestamp"].i64());
    int compared = 0;
    for (const auto& [name, entry] : cp["features"].obj) {
        const int slot = iap::feature_index(name);
        if (slot < 0) continue;  // not in the native sub-vector
        ++compared;
        const bool want_valid = entry["valid"].boolean;
        EXPECT_EQ(vec.valid[static_cast<std::size_t>(slot)], want_valid)
            << market << " cp " << cp_key << " " << name << " validity";
        if (!want_valid) {
            EXPECT_TRUE(
                std::isnan(vec.values[static_cast<std::size_t>(slot)]))
                << name << " invalid slot must carry NaN";
            continue;
        }
        const double want = entry["value"].num();
        const double got = vec.values[static_cast<std::size_t>(slot)];
        EXPECT_LE(std::fabs(got - want), 1e-9 + 1e-9 * std::fabs(want))
            << market << " cp " << cp_key << " " << name << ": got " << got
            << " want " << want;
    }
    // The checkpoints carry 16 native-40 names + vol_regime_ratio_v1 (an
    // implemented alpha-input extra): never silently compare fewer.
    EXPECT_GE(compared, 17) << market << " cp " << cp_key;
}

TEST(FeatureGolden, NativeNamesResolveAndCount) {
    // All 40 pinned native names (API_FEATURES.md section 3) must resolve.
    std::vector<std::string> native;
    for (int k : {1, 3, 5, 10}) {
        for (const char* w : {"1s", "5s", "30s"}) {
            native.push_back("ofi_l" + std::to_string(k) + "_w" + w + "_v1");
        }
    }
    for (int k : {1, 3, 5, 10}) {
        native.push_back("imbalance_l" + std::to_string(k) + "_v1");
    }
    for (const char* n : {"mid_price_v1", "microprice_v1",
                          "micro_mid_dev_bps_v1", "spread_ticks_v1",
                          "spread_bps_v1"}) {
        native.push_back(n);
    }
    for (int k : {1, 5, 10}) {
        native.push_back("depth_bid_l" + std::to_string(k) + "_v1");
        native.push_back("depth_ask_l" + std::to_string(k) + "_v1");
    }
    for (const char* w : {"1s", "10s", "1m"}) {
        native.push_back(std::string("signed_volume_w") + w + "_v1");
        native.push_back(std::string("trade_imbalance_w") + w + "_v1");
    }
    for (const char* w : {"10s", "1m", "5m"}) {
        native.push_back(std::string("rvol_w") + w + "_v1");
    }
    for (const char* n : {"ret_simple_1s_v1", "ret_log_1s_v1",
                          "ret_log_10s_v1", "ret_log_1m_v1"}) {
        native.push_back(n);
    }
    ASSERT_EQ(native.size(), 40u);
    std::set<int> slots;
    for (const auto& n : native) {
        const int s = iap::feature_index(n);
        EXPECT_GE(s, 0) << n << " must be implemented natively";
        slots.insert(s);
    }
    EXPECT_EQ(slots.size(), 40u);
    EXPECT_EQ(static_cast<int>(iap::NUM_FEATURES), 48);
    EXPECT_EQ(iap::feature_index("no_such_feature_v1"), -1);
    // Round-trip: feature_name(feature_index(name)) == name.
    for (int i = 0; i < iap::NUM_FEATURES; ++i) {
        EXPECT_EQ(iap::feature_index(iap::feature_name(i)), i);
    }
}

TEST(FeatureGolden, RegistryPinsAgree) {
    const auto golden = iap_test::load_golden_json("expected_features.json");
    // This port keeps the documented 40-slot sub-vector (by registry name);
    // it still asserts the golden file's registry pins are what it expects.
    EXPECT_EQ(golden["registered_count"].i64(), 205);
    EXPECT_EQ(golden["registry_hash"].s().size(), 64u);
    EXPECT_DOUBLE_EQ(golden["tolerance"]["abs"].num(), 1e-9);
    EXPECT_DOUBLE_EQ(golden["tolerance"]["rel"].num(), 1e-9);
}

TEST(FeatureGolden, EqCheckpoint500) {
    check_checkpoint("eq", "500", "events_eq_mbo.jsonl");
}
TEST(FeatureGolden, EqCheckpoint1000) {
    check_checkpoint("eq", "1000", "events_eq_mbo.jsonl");
}
TEST(FeatureGolden, EqCheckpoint1500) {
    check_checkpoint("eq", "1500", "events_eq_mbo.jsonl");
}
TEST(FeatureGolden, EqCheckpoint2000) {
    check_checkpoint("eq", "2000", "events_eq_mbo.jsonl");
}
TEST(FeatureGolden, FxCheckpoint400) {
    check_checkpoint("fx", "400", "events_fx_quote.jsonl");
}
TEST(FeatureGolden, FxCheckpoint800) {
    check_checkpoint("fx", "800", "events_fx_quote.jsonl");
}

TEST(FeatureGolden, NanNeverLeaksIntoValidSlot) {
    for (const char* file : {"events_eq_mbo.jsonl", "events_fx_quote.jsonl"}) {
        for (const auto& vec : run_vector(file)) {
            for (int i = 0; i < iap::NUM_FEATURES; ++i) {
                const auto ui = static_cast<std::size_t>(i);
                if (vec.valid[ui]) {
                    ASSERT_TRUE(std::isfinite(vec.values[ui]))
                        << iap::feature_name(i) << " at ts " << vec.timestamp;
                } else {
                    ASSERT_TRUE(std::isnan(vec.values[ui]))
                        << iap::feature_name(i) << " at ts " << vec.timestamp;
                }
            }
        }
    }
}

TEST(FeatureEngineBehavior, CadenceZeroEmitsEveryEvent) {
    const auto events = load_events("events_eq_mbo.jsonl");
    FeatureEngine engine(kTicks, 0);
    FeatureVector vec;
    for (const auto& ev : events) {
        EXPECT_TRUE(engine.apply(ev, vec));
    }
    EXPECT_EQ(engine.vectors_emitted(), events.size());
    EXPECT_EQ(engine.events_processed(), events.size());
}

TEST(FeatureEngineBehavior, CadenceThrottlesEmissions) {
    const auto events = load_events("events_eq_mbo.jsonl");
    FeatureEngine engine(kTicks, iap::W_1S);
    FeatureVector vec;
    std::vector<std::int64_t> emit_ts;
    for (const auto& ev : events) {
        if (engine.apply(ev, vec)) emit_ts.push_back(vec.timestamp);
    }
    ASSERT_GT(emit_ts.size(), 1u);
    EXPECT_LT(emit_ts.size(), events.size());
    for (std::size_t i = 1; i < emit_ts.size(); ++i) {
        EXPECT_GE(emit_ts[i] - emit_ts[i - 1], iap::W_1S);
    }
    EXPECT_THROW(FeatureEngine(kTicks, -1), std::invalid_argument);
}

TEST(FeatureEngineBehavior, WarmupGatesWindowedFeatures) {
    // Two-sided book from the first event, but windowed features stay
    // invalid until t - first_event_ts >= w.
    FeatureEngine engine({{7u, 0.01}}, 0);
    FeatureVector vec;
    auto add = [&](std::uint64_t seq, std::int64_t ts, std::uint8_t side,
                   std::int64_t px, std::int64_t qty, std::uint64_t oid) {
        return MarketEvent::of(seq, 7, 1, ts, ts, seq, 1, side, px, qty, oid,
                               0);
    };
    const std::int64_t t0 = 1'000'000'000'000;
    ASSERT_TRUE(engine.apply(add(1, t0, 0, 100, 10, 1), vec));
    ASSERT_TRUE(engine.apply(add(2, t0 + 1000, 1, 101, 10, 2), vec));
    // book_ok features valid immediately; windowed ones not yet warm.
    EXPECT_TRUE(vec.valid[iap::F_MID_PRICE]);
    EXPECT_FALSE(vec.valid[iap::F_OFI_L1_W1S]);
    EXPECT_FALSE(vec.valid[iap::F_SIGNED_VOLUME_W1S]);
    EXPECT_FALSE(vec.valid[iap::F_RVOL_W10S]);
    // 1s later: 1s windows warm (rates/sums valid with empty windows).
    ASSERT_TRUE(engine.apply(add(3, t0 + iap::W_1S + 1000, 0, 99, 5, 3), vec));
    EXPECT_TRUE(vec.valid[iap::F_OFI_L1_W1S]);
    EXPECT_TRUE(vec.valid[iap::F_SIGNED_VOLUME_W1S]);
    EXPECT_DOUBLE_EQ(vec.values[iap::F_SIGNED_VOLUME_W1S], 0.0);
    // trade imbalance needs traded volume even when warm.
    EXPECT_FALSE(vec.valid[iap::F_TRADE_IMBALANCE_W1S]);
    EXPECT_FALSE(vec.valid[iap::F_RVOL_W10S]);  // 10s not yet warm
}

TEST(FeatureEngineBehavior, OneSidedBookInvalidatesBookFeatures) {
    FeatureEngine engine({{7u, 0.01}}, 0);
    FeatureVector vec;
    const std::int64_t t0 = 1'000'000'000'000;
    const auto ev =
        MarketEvent::of(1, 7, 1, t0, t0, 1, 1, 0, 100, 10, 1, 0);  // bid only
    ASSERT_TRUE(engine.apply(ev, vec));
    EXPECT_FALSE(vec.valid[iap::F_MID_PRICE]);
    EXPECT_FALSE(vec.valid[iap::F_SPREAD_TICKS]);
    EXPECT_FALSE(vec.valid[iap::F_IMBALANCE_L1]);
    EXPECT_FALSE(vec.valid[iap::F_DEPTH_BID_L1]);
    EXPECT_FALSE(vec.valid[iap::F_RET_LOG_1S]);
    EXPECT_TRUE(std::isnan(vec.values[iap::F_MID_PRICE]));
}

TEST(FeatureEngineBehavior, UnknownInstrumentThrows) {
    FeatureEngine engine({{7u, 0.01}}, 0);
    FeatureVector vec;
    const auto ev = MarketEvent::of(1, 8, 1, 1000, 1000, 1, 1, 0, 100, 10, 1, 0);
    EXPECT_THROW(engine.apply(ev, vec), std::invalid_argument);
}

}  // namespace
