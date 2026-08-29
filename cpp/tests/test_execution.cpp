// Execution-simulator unit tests: queue-position rule, order-type
// semantics, partial fills, fee/rebate accounting, determinism and latency
// ordering (pinned rules: include/iap/execution/execution.hpp).

#include <gtest/gtest.h>

#include <cmath>
#include <vector>

#include "iap/execution/execution.hpp"
#include "iap/marketdata/events.hpp"

namespace {

using iap::ChildOrder;
using iap::ExecConfig;
using iap::ExecutionSimulator;
using iap::Liquidity;
using iap::MarketEvent;
using iap::OrderState;
using iap::OrderType;

constexpr std::int64_t T0 = 1'700'000'000'000'000'000;
constexpr std::uint32_t INS = 7;
constexpr std::uint16_t VEN = 1;

// Equity-style test config: one venue, zero latency jitter for exactness
// where a test wants it (jitter-specific tests build their own config).
ExecConfig test_config(std::int64_t jitter_ns = 0) {
    ExecConfig cfg;
    cfg.seed = 42;
    cfg.impact_coeff_bps_per_pct_adv = 2.0;
    iap::VenueSpec v;
    v.venue_id = VEN;
    v.name = "TST";
    v.is_fx = false;
    v.taker_fee_per_share = 0.003;
    v.maker_rebate_per_share = 0.002;
    v.latency_mean_ns = 150'000;
    v.latency_jitter_ns = jitter_ns;
    cfg.venues[VEN] = v;
    iap::InstrumentSpec ins;
    ins.instrument_id = INS;
    ins.tick_size = 0.01;
    ins.lot_size = 1.0;
    ins.adv = 1'000'000.0;
    cfg.instruments[INS] = ins;
    return cfg;
}

// Total internal + venue-mean latency of test_config (jitter 0).
constexpr std::int64_t LAT = 50'000 + 50'000 + 100'000 + 150'000;

struct EventFeeder {
    std::uint64_t seq = 0;
    MarketEvent ev(std::int64_t ts, std::uint8_t type, std::uint8_t side,
                   std::int64_t px, std::int64_t qty, std::uint64_t oid) {
        ++seq;
        return MarketEvent::of(seq, INS, VEN, ts, ts, seq, type, side, px, qty,
                               oid, 0);
    }
    MarketEvent add(std::int64_t ts, std::uint8_t side, std::int64_t px,
                    std::int64_t qty, std::uint64_t oid) {
        return ev(ts, 1, side, px, qty, oid);
    }
    MarketEvent cancel(std::int64_t ts, std::uint8_t side, std::int64_t px,
                       std::int64_t qty, std::uint64_t oid) {
        return ev(ts, 3, side, px, qty, oid);
    }
    MarketEvent exec(std::int64_t ts, std::uint8_t side, std::int64_t px,
                     std::int64_t qty, std::uint64_t oid) {
        return ev(ts, 4, side, px, qty, oid);
    }
    MarketEvent modify(std::int64_t ts, std::uint8_t side, std::int64_t px,
                       std::int64_t qty, std::uint64_t oid) {
        return ev(ts, 2, side, px, qty, oid);
    }
    MarketEvent heartbeat(std::int64_t ts) { return ev(ts, 9, 0, 0, 0, 0); }
};

// Seed a two-sided book: bids 100x300 (order 11), 99x400 (12); asks
// 101x200 (21), 102x500 (22).
void seed_book(ExecutionSimulator& sim, EventFeeder& f) {
    sim.on_event(f.add(T0, 0, 100, 300, 11));
    sim.on_event(f.add(T0 + 1, 0, 99, 400, 12));
    sim.on_event(f.add(T0 + 2, 1, 101, 200, 21));
    sim.on_event(f.add(T0 + 3, 1, 102, 500, 22));
}

ChildOrder child(std::uint8_t side, OrderType type, std::int64_t px,
                 std::int64_t qty, std::int64_t decision_ts) {
    ChildOrder c;
    c.parent_id = 99;
    c.instrument_id = INS;
    c.venue_id = VEN;
    c.side = side;
    c.type = type;
    c.limit_ticks = px;
    c.qty = qty;
    c.decision_ts = decision_ts;
    return c;
}

TEST(ExecQueue, EntryAheadEqualsDisplayedDepth) {
    ExecutionSimulator sim(test_config());
    EventFeeder f;
    seed_book(sim, f);
    // Passive buy joining the 100 bid (displayed 300 ahead of us).
    const auto id = sim.submit(child(0, OrderType::LIMIT, 100, 50, T0 + 10));
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1));  // activates the order
    const auto& o = sim.orders().at(id);
    EXPECT_EQ(o.state, OrderState::ACTIVE);
    EXPECT_TRUE(o.resting);
    EXPECT_EQ(o.ahead_qty, 300);
    EXPECT_TRUE(sim.fills().empty());
}

TEST(ExecQueue, ExecuteDepletesAheadThenFills) {
    ExecutionSimulator sim(test_config());
    EventFeeder f;
    seed_book(sim, f);
    const auto id = sim.submit(child(0, OrderType::LIMIT, 100, 50, T0 + 10));
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1));
    // EXECUTE 200 at our level: all ahead (300 -> 100), no fill yet.
    sim.on_event(f.exec(T0 + 2'000'000, 0, 100, 200, 11));
    EXPECT_EQ(sim.orders().at(id).ahead_qty, 100);
    EXPECT_TRUE(sim.fills().empty());
    // EXECUTE 130: 100 depletes the queue ahead, leftover 30 fills us.
    sim.on_event(f.exec(T0 + 3'000'000, 0, 100, 130, 12));
    ASSERT_EQ(sim.fills().size(), 1u);
    const auto& fill = sim.fills()[0];
    EXPECT_EQ(fill.qty, 30);
    EXPECT_EQ(fill.price_ticks, 100);
    EXPECT_EQ(fill.ts, T0 + 3'000'000);
    EXPECT_EQ(fill.liquidity, Liquidity::MAKER);
    EXPECT_EQ(sim.orders().at(id).remaining, 20);  // partial fill
    EXPECT_EQ(sim.orders().at(id).state, OrderState::ACTIVE);
    // Next EXECUTE fills the remainder (leftover capped at our remaining).
    sim.on_event(f.exec(T0 + 4'000'000, 0, 100, 500, 13));
    ASSERT_EQ(sim.fills().size(), 2u);
    EXPECT_EQ(sim.fills()[1].qty, 20);
    EXPECT_EQ(sim.orders().at(id).state, OrderState::FILLED);
}

TEST(ExecQueue, CancelAheadReducesPositionDeterministically) {
    ExecutionSimulator sim(test_config());
    EventFeeder f;
    seed_book(sim, f);
    const auto id = sim.submit(child(0, OrderType::LIMIT, 100, 50, T0 + 10));
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1));
    ASSERT_EQ(sim.orders().at(id).ahead_qty, 300);
    // Pinned rule: an observed CANCEL at our level reduces ahead by its
    // FULL qty (deterministic, no probabilistic split).
    sim.on_event(f.cancel(T0 + 2'000'000, 0, 100, 250, 11));
    EXPECT_EQ(sim.orders().at(id).ahead_qty, 50);
    // A cancel at another level does nothing.
    sim.on_event(f.cancel(T0 + 2'100'000, 0, 99, 400, 12));
    EXPECT_EQ(sim.orders().at(id).ahead_qty, 50);
    // MODIFY events never change queue position (pinned).
    sim.on_event(f.modify(T0 + 2'200'000, 0, 100, 10, 11));
    EXPECT_EQ(sim.orders().at(id).ahead_qty, 50);
    // Now a 60-EXECUTE: 50 ahead, 10 to us.
    sim.on_event(f.exec(T0 + 3'000'000, 0, 100, 60, 11));
    ASSERT_EQ(sim.fills().size(), 1u);
    EXPECT_EQ(sim.fills()[0].qty, 10);
}

TEST(ExecQueue, TradeThroughFillsInFullAtOurPrice) {
    ExecutionSimulator sim(test_config());
    EventFeeder f;
    seed_book(sim, f);
    const auto id = sim.submit(child(0, OrderType::LIMIT, 100, 50, T0 + 10));
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1));
    // EXECUTE on the bid side BELOW our price: the aggressor traded through
    // our level, so we must have filled first — full fill at OUR limit.
    sim.on_event(f.exec(T0 + 2'000'000, 0, 99, 100, 12));
    ASSERT_EQ(sim.fills().size(), 1u);
    EXPECT_EQ(sim.fills()[0].qty, 50);
    EXPECT_EQ(sim.fills()[0].price_ticks, 100);
    EXPECT_EQ(sim.fills()[0].liquidity, Liquidity::MAKER);
    EXPECT_EQ(sim.orders().at(id).state, OrderState::FILLED);
}

TEST(ExecQueue, MarketableAddConsumesQueueAhead) {
    ExecutionSimulator sim(test_config());
    EventFeeder f;
    seed_book(sim, f);
    const auto id = sim.submit(child(0, OrderType::LIMIT, 100, 50, T0 + 10));
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1));
    ASSERT_EQ(sim.orders().at(id).ahead_qty, 300);
    // A marketable sell ADD at 100 for 80 executes against the book with no
    // EXECUTE events; the expansion consumes 80 of the 300 ahead of us.
    sim.on_event(f.add(T0 + 2'000'000, 1, 100, 80, 23));
    EXPECT_TRUE(sim.fills().empty());
    EXPECT_EQ(sim.orders().at(id).ahead_qty, 220);
    // A deep marketable sell ADD (limit 99, qty 300): consumes the
    // remaining 220 displayed at 100 (queue ahead of us reaches 0), then
    // walks on to the 99 level — trading strictly through our 100 bid, so
    // our remaining 50 fills in full at 100.
    sim.on_event(f.add(T0 + 3'000'000, 1, 99, 300, 24));
    ASSERT_EQ(sim.fills().size(), 1u);
    EXPECT_EQ(sim.fills()[0].price_ticks, 100);
    EXPECT_EQ(sim.fills()[0].qty, 50);
    EXPECT_EQ(sim.fills()[0].liquidity, Liquidity::MAKER);
    EXPECT_EQ(sim.orders().at(id).state, OrderState::FILLED);
}

TEST(ExecQueue, MarketableAddTradingThroughFillsInFull) {
    ExecutionSimulator sim(test_config());
    EventFeeder f;
    seed_book(sim, f);
    // We bid 101 inside the spread (level not displayed): ahead_qty is 0.
    const auto id = sim.submit(child(0, OrderType::LIMIT, 101, 50, T0 + 10));
    // Cancel the displayed ask at 101 so the limit is not marketable at
    // activation and rests inside the spread.
    sim.on_event(f.cancel(T0 + 1'000, 1, 101, 200, 21));
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1));
    ASSERT_EQ(sim.orders().at(id).state, OrderState::ACTIVE);
    ASSERT_EQ(sim.orders().at(id).ahead_qty, 0);
    // Marketable sell ADD at 100 consumes the displayed 100-bid level —
    // strictly through our 101 bid, which must have filled first (in full).
    sim.on_event(f.add(T0 + 2'000'000, 1, 100, 120, 24));
    ASSERT_EQ(sim.fills().size(), 1u);
    EXPECT_EQ(sim.fills()[0].price_ticks, 101);
    EXPECT_EQ(sim.fills()[0].qty, 50);
    EXPECT_EQ(sim.orders().at(id).state, OrderState::FILLED);
}

TEST(ExecQueue, CrossingQuoteAfterL1ReplaceFills) {
    // FX-style: a QUOTE replaces the venue's L1; if the new opposite best
    // crosses our resting price, we fill at our limit (post-apply check).
    ExecutionSimulator sim(test_config());
    EventFeeder f;
    seed_book(sim, f);
    const auto id = sim.submit(child(0, OrderType::LIMIT, 100, 50, T0 + 10));
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1));
    ASSERT_EQ(sim.orders().at(id).state, OrderState::ACTIVE);
    // QUOTE: ask side replaced at 100 <= our bid 100 -> crossed.
    sim.on_event(f.ev(T0 + 2'000'000, 6, 1, 100, 250, 0));
    ASSERT_EQ(sim.fills().size(), 1u);
    EXPECT_EQ(sim.fills()[0].price_ticks, 100);
    EXPECT_EQ(sim.fills()[0].qty, 50);
    EXPECT_EQ(sim.orders().at(id).state, OrderState::FILLED);
}

TEST(ExecOrderTypes, MarketWalksDisplayedDepth) {
    ExecutionSimulator sim(test_config());
    EventFeeder f;
    seed_book(sim, f);
    // Market buy 250: 200 @ 101, then 50 @ 102 — one fill per level.
    const auto id = sim.submit(child(0, OrderType::MARKET, 0, 250, T0 + 10));
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1));
    ASSERT_EQ(sim.fills().size(), 2u);
    EXPECT_EQ(sim.fills()[0].price_ticks, 101);
    EXPECT_EQ(sim.fills()[0].qty, 200);
    EXPECT_EQ(sim.fills()[1].price_ticks, 102);
    EXPECT_EQ(sim.fills()[1].qty, 50);
    EXPECT_EQ(sim.fills()[0].liquidity, Liquidity::TAKER);
    // Aggressive fills are stamped with the order's arrival_ts.
    EXPECT_EQ(sim.fills()[0].ts, sim.orders().at(id).arrival_ts);
    EXPECT_EQ(sim.orders().at(id).state, OrderState::FILLED);
}

TEST(ExecOrderTypes, MarketPartialRemainderCancelled) {
    ExecutionSimulator sim(test_config());
    EventFeeder f;
    seed_book(sim, f);
    // Displayed ask depth is 700; a 1000 market buy part-fills and cancels.
    const auto id = sim.submit(child(0, OrderType::MARKET, 0, 1000, T0 + 10));
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1));
    ASSERT_EQ(sim.fills().size(), 2u);
    EXPECT_EQ(sim.fills()[0].qty + sim.fills()[1].qty, 700);
    EXPECT_EQ(sim.orders().at(id).state, OrderState::CANCELLED);
    EXPECT_EQ(sim.orders().at(id).remaining, 300);
}

TEST(ExecOrderTypes, MarketableLimitTakesThenRests) {
    ExecutionSimulator sim(test_config());
    EventFeeder f;
    seed_book(sim, f);
    // Buy limit 101 for 300: takes the 200 displayed at 101, remainder 100
    // rests at 101 with nothing ahead (we cleared the displayed level).
    const auto id = sim.submit(child(0, OrderType::LIMIT, 101, 300, T0 + 10));
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1));
    ASSERT_EQ(sim.fills().size(), 1u);
    EXPECT_EQ(sim.fills()[0].price_ticks, 101);
    EXPECT_EQ(sim.fills()[0].qty, 200);
    EXPECT_EQ(sim.fills()[0].liquidity, Liquidity::TAKER);
    const auto& o = sim.orders().at(id);
    EXPECT_EQ(o.state, OrderState::ACTIVE);
    EXPECT_EQ(o.remaining, 100);
    // Our remainder rests on the BID side at 101 (no displayed bid there):
    // nothing ahead of us at our own level.
    EXPECT_EQ(o.ahead_qty, 0);
    // The display still shows the ask liquidity we just consumed (the book
    // is never mutated) => the order is crossing-exempt and must NOT be
    // re-filled from that same displayed 200 (pinned double-count guard).
    EXPECT_TRUE(o.cross_exempt);
    EventFeeder f2 = f;
    sim.on_event(f2.heartbeat(T0 + 20'000'000));
    EXPECT_EQ(sim.fills().size(), 1u);  // still only the aggressive fill
    // Once the display goes uncrossed the exemption ends: cancel the stale
    // 101 ask (order 21), then a fresh crossing quote fills us.
    sim.on_event(f2.cancel(T0 + 21'000'000, 1, 101, 200, 21));
    EXPECT_FALSE(sim.orders().at(id).cross_exempt);
    sim.on_event(f2.ev(T0 + 22'000'000, 6, 1, 100, 300, 0));
    ASSERT_EQ(sim.fills().size(), 2u);
    EXPECT_EQ(sim.fills()[1].qty, 100);
    EXPECT_EQ(sim.fills()[1].price_ticks, 101);
    EXPECT_EQ(sim.orders().at(id).state, OrderState::FILLED);
}

TEST(ExecOrderTypes, IocFillsWhatItCanThenCancels) {
    ExecutionSimulator sim(test_config());
    EventFeeder f;
    seed_book(sim, f);
    // IOC buy limit 101 for 300: fills 200 @ 101, cancels the rest.
    const auto id = sim.submit(child(0, OrderType::IOC, 101, 300, T0 + 10));
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1));
    ASSERT_EQ(sim.fills().size(), 1u);
    EXPECT_EQ(sim.fills()[0].qty, 200);
    EXPECT_EQ(sim.orders().at(id).state, OrderState::CANCELLED);
    EXPECT_EQ(sim.orders().at(id).remaining, 100);
}

TEST(ExecOrderTypes, FokAllOrNone) {
    // Kill branch: 300 wanted within limit 101 but only 200 displayed.
    {
        ExecutionSimulator sim(test_config());
        EventFeeder f;
        seed_book(sim, f);
        const auto id =
            sim.submit(child(0, OrderType::FOK, 101, 300, T0 + 10));
        sim.on_event(f.heartbeat(T0 + 10 + LAT + 1));
        EXPECT_TRUE(sim.fills().empty());
        EXPECT_EQ(sim.orders().at(id).state, OrderState::CANCELLED);
        EXPECT_EQ(sim.orders().at(id).remaining, 300);
    }
    // Fill branch: limit 102 spans 200 + 500 displayed >= 300.
    {
        ExecutionSimulator sim(test_config());
        EventFeeder f;
        seed_book(sim, f);
        const auto id =
            sim.submit(child(0, OrderType::FOK, 102, 300, T0 + 10));
        sim.on_event(f.heartbeat(T0 + 10 + LAT + 1));
        ASSERT_EQ(sim.fills().size(), 2u);
        EXPECT_EQ(sim.fills()[0].qty + sim.fills()[1].qty, 300);
        EXPECT_EQ(sim.orders().at(id).state, OrderState::FILLED);
    }
}

TEST(ExecFees, TakerMakerAndImpactArithmetic) {
    ExecutionSimulator sim(test_config());
    EventFeeder f;
    seed_book(sim, f);
    // Taker: sell 100 into the 100 bid.
    sim.submit(child(1, OrderType::MARKET, 0, 100, T0 + 10));
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1));
    ASSERT_EQ(sim.fills().size(), 1u);
    const auto& taker = sim.fills()[0];
    EXPECT_EQ(taker.price_ticks, 100);
    EXPECT_DOUBLE_EQ(taker.fee, 0.003 * 100.0);
    // impact_bps = 2.0 * (100 / 1e6 * 100) = 0.02 bps over notional
    // 100 * 100 ticks * 0.01 = 100.0 => 0.02e-4 * 100 = 2e-4.
    EXPECT_NEAR(taker.impact_cost, 2e-4, 1e-15);
    // Maker: passive buy at 100, filled by trade-through.
    sim.submit(child(0, OrderType::LIMIT, 100, 40, T0 + 5'000'000));
    sim.on_event(f.heartbeat(T0 + 5'000'000 + LAT + 1));
    sim.on_event(f.exec(T0 + 8'000'000, 0, 99, 10, 12));
    ASSERT_EQ(sim.fills().size(), 2u);
    const auto& maker = sim.fills()[1];
    EXPECT_EQ(maker.liquidity, Liquidity::MAKER);
    EXPECT_DOUBLE_EQ(maker.fee, -0.002 * 40.0);  // rebate: negative fee
    EXPECT_DOUBLE_EQ(maker.impact_cost, 0.0);    // passive: no impact
}

TEST(ExecFees, FxCommissionPerMillionNotional) {
    ExecConfig cfg = test_config();
    cfg.venues[VEN].is_fx = true;
    cfg.venues[VEN].commission_per_million = 2.5;
    cfg.instruments[INS].tick_size = 1e-05;
    cfg.instruments[INS].lot_size = 1000.0;
    ExecutionSimulator sim(cfg);
    EventFeeder f;
    sim.on_event(f.add(T0, 0, 108650, 500, 11));
    sim.on_event(f.add(T0 + 1, 1, 108660, 500, 21));
    sim.submit(child(0, OrderType::MARKET, 0, 100, T0 + 10));
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1));
    ASSERT_EQ(sim.fills().size(), 1u);
    // notional = 100 * 1000 * 108660 * 1e-5 = 108660.0
    EXPECT_NEAR(sim.fills()[0].fee, 2.5 * 108660.0 / 1e6, 1e-12);
}

TEST(ExecLatency, ArrivalDecompositionAndJitterDraw) {
    // With jitter: arrival must equal decision + decision/risk/wire + venue
    // mean + SplitMix64(seed).below(jitter + 1), draws in submission order.
    const std::int64_t jitter_ns = 50'000;
    ExecutionSimulator sim(test_config(jitter_ns));
    EventFeeder f;
    seed_book(sim, f);
    iap::SplitMix64 rng(42);  // same seed as test_config
    const auto id1 = sim.submit(child(0, OrderType::LIMIT, 99, 10, T0 + 10));
    const auto id2 = sim.submit(child(1, OrderType::LIMIT, 102, 10, T0 + 20));
    const std::int64_t j1 = rng.below(jitter_ns + 1);
    const std::int64_t j2 = rng.below(jitter_ns + 1);
    EXPECT_EQ(sim.orders().at(id1).arrival_ts, T0 + 10 + LAT + j1);
    EXPECT_EQ(sim.orders().at(id2).arrival_ts, T0 + 20 + LAT + j2);
    EXPECT_GE(j1, 0);
    EXPECT_LE(j1, jitter_ns);
}

TEST(ExecLatency, NoFillBeforeArrivalAndOrderedActivation) {
    ExecutionSimulator sim(test_config());
    EventFeeder f;
    seed_book(sim, f);
    const auto id = sim.submit(child(0, OrderType::MARKET, 0, 50, T0 + 10));
    const std::int64_t arrival = sim.orders().at(id).arrival_ts;
    // Events strictly before arrival do NOT activate the order.
    sim.on_event(f.heartbeat(arrival - 1));
    EXPECT_EQ(sim.orders().at(id).state, OrderState::PENDING);
    EXPECT_TRUE(sim.fills().empty());
    // First event at/after arrival activates; fill stamped at arrival_ts.
    sim.on_event(f.heartbeat(arrival + 500));
    ASSERT_EQ(sim.fills().size(), 1u);
    EXPECT_EQ(sim.fills()[0].ts, arrival);
    EXPECT_LE(sim.fills()[0].ts, arrival + 500);
    // Every fill in the log is at/after its order's arrival.
    for (const auto& fill : sim.fills()) {
        EXPECT_GE(fill.ts, sim.orders().at(fill.order_id).arrival_ts);
    }
}

TEST(ExecDeterminism, SameConfigSameFills) {
    auto run = [](std::uint64_t seed) {
        ExecConfig cfg = test_config(50'000);
        cfg.seed = seed;
        ExecutionSimulator sim(cfg);
        EventFeeder f;
        seed_book(sim, f);
        sim.submit(child(0, OrderType::LIMIT, 100, 50, T0 + 10));
        sim.submit(child(1, OrderType::MARKET, 0, 120, T0 + 20));
        sim.on_event(f.heartbeat(T0 + 1'000'000));
        sim.on_event(f.exec(T0 + 2'000'000, 0, 100, 320, 11));
        sim.on_event(f.exec(T0 + 3'000'000, 0, 100, 100, 12));
        sim.cancel_all();
        return sim.fills();
    };
    const auto a = run(42);
    const auto b = run(42);
    ASSERT_EQ(a.size(), b.size());
    for (std::size_t i = 0; i < a.size(); ++i) {
        EXPECT_EQ(a[i].fill_id, b[i].fill_id);
        EXPECT_EQ(a[i].order_id, b[i].order_id);
        EXPECT_EQ(a[i].price_ticks, b[i].price_ticks);
        EXPECT_EQ(a[i].qty, b[i].qty);
        EXPECT_EQ(a[i].ts, b[i].ts);
        EXPECT_EQ(a[i].fee, b[i].fee);
        EXPECT_EQ(a[i].impact_cost, b[i].impact_cost);
    }
    EXPECT_FALSE(a.empty());
}

TEST(ExecLifecycle, CancelAndValidation) {
    ExecutionSimulator sim(test_config());
    EventFeeder f;
    seed_book(sim, f);
    const auto id = sim.submit(child(0, OrderType::LIMIT, 99, 10, T0 + 10));
    sim.cancel(id);
    EXPECT_EQ(sim.orders().at(id).state, OrderState::CANCELLED);
    sim.cancel(id);  // idempotent on terminal states
    sim.on_event(f.exec(T0 + 2'000'000, 0, 99, 500, 12));
    EXPECT_TRUE(sim.fills().empty());  // cancelled orders never fill
    EXPECT_THROW(sim.cancel(9999), std::invalid_argument);
    ChildOrder bad = child(0, OrderType::LIMIT, 0, 10, T0);
    EXPECT_THROW(sim.submit(bad), std::invalid_argument);  // limit needs px
    bad = child(0, OrderType::MARKET, 0, 0, T0);
    EXPECT_THROW(sim.submit(bad), std::invalid_argument);  // qty > 0
    bad = child(0, OrderType::MARKET, 0, 10, T0);
    bad.venue_id = 999;
    EXPECT_THROW(sim.submit(bad), std::invalid_argument);  // unknown venue
}

}  // namespace
