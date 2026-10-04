// Execution policies NATIVE / AGGRESSIVE / PASSIVE (algos.hpp, "EXECUTION
// POLICIES"): the pure posting rules, the POST -> REST -> REPRICE / CROSS
// state machine on synthetic streams, and the policy golden
// tests/golden/expected_replay_fills_passive.json.
//
// The golden scenario (tools/replay_fills_golden.hpp): events_eq_mbo.jsonl
// worked by parent 1 = VWAP BUY 400 PASSIVE (urgency 0.5), parent 2 = IS
// SELL 600 PASSIVE (urgency 0.2, risk_aversion 1.0), parent 3 = POV BUY 300
// PASSIVE (urgency 0.0, participation 0.1, SOR-routed) and parent 4 = TWAP
// SELL 200 AGGRESSIVE. The committed file is written by
// python/tools/make_golden_replay_passive.py; GeneratorReproducesBothFiles
// proves the C++ writer produces the same bytes (and the NATIVE golden's).

#include <gtest/gtest.h>

#include <cmath>
#include <cstdint>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

#include "../tools/replay_fills_golden.hpp"
#include "golden_util.hpp"
#include "iap/marketdata/codec.hpp"
#include "iap/replay/exec_replay.hpp"

namespace {

using iap::AlgoType;
using iap::ChildOrder;
using iap::ExecPolicy;
using iap::MarketEvent;
using iap::OrderState;
using iap::OrderType;
using iap::ParentOrder;
using iap::PassiveParams;

constexpr std::int64_t T0 = 1'700'000'000'000'000'000;
constexpr std::int64_t SEC = 1'000'000'000;

std::optional<iap::LevelEntry> lvl(std::int64_t price) {
    return iap::LevelEntry{price, 500};
}

// ------------------------------------------------------------ pure rules --

TEST(PassivePolicy, PostPriceJoinsImprovesAndNeverReachesTheOppositeTouch) {
    EXPECT_EQ(iap::post_price(0, lvl(100), lvl(101), 3).value(), 100);
    EXPECT_EQ(iap::post_price(1, lvl(100), lvl(101), 3).value(), 101);
    EXPECT_EQ(iap::post_price(0, lvl(100), lvl(102), 3).value(), 100);
    EXPECT_EQ(iap::post_price(0, lvl(100), lvl(103), 3).value(), 101);
    EXPECT_EQ(iap::post_price(1, lvl(100), lvl(103), 3).value(), 102);
    EXPECT_EQ(iap::post_price(0, lvl(100), lvl(110), 3).value(), 101);
    EXPECT_EQ(iap::post_price(0, lvl(100), lvl(110), 0).value(), 100);
    EXPECT_EQ(iap::post_price(0, lvl(100), lvl(102), 2).value(), 101);
    // No same-side quote: cannot post; a missing far side is tolerated.
    EXPECT_FALSE(iap::post_price(0, std::nullopt, lvl(101), 3).has_value());
    EXPECT_FALSE(iap::post_price(1, lvl(100), std::nullopt, 3).has_value());
    EXPECT_EQ(iap::post_price(0, lvl(100), std::nullopt, 3).value(), 100);
    EXPECT_EQ(iap::post_price(1, std::nullopt, lvl(101), 3).value(), 101);
    EXPECT_FALSE(iap::post_price(0, lvl(1), lvl(1), 3).has_value());
    for (std::int64_t bid = 95; bid <= 105; ++bid) {
        for (std::int64_t ask = 95; ask <= 105; ++ask) {
            for (std::int64_t improve = 0; improve <= 3; ++improve) {
                const auto buy = iap::post_price(0, lvl(bid), lvl(ask), improve);
                const auto sell = iap::post_price(1, lvl(bid), lvl(ask), improve);
                ASSERT_TRUE(buy.has_value());
                ASSERT_TRUE(sell.has_value());
                EXPECT_LT(*buy, ask);
                EXPECT_GT(*sell, bid);
                if (ask > bid) {  // an uncrossed book is never posted behind
                    EXPECT_GE(*buy, bid);
                    EXPECT_LE(*sell, ask);
                }
            }
        }
    }
}

TEST(PassivePolicy, PatienceFollowsUrgencyAndTheIsRiskAversion) {
    const PassiveParams p;
    EXPECT_EQ(iap::patience_ns(p, 0.0, false, 1.0), 30 * SEC);
    EXPECT_EQ(iap::patience_ns(p, 0.5, false, 1.0), 15 * SEC);
    EXPECT_EQ(iap::patience_ns(p, 1.0, false, 1.0), 0);
    EXPECT_EQ(iap::patience_ns(p, 7.0, false, 1.0), 0);           // clamped
    EXPECT_EQ(iap::patience_ns(p, -3.0, false, 1.0), 30 * SEC);   // clamped
    EXPECT_EQ(iap::patience_ns(p, 0.2, true, 1.0), 8'829'106'588);
    EXPECT_EQ(iap::patience_ns(p, 0.2, true, 0.0), 24 * SEC);
    EXPECT_EQ(iap::max_behind_qty(p, 405), 40);
}

TEST(PassivePolicy, ParamsAreValidated) {
    PassiveParams a;
    a.max_rest_ns = -1;
    PassiveParams b;
    b.end_margin_ns = -1;
    PassiveParams c;
    c.max_reprices = -1;
    PassiveParams d;
    d.max_behind_fraction = 1.5;
    PassiveParams e;
    e.improve_min_spread_ticks = -1;
    for (const PassiveParams& bad : {a, b, c, d, e}) {
        EXPECT_THROW(iap::validate_passive_params(bad), std::invalid_argument);
    }
    EXPECT_NO_THROW(iap::validate_passive_params(PassiveParams{}));
}

// --------------------------------------------------------- replay-driven --

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
    v2.taker_fee_per_share = 0.001;
    v2.maker_rebate_per_share = 0.0025;
    cfg.venues[2] = v2;
    iap::InstrumentSpec ins;
    ins.instrument_id = 7;
    ins.tick_size = 0.01;
    ins.qty_unit = 1.0;
    ins.adv = 1'000'000.0;
    cfg.instruments[7] = ins;
    return cfg;
}

ParentOrder parent(AlgoType algo, std::int64_t qty, int slices,
                   ExecPolicy policy, double urgency,
                   const PassiveParams& params = PassiveParams{}) {
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
    p.policy = policy;
    p.urgency = urgency;
    p.passive = params;
    return p;
}

PassiveParams params_with_rest(std::int64_t max_rest_ns,
                               double max_behind_fraction = 0.1) {
    PassiveParams p;
    p.max_rest_ns = max_rest_ns;
    p.max_behind_fraction = max_behind_fraction;
    return p;
}

// Venue-1 book bid x ask (100,000 a side) and one event per half second for
// 200 s. hit_bid_every > 0 adds a marketable ASK ADD of hit_qty at the bid
// every that many seconds (the replayed book matches it: a trade rule 4
// tracks); bid_move_at >= 0 adds a better bid one tick up at that second.
std::vector<MarketEvent> stream(std::int64_t bid, std::int64_t ask,
                                int hit_bid_every, std::int64_t hit_qty,
                                int bid_move_at) {
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
    push(T0 - SEC, 1, 0, bid, 100000, 1, 0);
    push(T0 - SEC + 1, 1, 1, ask, 100000, 2, 0);
    for (int i = 0; i < 200; ++i) {
        const std::int64_t ts = T0 + i * SEC;
        push(ts, 5, 0, bid, 40, 0, 1000 + i);  // TRADE 40
        if (hit_bid_every > 0 && i % hit_bid_every == hit_bid_every - 1) {
            push(ts + SEC / 4, 1, 1, bid, hit_qty, 5000 + i, 0);
        }
        if (i == bid_move_at) {
            push(ts + SEC / 4, 1, 0, bid + 1, 500, 9000, 0);
        }
        push(ts + SEC / 2, 9, 0, 0, 0, 0, 0);  // HEARTBEAT
    }
    return evs;
}

std::vector<ChildOrder> children(const iap::ExecutionReplay& replay) {
    std::vector<ChildOrder> out;
    for (const auto& [id, o] : replay.simulator().orders()) {
        (void)id;
        out.push_back(o);
    }
    return out;
}

void expect_stats(const iap::PassiveStats& s, std::int64_t posts,
                  std::int64_t reprices, std::int64_t extensions,
                  std::int64_t timeout, std::int64_t behind,
                  std::int64_t immediate) {
    EXPECT_EQ(s.posts, posts);
    EXPECT_EQ(s.reprices, reprices);
    EXPECT_EQ(s.rest_extensions, extensions);
    EXPECT_EQ(s.crosses_timeout, timeout);
    EXPECT_EQ(s.crosses_behind, behind);
    EXPECT_EQ(s.crosses_immediate, immediate);
}

TEST(PassivePolicy, NativeIsTheDefaultAndAggressiveSendsMarketChildren) {
    EXPECT_EQ(ParentOrder{}.policy, ExecPolicy::NATIVE);
    iap::ExecutionReplay nat(
        algo_config(), {parent(AlgoType::TWAP, 400, 4, ExecPolicy::NATIVE, 0.9)});
    const auto nres = nat.run(stream(100, 101, 3, 50, -1));
    EXPECT_TRUE(nres.passive.empty());
    for (const auto& o : children(nat)) {
        EXPECT_EQ(o.type, OrderType::LIMIT);  // joins the bid and waits
    }
    iap::ExecutionReplay agg(
        algo_config(),
        {parent(AlgoType::TWAP, 400, 4, ExecPolicy::AGGRESSIVE, 0.5)});
    const auto ares = agg.run(stream(100, 101, 0, 0, -1));
    EXPECT_EQ(ares.parents.at(1).filled_qty, 400);
    for (const auto& o : children(agg)) EXPECT_EQ(o.type, OrderType::MARKET);
    for (const auto& f : ares.fills) {
        EXPECT_EQ(f.liquidity, iap::Liquidity::TAKER);
        EXPECT_EQ(f.price_ticks, 101);
        EXPECT_DOUBLE_EQ(f.fee, 0.003 * static_cast<double>(f.qty));  // a cost
        EXPECT_GT(f.impact_cost, 0.0);
    }
}

TEST(PassivePolicy, RestExtensionThenCrossWhenTheQueueNeverClears) {
    // Spread of one tick: post AT the bid behind 100,000 displayed; the tape
    // never reaches us. Patience 10 s (urgency 0.5 of 20 s), one reprice.
    iap::ExecutionReplay replay(
        algo_config(), {parent(AlgoType::TWAP, 400, 4, ExecPolicy::PASSIVE, 0.5,
                               params_with_rest(20 * SEC))});
    const auto res = replay.run(stream(100, 101, 0, 0, -1));
    int posted = 0;
    int crossed = 0;
    for (const auto& o : children(replay)) {
        if (o.type == OrderType::LIMIT) {
            ++posted;
            EXPECT_EQ(o.limit_ticks, 100);  // the near touch, not through 101
            EXPECT_EQ(o.entry_ahead_qty, 100000);
            EXPECT_EQ(o.state, OrderState::CANCELLED);
            EXPECT_EQ(o.cancel_reason, iap::CancelReason::USER);
            EXPECT_EQ(o.remaining, 100);
        } else {
            // POST at the slice, extension at +10 s (price unchanged: queue
            // position kept), cancel at +20 s, MARKET once it took effect.
            const std::int64_t due = T0 + crossed * 25 * SEC;
            EXPECT_GE(o.decision_ts, due + 20 * SEC);
            EXPECT_LE(o.decision_ts, due + 21 * SEC);
            ++crossed;
        }
    }
    EXPECT_EQ(posted, 4);
    EXPECT_EQ(crossed, 4);
    expect_stats(res.passive.at(1), 4, 0, 4, 4, 0, 0);
    EXPECT_EQ(res.parents.at(1).filled_qty, 400);
    for (const auto& f : res.fills) {
        EXPECT_EQ(f.liquidity, iap::Liquidity::TAKER);
        EXPECT_EQ(f.price_ticks, 101);
    }
}

TEST(PassivePolicy, PostsInsideAWideSpreadAndEarnsTheRebate) {
    // Spread 4 ticks: post one tick inside (101), nothing ahead of us; every
    // marketable sell trades through 101 and fills us there.
    iap::ExecutionReplay replay(
        algo_config(), {parent(AlgoType::TWAP, 400, 4, ExecPolicy::PASSIVE, 0.0)});
    const auto res = replay.run(stream(100, 104, 2, 60, -1));
    const auto kids = children(replay);
    ASSERT_EQ(kids.size(), 4u);
    for (const auto& o : kids) {
        EXPECT_EQ(o.type, OrderType::LIMIT);
        EXPECT_EQ(o.limit_ticks, 101);
        EXPECT_EQ(o.entry_ahead_qty, 0);
    }
    const auto& rep = res.parents.at(1);
    EXPECT_EQ(rep.filled_qty, 400);
    for (const auto& f : res.fills) {
        EXPECT_EQ(f.liquidity, iap::Liquidity::MAKER);
        EXPECT_EQ(f.price_ticks, 101);  // our limit, never better
        EXPECT_LE(f.qty, 60);           // bounded by the traded volume (rule 4)
        EXPECT_DOUBLE_EQ(f.fee, -0.002 * static_cast<double>(f.qty));  // rebate
        EXPECT_LT(f.fee, 0.0);
        EXPECT_EQ(f.impact_cost, 0.0);
    }
    EXPECT_NEAR(rep.rebates, 0.002 * 400, 1e-12);
    EXPECT_EQ(rep.fees, 0.0);
    EXPECT_EQ(res.passive.at(1).crosses_timeout, 0);
    EXPECT_EQ(res.passive.at(1).crosses_behind, 0);
}

TEST(PassivePolicy, RepricesWhenTheTouchMovesAway) {
    // A better bid appears 5 s after the first post: at the 10 s deadline the
    // post price is 101, not our 100 -> cancel, re-post at 101.
    iap::ExecutionReplay replay(
        algo_config(), {parent(AlgoType::TWAP, 100, 1, ExecPolicy::PASSIVE, 0.5,
                               params_with_rest(20 * SEC))});
    const auto res = replay.run(stream(100, 102, 0, 0, 5));
    const auto kids = children(replay);
    ASSERT_EQ(kids.size(), 3u);
    EXPECT_EQ(kids[0].type, OrderType::LIMIT);
    EXPECT_EQ(kids[0].limit_ticks, 100);
    EXPECT_EQ(kids[0].cancel_reason, iap::CancelReason::USER);
    EXPECT_EQ(kids[1].type, OrderType::LIMIT);
    EXPECT_EQ(kids[1].limit_ticks, 101);
    EXPECT_EQ(kids[1].qty, 100);  // the cancelled remainder, not more
    EXPECT_GT(kids[1].decision_ts, kids[0].decision_ts + 10 * SEC);
    EXPECT_EQ(kids[2].type, OrderType::MARKET);
    expect_stats(res.passive.at(1), 2, 1, 0, 1, 0, 0);
    EXPECT_EQ(res.parents.at(1).filled_qty, 100);
}

TEST(PassivePolicy, CrossesAsSoonAsTheScheduleIsBehind) {
    // Patience 60 s exceeds the 25 s slice interval and nothing fills: when
    // slice 1 comes due the backlog is slice 0 (100 > floor(0.1 * 400)).
    iap::ExecutionReplay replay(
        algo_config(), {parent(AlgoType::TWAP, 400, 4, ExecPolicy::PASSIVE, 0.0,
                               params_with_rest(60 * SEC))});
    const auto res = replay.run(stream(100, 101, 0, 0, -1));
    std::int64_t first_cross_ts = 0;
    for (const auto& o : children(replay)) {
        if (o.type == OrderType::MARKET) {
            first_cross_ts = o.decision_ts;
            break;
        }
    }
    EXPECT_GE(first_cross_ts, T0 + 25 * SEC);
    EXPECT_LT(first_cross_ts, T0 + 26 * SEC);
    EXPECT_GE(res.passive.at(1).crosses_behind, 3);
    EXPECT_EQ(res.passive.at(1).rest_extensions, 0);
    // A tolerance of the whole order never declares the schedule behind.
    iap::ExecutionReplay lax(
        algo_config(), {parent(AlgoType::TWAP, 400, 4, ExecPolicy::PASSIVE, 0.0,
                               params_with_rest(60 * SEC, 1.0))});
    const auto res2 = lax.run(stream(100, 101, 0, 0, -1));
    EXPECT_EQ(res2.passive.at(1).crosses_behind, 0);
}

TEST(PassivePolicy, UrgencyOneCrossesImmediatelyAndQuantityIsConserved) {
    iap::ExecutionReplay replay(
        algo_config(), {parent(AlgoType::TWAP, 400, 4, ExecPolicy::PASSIVE, 1.0)});
    const auto res = replay.run(stream(100, 101, 0, 0, -1));
    for (const auto& o : children(replay)) EXPECT_EQ(o.type, OrderType::MARKET);
    expect_stats(res.passive.at(1), 0, 0, 0, 0, 0, 4);

    const std::vector<AlgoType> algos{AlgoType::TWAP, AlgoType::VWAP,
                                      AlgoType::IS, AlgoType::POV};
    for (const double u : {0.0, 0.3, 0.7, 1.0}) {
        for (const double fr : {0.0, 0.1, 1.0}) {
            std::vector<ParentOrder> parents;
            for (std::size_t i = 0; i < algos.size(); ++i) {
                ParentOrder p = parent(algos[i], 300, 3, ExecPolicy::PASSIVE, u,
                                       params_with_rest(40 * SEC, fr));
                p.parent_id = i + 1;
                p.side = static_cast<std::uint8_t>(i % 2);
                p.participation = 0.2;
                parents.push_back(p);
            }
            iap::ExecutionReplay rp(algo_config(), parents);
            const auto r = rp.run(stream(100, 103, 3, 50, 7));
            for (const auto& p : parents) {
                const auto& rep = r.parents.at(p.parent_id);
                EXPECT_GE(rep.filled_qty, 0);
                EXPECT_LE(rep.filled_qty, p.qty);
                EXPECT_EQ(rep.filled_qty + rep.unfilled_qty, p.qty);
                EXPECT_NEAR(rep.total_cost, rep.fees - rep.rebates + rep.impact,
                            1e-12);
            }
            for (const auto& f : r.fills) {
                EXPECT_EQ(f.liquidity == iap::Liquidity::MAKER, f.fee < 0.0);
                EXPECT_GE(f.ts, T0);
                EXPECT_LE(f.ts, T0 + 100 * SEC);
            }
            for (const auto& o : children(rp)) {
                EXPECT_TRUE(o.state == OrderState::FILLED ||
                            o.state == OrderState::CANCELLED);
            }
        }
    }
}

// ----------------------------------------------------------------- golden --

std::string configs_dir() { return iap_test::golden_dir() + "/../../configs"; }

iap::ExecReplayResult run_policy_scenario() {
    const auto events =
        iap::read_jsonl(iap_test::golden_path("events_eq_mbo.jsonl"));
    iap::ExecutionReplay replay(
        iap_golden::golden_exec_config(configs_dir()),
        iap_golden::passive_parents(events.front().exchange_ts));
    return replay.run(events);
}

TEST(ReplayFillsPassiveGolden, ExactFillListAndTransitionCountersMatch) {
    const auto golden =
        iap_test::load_golden_json("expected_replay_fills_passive.json");
    EXPECT_EQ(golden["x-version"].i64(), 1);
    const auto res = run_policy_scenario();
    EXPECT_EQ(res.events_processed,
              static_cast<std::uint64_t>(golden["events_processed"].i64()));
    const auto& fills = golden["fills"].a();
    ASSERT_EQ(res.fills.size(), fills.size());
    for (std::size_t i = 0; i < fills.size(); ++i) {
        const auto& want = fills[i];
        const auto& got = res.fills[i];
        const std::string what = "fill " + std::to_string(i + 1);
        EXPECT_EQ(got.fill_id, want["fill_id"].u64()) << what;
        EXPECT_EQ(got.order_id, want["order_id"].u64()) << what;
        EXPECT_EQ(got.parent_id, want["parent_id"].u64()) << what;
        EXPECT_EQ(got.instrument_id, want["instrument_id"].u64()) << what;
        EXPECT_EQ(got.venue_id, want["venue_id"].u64()) << what;
        EXPECT_EQ(got.side, want["side"].u64()) << what;
        EXPECT_EQ(got.price_ticks, want["price_ticks"].i64()) << what;  // exact
        EXPECT_EQ(got.qty, want["qty"].i64()) << what;                  // exact
        EXPECT_EQ(got.ts, want["ts"].i64()) << what;                    // exact
        EXPECT_EQ(got.liquidity == iap::Liquidity::MAKER ? "MAKER" : "TAKER",
                  want["liquidity"].s())
            << what;
        EXPECT_NEAR(got.fee, want["fee"].num(), 1e-9) << what;
        EXPECT_NEAR(got.impact_cost, want["impact_cost"].num(), 1e-9) << what;
    }
    ASSERT_EQ(res.parents.size(), 4u);
    for (const char* pid : {"1", "2", "3", "4"}) {
        const auto& want = golden["parents"][pid];
        const auto& got = res.parents.at(std::stoull(pid));
        EXPECT_EQ(got.filled_qty, want["filled_qty"].i64());
        EXPECT_EQ(got.unfilled_qty, want["unfilled_qty"].i64());
        EXPECT_EQ(got.children, want["children"].i64());
        EXPECT_NEAR(got.notional, want["notional"].num(), 1e-9);
        EXPECT_NEAR(got.avg_price, want["avg_price"].num(), 1e-9);
        EXPECT_NEAR(got.fees, want["fees"].num(), 1e-9);
        EXPECT_NEAR(got.rebates, want["rebates"].num(), 1e-9);
        EXPECT_NEAR(got.impact, want["impact"].num(), 1e-9);
        EXPECT_NEAR(got.total_cost, want["total_cost"].num(), 1e-9);
        EXPECT_NEAR(got.total_cost, got.fees - got.rebates + got.impact, 1e-12);
    }
    ASSERT_EQ(res.passive.size(), 3u);
    for (const char* pid : {"1", "2", "3"}) {
        const auto& want = golden["passive"][pid];
        const auto& got = res.passive.at(std::stoull(pid));
        EXPECT_EQ(got.posts, want["posts"].i64()) << pid;
        EXPECT_EQ(got.reprices, want["reprices"].i64()) << pid;
        EXPECT_EQ(got.rest_extensions, want["rest_extensions"].i64()) << pid;
        EXPECT_EQ(got.crosses_timeout, want["crosses_timeout"].i64()) << pid;
        EXPECT_EQ(got.crosses_behind, want["crosses_behind"].i64()) << pid;
        EXPECT_EQ(got.crosses_immediate, want["crosses_immediate"].i64()) << pid;
    }
}

TEST(ReplayFillsPassiveGolden, GeneratorReproducesBothFilesByteForByte) {
    const auto events =
        iap::read_jsonl(iap_test::golden_path("events_eq_mbo.jsonl"));
    const std::int64_t t0 = events.front().exchange_ts;
    // The policy golden (committed from the Python generator).
    const auto policy = run_policy_scenario();
    EXPECT_EQ(iap_golden::render_replay_fills(
                  policy, t0, 1, iap_golden::kPassiveDescription, true),
              iap_test::read_text_file(
                  iap_test::golden_path("expected_replay_fills_passive.json")));
    // The NATIVE golden: adding the policies moved no byte of it.
    iap::ExecutionReplay native(iap_golden::golden_exec_config(configs_dir()),
                                iap_golden::native_parents(t0));
    const auto nat = native.run(events);
    EXPECT_TRUE(nat.passive.empty());
    EXPECT_EQ(iap_golden::render_replay_fills(
                  nat, t0, 2, iap_golden::kNativeDescription, false),
              iap_test::read_text_file(
                  iap_test::golden_path("expected_replay_fills.json")));
}

TEST(ReplayFillsPassiveGolden, FeeSignsFollowTheLiquidityFlagAndNoParentOverfills) {
    const auto res = run_policy_scenario();
    std::int64_t maker_qty = 0;
    std::int64_t taker_qty = 0;
    for (const auto& f : res.fills) {
        if (f.liquidity == iap::Liquidity::MAKER) {
            EXPECT_LT(f.fee, 0.0);          // rebate received
            EXPECT_EQ(f.impact_cost, 0.0);
            EXPECT_NE(f.parent_id, 4u);     // AGGRESSIVE never rests
            maker_qty += f.qty;
        } else {
            EXPECT_GT(f.fee, 0.0);          // taker fee paid
            EXPECT_GT(f.impact_cost, 0.0);
            taker_qty += f.qty;
        }
    }
    EXPECT_GT(maker_qty, 0);
    EXPECT_GT(taker_qty, 0);
    const std::int64_t qty[] = {0, 400, 600, 300, 200};
    for (std::uint64_t pid = 1; pid <= 4; ++pid) {
        const auto& rep = res.parents.at(pid);
        EXPECT_EQ(rep.filled_qty + rep.unfilled_qty, qty[pid]);
        EXPECT_LE(rep.filled_qty, qty[pid]);
    }
}

TEST(ReplayFillsPassiveGolden, DeterministicRerun) {
    const auto a = run_policy_scenario();
    const auto b = run_policy_scenario();
    ASSERT_EQ(a.fills.size(), b.fills.size());
    for (std::size_t i = 0; i < a.fills.size(); ++i) {
        EXPECT_EQ(a.fills[i].ts, b.fills[i].ts);
        EXPECT_EQ(a.fills[i].price_ticks, b.fills[i].price_ticks);
        EXPECT_EQ(a.fills[i].qty, b.fills[i].qty);
        EXPECT_EQ(a.fills[i].fee, b.fills[i].fee);
        EXPECT_EQ(a.fills[i].impact_cost, b.fills[i].impact_cost);
    }
}

}  // namespace
