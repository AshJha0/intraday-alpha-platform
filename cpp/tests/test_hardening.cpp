// Regression tests for the verified undefined-behaviour / parity defects in
// the native feature engine, JSONL codec and order-book checkpoint restore.
//
// Every case here is reachable only with extreme (but wire-legal) inputs, so
// none of it touches the golden vectors. Each test names the defect it pins
// and asserts the behaviour of the Python reference, which all three ports
// must agree on.

#include <cmath>
#include <cstdint>
#include <limits>
#include <map>
#include <string>
#include <vector>

#include <gtest/gtest.h>

#include "iap/features/feature_engine.hpp"
#include "iap/features/rolling.hpp"
#include "iap/marketdata/codec.hpp"
#include "iap/marketdata/events.hpp"
#include "iap/orderbook/book.hpp"

using namespace iap;

namespace {

constexpr std::int64_t kHugeQty = std::int64_t{1} << 62;
constexpr double kTick = 0.01;

MarketEvent add(std::uint64_t eid, std::uint16_t venue, std::int64_t ts,
                std::uint64_t seq, Side side, std::int64_t price,
                std::int64_t qty, std::uint64_t oid) {
    return MarketEvent::of(eid, 1, venue, ts, ts, seq,
                           static_cast<std::uint8_t>(EventType::ADD),
                           static_cast<std::uint8_t>(side), price, qty, oid, 0);
}

double value_of(const FeatureVector& v, const char* name, bool& valid) {
    const int slot = feature_index(name);
    EXPECT_GE(slot, 0) << name;
    valid = v.valid[static_cast<std::size_t>(slot)];
    return v.values[static_cast<std::size_t>(slot)];
}

}  // namespace

// -------------------------------------------------------------- defect 1
// Cross-venue merged depth accumulated in int64 BEFORE the FEATURE_MAX_QTY
// guard, so two venues each resting 2^62 at the same price wrapped the total
// negative and the guard never fired: `depth_bid_l1_v1` was emitted as
// -9223372036854775808 with valid == 1. Reference verdict: the merged view is
// oversized -> cleared, book_ok false, oversized_depth_skipped counted.
TEST(Hardening, MergedDepthOverflowIsOversizedNotNegative) {
    FeatureEngine eng({{1u, kTick}}, 0);
    FeatureVector vec;
    std::vector<MarketEvent> evs = {
        add(1, 1, 1'000, 1, Side::BID, 100, kHugeQty, 11),
        add(2, 1, 1'001, 2, Side::ASK, 101, 5, 12),
        add(3, 2, 1'002, 1, Side::BID, 100, kHugeQty, 21),
        add(4, 2, 1'003, 2, Side::ASK, 101, 5, 22),
    };
    bool emitted = false;
    for (const auto& ev : evs) emitted = eng.apply(ev, vec);
    ASSERT_TRUE(emitted);

    EXPECT_FALSE(eng.book_ok(1));
    // Every one of the four refreshes sees an oversized merged bid level —
    // the count the reference reports for this stream.
    EXPECT_EQ(eng.oversized_depth_skipped(), 4u);
    bool valid = true;
    const double depth = value_of(vec, "depth_bid_l1_v1", valid);
    EXPECT_FALSE(valid);
    EXPECT_TRUE(std::isnan(depth));
    bool mid_valid = true;
    value_of(vec, "mid_price_v1", mid_valid);
    EXPECT_FALSE(mid_valid);
}

// The guard must still pass a merged level that is large but legal: two
// venues at FEATURE_MAX_QTY / 2 sum to exactly FEATURE_MAX_QTY.
TEST(Hardening, MergedDepthAtTheLimitIsStillUsable) {
    FeatureEngine eng({{1u, kTick}}, 0);
    FeatureVector vec;
    const std::int64_t half = FEATURE_MAX_QTY / 2;
    std::vector<MarketEvent> evs = {
        add(1, 1, 1'000, 1, Side::BID, 100, half, 11),
        add(2, 1, 1'001, 2, Side::ASK, 101, 5, 12),
        add(3, 2, 1'002, 1, Side::BID, 100, half, 21),
        add(4, 2, 1'003, 2, Side::ASK, 101, 5, 22),
    };
    bool emitted = false;
    for (const auto& ev : evs) emitted = eng.apply(ev, vec);
    ASSERT_TRUE(emitted);
    EXPECT_TRUE(eng.book_ok(1));
    EXPECT_EQ(eng.oversized_depth_skipped(), 0u);
    bool valid = false;
    const double depth = value_of(vec, "depth_bid_l1_v1", valid);
    EXPECT_TRUE(valid);
    EXPECT_DOUBLE_EQ(depth, static_cast<double>(FEATURE_MAX_QTY));
}

// -------------------------------------------------------------- defect 2
// `mid2 = bid_p + ask_p` overflowed int64 for large but legal prices and
// emitted mid_price_v1 = -4.61169e+16 with valid == 1 while microprice_v1 was
// +4.61169e+16. The doubled mid is now exact, so mid and microprice agree in
// sign and magnitude (as in the arbitrary-precision reference).
TEST(Hardening, DoubledMidBeyondInt64StaysExact) {
    FeatureEngine eng({{1u, kTick}}, 0);
    FeatureVector vec;
    const std::int64_t bid = std::int64_t{1} << 62;
    const std::int64_t ask = bid + 1;
    ASSERT_TRUE(eng.apply(add(1, 1, 1'000, 1, Side::BID, bid, 7, 11), vec));
    ASSERT_TRUE(eng.apply(add(2, 1, 1'001, 2, Side::ASK, ask, 7, 12), vec));

    bool mid_valid = false, micro_valid = false;
    const double mid = value_of(vec, "mid_price_v1", mid_valid);
    const double micro = value_of(vec, "microprice_v1", micro_valid);
    ASSERT_TRUE(mid_valid);
    ASSERT_TRUE(micro_valid);
    EXPECT_GT(mid, 0.0);
    const double expect =
        (static_cast<double>(bid) + static_cast<double>(ask)) * kTick / 2.0;
    EXPECT_DOUBLE_EQ(mid, expect);
    // Equal sizes on both sides put the microprice exactly at the mid.
    EXPECT_NEAR(micro, mid, std::abs(mid) * 1e-12);
}

// -------------------------------------------------------------- defect 3
// A rolling window folds in an unbounded number of samples: an int64 running
// sum wrapped and, because trim() then subtracts from the wrapped total, the
// window sum stayed corrupted for the rest of the session. The accumulator is
// now exact like the reference's arbitrary-precision int.
TEST(Hardening, RollingSumAccumulatesBeyondInt64AndDrainsBackToZero) {
    RollingSum<std::int64_t, 1> win(1'000'000);
    const std::int64_t big = std::int64_t{1} << 62;
    for (int i = 0; i < 4; ++i) win.add(i, {big});
    EXPECT_EQ(win.count(), 4u);
    EXPECT_EQ(win.sum(0), static_cast<__int128>(big) * 4);
    // Evicting every sample must return the running sum to exactly zero —
    // the property an int64 accumulator lost permanently once it wrapped.
    win.trim(2'000'000);
    EXPECT_EQ(win.count(), 0u);
    EXPECT_EQ(win.sum(0), __int128{0});
}

// -------------------------------------------------------------- defect 4
// exchange_ts is a signed 64-bit wire field the codec accepts down to
// INT64_MIN; every window/warmup/cadence computation was `t - constant` on
// unchecked int64 (six UBSan reports on this three-event stream).
TEST(Hardening, ExtremeExchangeTsDoesNotOverflowWindowArithmetic) {
    FeatureEngine eng({{1u, kTick}}, 1'000);
    FeatureVector vec;
    const std::int64_t tmin = std::numeric_limits<std::int64_t>::min();
    ASSERT_NO_THROW({
        eng.apply(add(1, 1, tmin, 1, Side::BID, 100, 5, 11), vec);
        eng.apply(add(2, 1, tmin + 1, 2, Side::ASK, 101, 5, 12), vec);
        eng.apply(add(3, 1, tmin + 2, 3, Side::BID, 99, 5, 13), vec);
    });
    EXPECT_EQ(eng.events_processed(), 3u);
    // The warmup anchor is the FIRST event even when it is negative: with
    // `first_ts < 0` as the "no events yet" sentinel the anchor re-armed on
    // every event and warmup restarted forever.
    EXPECT_EQ(eng.warm_ts(1), tmin);
    // Cadence 1 us over three 1 ns apart events: one vector, like the
    // reference (a wrapped `t - last_emit` emitted on every event).
    EXPECT_EQ(eng.vectors_emitted(), 1u);
    // No window can be warm at the anchor itself.
    bool valid = true;
    value_of(vec, "ofi_l1_w1s_v1", valid);
    EXPECT_FALSE(valid);
}

// The mirror case: a very negative warmup anchor with t at INT64_MAX
// overflowed warm() and the cadence test in emit_if_due.
TEST(Hardening, ExchangeTsSpanningTheWholeRangeIsWarmNotWrapped) {
    FeatureEngine eng({{1u, kTick}}, 1'000);
    FeatureVector vec;
    const std::int64_t tmin = std::numeric_limits<std::int64_t>::min();
    const std::int64_t tmax = std::numeric_limits<std::int64_t>::max();
    ASSERT_NO_THROW({
        eng.apply(add(1, 1, tmin, 1, Side::BID, 100, 5, 11), vec);
        eng.apply(add(2, 1, tmin + 1, 2, Side::ASK, 101, 5, 12), vec);
        eng.apply(add(3, 1, tmax, 3, Side::BID, 99, 5, 13), vec);
    });
    // t - warm_ts is ~2^64 ns: every window is warm (a wrapped subtraction
    // reported the opposite). Reference: two vectors, ofi_l1_w30s = 0.
    EXPECT_EQ(eng.warm_ts(1), tmin);
    EXPECT_EQ(eng.vectors_emitted(), 2u);
    bool valid = false;
    const double ofi = value_of(vec, "ofi_l1_w30s_v1", valid);
    EXPECT_TRUE(valid);
    EXPECT_DOUBLE_EQ(ofi, 0.0);
}

// -------------------------------------------------------------- defect 5
// The pinned JSONL whitespace set is ASCII space / tab / CR / LF — exactly
// the reference line regex's `[ \t\r\n]*`. LF was missing from skip_ws, so a
// line the Python and Rust ports accepted was rejected here.
TEST(Hardening, JsonlWhitespaceSetIsPinnedAscii) {
    const std::string body =
        R"({"event_id":1,"instrument_id":2,"venue_id":3,"exchange_ts":4,)"
        R"("receive_ts":5,"sequence":6,"event_type":1,"side":0,)"
        R"("price_ticks":7,"qty":8,"order_id":9,"trade_id":0})";

    for (const char ws : {' ', '\t', '\r', '\n'}) {
        const std::string line = std::string(1, ws) + body + std::string(1, ws);
        MarketEvent ev{};
        ASSERT_NO_THROW(ev = decode_jsonl_line(line))
            << "byte 0x" << std::hex << static_cast<int>(ws);
        EXPECT_EQ(ev.event_id, 1u);
        EXPECT_EQ(ev.price_ticks, 7);
    }
    // Everything else is content, not whitespace: VT, FF and NBSP are
    // rejected here and must be rejected by the other two ports too.
    for (const std::string& bad : {std::string("\x0b"), std::string("\x0c"),
                                   std::string("\xc2\xa0")}) {
        EXPECT_THROW(decode_jsonl_line(bad + body), std::invalid_argument);
    }
    // Interior newlines are whitespace as well (the reference regex allows
    // `[ \t\r\n]*` between every token).
    const std::string split =
        R"({"event_id":1,)"
        "\n"
        R"("instrument_id":2,"venue_id":3,"exchange_ts":4,)"
        R"("receive_ts":5,"sequence":6,"event_type":1,"side":0,)"
        R"("price_ticks":7,"qty":8,"order_id":9,"trade_id":0})";
    MarketEvent ev{};
    ASSERT_NO_THROW(ev = decode_jsonl_line(split));
    EXPECT_EQ(ev.instrument_id, 2u);
}

// -------------------------------------------------------------- defect 6
// restore() validated side, duplicate ids and arrival consistency but not
// qty or price_ticks, while the live path requires both > 0: a hand-written
// checkpoint produced best_bid() == (-5, -1000000), a state unreachable
// through apply().
TEST(Hardening, RestoreRejectsNonPositiveQtyAndPrice) {
    BookCheckpoint cp;
    cp.instrument_id = 1;
    cp.venue_id = 1;
    cp.levels.push_back(LevelCheckpoint{0, -5, {{7u, -1'000'000}}});
    cp.arrival_order = {7u};
    EXPECT_THROW(OrderBook::restore(cp), std::invalid_argument);

    // price alone
    cp.levels[0] = LevelCheckpoint{0, -5, {{7u, 10}}};
    EXPECT_THROW(OrderBook::restore(cp), std::invalid_argument);

    // qty alone
    cp.levels[0] = LevelCheckpoint{0, 100, {{7u, 0}}};
    EXPECT_THROW(OrderBook::restore(cp), std::invalid_argument);

    // a well-formed level still restores
    cp.levels[0] = LevelCheckpoint{0, 100, {{7u, 10}}};
    OrderBook book = OrderBook::restore(cp);
    ASSERT_TRUE(book.best_bid().has_value());
    EXPECT_EQ(book.best_bid()->first, 100);
    EXPECT_EQ(book.best_bid()->second, 10);
}
