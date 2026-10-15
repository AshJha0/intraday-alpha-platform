// Shared helpers of the v1.17 F7 parity tests (test_f7_*.cpp): loading the
// goldens python/tools/make_golden_f7.py writes, rebuilding their inputs
// (MarketEvent rows, venue models, router options, the committed real
// slice), and the byte-for-byte comparison of canonical JSON trees.

#pragma once

// GCC 13 (-O2, -Werror=maybe-uninitialized) reports a false positive on
// std::variant temporaries pushed into a contracts::Array (the Value
// returned by a port, e.g. cluster_mean, moved into the result tree);
// the sanitizer build sees no uninitialised read. Test-only translation
// units, so the diagnostic is silenced here for every F7 test.
#if defined(__GNUC__) && !defined(__clang__)
#pragma GCC diagnostic ignored "-Wmaybe-uninitialized"
#endif

#include <gtest/gtest.h>

#include <cmath>
#include <cstdint>
#include <fstream>
#include <iterator>
#include <limits>
#include <map>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

#include "iap/contracts/canonical_json.hpp"
#include "iap/execution/execution.hpp"
#include "iap/marketdata/codec.hpp"
#include "iap/marketdata/events.hpp"
#include "iap/orderbook/book.hpp"
#include "iap/sor/sor.hpp"
#include "iap/sor/sor_v2.hpp"
#include "iap/util/data_paths.hpp"

namespace iap_f7 {

using iap::contracts::Array;
using iap::contracts::Object;
using iap::contracts::Value;

// The real slice (benchmarks/slices/itch50_20190130_AAPL, F4) sampling rule
// of the goldens: after the k-th event (1-based), k > prefix, k % every == 0.
constexpr int kSlicePrefix = 1132;
constexpr int kSampleEvery = 500;
constexpr std::uint32_t kSliceInstrument = 1;
constexpr std::uint16_t kSliceVenue = 101;
constexpr double kSliceTick = 0.01;

inline bool is_sample(std::size_t k) {
    return k > static_cast<std::size_t>(kSlicePrefix) && k % kSampleEvery == 0;
}

inline std::string read_text(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) throw std::runtime_error("cannot open: " + path);
    std::ostringstream ss;
    ss << f.rdbuf();
    return ss.str();
}

inline Value load_golden(const std::string& name) {
    return iap::contracts::parse(read_text(iap::golden_file(name)));
}

inline std::vector<iap::MarketEvent> read_slice() {
    const std::string path = iap::repo_root() +
                             "/benchmarks/slices/itch50_20190130_AAPL/normalized/"
                             "eq_20190130.normalized.iap1";
    std::ifstream f(path, std::ios::binary);
    if (!f) throw std::runtime_error("cannot open the real slice: " + path);
    std::vector<std::uint8_t> bytes((std::istreambuf_iterator<char>(f)),
                                    std::istreambuf_iterator<char>());
    return iap::decode_iap1(bytes);
}

// Value constructors with the Python types spelled out.
inline Value vi(std::int64_t v) { return Value(static_cast<long long>(v)); }
inline Value vu(std::uint64_t v) { return Value(static_cast<unsigned long long>(v)); }
inline Value vd(double v) { return Value(v); }
inline Value vod(std::optional<double> v) { return v.has_value() ? Value(*v) : Value(nullptr); }
inline Value voi(std::optional<std::int64_t> v) {
    return v.has_value() ? vi(*v) : Value(nullptr);
}
inline Value vs(const std::string& s) { return Value(s); }

inline std::optional<double> opt_num(const Value& v) {
    if (v.is_null()) return std::nullopt;
    return v.as_number();
}
inline std::optional<std::int64_t> opt_i64(const Value& v) {
    if (v.is_null()) return std::nullopt;
    return v.as_int64();
}

// MarketEvent from the 12-field row of the goldens (MarketEvent::of order).
inline iap::MarketEvent event_from_row(const Value& r) {
    return iap::MarketEvent::of(
        r.at(0).as_uint64(), static_cast<std::uint32_t>(r.at(1).as_uint64()),
        static_cast<std::uint16_t>(r.at(2).as_uint64()), r.at(3).as_int64(),
        r.at(4).as_int64(), r.at(5).as_uint64(),
        static_cast<std::uint8_t>(r.at(6).as_uint64()),
        static_cast<std::uint8_t>(r.at(7).as_uint64()), r.at(8).as_int64(),
        r.at(9).as_int64(), r.at(10).as_uint64(), r.at(11).as_uint64());
}

inline std::vector<iap::MarketEvent> events_from_rows(const Value& rows) {
    std::vector<iap::MarketEvent> out;
    for (const Value& r : rows.as_array()) out.push_back(event_from_row(r));
    return out;
}

inline iap::VenueSpec venue_spec_from(const Value& v) {
    iap::VenueSpec s;
    s.venue_id = static_cast<std::uint16_t>(v.at("venue_id").as_uint64());
    s.name = v.at("name").as_string();
    s.is_fx = v.at("is_fx").as_bool();
    s.taker_fee_per_share = v.at("taker_fee_per_share").as_number();
    s.maker_rebate_per_share = v.at("maker_rebate_per_share").as_number();
    s.commission_per_million = v.at("commission_per_million").as_number();
    s.latency_mean_ns = v.at("latency_mean_ns").as_int64();
    s.latency_jitter_ns = v.at("latency_jitter_ns").as_int64();
    return s;
}

inline iap::InstrumentSpec instrument_from(const Value& v) {
    iap::InstrumentSpec s;
    s.instrument_id = static_cast<std::uint32_t>(v.at("instrument_id").as_uint64());
    s.tick_size = v.at("tick_size").as_number();
    s.qty_unit = v.at("qty_unit").as_number();
    s.adv = v.at("adv").as_number();
    s.quote_ccy = v.at("quote_ccy").as_string();
    return s;
}

inline iap::VenueModel venue_model_from(const Value& v) {
    iap::VenueModel m;
    m.spec = venue_spec_from(v.at("spec"));
    m.p_fill_touch = v.at("p_fill_touch").as_number();
    for (const auto& [h, t] : v.at("toxicity_bps").as_object()) m.toxicity_bps[h] = t.as_number();
    m.latency_ns = opt_i64(v.at("latency_ns"));
    for (const Value& t : v.at("tiers").as_array()) {
        m.tiers.push_back({t.at(0).as_int64(), t.at(1).as_number(), t.at(2).as_number()});
    }
    return m;
}

inline std::map<std::uint16_t, iap::VenueModel> venue_models_from(const Value& arr) {
    std::map<std::uint16_t, iap::VenueModel> out;
    for (const Value& v : arr.as_array()) {
        iap::VenueModel m = venue_model_from(v);
        out[m.venue_id()] = m;
    }
    return out;
}

inline iap::RouterOptions router_options_from(const Value& v) {
    iap::RouterOptions o;
    o.tox_horizon = v.at("tox_horizon").as_string();
    o.latency_penalty_bps_per_ms = v.at("latency_penalty_bps_per_ms").as_number();
    o.miss_cost_bps = opt_num(v.at("miss_cost_bps"));
    o.stagger = v.at("stagger").as_string() == "none" ? iap::StaggerMode::NONE
                                                       : iap::StaggerMode::SYNC_ARRIVAL;
    o.sweep_rank = v.at("sweep_rank").as_string() == "all_in" ? iap::SweepRank::ALL_IN
                                                               : iap::SweepRank::PRICE;
    o.sweep_levels = static_cast<int>(v.at("sweep_levels").as_int64());
    const Value& mt = v.at("max_toxicity_bps");  // null = inf (not JSON)
    o.max_toxicity_bps = mt.is_null() ? std::numeric_limits<double>::infinity() : mt.as_number();
    o.tox_scale_bps = v.at("tox_scale_bps").as_number();
    o.lot = v.at("lot").as_int64();
    return o;
}

inline iap::SorOptions sor_options_from(const Value& v) {
    iap::SorOptions o;
    o.prefer_rebate = v.at("prefer_rebate").as_bool();
    o.max_venue_latency_ns = v.at("max_venue_latency_ns").as_int64();
    return o;
}

inline std::optional<std::vector<std::uint16_t>> candidates_from(const Value& v) {
    if (v.is_null()) return std::nullopt;
    std::vector<std::uint16_t> out;
    for (const Value& c : v.as_array()) out.push_back(static_cast<std::uint16_t>(c.as_uint64()));
    return out;
}

// Byte-for-byte: the canonical JSON of `got` equals that of `expected`.
// On a mismatch the failure names the first differing offset with context.
inline void expect_canonical_eq(const Value& expected, const Value& got,
                                const std::string& what) {
    const std::string a = iap::contracts::canonical_json(expected);
    const std::string b = iap::contracts::canonical_json(got);
    if (a == b) {
        SUCCEED();
        return;
    }
    std::size_t i = 0;
    while (i < a.size() && i < b.size() && a[i] == b[i]) ++i;
    const std::size_t lo = i > 120 ? i - 120 : 0;
    ADD_FAILURE() << what << ": canonical bytes differ at offset " << i << " of "
                  << a.size() << " (expected) / " << b.size() << " (got)\n"
                  << "  expected ..." << a.substr(lo, 240) << "\n"
                  << "  got      ..." << b.substr(lo, 240);
}

// (plain, tol): `doc` with every `keys` member moved into a parallel tree
// (python/tools/f7_golden/r10_markout_decomposition.split_tol).
inline std::pair<Value, Value> split_tol(const Value& doc, const std::vector<std::string>& keys) {
    if (doc.is_object()) {
        Object plain, tol;
        for (const auto& [k, v] : doc.as_object()) {
            bool is_tol = false;
            for (const auto& key : keys) is_tol = is_tol || key == k;
            if (is_tol) {
                tol[k] = v;
                continue;
            }
            auto [p, t] = split_tol(v, keys);
            plain[k] = p;
            const bool empty = t.is_null() || (t.is_object() && t.as_object().empty()) ||
                               (t.is_array() && t.as_array().empty());
            if (!empty) tol[k] = t;
        }
        return {Value(std::move(plain)), Value(std::move(tol))};
    }
    if (doc.is_array()) {
        Array plain, tol;
        bool any = false;
        for (const Value& v : doc.as_array()) {
            auto [p, t] = split_tol(v, keys);
            plain.push_back(p);
            const bool empty = t.is_null() || (t.is_object() && t.as_object().empty()) ||
                               (t.is_array() && t.as_array().empty());
            if (!empty) any = true;
            tol.push_back(t);
        }
        return {Value(std::move(plain)), any ? Value(std::move(tol)) : Value(Array{})};
    }
    return {doc, Value(nullptr)};
}

// Every number within `tol`, every other leaf equal, same shape.
inline void expect_tree_close(const Value& expected, const Value& got, double tol,
                              const std::string& what) {
    if (expected.is_object()) {
        ASSERT_TRUE(got.is_object()) << what;
        ASSERT_EQ(expected.as_object().size(), got.as_object().size()) << what;
        for (const auto& [k, v] : expected.as_object()) {
            ASSERT_TRUE(got.has(k)) << what << "." << k;
            expect_tree_close(v, got.at(k), tol, what + "." + k);
        }
        return;
    }
    if (expected.is_array()) {
        ASSERT_TRUE(got.is_array()) << what;
        ASSERT_EQ(expected.as_array().size(), got.as_array().size()) << what;
        for (std::size_t i = 0; i < expected.as_array().size(); ++i) {
            expect_tree_close(expected.at(i), got.at(i), tol, what + "[" + std::to_string(i) + "]");
        }
        return;
    }
    if (expected.is_double() || expected.is_integer()) {
        ASSERT_TRUE(got.is_double() || got.is_integer()) << what;
        EXPECT_NEAR(expected.as_number(), got.as_number(), tol) << what;
        return;
    }
    EXPECT_EQ(expected, got) << what;
}

}  // namespace iap_f7
