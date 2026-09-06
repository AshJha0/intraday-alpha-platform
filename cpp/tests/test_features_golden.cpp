// Feature-engine golden parity (API_FEATURES.md section 5) + engine
// behavior: cadence, warmup, validity rules.
//
// Golden comparisons are BY FEATURE NAME: every checkpoint feature that this
// port implements natively (the pinned native 40 plus the alpha-input
// extras) must match tests/golden/expected_features.json at abs 1e-9 /
// rel 1e-9, with exact validity agreement.

#include <gtest/gtest.h>

#include <cmath>
#include <limits>
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

// ---------------------------------------------------------------------------
// Anomaly-vector golden (API_FEATURES.md section 2 ingestion rules)
// ---------------------------------------------------------------------------

namespace {

// Replay one anomaly vector and check every pinned checkpoint: emission
// timestamp, engine drop counters, stale-recovery bookkeeping and the whole
// native sub-vector.
void check_anomaly_side(const std::string& side, double tick) {
    const auto golden =
        iap_test::load_golden_json("expected_features_anomalies.json");
    const auto& doc = golden[side];
    const std::string vector_file = doc["vector"].s();
    const auto iid = static_cast<std::uint32_t>(doc["instrument_id"].i64());
    const auto events = load_events(vector_file);
    ASSERT_EQ(static_cast<std::int64_t>(events.size()), doc["n_events"].i64());

    std::map<std::uint32_t, double> ticks = {{iid, tick}};
    FeatureEngine engine(ticks, 0);
    FeatureVector vec;
    int compared = 0;
    for (std::size_t i = 0; i < events.size(); ++i) {
        const bool emitted = engine.apply(events[i], vec);
        const std::string key = std::to_string(i + 1);
        if (!doc["checkpoints"].has(key)) continue;
        const auto& cp = doc["checkpoints"][key];
        ASSERT_TRUE(emitted) << side << "@" << key << ": cadence 0 must emit";
        EXPECT_EQ(vec.timestamp, cp["timestamp"].i64()) << side << "@" << key;
        EXPECT_EQ(static_cast<std::int64_t>(engine.events_processed()),
                  cp["events_processed"].i64())
            << side << "@" << key;
        EXPECT_EQ(static_cast<std::int64_t>(engine.events_dropped()),
                  cp["events_dropped"].i64())
            << side << "@" << key << ": only APPLIED events feed state";
        EXPECT_EQ(static_cast<std::int64_t>(engine.ts_regressions_dropped()),
                  cp["ts_regressions_dropped"].i64())
            << side << "@" << key;
        EXPECT_EQ(static_cast<std::int64_t>(engine.oversized_qty_dropped()),
                  cp["oversized_qty_dropped"].i64())
            << side << "@" << key;
        EXPECT_EQ(static_cast<std::int64_t>(engine.oversized_depth_skipped()),
                  cp["oversized_depth_skipped"].i64())
            << side << "@" << key;
        EXPECT_EQ(static_cast<std::int64_t>(engine.recoveries(iid)),
                  cp["recoveries"].i64())
            << side << "@" << key << ": stale->fresh recoveries";
        EXPECT_EQ(engine.warm_ts(iid), cp["warm_ts"].i64())
            << side << "@" << key << ": warmup anchor";
        EXPECT_EQ(engine.book_ok(iid), cp["book_ok"].boolean)
            << side << "@" << key;
        for (const auto& [name, entry] : cp["features"].obj) {
            const int slot = iap::feature_index(name);
            if (slot < 0) continue;
            const bool want_valid = entry["valid"].boolean;
            EXPECT_EQ(vec.valid[static_cast<std::size_t>(slot)], want_valid)
                << side << "@" << key << " " << name << " validity";
            if (!want_valid) continue;
            ++compared;
            const double want = entry["value"].num();
            const double got = vec.values[static_cast<std::size_t>(slot)];
            EXPECT_LE(std::fabs(got - want), 1e-9 + 1e-9 * std::fabs(want))
                << side << "@" << key << " " << name;
        }
    }
    EXPECT_GT(compared, 50) << side << ": too few valid features compared";
}

}  // namespace

TEST(FeatureGolden, AnomalyEqCheckpoints) { check_anomaly_side("eq", 0.01); }
TEST(FeatureGolden, AnomalyFxCheckpoints) { check_anomaly_side("fx", 1e-05); }

TEST(FeatureGolden, AnomalyVectorExercisesDropPaths) {
    const auto golden =
        iap_test::load_golden_json("expected_features_anomalies.json");
    for (const char* side : {"eq", "fx"}) {
        std::int64_t best = -1;
        const iap_test::Json* last = nullptr;
        for (const auto& [key, cp] : golden[side]["checkpoints"].obj) {
            const std::int64_t k = std::stoll(key);
            if (k > best) {
                best = k;
                last = &cp;
            }
        }
        ASSERT_NE(last, nullptr);
        EXPECT_GT((*last)["events_dropped"].i64(), 0) << side;
        EXPECT_GT((*last)["ts_regressions_dropped"].i64(), 0) << side;
        EXPECT_GT((*last)["recoveries"].i64(), 0) << side;
    }
}

// ---------------------------------------------------------------------------
// Ingestion scenarios (mirror of python/tests/test_feature_ingestion.py)
// ---------------------------------------------------------------------------

namespace {

constexpr std::int64_t kNs = 1'000'000'000;
constexpr std::int64_t kT0 = 1'787'578'200LL * kNs;

// Single-instrument feed with explicit per-venue sequence numbers.
class Feed {
public:
    Feed(std::uint32_t iid, double tick)
        : engine_({{iid, tick}}, 0), iid_(iid) {}

    bool send(iap::EventType et, std::int64_t ts, std::uint16_t venue,
              std::uint64_t seq, std::uint8_t side, std::int64_t price,
              std::int64_t qty, std::uint64_t order_id,
              std::uint64_t trade_id = 0) {
        if (seq == 0) seq = ++seq_[venue];
        seq_[venue] = std::max(seq_[venue], seq);
        MarketEvent ev{};
        ev.event_id = ++id_;
        ev.instrument_id = iid_;
        ev.venue_id = venue;
        ev.exchange_ts = ts;
        ev.receive_ts = ts + 150'000;
        ev.sequence = seq;
        ev.event_type = static_cast<std::uint8_t>(et);
        ev.side = side;
        ev.price_ticks = price;
        ev.qty = qty;
        ev.order_id = order_id;
        ev.trade_id = trade_id;
        return engine_.apply(ev, vec_);
    }

    void add(std::int64_t ts, std::uint8_t side, std::int64_t price,
             std::int64_t qty, std::uint64_t oid) {
        send(iap::EventType::ADD, ts, 1, 0, side, price, qty, oid);
    }

    bool valid(const char* name) const {
        return vec_.valid[static_cast<std::size_t>(iap::feature_index(name))];
    }
    double value(const char* name) const {
        return vec_.values[static_cast<std::size_t>(iap::feature_index(name))];
    }
    std::uint64_t seq_of(std::uint16_t v) { return seq_[v]; }

    FeatureEngine engine_;
    const FeatureVector& vec() const { return vec_; }

private:
    std::uint32_t iid_;
    std::map<std::uint16_t, std::uint64_t> seq_;
    std::uint64_t id_ = 0;
    FeatureVector vec_;
};

void expect_no_valid_nan(const FeatureVector& v) {
    for (std::size_t i = 0; i < v.values.size(); ++i) {
        if (v.valid[i]) {
            EXPECT_TRUE(std::isfinite(v.values[i])) << i;
        }
    }
}

}  // namespace

TEST(FeatureScenario, GatewayReplayAfterReconnect) {
    Feed f(1, 0.01);
    std::int64_t t = kT0;
    f.add(t, 0, 1000, 100, 1);
    f.add(t, 1, 1002, 100, 2);
    t += 2 * kNs;
    f.send(iap::EventType::TRADE, t, 1, 0, 0, 1001, 50, 0, 1);
    EXPECT_TRUE(f.valid("signed_volume_w1s_v1"));
    EXPECT_DOUBLE_EQ(f.value("signed_volume_w1s_v1"), 50.0);

    // the gateway replays the same message (duplicate sequence)
    f.send(iap::EventType::TRADE, t, 1, f.seq_of(1), 0, 1001, 50, 0, 1);
    EXPECT_DOUBLE_EQ(f.value("signed_volume_w1s_v1"), 50.0)
        << "a duplicate trade must not double-count signed volume";
    EXPECT_EQ(f.engine_.events_dropped(), 1u);

    // side > 1 on an ADD: dropped by the book, no depth change
    const double depth_before = f.value("depth_bid_l1_v1");
    f.send(iap::EventType::ADD, t, 1, 0, 2, 1000, 70, 900);
    EXPECT_EQ(f.engine_.events_dropped(), 2u);
    EXPECT_DOUBLE_EQ(f.value("depth_bid_l1_v1"), depth_before);

    // gap -> stale venue; a CANCEL arriving while stale is dropped
    f.send(iap::EventType::ADD, t, 1, f.seq_of(1) + 10, 0, 999, 10, 901);
    f.send(iap::EventType::CANCEL, t, 1, 0, 0, 1000, 0, 1);
    EXPECT_GE(f.engine_.events_dropped(), 3u);
    EXPECT_FALSE(f.valid("mid_price_v1")) << "stale venue leaves the view";
}

TEST(FeatureScenario, VenueDisconnectThenSnapshotRecovery) {
    Feed f(1, 0.01);
    std::int64_t t = kT0;
    f.add(t, 0, 1000, 100, 1);
    f.add(t, 1, 1002, 100, 2);
    std::int64_t bid = 1000;
    std::uint64_t oid = 10;
    for (int k = 0; k < 200; ++k) {
        t += 2 * kNs;
        const std::int64_t step = (k % 2 == 0) ? 1 : -1;
        f.add(t, 0, bid + step, 100, oid);
        f.send(iap::EventType::CANCEL, t, 1, 0, 0, bid, 0,
               k == 0 ? 1 : oid - 1);
        bid += step;
        ++oid;
    }
    ASSERT_TRUE(f.valid("rvol_w1m_v1"));
    EXPECT_GT(f.value("rvol_w1m_v1"), 0.0);
    EXPECT_TRUE(f.valid("ret_vol_adj_10s_v1"));

    // gap then 120 s of silence
    t += kNs;
    f.send(iap::EventType::ADD, t, 1, f.seq_of(1) + 50, 0, 1000, 10, oid + 50);
    EXPECT_FALSE(f.valid("mid_price_v1"));

    // recovery: a complete SNAPSHOT burst 1 % higher
    t += 120 * kNs;
    const std::uint64_t s = f.seq_of(1) + 1;
    f.send(iap::EventType::SNAPSHOT, t, 1, s, 0, 1010, 100, 0, 3);
    f.send(iap::EventType::SNAPSHOT, t, 1, s + 1, 1, 1012, 100, 0, 2);
    f.send(iap::EventType::SNAPSHOT, t, 1, s + 2, 0, 1009, 90, 0, 1);
    f.send(iap::EventType::SNAPSHOT, t, 1, s + 3, 1, 1013, 90, 0, 0);

    EXPECT_EQ(f.engine_.recoveries(1), 1u);
    EXPECT_EQ(f.engine_.warm_ts(1), t);
    EXPECT_TRUE(f.engine_.book_ok(1));
    for (const char* name :
         {"rvol_w10s_v1", "rvol_w1m_v1", "rvol_w5m_v1", "ret_log_10s_v1",
          "ret_log_1m_v1", "ret_vol_adj_10s_v1", "vol_regime_ratio_v1",
          "ofi_l1_w1s_v1", "signed_volume_w1m_v1"}) {
        EXPECT_FALSE(f.valid(name)) << name << " valid right after recovery";
    }
    EXPECT_TRUE(f.valid("mid_price_v1"));
    expect_no_valid_nan(f.vec());
}

TEST(FeatureScenario, ZeroRvolMakesRatiosInvalidNotHuge) {
    Feed f(1, 0.01);
    std::int64_t t = kT0;
    f.add(t, 0, 1000, 100, 1);
    f.add(t, 1, 1002, 100, 2);
    for (std::uint64_t k = 0; k < 70; ++k) {
        t += kNs;
        f.add(t, 0, 990, 5, 500 + k);
    }
    ASSERT_TRUE(f.valid("rvol_w1m_v1"));
    EXPECT_DOUBLE_EQ(f.value("rvol_w1m_v1"), 0.0);
    EXPECT_FALSE(f.valid("ret_vol_adj_10s_v1"));
    EXPECT_FALSE(f.valid("vol_regime_ratio_v1"));
}

TEST(FeatureScenario, OneSidedFlickerKeepsRvolSamples) {
    Feed f(1, 0.01);
    std::int64_t t = kT0;
    f.add(t, 0, 1000, 100, 1);
    f.add(t, 1, 1002, 100, 2);
    t += 11 * kNs;
    f.send(iap::EventType::CANCEL, t, 1, 0, 0, 1000, 0, 1);
    EXPECT_FALSE(f.valid("mid_price_v1"));
    t += 1000;
    f.add(t, 0, 999, 100, 3);
    ASSERT_TRUE(f.valid("rvol_w10s_v1"));
    EXPECT_GT(f.value("rvol_w10s_v1"), 0.0);
}

TEST(FeatureScenario, ZeroPriceQuoteIsDroppedNotApplied) {
    for (std::int64_t price : {std::int64_t{0}, std::int64_t{-5}}) {
        Feed f(101, 1e-05);
        const std::int64_t t = kT0;
        f.send(iap::EventType::QUOTE, t, 10, 0, 0, 110000, 1000, 0);
        f.send(iap::EventType::QUOTE, t, 10, 0, 1, 110002, 1000, 0);
        ASSERT_TRUE(f.valid("mid_price_v1"));
        f.send(iap::EventType::QUOTE, t + kNs, 10, 0, 0, price, 1000, 0);
        EXPECT_EQ(f.engine_.events_dropped(), 1u) << "price " << price;
        EXPECT_TRUE(f.valid("mid_price_v1")) << "previous quote prevails";
        expect_no_valid_nan(f.vec());
    }
}

TEST(FeatureScenario, CrossVenueTimestampRegression) {
    Feed f(1, 0.01);
    const std::int64_t t = kT0;
    f.send(iap::EventType::ADD, t, 1, 0, 0, 1000, 100, 1);
    f.send(iap::EventType::ADD, t, 1, 0, 1, 1002, 100, 2);
    const double mid_before = f.value("mid_price_v1");

    // venue 2's gateway clock runs 5 ms behind venue 1's
    const bool emitted =
        f.send(iap::EventType::ADD, t - 5'000'000, 2, 0, 0, 1001, 100, 3);
    EXPECT_FALSE(emitted) << "a ts regression emits no vector";
    EXPECT_EQ(f.engine_.ts_regressions_dropped(), 1u);
    EXPECT_EQ(f.engine_.events_dropped(), 1u);

    f.send(iap::EventType::ADD, t + kNs, 2, 0, 0, 1001, 100, 4);
    EXPECT_GT(f.value("mid_price_v1"), mid_before);
}

TEST(FeatureScenario, OversizedQuantitiesNeverOverflowWindowSums) {
    Feed f(1, 0.01);
    const std::int64_t t = kT0;
    f.add(t, 0, 1000, 100, 1);
    f.add(t, 1, 1002, 100, 2);
    f.send(iap::EventType::TRADE, t + kNs, 1, 0, 0, 1001,
           std::numeric_limits<std::int64_t>::max(), 0, 1);
    EXPECT_EQ(f.engine_.oversized_qty_dropped(), 1u);
    f.send(iap::EventType::ADD, t + 2 * kNs, 1, 0, 0, 998,
           std::numeric_limits<std::int64_t>::max(), 7);
    EXPECT_EQ(f.engine_.oversized_qty_dropped(), 2u);
    EXPECT_GE(f.engine_.oversized_depth_skipped(), 1u);
    EXPECT_FALSE(f.engine_.book_ok(1));
    expect_no_valid_nan(f.vec());
}
