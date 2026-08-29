// Shared helpers for the golden test suite: locating tests/golden/, loading
// golden JSON, and converting book state to a golden-comparable form.

#pragma once

#include <fstream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

#include "iap/orderbook/book.hpp"
#include "json_min.hpp"

namespace iap_test {

inline std::string golden_dir() { return IAP_GOLDEN_DIR; }

inline std::string golden_path(const std::string& name) {
    return golden_dir() + "/" + name;
}

inline std::string read_text_file(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) throw std::runtime_error("cannot open: " + path);
    std::ostringstream ss;
    ss << f.rdbuf();
    return ss.str();
}

inline Json load_golden_json(const std::string& name) {
    return JsonParser::parse(read_text_file(golden_path(name)));
}

// Compare a [(price, value), ...] list against golden JSON [[p, v], ...].
inline void expect_levels_eq(const std::vector<iap::LevelEntry>& got,
                             const Json& expected, const std::string& what) {
    const auto& rows = expected.a();
    if (got.size() != rows.size()) {
        throw std::runtime_error(what + ": size mismatch, got " +
                                 std::to_string(got.size()) + " expected " +
                                 std::to_string(rows.size()));
    }
    for (std::size_t i = 0; i < rows.size(); ++i) {
        const auto& row = rows[i].a();
        if (row.size() != 2 || got[i].first != row[0].i64() ||
            got[i].second != row[1].i64()) {
            throw std::runtime_error(
                what + "[" + std::to_string(i) + "]: got (" +
                std::to_string(got[i].first) + ", " +
                std::to_string(got[i].second) + ") expected (" +
                std::to_string(row[0].i64()) + ", " +
                std::to_string(row[1].i64()) + ")");
        }
    }
}

// Compare a full state summary against one golden expected_book_states entry.
inline void expect_summary_eq(const iap::BookStateSummary& s,
                              const Json& exp, const std::string& what) {
    auto check = [&](const char* field, std::int64_t got, std::int64_t want) {
        if (got != want) {
            throw std::runtime_error(what + "." + field + ": got " +
                                     std::to_string(got) + " expected " +
                                     std::to_string(want));
        }
    };
    check("best_bid_ticks", s.best_bid_ticks, exp["best_bid_ticks"].i64());
    check("best_bid_size", s.best_bid_size, exp["best_bid_size"].i64());
    check("best_ask_ticks", s.best_ask_ticks, exp["best_ask_ticks"].i64());
    check("best_ask_size", s.best_ask_size, exp["best_ask_size"].i64());
    expect_levels_eq(s.depth_bid_top5, exp["depth_bid_top5"],
                     what + ".depth_bid_top5");
    expect_levels_eq(s.depth_ask_top5, exp["depth_ask_top5"],
                     what + ".depth_ask_top5");
    expect_levels_eq(s.order_count_bid_top3, exp["order_count_bid_top3"],
                     what + ".order_count_bid_top3");
    expect_levels_eq(s.order_count_ask_top3, exp["order_count_ask_top3"],
                     what + ".order_count_ask_top3");
    check("trade_flow", s.trade_flow, exp["trade_flow"].i64());
    check("sequence", static_cast<std::int64_t>(s.sequence),
          exp["sequence"].i64());
}

}  // namespace iap_test
