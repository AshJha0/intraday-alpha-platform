// Parent-algo schedule-shape tests (VWAP/TWAP/POV/IS) and SOR routing
// (pinned rules: include/iap/execution/algos.hpp, include/iap/sor/sor.hpp).

#include <gtest/gtest.h>

#include <cmath>
#include <numeric>
#include <vector>

#include "iap/execution/algos.hpp"
#include "iap/marketdata/events.hpp"
#include "iap/replay/exec_replay.hpp"
#include "iap/sor/sor.hpp"

namespace {

using iap::AlgoType;
using iap::MarketEvent;
using iap::ParentOrder;

constexpr std::int64_t T0 = 1'700'000'000'000'000'000;
constexpr std::int64_t SEC = 1'000'000'000;

ParentOrder parent(AlgoType algo, std::int64_t qty, int slices) {
    ParentOrder p;
    p.parent_id = 1;
    p.instrument_id = 7;
    p.venue_id = 1;
    p.side = 0;
    p.qty = qty;
    p.algo = algo;
    p.start_ts = T0;
    p.end_ts = T0 + 100 * SEC;
    p.slices = slices;
    return p;
}

TEST(AlgoSchedule, TwapUniformSlicesSumExactly) {
    const auto q = iap::slice_quantities(parent(AlgoType::TWAP, 1000, 8));
    ASSERT_EQ(q.size(), 8u);
    EXPECT_EQ(std::accumulate(q.begin(), q.end(), std::int64_t{0}), 1000);
    for (std::size_t i = 0; i < q.size(); ++i) EXPECT_EQ(q[i], 125);
    // Non-divisible: remainder goes to the earliest slices (pinned).
    const auto r = iap::slice_quantities(parent(AlgoType::TWAP, 1002, 8));
    EXPECT_EQ(std::accumulate(r.begin(), r.end(), std::int64_t{0}), 1002);
    EXPECT_EQ(r[0], 126);
    EXPECT_EQ(r[1], 126);
    for (std::size_t i = 2; i < r.size(); ++i) EXPECT_EQ(r[i], 125);
}

TEST(AlgoSchedule, VwapUShapedCurve) {
    // Pinned curve w_i = 1 + x_i^2: heaviest at the window edges,
    // lightest in the middle, symmetric; quantities sum exactly.
    const auto q = iap::slice_quantities(parent(AlgoType::VWAP, 1000, 9));
    ASSERT_EQ(q.size(), 9u);
    EXPECT_EQ(std::accumulate(q.begin(), q.end(), std::int64_t{0}), 1000);
    EXPECT_GT(q.front(), q[4]);  // edge > middle
    EXPECT_GT(q.back(), q[4]);
    for (std::size_t i = 0; i < 4; ++i) {
        EXPECT_GE(q[i], q[i + 1]) << "front half must decay toward middle";
        EXPECT_LE(std::labs(q[i] - q[8 - i]), 1)  // symmetric up to rounding
            << "slice " << i;
    }
    // Exact weights for N=4 and qty 400: w = {2, 10/9, 10/9, 2} =>
    // targets {128.571.., 71.428.., 71.428.., 128.571..} => {129, 71, 71,
    // 129} by largest remainder (golden-scenario apportionment, hand-checked).
    const auto g = iap::slice_quantities(parent(AlgoType::VWAP, 400, 4));
    EXPECT_EQ(g, (std::vector<std::int64_t>{129, 71, 71, 129}));
}

TEST(AlgoSchedule, IsFrontLoadedByRiskAversion) {
    auto p = parent(AlgoType::IS, 900, 6);
    p.risk_aversion = 1.0;
    const auto q = iap::slice_quantities(p);
    EXPECT_EQ(std::accumulate(q.begin(), q.end(), std::int64_t{0}), 900);
    for (std::size_t i = 1; i < q.size(); ++i) {
        EXPECT_LE(q[i], q[i - 1]) << "IS schedule must be non-increasing";
    }
    EXPECT_GT(q.front(), q.back());
    // Higher urgency shifts more quantity into the first slice.
    auto hot = p;
    hot.risk_aversion = 3.0;
    const auto qh = iap::slice_quantities(hot);
    EXPECT_GT(qh.front(), q.front());
    // Golden-scenario apportionment (600 over 3 slices, lambda 1.0):
    // w = {1, e^-0.5, e^-1} => {304, 184, 112} (hand-checked).
    auto g = parent(AlgoType::IS, 600, 3);
    g.risk_aversion = 1.0;
    EXPECT_EQ(iap::slice_quantities(g),
              (std::vector<std::int64_t>{304, 184, 112}));
}

TEST(AlgoSchedule, SliceTimesEvenlySpaced) {
    const auto t = iap::slice_times(parent(AlgoType::TWAP, 100, 4));
    ASSERT_EQ(t.size(), 4u);
    EXPECT_EQ(t[0], T0);
    EXPECT_EQ(t[1], T0 + 25 * SEC);
    EXPECT_EQ(t[2], T0 + 50 * SEC);
    EXPECT_EQ(t[3], T0 + 75 * SEC);
    auto bad = parent(AlgoType::TWAP, 100, 4);
    bad.end_ts = bad.start_ts;
    EXPECT_THROW(iap::slice_times(bad), std::invalid_argument);
    EXPECT_THROW(iap::slice_weights(parent(AlgoType::POV, 100, 4)),
                 std::invalid_argument);
}

// --------------------------------------------------------- replay-driven ---

iap::ExecConfig algo_config() {
    iap::ExecConfig cfg;
    cfg.seed = 7;
    iap::VenueSpec v;
    v.venue_id = 1;
    v.name = "TST";
    v.taker_fee_per_share = 0.003;
    v.maker_rebate_per_share = 0.002;
    v.latency_mean_ns = 100'000;
    v.latency_jitter_ns = 0;
    cfg.venues[1] = v;
    iap::VenueSpec v2 = v;
    v2.venue_id = 2;
    v2.taker_fee_per_share = 0.001;  // cheaper taker venue
    v2.maker_rebate_per_share = 0.0025;
    cfg.venues[2] = v2;
    iap::InstrumentSpec ins;
    ins.instrument_id = 7;
    ins.tick_size = 0.01;
    ins.lot_size = 1.0;
    ins.adv = 1'000'000.0;
    cfg.instruments[7] = ins;
    return cfg;
}

// A stream that keeps a two-sided venue-1 book plus periodic TRADEs.
std::vector<MarketEvent> algo_stream() {
    std::vector<MarketEvent> evs;
    std::uint64_t seq = 0;
    auto push = [&](std::int64_t ts, std::uint8_t type, std::uint8_t side,
                    std::int64_t px, std::int64_t qty, std::uint64_t oid,
                    std::uint64_t tid) {
        ++seq;
        evs.push_back(
            MarketEvent::of(seq, 7, 1, ts, ts, seq, type, side, px, qty, oid,
                            tid));
    };
    push(T0 - SEC, 1, 0, 100, 100000, 1, 0);
    push(T0 - SEC + 1, 1, 1, 101, 100000, 2, 0);
    for (int i = 0; i < 200; ++i) {
        const std::int64_t ts = T0 + i * SEC;
        push(ts, 5, i % 2 == 0 ? 0 : 1, 100, 40, 0, 1000 + i);  // TRADE 40
        push(ts + SEC / 2, 9, 0, 0, 0, 0, 0);                   // HEARTBEAT
    }
    return evs;
}

TEST(AlgoReplay, PovTracksParticipationCap) {
    auto p = parent(AlgoType::POV, 500, 1);
    p.algo = AlgoType::POV;
    p.participation = 0.10;
    p.max_child_qty = 25;
    iap::ExecutionReplay replay(algo_config(), {p});
    const auto res = replay.run(algo_stream());
    const auto& rep = res.parents.at(1);
    // Window volume = 100 TRADEs of 40 inside [T0, T0+100s) = 4000;
    // 10% participation = 400 <= parent 500 and every child <= 25.
    EXPECT_EQ(rep.filled_qty, 400);
    EXPECT_GE(rep.children, 400 / 25);
    std::int64_t sent = 0;
    std::int64_t vol_seen = 0;
    std::size_t fi = 0;
    for (int i = 0; i < 100; ++i) {
        vol_seen += 40;
        while (fi < res.fills.size() &&
               res.fills[fi].ts <= T0 + i * SEC + SEC / 2) {
            sent += res.fills[fi].qty;
            ++fi;
        }
        // Participation never runs ahead of the observed volume.
        EXPECT_LE(sent, static_cast<std::int64_t>(0.10 * vol_seen) + 25);
    }
    for (const auto& f : res.fills) {
        EXPECT_LE(f.qty, 25);
        EXPECT_EQ(f.liquidity, iap::Liquidity::TAKER);  // POV children: MARKET
    }
}

TEST(AlgoReplay, TwapChildrenSpreadAcrossWindow) {
    auto p = parent(AlgoType::TWAP, 400, 4);
    iap::ExecutionReplay replay(algo_config(), {p});
    const auto res = replay.run(algo_stream());
    const auto& rep = res.parents.at(1);
    EXPECT_EQ(rep.children, 4);
    // Children decided at the first event at/after each due time, i.e. one
    // per 25 s quarter of the window.
    std::vector<std::int64_t> due = {T0, T0 + 25 * SEC, T0 + 50 * SEC,
                                     T0 + 75 * SEC};
    std::vector<std::int64_t> decisions;
    for (const auto& [oid, o] : replay.simulator().orders()) {
        (void)oid;
        decisions.push_back(o.decision_ts);
    }
    std::sort(decisions.begin(), decisions.end());
    ASSERT_EQ(decisions.size(), 4u);
    for (std::size_t i = 0; i < 4; ++i) {
        EXPECT_GE(decisions[i], due[i]);
        EXPECT_LT(decisions[i], due[i] + SEC);  // stream ticks every 0.5 s
    }
}

TEST(SorRouting, AggressivePrefersPriceThenFee) {
    const auto cfg = algo_config();
    iap::SmartOrderRouter sor(cfg.venues);
    iap::ConsolidatedBook book(7);
    std::uint64_t seq1 = 0, seq2 = 0;
    auto add = [&](std::uint16_t vid, std::uint8_t side, std::int64_t px,
                   std::int64_t qty, std::uint64_t oid) {
        std::uint64_t& s = vid == 1 ? seq1 : seq2;
        ++s;
        book.apply(MarketEvent::of(s, 7, vid, T0 + static_cast<std::int64_t>(s),
                                   T0 + static_cast<std::int64_t>(s), s, 1,
                                   side, px, qty, oid, 0));
    };
    // Venue 1 asks 101; venue 2 asks 100 (better) -> buy routes to 2.
    add(1, 1, 101, 500, 11);
    add(2, 1, 100, 500, 21);
    EXPECT_EQ(sor.route_aggressive(book, 0, {1, 2}), 2);
    // Equal best ask: tie broken by lower taker fee (venue 2 at 0.001).
    add(2, 1, 101, 100, 22);
    add(1, 1, 100, 100, 12);  // now both quote 100
    EXPECT_EQ(sor.route_aggressive(book, 0, {1, 2}), 2);
    // A sell routes to the best (highest) bid.
    add(1, 0, 99, 500, 13);
    add(2, 0, 98, 500, 23);
    EXPECT_EQ(sor.route_aggressive(book, 1, {1, 2}), 1);
}

TEST(SorRouting, PassivePrefersRebateAndFallsBack) {
    const auto cfg = algo_config();
    iap::SmartOrderRouter sor(cfg.venues);
    iap::ConsolidatedBook book(7);
    // No venue quotes anything: fallback = lowest candidate id.
    EXPECT_EQ(sor.route_passive(book, 0, {2, 1}), 1);
    EXPECT_THROW(sor.route_passive(book, 0, {}), std::invalid_argument);
    // Both venues quote the bid side: venue 2 pays the higher rebate.
    book.apply(MarketEvent::of(1, 7, 1, T0, T0, 1, 1, 0, 99, 100, 11, 0));
    book.apply(MarketEvent::of(2, 7, 2, T0 + 1, T0 + 1, 1, 1, 0, 99, 100, 21,
                               0));
    EXPECT_EQ(sor.route_passive(book, 0, {1, 2}), 2);
    // Only venue 1 quotes the ask side.
    book.apply(MarketEvent::of(3, 7, 1, T0 + 2, T0 + 2, 2, 1, 1, 101, 100, 12,
                               0));
    EXPECT_EQ(sor.route_passive(book, 1, {1, 2}), 1);
}

TEST(AlgoReplay, AccountingIdentityAndDeterminism) {
    auto p1 = parent(AlgoType::VWAP, 300, 3);
    auto p2 = parent(AlgoType::IS, 200, 2);
    p2.parent_id = 2;
    p2.side = 1;
    const auto events = algo_stream();
    auto run = [&]() {
        iap::ExecutionReplay replay(algo_config(), {p1, p2});
        return replay.run(events);
    };
    const auto a = run();
    const auto b = run();
    // Determinism: identical fills, bit for bit.
    ASSERT_EQ(a.fills.size(), b.fills.size());
    for (std::size_t i = 0; i < a.fills.size(); ++i) {
        EXPECT_EQ(a.fills[i].ts, b.fills[i].ts);
        EXPECT_EQ(a.fills[i].qty, b.fills[i].qty);
        EXPECT_EQ(a.fills[i].price_ticks, b.fills[i].price_ticks);
        EXPECT_EQ(a.fills[i].fee, b.fills[i].fee);
    }
    // Accounting identity per parent: total_cost = fees - rebates + impact,
    // and the components re-derive exactly from the fill list.
    for (const auto& [pid, rep] : a.parents) {
        double fees = 0.0, rebates = 0.0, impact = 0.0, notional = 0.0;
        std::int64_t qty = 0;
        for (const auto& f : a.fills) {
            if (f.parent_id != pid) continue;
            if (f.fee >= 0.0) {
                fees += f.fee;
            } else {
                rebates += -f.fee;
            }
            impact += f.impact_cost;
            notional += static_cast<double>(f.qty) *
                        static_cast<double>(f.price_ticks) * 0.01;
            qty += f.qty;
        }
        EXPECT_EQ(rep.filled_qty, qty);
        EXPECT_DOUBLE_EQ(rep.fees, fees);
        EXPECT_DOUBLE_EQ(rep.rebates, rebates);
        EXPECT_DOUBLE_EQ(rep.impact, impact);
        EXPECT_DOUBLE_EQ(rep.notional, notional);
        EXPECT_DOUBLE_EQ(rep.total_cost, fees - rebates + impact);
        EXPECT_EQ(rep.filled_qty + rep.unfilled_qty,
                  pid == 1 ? p1.qty : p2.qty);
    }
}

}  // namespace
