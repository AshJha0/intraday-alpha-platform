// Golden execution-replay fills (tests/golden/expected_replay_fills.json).
//
// The pinned scenario (this C++ port is the reference that generated the
// file via cpp/tools/make_replay_fills_golden.cpp): events_eq_mbo.jsonl
// (instrument 1, venue XV1, tick 0.01) worked by
//   parent 1: VWAP BUY 400 over [t0+60s, t0+660s), 4 passive LIMIT slices
//             joining the best bid  -> slice quantities {129, 71, 71, 129}
//             (U-curve weights {2, 10/9, 10/9, 2}, largest remainder);
//   parent 2: IS SELL 600 over [t0+120s, t0+720s), 3 MARKET slices,
//             risk_aversion 1.0    -> quantities {304, 184, 112}
//             (weights {1, e^-0.5, e^-1}).
//
// HAND TRACE of the first two fills (verified against the raw event vector;
// t0 = 1787578200000000000, latency = 50+50+100 us internal + 150 us venue
// mean + SplitMix64(20260829) jitter draws, in submission order:
// {21675, 14614, 24600, 13721, 18924, 15550, 8663} ns; order 1 = VWAP
// slice 0 is submitted first, so it takes jitter draw #1 even though the
// IS market order fills earlier):
//
// Fill 1 — IS slice 0 (order 2, SELL 304):
//   * due t0+120s; the first event at/after that is event 74
//     (ts 1787578329585937884) => decision_ts = 1787578329585937884;
//   * arrival = decision + 350000 + 14614 = 1787578329586302498 (jitter
//     draw #2 — the VWAP slice was submitted first and took draw #1);
//   * activated while processing event 75; the pre-event displayed bids
//     were (2451 x 600, 2449 x 1400, ...) so the MARKET sell walks one
//     level: 304 @ 2451, stamped with the ARRIVAL timestamp (aggressive
//     fills, pinned rule 2). Taker fee 0.003 * 304 = 0.912; linear impact
//     = 2.0 bps/%ADV * (304/38e6*100)% * 1e-4 * (304*24.51) =
//     0.0011921664.  == golden fill 1.
//
// Fill 2 — VWAP slice 0 (order 1, BUY 129):
//   * due t0+60s; the first event at/after that is event 48
//     (ts 1787578263509538609) => decision_ts = 1787578263509538609;
//   * arrival = decision + 350000 + 21675 = 1787578263509910284 (draw #1);
//   * at decision the venue-1 book showed best bid 2449 x 1100 => passive
//     LIMIT join at 2449; activation happens while processing event 52
//     (first event at/after arrival), which shows 1100 displayed at 2449
//     => ahead_qty = 1100;
//   * the displayed 2449 liquidity then drains WITHOUT ever leaving
//     leftover EXECUTE volume for us: EXECUTEs at 2449 (events 52, 57, 59,
//     77: 300+100+100+200) and the marketable ASK ADD event 63 (limit
//     2449, consumes 100 @ 2449) take ahead 1100 -> 300; CANCEL event 88
//     (300 @ 2449) takes it to exactly 0, and CANCELs 89-91 empty the
//     level (cancels never fill us). Display best bid falls to 2448;
//   * event 98 (ts 1787578386181246977) is a marketable ASK ADD (limit
//     2448, qty 100): its expansion consumes 100 @ 2448 — strictly BELOW
//     our 2449 level, so the market traded THROUGH us and the pinned
//     trade-through rule fills the full 129 at OUR price 2449, stamped
//     with event 98's exchange_ts. Maker rebate 0.002/share => fee
//     -0.258.  == golden fill 2.

#include <gtest/gtest.h>

#include <cmath>
#include <string>
#include <vector>

#include "golden_util.hpp"
#include "iap/marketdata/codec.hpp"
#include "iap/marketdata/rng.hpp"
#include "iap/replay/exec_replay.hpp"

namespace {

constexpr std::int64_t SEC = 1'000'000'000;

iap::ExecConfig golden_config() {
    iap::ExecConfig cfg;
    cfg.seed = 20260829;
    cfg.impact_coeff_bps_per_pct_adv = 2.0;
    cfg.venues =
        iap::load_venues(iap_test::golden_dir() + "/../../configs/venues.json");
    iap::InstrumentSpec ins;
    ins.instrument_id = 1;
    ins.tick_size = 0.01;
    ins.lot_size = 1.0;
    ins.adv = 38000000.0;
    cfg.instruments[1] = ins;
    return cfg;
}

std::vector<iap::ParentOrder> golden_parents(std::int64_t t0) {
    iap::ParentOrder vwap;
    vwap.parent_id = 1;
    vwap.instrument_id = 1;
    vwap.venue_id = 1;
    vwap.side = 0;
    vwap.qty = 400;
    vwap.algo = iap::AlgoType::VWAP;
    vwap.start_ts = t0 + 60 * SEC;
    vwap.end_ts = t0 + 660 * SEC;
    vwap.slices = 4;
    iap::ParentOrder is;
    is.parent_id = 2;
    is.instrument_id = 1;
    is.venue_id = 1;
    is.side = 1;
    is.qty = 600;
    is.algo = iap::AlgoType::IS;
    is.start_ts = t0 + 120 * SEC;
    is.end_ts = t0 + 720 * SEC;
    is.slices = 3;
    is.risk_aversion = 1.0;
    return {vwap, is};
}

iap::ExecReplayResult run_golden_scenario() {
    const auto events = iap::read_jsonl(
        iap_test::golden_path("events_eq_mbo.jsonl"));
    iap::ExecutionReplay replay(golden_config(),
                                golden_parents(events.front().exchange_ts));
    return replay.run(events);
}

TEST(ReplayFillsGolden, ExactFillListMatches) {
    const auto golden =
        iap_test::load_golden_json("expected_replay_fills.json");
    const auto res = run_golden_scenario();
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
    // Parent reports.
    for (const char* pid : {"1", "2"}) {
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
        // Identity: total_cost = fees - rebates + impact (1e-9).
        EXPECT_NEAR(got.total_cost, got.fees - got.rebates + got.impact,
                    1e-12);
    }
}

TEST(ReplayFillsGolden, HandTracedFirstFills) {
    const auto res = run_golden_scenario();
    ASSERT_GE(res.fills.size(), 2u);

    // The jitter draws come from SplitMix64(20260829) in submission order
    // (order 1 = VWAP slice 0 was submitted first: draw #1).
    iap::SplitMix64 rng(20260829);
    const std::int64_t j1 = rng.below(50'000 + 1);
    const std::int64_t j2 = rng.below(50'000 + 1);
    EXPECT_EQ(j1, 21675);
    EXPECT_EQ(j2, 14614);

    // Fill 1: IS slice 0 — aggressive, stamped with its arrival_ts (see the
    // hand trace at the top of this file).
    const auto& f1 = res.fills[0];
    EXPECT_EQ(f1.order_id, 2u);
    EXPECT_EQ(f1.parent_id, 2u);
    EXPECT_EQ(f1.qty, 304);          // IS slice quantities {304, 184, 112}
    EXPECT_EQ(f1.price_ticks, 2451); // pre-event best bid 2451 x 600
    EXPECT_EQ(f1.liquidity, iap::Liquidity::TAKER);
    const std::int64_t decision2 = 1787578329585937884;  // event 74
    EXPECT_EQ(f1.ts, decision2 + 350'000 + j2);
    EXPECT_DOUBLE_EQ(f1.fee, 0.003 * 304);
    // Linear impact: 2.0 * (304/38e6*100) bps over notional 304*24.51.
    const double impact_bps = 2.0 * (304.0 / 38000000.0 * 100.0);
    EXPECT_NEAR(f1.impact_cost, impact_bps * 1e-4 * (304.0 * 2451 * 0.01),
                1e-15);

    // Fill 2: VWAP slice 0 — passive trade-through fill at our own level.
    const auto& f2 = res.fills[1];
    EXPECT_EQ(f2.order_id, 1u);
    EXPECT_EQ(f2.parent_id, 1u);
    EXPECT_EQ(f2.qty, 129);          // VWAP slice quantities {129,71,71,129}
    EXPECT_EQ(f2.price_ticks, 2449); // best bid at the slice-0 decision
    EXPECT_EQ(f2.liquidity, iap::Liquidity::MAKER);
    EXPECT_EQ(f2.ts, 1787578386181246977);  // event 98 (trade-through)
    EXPECT_DOUBLE_EQ(f2.fee, -0.002 * 129);
    // Decision at event 48 => arrival = decision + 350000 + j1.
    const std::int64_t decision1 = 1787578263509538609;
    EXPECT_EQ(decision1 + 350'000 + j1, 1787578263509910284);
    EXPECT_GT(f2.ts, decision1 + 350'000 + j1);  // fill after arrival
}

TEST(ReplayFillsGolden, DeterministicRerun) {
    const auto a = run_golden_scenario();
    const auto b = run_golden_scenario();
    ASSERT_EQ(a.fills.size(), b.fills.size());
    for (std::size_t i = 0; i < a.fills.size(); ++i) {
        EXPECT_EQ(a.fills[i].ts, b.fills[i].ts);
        EXPECT_EQ(a.fills[i].price_ticks, b.fills[i].price_ticks);
        EXPECT_EQ(a.fills[i].qty, b.fills[i].qty);
        EXPECT_EQ(a.fills[i].fee, b.fills[i].fee);
        EXPECT_EQ(a.fills[i].impact_cost, b.fills[i].impact_cost);
    }
}

TEST(ReplayFillsGolden, BothParentsCompleteWithMixedLiquidity) {
    const auto res = run_golden_scenario();
    // Scenario shape: parent 1 fills entirely passively (maker), parent 2
    // entirely aggressively (taker), both in full.
    std::int64_t maker_qty = 0, taker_qty = 0;
    for (const auto& f : res.fills) {
        if (f.liquidity == iap::Liquidity::MAKER) {
            EXPECT_EQ(f.parent_id, 1u);
            EXPECT_EQ(f.impact_cost, 0.0);  // passive fills: no impact
            maker_qty += f.qty;
        } else {
            EXPECT_EQ(f.parent_id, 2u);
            EXPECT_GT(f.impact_cost, 0.0);
            taker_qty += f.qty;
        }
    }
    EXPECT_EQ(maker_qty, 400);
    EXPECT_EQ(taker_qty, 600);
}

}  // namespace
