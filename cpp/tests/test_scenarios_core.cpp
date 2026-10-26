// Real-life market-data scenarios for the core layer (docs/SCENARIOS.md,
// CORE). Mirrors python/tests/test_scenarios_core.py: every test is named
// after the scenario and pins the behaviour of PLATFORM_CONVENTIONS.md
// section 4 / API_CORE.md sections 3-5.

#include <gtest/gtest.h>

#include <cstdint>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

#include "iap/marketdata/codec.hpp"
#include "iap/marketdata/events.hpp"
#include "iap/orderbook/book.hpp"
#include "iap/replay/replay.hpp"

using iap::BookCheckpoint;
using iap::ConsolidatedBook;
using iap::EventType;
using iap::MarketEvent;
using iap::OrderBook;
using iap::ReplayEngine;
using iap::SessionStatus;
using iap::Side;

namespace {

constexpr std::uint8_t ADD = static_cast<std::uint8_t>(EventType::ADD);
constexpr std::uint8_t MODIFY = static_cast<std::uint8_t>(EventType::MODIFY);
constexpr std::uint8_t CANCEL = static_cast<std::uint8_t>(EventType::CANCEL);
constexpr std::uint8_t EXECUTE = static_cast<std::uint8_t>(EventType::EXECUTE);
constexpr std::uint8_t TRADE = static_cast<std::uint8_t>(EventType::TRADE);
constexpr std::uint8_t QUOTE = static_cast<std::uint8_t>(EventType::QUOTE);
constexpr std::uint8_t SNAPSHOT = static_cast<std::uint8_t>(EventType::SNAPSHOT);
constexpr std::uint8_t STATUS = static_cast<std::uint8_t>(EventType::STATUS);
constexpr std::uint8_t HEARTBEAT = static_cast<std::uint8_t>(EventType::HEARTBEAT);
constexpr std::uint8_t BID = 0;
constexpr std::uint8_t ASK = 1;
constexpr std::int64_t I64_MAX = std::numeric_limits<std::int64_t>::max();
constexpr std::int64_t TS0 = 1700000000000000000LL;

iap::LevelEntry LE(std::int64_t price, std::int64_t value) { return {price, value}; }

// Explicit-sequence event builder (mirrors the Python conftest mkev()).
MarketEvent mk(std::uint64_t seq, std::uint8_t type, std::uint8_t side = 0,
               std::int64_t price = 0, std::int64_t qty = 0,
               std::uint64_t order_id = 0, std::uint64_t trade_id = 0,
               std::uint32_t instrument = 1, std::uint16_t venue = 1,
               std::int64_t ts = 0) {
    if (ts == 0) ts = TS0 + static_cast<std::int64_t>(seq % 1000000) * 1000000;
    return MarketEvent::of(seq, instrument, venue, ts, ts + 150000, seq, type,
                           side, price, qty, order_id, trade_id);
}

MarketEvent add(std::uint64_t seq, std::uint8_t side, std::int64_t price,
                std::int64_t qty, std::uint64_t oid, std::uint16_t venue = 1,
                std::int64_t ts = 0) {
    return mk(seq, ADD, side, price, qty, oid, 0, 1, venue, ts);
}

struct Rec {
    std::uint8_t side;
    std::int64_t price;
    std::int64_t qty;
    std::uint64_t oid;
};

const std::vector<Rec> BURST = {{BID, 2450, 500, 101}, {BID, 2449, 400, 102},
                                {ASK, 2451, 600, 103}};

std::uint64_t burst(OrderBook& b, std::uint64_t seq, const std::vector<Rec>& recs,
                    bool ids = true, std::uint16_t venue = 1) {
    const std::uint64_t n = recs.size();
    for (std::uint64_t i = 0; i < n; ++i) {
        const Rec& r = recs[i];
        b.apply(mk(seq + i, SNAPSHOT, r.side, r.price, r.qty, ids ? r.oid : 0,
                   n - 1 - i, 1, venue));
    }
    return seq + n;
}

// Bids 2449 (11: 100, 12: 200) / 2448 (13: 300); asks 2451 (21: 150) / 2452 (22: 250).
OrderBook seeded(std::size_t window = 0) {
    OrderBook b(1, 1, window);
    b.apply(add(1, BID, 2449, 100, 11));
    b.apply(add(2, BID, 2449, 200, 12));
    b.apply(add(3, BID, 2448, 300, 13));
    b.apply(add(4, ASK, 2451, 150, 21));
    b.apply(add(5, ASK, 2452, 250, 22));
    return b;
}

std::vector<std::uint64_t> resting_ids(const OrderBook& b) {
    std::vector<std::uint64_t> out;
    for (const auto& o : b.resting_orders()) out.push_back(o.order_id);
    return out;
}

}  // namespace

// ------------------------------------------------ #2 snapshot-after-gap variants

TEST(ScenarioCore, SnapshotGapOnFirstBurstRecordRecovers) {
    OrderBook b = seeded();
    std::uint64_t nxt = burst(b, 9, BURST);
    EXPECT_EQ(b.counters().gaps_detected, 1u);
    EXPECT_FALSE(b.stale());
    EXPECT_EQ(*b.best_bid(), LE(2450, 500));
    EXPECT_EQ(*b.best_ask(), LE(2451, 600));
    b.apply(add(nxt, BID, 2450, 100, 105));
    EXPECT_EQ(*b.best_bid(), LE(2450, 600));
}

TEST(ScenarioCore, SnapshotGapBetweenTwoBursts) {
    OrderBook b = seeded();
    std::uint64_t nxt = burst(b, 6, BURST);
    EXPECT_FALSE(b.stale());
    burst(b, nxt + 3, {{BID, 2447, 50, 201}, {ASK, 2453, 60, 202}});
    EXPECT_EQ(b.counters().gaps_detected, 1u);
    EXPECT_FALSE(b.stale());
    EXPECT_EQ(*b.best_bid(), LE(2447, 50));
    EXPECT_EQ(b.order_count_total(), 2u);
}

TEST(ScenarioCore, SnapshotDuplicateInsideBurstIgnored) {
    OrderBook b = seeded();
    b.apply(add(9, BID, 2449, 100, 44));
    MarketEvent rec = mk(10, SNAPSHOT, BID, 2450, 500, 101, 2);
    b.apply(rec);
    b.apply(rec);
    b.apply(mk(11, SNAPSHOT, BID, 2449, 400, 102, 1));
    b.apply(mk(12, SNAPSHOT, ASK, 2451, 600, 103, 0));
    EXPECT_EQ(b.counters().duplicates_dropped, 1u);
    EXPECT_FALSE(b.stale());
    EXPECT_EQ(b.order_count_total(), 3u);
    EXPECT_EQ(b.counters().snapshot_restarts, 0u);
}

TEST(ScenarioCore, SnapshotHeartbeatAndTradeInterleavedInsideBurst) {
    OrderBook b = seeded();
    b.apply(add(9, BID, 2449, 100, 44));
    b.apply(mk(10, SNAPSHOT, BID, 2450, 500, 101, 1));
    b.apply(mk(11, HEARTBEAT));
    b.apply(mk(12, TRADE, BID, 2451, 30, 0, 7));
    b.apply(mk(13, SNAPSHOT, ASK, 2451, 600, 103, 0));
    EXPECT_FALSE(b.stale());
    EXPECT_EQ(b.trade_flow(), 30);
    EXPECT_EQ(b.order_count_total(), 2u);
}

// ------------------------------------------------------- #3 venue sequence reset

TEST(ScenarioCore, VenueSequenceResetDailyRestart) {
    OrderBook b(1, 1);
    for (std::uint64_t s = 1; s <= 500; ++s) {
        const bool bid = s % 2 == 1;
        b.apply(add(s, bid ? BID : ASK,
                    bid ? 2400 - static_cast<std::int64_t>(s % 7)
                        : 2410 + static_cast<std::int64_t>(s % 7),
                    100, 1000 + s));
    }
    EXPECT_EQ(b.last_sequence(), 500u);
    EXPECT_FALSE(b.stale());
    burst(b, 1, BURST);
    EXPECT_EQ(b.counters().sequence_resets, 1u);
    EXPECT_EQ(b.sequence_epoch(), 1u);
    EXPECT_FALSE(b.stale());
    EXPECT_EQ(b.counters().duplicates_dropped, 0u);
    EXPECT_EQ(*b.best_bid(), LE(2450, 500));
    EXPECT_EQ(*b.best_ask(), LE(2451, 600));
    EXPECT_EQ(b.order_count_total(), 3u);
    b.apply(add(4, BID, 2450, 100, 5001));
    EXPECT_EQ(*b.best_bid(), LE(2450, 600));
    EXPECT_EQ(b.last_sequence(), 4u);
    b.apply(add(4, BID, 2450, 100, 5002));
    EXPECT_EQ(b.counters().duplicates_dropped, 1u);
}

TEST(ScenarioCore, PartitionFailoverResetStaysStaleUntilBurstCompletes) {
    OrderBook b = seeded();
    b.apply(mk(1, SNAPSHOT, BID, 2450, 500, 101, 2));
    EXPECT_EQ(b.counters().sequence_resets, 1u);
    EXPECT_TRUE(b.stale());
    b.apply(mk(3, SNAPSHOT, ASK, 2451, 600, 103, 0));  // gap inside the burst
    EXPECT_TRUE(b.stale());
    EXPECT_EQ(b.counters().gaps_detected, 1u);
    burst(b, 4, BURST);
    EXPECT_FALSE(b.stale());
}

TEST(ScenarioCore, ExplicitResetSequenceApi) {
    OrderBook b = seeded();
    b.reset_sequence();
    EXPECT_TRUE(b.stale());
    EXPECT_FALSE(b.has_sequence());
    EXPECT_EQ(b.counters().sequence_resets, 1u);
    b.apply(add(1, BID, 2449, 100, 44));
    EXPECT_EQ(b.counters().duplicates_dropped, 0u);
    EXPECT_EQ(b.counters().dropped_while_stale, 1u);
    burst(b, 2, BURST);
    EXPECT_FALSE(b.stale());
    EXPECT_EQ(b.last_sequence(), 4u);
}

// --------------------------------------------------------- #5 auction call phase

TEST(ScenarioCore, HaltThenReopenAuction) {
    OrderBook b = seeded();
    b.apply(mk(6, STATUS, 0, 0, static_cast<std::int64_t>(SessionStatus::HALT)));
    b.apply(mk(7, STATUS, 0, 0, static_cast<std::int64_t>(SessionStatus::AUCTION)));
    b.apply(add(8, BID, 2452, 100, 31));  // crosses both asks: rests
    EXPECT_EQ(*b.best_bid(), LE(2452, 100));
    EXPECT_EQ(*b.best_ask(), LE(2451, 150));
    EXPECT_TRUE(b.is_crossed());
    EXPECT_EQ(b.order_count_total(), 6u);
    b.apply(mk(9, EXECUTE, BID, 2452, 100, 31));
    b.apply(mk(10, EXECUTE, ASK, 2451, 100, 21));
    EXPECT_FALSE(b.is_crossed());
    EXPECT_EQ(b.counters().unknown_order_events, 0u);
    EXPECT_EQ(*b.best_ask(), LE(2451, 50));
    EXPECT_EQ(*b.best_bid(), LE(2449, 300));
    b.apply(mk(11, STATUS, 0, 0, static_cast<std::int64_t>(SessionStatus::TRADING)));
    b.apply(add(12, BID, 2452, 100, 32));  // same ADD during TRADING executes
    EXPECT_EQ(*b.best_ask(), LE(2452, 200));
    EXPECT_EQ(*b.best_bid(), LE(2449, 300));
    EXPECT_EQ(b.order_count_total(), 4u);
}

TEST(ScenarioCore, NoMatchingWhileHaltedOrClosed) {
    for (SessionStatus st : {SessionStatus::HALT, SessionStatus::CLOSE}) {
        OrderBook b = seeded();
        b.apply(mk(6, STATUS, 0, 0, static_cast<std::int64_t>(st)));
        b.apply(add(7, ASK, 2448, 300, 31));
        EXPECT_TRUE(b.is_crossed());
        EXPECT_EQ(b.order_count_total(), 6u);
        EXPECT_EQ(*b.best_bid(), LE(2449, 300));
    }
}

// ------------------------------------------------ #6 payload-domain malformed events

TEST(ScenarioCore, PayloadDomainMalformedEventsDroppedAndCounted) {
    OrderBook b = seeded();
    const BookCheckpoint before = b.checkpoint();
    std::vector<MarketEvent> bad = {
        mk(6, EXECUTE, BID, 2449, -50, 11),
        mk(7, EXECUTE, BID, 2449, 0, 11),
        mk(8, QUOTE, BID, 2449, 0, 77),
        mk(9, SNAPSHOT, BID, 0, 10, 78, 0),
        mk(10, TRADE, ASK, 2449, -1, 0, 9),
        mk(11, ADD, BID, 2447, 100, 0),
        mk(12, ADD, BID, 2447, 100, iap::SYNTHETIC_ID_BASE + 1),
        mk(13, STATUS, 0, 0, 7),
        mk(14, CANCEL, BID, 0, 0, 0),
        mk(15, ADD, BID, -5, 100, 79),
    };
    for (const auto& ev : bad) b.apply(ev);
    EXPECT_EQ(b.counters().invalid_payload_dropped, bad.size());
    const BookCheckpoint after = b.checkpoint();
    EXPECT_EQ(after.levels, before.levels);
    EXPECT_EQ(after.trade_flow, 0);
    EXPECT_EQ(after.last_sequence, 15u);
    EXPECT_EQ(b.status(), static_cast<std::int64_t>(SessionStatus::TRADING));
    EXPECT_EQ(*b.best_bid(), LE(2449, 300));
}

TEST(ScenarioCore, UnknownEventTypeDroppedNotThrown) {
    OrderBook b = seeded();
    EXPECT_NO_THROW(b.apply(mk(6, 0, BID, 2449, 100, 55)));
    EXPECT_NO_THROW(b.apply(mk(7, 42, BID, 2449, 100, 56)));
    EXPECT_EQ(b.counters().unknown_type_dropped, 2u);
    EXPECT_EQ(b.last_sequence(), 7u);
    b.apply(add(8, BID, 2449, 50, 57));
    EXPECT_EQ(b.counters().gaps_detected, 0u);
    EXPECT_EQ(*b.best_bid(), LE(2449, 350));
}

// ------------------------------------------- #7 interrupted SNAPSHOT burst restart

TEST(ScenarioCore, InterruptedSnapshotBurstRestart) {
    OrderBook b = seeded();
    b.apply(mk(6, SNAPSHOT, BID, 2430, 10, 301, 3));
    b.apply(mk(7, SNAPSHOT, BID, 2429, 10, 302, 2));
    burst(b, 8, {{BID, 2440, 1, 401}, {ASK, 2441, 2, 402}, {ASK, 2442, 3, 403}});
    EXPECT_EQ(b.counters().snapshot_restarts, 1u);
    EXPECT_FALSE(b.stale());
    EXPECT_EQ(resting_ids(b), (std::vector<std::uint64_t>{401, 402, 403}));
}

TEST(ScenarioCore, SnapshotCountdownSkipMarksBurstBroken) {
    OrderBook b = seeded();
    b.apply(add(9, BID, 2449, 100, 44));
    b.apply(mk(10, SNAPSHOT, BID, 2450, 500, 101, 3));
    b.apply(mk(11, SNAPSHOT, ASK, 2451, 600, 103, 0));  // skipped 2, 1
    EXPECT_TRUE(b.stale());
    EXPECT_EQ(b.counters().snapshot_restarts, 0u);
    burst(b, 12, BURST);
    EXPECT_FALSE(b.stale());
}

// ----------------------------------------- #8 consolidated excludes stale / crossed

TEST(ScenarioCore, BzxStallConsolidatedNbboExcludesStaleVenue) {
    ConsolidatedBook cons(1);
    cons.apply(add(1, BID, 100, 10, 11, 1));
    cons.apply(add(2, ASK, 101, 10, 12, 1));
    cons.apply(add(1, BID, 105, 10, 21, 2));
    cons.apply(add(2, ASK, 106, 10, 22, 2));
    EXPECT_EQ(*cons.best_bid(), LE(105, 10));
    EXPECT_TRUE(cons.is_crossed());
    cons.apply(add(9, BID, 107, 10, 23, 2));  // gap on venue 2 -> stale
    EXPECT_EQ(cons.stale_venues(), (std::vector<std::uint16_t>{2}));
    EXPECT_EQ(cons.active_venues(), (std::vector<std::uint16_t>{1}));
    EXPECT_EQ(*cons.best_bid(), LE(100, 10));
    EXPECT_EQ(*cons.best_ask(), LE(101, 10));
    EXPECT_FALSE(cons.is_crossed());
    EXPECT_FALSE(cons.is_locked());
    EXPECT_EQ(cons.depth(Side::BID), (std::vector<iap::LevelEntry>{LE(100, 10)}));
    EXPECT_EQ(cons.order_count(Side::ASK), (std::vector<iap::LevelEntry>{LE(101, 1)}));
    EXPECT_EQ(*cons.venue_status(2), static_cast<std::int64_t>(SessionStatus::TRADING));
    EXPECT_FALSE(cons.venue_status(9).has_value());
    burst(cons.venue_book(2), 10, {{BID, 100, 5, 31}, {ASK, 101, 5, 32}}, true, 2);
    EXPECT_EQ(cons.active_venues(), (std::vector<std::uint16_t>{1, 2}));
    EXPECT_EQ(*cons.best_bid(), LE(100, 15));
    EXPECT_EQ(cons.order_count(Side::BID), (std::vector<iap::LevelEntry>{LE(100, 2)}));
}

TEST(ScenarioCore, ConsolidatedLockedAndCrossedPredicates) {
    ConsolidatedBook cons(1);
    cons.apply(add(1, BID, 100, 10, 11, 1));
    cons.apply(add(1, ASK, 100, 10, 21, 2));
    EXPECT_TRUE(cons.is_locked());
    EXPECT_FALSE(cons.is_crossed());
    cons.apply(add(2, ASK, 99, 10, 22, 2));
    EXPECT_TRUE(cons.is_crossed());
}

TEST(ScenarioCore, IsFreshTimeBasedStaleness) {
    OrderBook b(1, 1);
    EXPECT_FALSE(b.is_fresh(0, 1000000000LL));
    b.apply(add(1, BID, 100, 10, 1, 1, TS0));
    EXPECT_TRUE(b.is_fresh(TS0 + 150000 + 1000000000LL, 1000000000LL));
    EXPECT_FALSE(b.is_fresh(TS0 + 150000 + 2000000000LL, 1000000000LL));
}

TEST(ScenarioCore, VenueDisconnectSilentFeed) {
    ConsolidatedBook cons(1);
    cons.apply(add(1, BID, 100, 10, 11, 1, TS0));
    cons.apply(add(1, BID, 99, 10, 21, 2, TS0));
    const std::int64_t now = TS0 + 5000000000LL;
    cons.apply(add(2, ASK, 101, 10, 12, 1, now));
    std::vector<std::uint16_t> fresh;
    for (const auto& [vid, book] : cons.books()) {
        if (book.is_fresh(now + 150000, 1000000000LL)) fresh.push_back(vid);
    }
    EXPECT_EQ(fresh, (std::vector<std::uint16_t>{1}));
    EXPECT_EQ(cons.active_venues(), (std::vector<std::uint16_t>{1, 2}));
}

// --------------------------------------------- #9 late retransmission (reorder window)

TEST(ScenarioCore, AbFeedRetransmissionWithReorderWindow) {
    OrderBook b(1, 1, 3);
    for (std::uint64_t s : {1, 2, 3, 6, 4, 5, 7}) {
        b.apply(add(s, BID, 2400 + static_cast<std::int64_t>(s), 10, 100 + s));
    }
    EXPECT_EQ(b.counters().gaps_detected, 0u);
    EXPECT_FALSE(b.stale());
    EXPECT_EQ(b.counters().late_recovered, 2u);
    EXPECT_EQ(b.pending_count(), 0u);
    EXPECT_EQ(b.last_sequence(), 7u);
    EXPECT_EQ(resting_ids(b), (std::vector<std::uint64_t>{101, 102, 103, 104, 105, 106, 107}));
    EXPECT_EQ(b.counters().events_applied, 7u);
    EXPECT_EQ(b.counters().drops(), 0u);
}

TEST(ScenarioCore, ReorderWindowZeroReproducesStaleBehaviour) {
    OrderBook b(1, 1);
    for (std::uint64_t s : {1, 2, 3, 6, 4, 5, 7}) {
        b.apply(add(s, BID, 2400 + static_cast<std::int64_t>(s), 10, 100 + s));
    }
    EXPECT_EQ(b.counters().gaps_detected, 1u);
    EXPECT_TRUE(b.stale());
    EXPECT_EQ(b.counters().duplicates_dropped, 2u);
    EXPECT_EQ(b.counters().dropped_while_stale, 2u);
    EXPECT_EQ(b.counters().late_recovered, 0u);
}

TEST(ScenarioCore, ReorderWindowOverflowDeclaresGapAndFlushesInOrder) {
    OrderBook b(1, 1, 2);
    b.apply(add(1, BID, 2401, 10, 101));
    for (std::uint64_t s : {5, 3, 6}) {
        b.apply(add(s, BID, 2400 + static_cast<std::int64_t>(s), 10, 100 + s));
    }
    EXPECT_EQ(b.counters().gaps_detected, 2u);
    EXPECT_TRUE(b.stale());
    EXPECT_EQ(b.pending_count(), 0u);
    EXPECT_EQ(b.last_sequence(), 6u);
    EXPECT_EQ(b.counters().dropped_while_stale, 3u);
    EXPECT_EQ(b.counters().duplicates_dropped, 0u);
}

TEST(ScenarioCore, ReorderWindowDuplicateInBufferAndCheckpointRoundTrip) {
    OrderBook b(1, 1, 4);
    b.apply(add(1, BID, 2401, 10, 101));
    b.apply(add(4, BID, 2404, 10, 104));
    b.apply(add(4, BID, 2404, 10, 104));
    EXPECT_EQ(b.counters().duplicates_dropped, 1u);
    EXPECT_EQ(b.pending_count(), 1u);
    const BookCheckpoint cp = b.checkpoint();
    EXPECT_EQ(cp.reorder_window, 4u);
    ASSERT_EQ(cp.reorder_pending.size(), 1u);
    OrderBook r = OrderBook::restore(iap::book_checkpoint_from_json(
        iap::book_checkpoint_to_json(cp)));
    for (OrderBook* book : {&b, &r}) {
        book->apply(add(2, BID, 2402, 10, 102));
        book->apply(add(3, BID, 2403, 10, 103));
    }
    EXPECT_EQ(b.checkpoint(), r.checkpoint());
    EXPECT_EQ(r.pending_count(), 0u);
    EXPECT_EQ(r.counters().late_recovered, 2u);
    EXPECT_EQ(r.last_sequence(), 4u);
}

TEST(ScenarioCore, ReorderWindowBoundsPinned) {
    EXPECT_NO_THROW(OrderBook(1, 1, iap::MAX_REORDER_WINDOW));
    EXPECT_THROW(OrderBook(1, 1, iap::MAX_REORDER_WINDOW + 1), std::invalid_argument);
}

// ----------------------------------------------------------- #10 first sequence 0

TEST(ScenarioCore, FirstSequenceZeroBootstraps) {
    OrderBook b(1, 1);
    b.apply(add(0, BID, 100, 10, 1));
    b.apply(add(1, BID, 101, 10, 2));
    b.apply(add(0, BID, 102, 10, 3));
    EXPECT_EQ(b.order_count_total(), 2u);
    EXPECT_EQ(b.counters().duplicates_dropped, 1u);
    EXPECT_EQ(b.counters().gaps_detected, 0u);
    EXPECT_FALSE(b.stale());
    EXPECT_TRUE(b.has_sequence());
}

// ------------------------------------------------------------ #11 arithmetic limits

TEST(ScenarioCore, ArithmeticLimitsNoWrapEventsDroppedAndCounted) {
    OrderBook b(1, 1);
    b.apply(mk(1, TRADE, BID, 100, I64_MAX, 0, 1));
    EXPECT_EQ(b.trade_flow(), I64_MAX);
    b.apply(mk(2, TRADE, BID, 100, 1, 0, 2));  // overflow: dropped
    EXPECT_EQ(b.trade_flow(), I64_MAX);
    EXPECT_EQ(b.counters().invalid_payload_dropped, 1u);
    b.apply(mk(3, TRADE, ASK, 100, I64_MAX, 0, 3));
    b.apply(mk(4, TRADE, ASK, 100, I64_MAX, 0, 4));
    b.apply(mk(5, TRADE, ASK, 100, 2, 0, 5));  // would underflow
    EXPECT_EQ(b.trade_flow(), -I64_MAX);
    EXPECT_EQ(b.counters().invalid_payload_dropped, 2u);
    b.apply(add(6, BID, 100, I64_MAX, 1));
    b.apply(add(7, BID, 100, I64_MAX, 2));  // level total overflow: dropped
    EXPECT_EQ(*b.best_bid(), LE(100, I64_MAX));
    EXPECT_EQ(b.counters().invalid_payload_dropped, 3u);
    b.apply(mk(8, MODIFY, BID, 100, 1, 1));
    b.apply(add(9, BID, 100, I64_MAX - 1, 3));
    EXPECT_EQ(*b.best_bid(), LE(100, I64_MAX));
    b.apply(mk(10, MODIFY, BID, 100, 2, 1));  // +1 overflows
    EXPECT_EQ(b.counters().invalid_payload_dropped, 4u);
    EXPECT_EQ(*b.best_bid(), LE(100, I64_MAX));
    OrderBook c(1, 1);
    c.apply(add(std::numeric_limits<std::uint64_t>::max(), BID, 100, 10, 1, 1, TS0));
    c.apply(add(0, BID, 101, 10, 2, 1, TS0));
    EXPECT_EQ(c.counters().duplicates_dropped, 1u);
    EXPECT_EQ(c.last_sequence(), std::numeric_limits<std::uint64_t>::max());
    OrderBook restored = OrderBook::restore(b.checkpoint());
    EXPECT_EQ(restored.checkpoint(), b.checkpoint());
}

TEST(ScenarioCore, ConsolidatedTradeFlowSaturates) {
    ConsolidatedBook cons(1);
    cons.apply(mk(1, TRADE, BID, 100, I64_MAX, 0, 1, 1, 1));
    cons.apply(mk(1, TRADE, BID, 100, I64_MAX, 0, 1, 1, 2));
    EXPECT_EQ(cons.trade_flow(), I64_MAX);
    cons.apply(mk(2, TRADE, ASK, 100, I64_MAX, 0, 2, 1, 2));
    cons.apply(mk(3, TRADE, ASK, 100, 5, 0, 3, 1, 2));
    EXPECT_EQ(cons.trade_flow(), I64_MAX - 5);
}

// --------------------------------------------------------- #13 ids >= 2^63 in the book

TEST(ScenarioCore, BookWithOrderIdsAbove2Pow63) {
    const std::uint64_t base = (std::uint64_t{1} << 63) + 1;
    const std::uint64_t s0 = std::uint64_t{1} << 63;
    OrderBook b(1, 1);
    b.apply(add(s0, BID, 100, 10, base, 1, TS0));
    b.apply(add(s0 + 1, BID, 100, 20, base + 1, 1, TS0));
    b.apply(mk(s0 + 2, MODIFY, BID, 100, 30, base, 0, 1, 1, TS0));
    b.apply(mk(s0 + 3, EXECUTE, BID, 100, 5, base + 1, 0, 1, 1, TS0));
    EXPECT_EQ(resting_ids(b), (std::vector<std::uint64_t>{base, base + 1}));
    const auto cp = b.checkpoint();
    ASSERT_EQ(cp.levels.size(), 1u);
    EXPECT_EQ(cp.levels[0].orders,
              (std::vector<std::pair<std::uint64_t, std::int64_t>>{{base + 1, 15}, {base, 30}}));
    b.apply(mk(s0 + 4, CANCEL, BID, 100, 0, base + 1, 0, 1, 1, TS0));
    const std::string json = iap::book_checkpoint_to_json(b.checkpoint());
    EXPECT_EQ(OrderBook::restore(iap::book_checkpoint_from_json(json)).checkpoint(),
              b.checkpoint());
    EXPECT_EQ(b.counters().unknown_order_events, 0u);
    EXPECT_EQ(b.counters().gaps_detected, 0u);
}

// ------------------------------------------------------- #14 replay universe validation

TEST(ScenarioCore, ReplayRejectsUnknownInstrumentAndVenue) {
    iap::Universe u;
    u[1] = {1, 2};
    ReplayEngine engine(0, 0, 4, 4, 0, u);
    engine.apply(add(1, BID, 100, 10, 9, 1));
    engine.apply(mk(1, ADD, BID, 100, 10, 9, 0, 9999, 1));
    engine.apply(mk(2, ADD, BID, 100, 10, 9, 0, 1, 10));
    EXPECT_EQ(engine.unknown_instrument_dropped(), 1u);
    EXPECT_EQ(engine.unknown_venue_dropped(), 1u);
    EXPECT_EQ(engine.events_processed(), 3u);
    ASSERT_EQ(engine.books().size(), 1u);
    EXPECT_EQ(engine.books().at(1).books().size(), 1u);
    const std::string json = iap::checkpoint_to_json(engine.checkpoint());
    ReplayEngine resumed = ReplayEngine::restore(iap::checkpoint_from_json(json));
    ASSERT_TRUE(resumed.universe().has_value());
    resumed.apply(mk(2, ADD, BID, 100, 10, 9, 0, 7777, 1));
    EXPECT_EQ(resumed.unknown_instrument_dropped(), 2u);
    EXPECT_EQ(resumed.checkpoint().universe, u);
}

// ------------------------------------------------------ #15 corrupt record mid-file

TEST(ScenarioCore, BitFlipInIap1RecordIsDetected) {
    std::vector<MarketEvent> events;
    for (std::uint64_t s = 1; s <= 600; ++s) events.push_back(add(s, BID, 100, 10, s));
    auto data = iap::encode_iap1(events);
    const std::size_t off = 16 + 72 * 499;
    data[off + 14] = 0;     // event_type of record 500 -> 0
    data[off + 55] ^= 0x80;  // qty sign
    EXPECT_THROW(iap::decode_iap1(data), std::invalid_argument);
    // Legacy v1 (no trailer) cannot be verified: the book still never throws.
    data[4] = 1;
    data.resize(data.size() - 16);
    auto legacy = iap::decode_iap1_ex(data.data(), data.size());
    EXPECT_FALSE(legacy.integrity_checked);
    OrderBook book(1, 1);
    for (const auto& ev : legacy.events) EXPECT_NO_THROW(book.apply(ev));
    EXPECT_EQ(book.counters().unknown_type_dropped, 1u);
    EXPECT_EQ(book.counters().events_applied, 599u);
}

// ------------------------------------------------------------ #16 memory bounds

TEST(ScenarioCore, LongSessionSnapshotRetentionBounded) {
    std::vector<MarketEvent> events;
    for (std::uint64_t s = 1; s <= 300; ++s) events.push_back(add(s, BID, 100 + s % 5, 10, s));
    ReplayEngine engine(0, 1, 4, 3);
    std::uint64_t seen = 0;
    for (int pass = 0; pass < 5; ++pass) {
        engine.run(events, [&](std::uint64_t, const iap::ReplaySnapshot&) { ++seen; });
    }
    EXPECT_EQ(seen, 1500u);
    EXPECT_EQ(engine.snapshots_emitted(), 1500u);
    ASSERT_EQ(engine.snapshots().size(), 3u);
    EXPECT_EQ(engine.snapshots()[2].index, 1500u);
}

// ------------------------------------------------------ #20 QUOTE feed without ids

TEST(ScenarioCore, FxLpQuoteFeedWithoutIds) {
    ConsolidatedBook cons(101);
    std::uint64_t seqs[3] = {0, 0, 0};
    for (int k = 0; k < 10000; ++k) {
        const int vi = k % 3;
        const std::uint16_t vid = static_cast<std::uint16_t>(10 + vi);
        ++seqs[vi];
        const std::uint8_t side = ((k / 3) % 2 == 0) ? BID : ASK;
        const std::int64_t price = side == BID ? 108650 - 3 + (k % 5) : 108650 + 3 + (k % 5);
        cons.apply(mk(seqs[vi], QUOTE, side, price, 5 + (k % 7), 0, 0, 101, vid));
    }
    for (const auto& [vid, book] : cons.books()) {
        EXPECT_EQ(book.order_count(Side::BID), (std::vector<iap::LevelEntry>{LE(book.best_bid()->first, 1)}));
        EXPECT_EQ(book.order_count(Side::ASK), (std::vector<iap::LevelEntry>{LE(book.best_ask()->first, 1)}));
        const auto ids = resting_ids(book);
        ASSERT_EQ(ids.size(), 2u);
        EXPECT_TRUE(ids[0] == iap::synthetic_order_id(BID, 0) || ids[0] == iap::synthetic_order_id(ASK, 0));
        EXPECT_EQ(book.counters().drops(), 0u);
        EXPECT_EQ(book.counters().events_applied, seqs[vid - 10]);
    }
    EXPECT_EQ(iap::ConsolidatedBook::restore(cons.checkpoint()).checkpoint(), cons.checkpoint());
}

TEST(ScenarioCore, QuoteExplicitIdRules) {
    OrderBook b(1, 1);
    b.apply(mk(1, QUOTE, BID, 100, 5, 7));
    b.apply(mk(2, QUOTE, ASK, 101, 7, 7));  // rests on BID: dropped
    EXPECT_EQ(b.counters().unknown_order_events, 1u);
    EXPECT_FALSE(b.best_ask().has_value());
    b.apply(mk(3, QUOTE, BID, 99, 4, 7));  // same side: replace
    EXPECT_EQ(*b.best_bid(), LE(99, 4));
    EXPECT_EQ(b.order_count_total(), 1u);
    b.apply(mk(4, QUOTE, ASK, 101, 7, 0));
    b.apply(mk(5, QUOTE, BID, 98, 3, 0));
    EXPECT_EQ(resting_ids(b), (std::vector<std::uint64_t>{iap::synthetic_order_id(ASK, 0),
                                                          iap::synthetic_order_id(BID, 0)}));
}

TEST(ScenarioCore, SnapshotWithZeroIdsAssignsDeterministicSyntheticIds) {
    OrderBook b(1, 1);
    burst(b, 1, {{BID, 100, 5, 0}, {BID, 99, 6, 0}, {ASK, 101, 7, 0}}, false);
    EXPECT_EQ(resting_ids(b), (std::vector<std::uint64_t>{iap::synthetic_order_id(BID, 0),
                                                          iap::synthetic_order_id(BID, 1),
                                                          iap::synthetic_order_id(ASK, 0)}));
    b.apply(mk(4, EXECUTE, BID, 100, 2, iap::synthetic_order_id(BID, 0)));
    EXPECT_EQ(*b.best_bid(), LE(100, 3));
    b.apply(mk(5, SNAPSHOT, BID, 100, 5, 9, 1));
    b.apply(mk(6, SNAPSHOT, ASK, 101, 5, 9, 0));
    EXPECT_EQ(b.counters().unknown_order_events, 1u);
    EXPECT_EQ(b.order_count_total(), 1u);
}

// ------------------------------------------------- #21 STATUS while stale, ADD after CLOSE

TEST(ScenarioCore, StatusWhileStaleAndAddAfterClose) {
    OrderBook b = seeded();
    b.apply(add(9, BID, 2449, 100, 44));
    for (SessionStatus code : {SessionStatus::HALT, SessionStatus::AUCTION, SessionStatus::CLOSE}) {
        b.apply(mk(b.last_sequence() + 1, STATUS, 0, 0, static_cast<std::int64_t>(code)));
        EXPECT_EQ(b.status(), static_cast<std::int64_t>(code));
    }
    burst(b, b.last_sequence() + 1, BURST);
    EXPECT_FALSE(b.stale());
    EXPECT_EQ(b.status(), static_cast<std::int64_t>(SessionStatus::CLOSE));
    b.apply(add(b.last_sequence() + 1, BID, 2452, 100, 45));
    EXPECT_EQ(*b.best_bid(), LE(2452, 100));
    EXPECT_TRUE(b.is_crossed());
    EXPECT_EQ(b.order_count_total(), 4u);
}
