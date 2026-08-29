// Incremental-vs-brute-force recompute tests (8 representative features on
// the golden EQ vector).
//
// The brute-force side deliberately shares NO rolling machinery with the
// engine: it rebuilds the merged depth from ConsolidatedBook::depth() (a
// different merge path than the engine's per-venue cache), computes OFI
// deltas with std::map unions, keeps plain append-only logs, and evaluates
// every window by scanning the log per emission ((t - w, t] half-open).
// Any drift in the engine's ring buffers, eviction order or at-or-before
// lookups shows up as a mismatch here.

#include <gtest/gtest.h>

#include <cmath>
#include <map>
#include <string>
#include <vector>

#include "golden_util.hpp"
#include "iap/features/feature_engine.hpp"
#include "iap/marketdata/codec.hpp"
#include "iap/orderbook/book.hpp"

namespace {

using iap::FeatureVector;
using iap::LevelEntry;
using iap::MarketEvent;

struct BruteRow {
    std::int64_t t = 0;
    bool ok = false;
    std::int64_t db5 = 0, da5 = 0, db10 = 0;
    std::int64_t mid2 = 0;
    // log lengths as of this event (equal-timestamp events later in file
    // order must NOT leak into earlier emissions)
    std::size_t n_ofi = 0, n_tr = 0, n_dlm = 0, n_logmid = 0;
};

struct Logs {
    // (ts, value) append-only logs
    std::vector<std::pair<std::int64_t, std::int64_t>> ofi_l1;
    std::vector<std::pair<std::int64_t, std::int64_t>> ofi_l3;
    std::vector<std::pair<std::int64_t, std::int64_t>> trade_signed;
    std::vector<std::pair<std::int64_t, std::int64_t>> trade_buy;
    std::vector<std::pair<std::int64_t, std::int64_t>> trade_sell;
    std::vector<std::pair<std::int64_t, double>> dlm_sq;
    std::vector<std::pair<std::int64_t, double>> logmid;  // at mid changes
    std::vector<BruteRow> rows;  // one per event (post-event state)
    std::int64_t first_ts = 0;
};

// Window sum over (t - w, t] by scanning the first `n` log entries.
template <typename T>
T window_sum(const std::vector<std::pair<std::int64_t, T>>& log,
             std::size_t n, std::int64_t t, std::int64_t w) {
    T s{};
    for (std::size_t i = 0; i < n && i < log.size(); ++i) {
        if (log[i].first > t - w && log[i].first <= t) s += log[i].second;
    }
    return s;
}

// Latest value with ts <= t among the first `n` log entries.
bool at_or_before(const std::vector<std::pair<std::int64_t, double>>& log,
                  std::size_t n, std::int64_t t, double& out) {
    bool found = false;
    for (std::size_t i = 0; i < n && i < log.size(); ++i) {
        if (log[i].first <= t) {
            out = log[i].second;
            found = true;
        }
    }
    return found;
}

std::int64_t map_delta(const std::map<std::int64_t, std::int64_t>& prev,
                       const std::map<std::int64_t, std::int64_t>& curr) {
    std::int64_t d = 0;
    for (const auto& [p, q] : curr) {
        auto it = prev.find(p);
        d += q - (it == prev.end() ? 0 : it->second);
    }
    for (const auto& [p, q] : prev) {
        if (curr.find(p) == curr.end()) d -= q;
    }
    return d;
}

std::map<std::int64_t, std::int64_t> topk(const std::vector<LevelEntry>& d,
                                          std::size_t k) {
    std::map<std::int64_t, std::int64_t> m;
    for (std::size_t i = 0; i < d.size() && i < k; ++i) {
        m[d[i].first] = d[i].second;
    }
    return m;
}

class BruteForceTest : public ::testing::Test {
protected:
    static void SetUpTestSuite() {
        events_ = new std::vector<MarketEvent>(
            iap::read_jsonl(iap_test::golden_path("events_eq_mbo.jsonl")));
        engine_rows_ = new std::vector<FeatureVector>();
        iap::FeatureEngine engine({{1u, 0.01}}, 0);
        engine.run(*events_, engine_rows_);
        logs_ = new Logs();
        build_brute(*events_, *logs_);
    }
    static void TearDownTestSuite() {
        delete events_;
        delete engine_rows_;
        delete logs_;
        events_ = nullptr;
        engine_rows_ = nullptr;
        logs_ = nullptr;
    }

    static void build_brute(const std::vector<MarketEvent>& events, Logs& lg) {
        iap::ConsolidatedBook cons(1);
        std::vector<LevelEntry> prev_bid, prev_ask;
        bool prev_ok = false;
        std::int64_t prev_mid2 = 0;
        bool have_hist = false;
        double last_logmid = 0.0;
        lg.first_ts = events.front().exchange_ts;
        for (const auto& ev : events) {
            cons.apply(ev);
            const std::int64_t t = ev.exchange_ts;
            const auto et = static_cast<iap::EventType>(ev.event_type);
            if (et == iap::EventType::TRADE) {
                const std::int64_t buy = ev.side == 0 ? ev.qty : 0;
                lg.trade_signed.emplace_back(t, buy - (ev.qty - buy));
                lg.trade_buy.emplace_back(t, buy);
                lg.trade_sell.emplace_back(t, ev.qty - buy);
            }
            const bool touch = et == iap::EventType::ADD ||
                               et == iap::EventType::MODIFY ||
                               et == iap::EventType::CANCEL ||
                               et == iap::EventType::EXECUTE ||
                               et == iap::EventType::QUOTE ||
                               (et == iap::EventType::SNAPSHOT &&
                                ev.trade_id == 0);
            BruteRow row;
            row.t = t;
            if (touch) {
                const auto bid = cons.depth(iap::Side::BID, 10);
                const auto ask = cons.depth(iap::Side::ASK, 10);
                if (!prev_bid.empty() || !prev_ask.empty() || !bid.empty() ||
                    !ask.empty()) {
                    lg.ofi_l1.emplace_back(
                        t, map_delta(topk(prev_bid, 1), topk(bid, 1)) -
                               map_delta(topk(prev_ask, 1), topk(ask, 1)));
                    lg.ofi_l3.emplace_back(
                        t, map_delta(topk(prev_bid, 3), topk(bid, 3)) -
                               map_delta(topk(prev_ask, 3), topk(ask, 3)));
                }
                const bool ok = !bid.empty() && !ask.empty();
                if (ok) {
                    const std::int64_t mid2 = bid[0].first + ask[0].first;
                    if (!prev_ok || mid2 != prev_mid2) {
                        const double lm =
                            std::log(static_cast<double>(mid2));
                        if (prev_ok && have_hist) {
                            const double dlm = lm - last_logmid;
                            lg.dlm_sq.emplace_back(t, dlm * dlm);
                        }
                        lg.logmid.emplace_back(t, lm);
                        last_logmid = lm;
                        have_hist = true;
                    }
                    prev_mid2 = mid2;
                }
                prev_ok = ok;
                prev_bid = bid;
                prev_ask = ask;
            }
            // Post-event instantaneous state (whether touched or not).
            row.n_ofi = lg.ofi_l1.size();
            row.n_tr = lg.trade_signed.size();
            row.n_dlm = lg.dlm_sq.size();
            row.n_logmid = lg.logmid.size();
            row.ok = prev_ok;
            if (prev_ok) {
                for (std::size_t i = 0; i < prev_bid.size(); ++i) {
                    if (i < 5) row.db5 += prev_bid[i].second;
                    row.db10 += prev_bid[i].second;
                }
                for (std::size_t i = 0; i < prev_ask.size() && i < 5; ++i) {
                    row.da5 += prev_ask[i].second;
                }
                row.mid2 = prev_mid2;
            }
            lg.rows.push_back(row);
        }
    }

    static bool warm(std::int64_t t, std::int64_t w) {
        return t - logs_->first_ts >= w;
    }

    static std::vector<MarketEvent>* events_;
    static std::vector<FeatureVector>* engine_rows_;
    static Logs* logs_;
};

std::vector<MarketEvent>* BruteForceTest::events_ = nullptr;
std::vector<FeatureVector>* BruteForceTest::engine_rows_ = nullptr;
Logs* BruteForceTest::logs_ = nullptr;

TEST_F(BruteForceTest, OfiL1W30s) {
    for (std::size_t i = 0; i < engine_rows_->size(); ++i) {
        const auto& vec = (*engine_rows_)[i];
        const std::int64_t t = vec.timestamp;
        const bool w = warm(t, iap::W_30S);
        ASSERT_EQ(vec.valid[iap::F_OFI_L1_W30S], w) << "row " << i;
        if (!w) continue;
        const std::int64_t brute =
            window_sum(logs_->ofi_l1, logs_->rows[i].n_ofi, t, iap::W_30S);
        ASSERT_EQ(vec.values[iap::F_OFI_L1_W30S],
                  static_cast<double>(brute))
            << "row " << i;  // integer sums: exact
    }
}

TEST_F(BruteForceTest, OfiL3W5s) {
    for (std::size_t i = 0; i < engine_rows_->size(); ++i) {
        const auto& vec = (*engine_rows_)[i];
        const std::int64_t t = vec.timestamp;
        const bool w = warm(t, iap::W_5S);
        ASSERT_EQ(vec.valid[iap::F_OFI_L3_W5S], w) << "row " << i;
        if (!w) continue;
        const std::int64_t brute =
            window_sum(logs_->ofi_l3, logs_->rows[i].n_ofi, t, iap::W_5S);
        ASSERT_EQ(vec.values[iap::F_OFI_L3_W5S], static_cast<double>(brute))
            << "row " << i;
    }
}

TEST_F(BruteForceTest, SignedVolumeW10s) {
    for (std::size_t i = 0; i < engine_rows_->size(); ++i) {
        const auto& vec = (*engine_rows_)[i];
        const std::int64_t t = vec.timestamp;
        const bool w = warm(t, iap::W_10S);
        ASSERT_EQ(vec.valid[iap::F_SIGNED_VOLUME_W10S], w) << "row " << i;
        if (!w) continue;
        const std::int64_t brute = window_sum(
            logs_->trade_signed, logs_->rows[i].n_tr, t, iap::W_10S);
        ASSERT_EQ(vec.values[iap::F_SIGNED_VOLUME_W10S],
                  static_cast<double>(brute))
            << "row " << i;
    }
}

TEST_F(BruteForceTest, TradeImbalanceW1m) {
    for (std::size_t i = 0; i < engine_rows_->size(); ++i) {
        const auto& vec = (*engine_rows_)[i];
        const std::int64_t t = vec.timestamp;
        const std::int64_t buys = window_sum(
            logs_->trade_buy, logs_->rows[i].n_tr, t, iap::W_1M);
        const std::int64_t sells = window_sum(
            logs_->trade_sell, logs_->rows[i].n_tr, t, iap::W_1M);
        const bool valid = warm(t, iap::W_1M) && (buys + sells) > 0;
        ASSERT_EQ(vec.valid[iap::F_TRADE_IMBALANCE_W1M], valid) << "row " << i;
        if (!valid) continue;
        const double brute = static_cast<double>(buys - sells) /
                             static_cast<double>(buys + sells);
        ASSERT_NEAR(vec.values[iap::F_TRADE_IMBALANCE_W1M], brute, 1e-12)
            << "row " << i;
    }
}

TEST_F(BruteForceTest, RvolW1m) {
    for (std::size_t i = 0; i < engine_rows_->size(); ++i) {
        const auto& vec = (*engine_rows_)[i];
        const std::int64_t t = vec.timestamp;
        const bool w = warm(t, iap::W_1M);
        ASSERT_EQ(vec.valid[iap::F_RVOL_W1M], w) << "row " << i;
        if (!w) continue;
        const double s = window_sum(logs_->dlm_sq, logs_->rows[i].n_dlm,
                                    t, iap::W_1M);
        const double brute = std::sqrt(std::max(s, 0.0) / 60.0);
        ASSERT_NEAR(vec.values[iap::F_RVOL_W1M], brute,
                    1e-9 + 1e-9 * std::fabs(brute))
            << "row " << i;
    }
}

TEST_F(BruteForceTest, RetLog10s) {
    for (std::size_t i = 0; i < engine_rows_->size(); ++i) {
        const auto& vec = (*engine_rows_)[i];
        const std::int64_t t = vec.timestamp;
        const BruteRow& row = logs_->rows[i];
        double past = 0.0;
        const bool has = row.ok && at_or_before(logs_->logmid, row.n_logmid,
                                                t - iap::W_10S, past);
        ASSERT_EQ(vec.valid[iap::F_RET_LOG_10S], has) << "row " << i;
        if (!has) continue;
        const double brute =
            std::log(static_cast<double>(row.mid2)) - past;
        ASSERT_NEAR(vec.values[iap::F_RET_LOG_10S], brute,
                    1e-9 + 1e-9 * std::fabs(brute))
            << "row " << i;
    }
}

TEST_F(BruteForceTest, ImbalanceL5) {
    for (std::size_t i = 0; i < engine_rows_->size(); ++i) {
        const auto& vec = (*engine_rows_)[i];
        const BruteRow& row = logs_->rows[i];
        const bool valid = row.ok && (row.db5 + row.da5) > 0;
        ASSERT_EQ(vec.valid[iap::F_IMBALANCE_L5], valid) << "row " << i;
        if (!valid) continue;
        const double brute = static_cast<double>(row.db5 - row.da5) /
                             static_cast<double>(row.db5 + row.da5);
        ASSERT_NEAR(vec.values[iap::F_IMBALANCE_L5], brute, 1e-12)
            << "row " << i;
    }
}

TEST_F(BruteForceTest, DepthBidL10) {
    for (std::size_t i = 0; i < engine_rows_->size(); ++i) {
        const auto& vec = (*engine_rows_)[i];
        const BruteRow& row = logs_->rows[i];
        ASSERT_EQ(vec.valid[iap::F_DEPTH_BID_L10], row.ok) << "row " << i;
        if (!row.ok) continue;
        ASSERT_EQ(vec.values[iap::F_DEPTH_BID_L10],
                  static_cast<double>(row.db10))
            << "row " << i;
    }
}

}  // namespace
