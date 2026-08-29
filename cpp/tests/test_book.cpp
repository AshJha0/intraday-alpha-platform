// Order book unit tests: every pinned semantic and error path
// (PLATFORM_CONVENTIONS.md section 4; API_CORE.md section 4).

#include <gtest/gtest.h>

#include <cstdint>
#include <stdexcept>
#include <unordered_map>

#include "iap/marketdata/events.hpp"
#include "iap/marketdata/rng.hpp"
#include "iap/orderbook/book.hpp"
#include "iap/orderbook/order_index.hpp"

using iap::BookCheckpoint;
using iap::ConsolidatedBook;
using iap::EventType;
using iap::MarketEvent;
using iap::OrderBook;
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
constexpr std::uint8_t HEARTBEAT =
    static_cast<std::uint8_t>(EventType::HEARTBEAT);
constexpr std::uint8_t BID = 0;
constexpr std::uint8_t ASK = 1;

// (price, size) literal — avoids commas inside EXPECT_EQ macro arguments.
iap::LevelEntry LE(std::int64_t price, std::int64_t value) {
    return {price, value};
}

// Sequence-stamped event builder for one instrument/venue stream.
struct Stream {
    std::uint64_t seq = 0;
    std::uint64_t eid = 0;
    std::uint32_t instrument = 1;
    std::uint16_t venue = 1;

    MarketEvent ev(std::uint8_t type, std::uint8_t side, std::int64_t price,
                   std::int64_t qty, std::uint64_t order_id,
                   std::uint64_t trade_id = 0) {
        ++seq;
        ++eid;
        return MarketEvent::of(eid, instrument, venue,
                               static_cast<std::int64_t>(1000 + seq),
                               static_cast<std::int64_t>(1100 + seq), seq,
                               type, side, price, qty, order_id, trade_id);
    }
    // Same as ev() but with an explicit sequence (for gap/duplicate tests).
    MarketEvent ev_seq(std::uint64_t sequence, std::uint8_t type,
                       std::uint8_t side, std::int64_t price, std::int64_t qty,
                       std::uint64_t order_id, std::uint64_t trade_id = 0) {
        seq = sequence;
        ++eid;
        return MarketEvent::of(eid, instrument, venue,
                               static_cast<std::int64_t>(1000 + seq),
                               static_cast<std::int64_t>(1100 + seq), seq,
                               type, side, price, qty, order_id, trade_id);
    }
};

}  // namespace

// ------------------------------------------------------------------- basics

TEST(Book, AddCreatesLevelsAndBest) {
    OrderBook book(1, 1);
    Stream s;
    book.apply(s.ev(ADD, BID, 100, 10, 1));
    book.apply(s.ev(ADD, BID, 99, 20, 2));
    book.apply(s.ev(ADD, ASK, 102, 30, 3));
    ASSERT_TRUE(book.best_bid().has_value());
    EXPECT_EQ(*book.best_bid(), LE(100, 10));
    ASSERT_TRUE(book.best_ask().has_value());
    EXPECT_EQ(*book.best_ask(), LE(102, 30));
    EXPECT_EQ(book.order_count_total(), 3u);
    EXPECT_EQ(book.counters().events_applied, 3u);
}

TEST(Book, EmptyBookHasNoBest) {
    OrderBook book(1, 1);
    EXPECT_FALSE(book.best_bid().has_value());
    EXPECT_FALSE(book.best_ask().has_value());
    EXPECT_TRUE(book.depth(Side::BID).empty());
    EXPECT_TRUE(book.order_count(Side::ASK).empty());
}

TEST(Book, FifoAggregationAtLevel) {
    OrderBook book(1, 1);
    Stream s;
    book.apply(s.ev(ADD, BID, 100, 10, 1));
    book.apply(s.ev(ADD, BID, 100, 20, 2));
    book.apply(s.ev(ADD, BID, 100, 30, 3));
    EXPECT_EQ(*book.best_bid(), LE(100, 60));
    auto oc = book.order_count(Side::BID);
    ASSERT_EQ(oc.size(), 1u);
    EXPECT_EQ(oc[0].second, 3);
    // EXECUTE against the head (order 1) leaves 2 and 3.
    book.apply(s.ev(EXECUTE, BID, 100, 10, 1));
    EXPECT_EQ(*book.best_bid(), LE(100, 50));
    EXPECT_EQ(book.order_count(Side::BID)[0].second, 2);
}

TEST(Book, DuplicateAddOrderIdDroppedAndCounted) {
    OrderBook book(1, 1);
    Stream s;
    book.apply(s.ev(ADD, BID, 100, 10, 1));
    book.apply(s.ev(ADD, BID, 101, 25, 1));  // same order_id
    EXPECT_EQ(book.counters().unknown_order_events, 1u);
    EXPECT_EQ(*book.best_bid(), LE(100, 10));
    EXPECT_EQ(book.order_count_total(), 1u);
    // The dropped ADD still advanced sequence and counted as applied.
    EXPECT_EQ(book.last_sequence(), 2u);
    EXPECT_EQ(book.counters().events_applied, 2u);
}

// ------------------------------------------------------------------- MODIFY

TEST(Book, ModifyDecreaseKeepsQueuePosition) {
    OrderBook book(1, 1);
    Stream s;
    book.apply(s.ev(ADD, BID, 100, 10, 1));
    book.apply(s.ev(ADD, BID, 100, 20, 2));
    book.apply(s.ev(MODIFY, BID, 100, 5, 1));  // decrease head
    EXPECT_EQ(*book.best_bid(), LE(100, 25));
    // Head is still order 1: an EXECUTE of 5 removes it entirely.
    book.apply(s.ev(EXECUTE, BID, 100, 5, 1));
    auto cp = book.checkpoint();
    ASSERT_EQ(cp.levels.size(), 1u);
    ASSERT_EQ(cp.levels[0].orders.size(), 1u);
    EXPECT_EQ(cp.levels[0].orders[0].first, 2u);
}

TEST(Book, ModifyIncreaseMovesToTail) {
    OrderBook book(1, 1);
    Stream s;
    book.apply(s.ev(ADD, BID, 100, 10, 1));
    book.apply(s.ev(ADD, BID, 100, 20, 2));
    book.apply(s.ev(MODIFY, BID, 100, 15, 1));  // increase: 1 moves behind 2
    EXPECT_EQ(*book.best_bid(), LE(100, 35));
    auto cp = book.checkpoint();
    ASSERT_EQ(cp.levels.size(), 1u);
    ASSERT_EQ(cp.levels[0].orders.size(), 2u);
    EXPECT_EQ(cp.levels[0].orders[0].first, 2u);  // FIFO head is now 2
    EXPECT_EQ(cp.levels[0].orders[1].first, 1u);
    EXPECT_EQ(cp.levels[0].orders[1].second, 15);
}

TEST(Book, ModifyPriceFieldIgnored) {
    OrderBook book(1, 1);
    Stream s;
    book.apply(s.ev(ADD, BID, 100, 10, 1));
    book.apply(s.ev(MODIFY, BID, 999, 8, 1));  // qty change only
    EXPECT_EQ(*book.best_bid(), LE(100, 8));
    EXPECT_TRUE(book.depth(Side::BID, 10).size() == 1);
}

TEST(Book, ModifyToNonPositiveRemoves) {
    OrderBook book(1, 1);
    Stream s;
    book.apply(s.ev(ADD, BID, 100, 10, 1));
    book.apply(s.ev(MODIFY, BID, 100, 0, 1));
    EXPECT_FALSE(book.best_bid().has_value());
    EXPECT_EQ(book.order_count_total(), 0u);
}

TEST(Book, ModifyUnknownOrderCounted) {
    OrderBook book(1, 1);
    Stream s;
    book.apply(s.ev(MODIFY, BID, 100, 10, 77));
    EXPECT_EQ(book.counters().unknown_order_events, 1u);
    EXPECT_FALSE(book.best_bid().has_value());
}

// ------------------------------------------------------------------- CANCEL

TEST(Book, CancelRemovesAndEmptiesLevel) {
    OrderBook book(1, 1);
    Stream s;
    book.apply(s.ev(ADD, ASK, 102, 30, 1));
    book.apply(s.ev(ADD, ASK, 103, 40, 2));
    book.apply(s.ev(CANCEL, ASK, 0, 0, 1));
    EXPECT_EQ(*book.best_ask(), LE(103, 40));
    book.apply(s.ev(CANCEL, ASK, 0, 0, 2));
    EXPECT_FALSE(book.best_ask().has_value());
}

TEST(Book, CancelUnknownOrderCounted) {
    OrderBook book(1, 1);
    Stream s;
    book.apply(s.ev(CANCEL, BID, 0, 0, 5));
    EXPECT_EQ(book.counters().unknown_order_events, 1u);
}

// ------------------------------------------------------------------ EXECUTE

TEST(Book, ExecutePartialKeepsPositionFullRemoves) {
    OrderBook book(1, 1);
    Stream s;
    book.apply(s.ev(ADD, BID, 100, 10, 1));
    book.apply(s.ev(ADD, BID, 100, 20, 2));
    book.apply(s.ev(EXECUTE, BID, 100, 4, 1));  // partial
    EXPECT_EQ(*book.best_bid(), LE(100, 26));
    auto cp = book.checkpoint();
    EXPECT_EQ(cp.levels[0].orders[0].first, 1u);  // still at head
    EXPECT_EQ(cp.levels[0].orders[0].second, 6);
    book.apply(s.ev(EXECUTE, BID, 100, 6, 1));  // full
    EXPECT_EQ(*book.best_bid(), LE(100, 20));
    EXPECT_EQ(book.order_count_total(), 1u);
}

TEST(Book, ExecuteOverfillClampsToOrderQty) {
    OrderBook book(1, 1);
    Stream s;
    book.apply(s.ev(ADD, BID, 100, 10, 1));
    book.apply(s.ev(EXECUTE, BID, 100, 999, 1));
    EXPECT_FALSE(book.best_bid().has_value());
}

TEST(Book, ExecuteDoesNotTouchTradeFlow) {
    OrderBook book(1, 1);
    Stream s;
    book.apply(s.ev(ADD, BID, 100, 10, 1));
    book.apply(s.ev(EXECUTE, BID, 100, 10, 1));
    EXPECT_EQ(book.trade_flow(), 0);
}

TEST(Book, ExecuteUnknownOrderCounted) {
    OrderBook book(1, 1);
    Stream s;
    book.apply(s.ev(EXECUTE, BID, 100, 10, 9));
    EXPECT_EQ(book.counters().unknown_order_events, 1u);
}

// -------------------------------------------------------------------- TRADE

TEST(Book, TradeFlowSignedByAggressorSide) {
    OrderBook book(1, 1);
    Stream s;
    book.apply(s.ev(ADD, BID, 100, 10, 1));
    book.apply(s.ev(TRADE, BID, 100, 300, 0, 71));
    EXPECT_EQ(book.trade_flow(), 300);
    book.apply(s.ev(TRADE, ASK, 100, 120, 0, 72));
    EXPECT_EQ(book.trade_flow(), 180);
    // Book levels untouched.
    EXPECT_EQ(*book.best_bid(), LE(100, 10));
}

// ---------------------------------------------------------- marketable ADDs

TEST(Book, MarketableAddFullyFilledDoesNotPost) {
    OrderBook book(1, 1);
    Stream s;
    book.apply(s.ev(ADD, ASK, 102, 30, 1));
    book.apply(s.ev(ADD, BID, 102, 30, 2));  // crosses, exact fill
    EXPECT_FALSE(book.best_ask().has_value());
    EXPECT_FALSE(book.best_bid().has_value());
    EXPECT_EQ(book.order_count_total(), 0u);
}

TEST(Book, MarketableAddPartialHeadFill) {
    OrderBook book(1, 1);
    Stream s;
    book.apply(s.ev(ADD, ASK, 102, 30, 1));
    book.apply(s.ev(ADD, ASK, 102, 40, 2));
    book.apply(s.ev(ADD, BID, 102, 10, 3));  // fills 10 of head order 1
    EXPECT_EQ(*book.best_ask(), LE(102, 60));
    EXPECT_FALSE(book.best_bid().has_value());
    auto cp = book.checkpoint();
    EXPECT_EQ(cp.levels[0].orders[0].first, 1u);
    EXPECT_EQ(cp.levels[0].orders[0].second, 20);  // head reduced in place
}

TEST(Book, MarketableAddSweepsLevelsAndPostsLeftover) {
    OrderBook book(1, 1);
    Stream s;
    book.apply(s.ev(ADD, ASK, 102, 30, 1));
    book.apply(s.ev(ADD, ASK, 103, 20, 2));
    book.apply(s.ev(ADD, ASK, 105, 50, 3));
    book.apply(s.ev(ADD, BID, 103, 60, 4));  // sweeps 102(30)+103(20), 10 left
    EXPECT_EQ(*book.best_ask(), LE(105, 50));
    ASSERT_TRUE(book.best_bid().has_value());
    EXPECT_EQ(*book.best_bid(), LE(103, 10));
    EXPECT_EQ(book.order_count_total(), 2u);
}

TEST(Book, MarketableAskAddCrossesBids) {
    OrderBook book(1, 1);
    Stream s;
    book.apply(s.ev(ADD, BID, 100, 25, 1));
    book.apply(s.ev(ADD, ASK, 99, 40, 2));  // sells through the bid
    EXPECT_FALSE(book.best_bid().has_value());
    EXPECT_EQ(*book.best_ask(), LE(99, 15));
}

// -------------------------------------------------------------------- QUOTE

TEST(Book, QuoteReplacesWholeSideAtL1) {
    OrderBook book(101, 10);
    Stream s;
    s.instrument = 101;
    s.venue = 10;
    book.apply(s.ev(QUOTE, BID, 108649, 20, 1001));
    book.apply(s.ev(QUOTE, BID, 108650, 15, 1002));  // replaces the bid side
    EXPECT_EQ(*book.best_bid(),
              LE(108650, 15));
    EXPECT_EQ(book.depth(Side::BID).size(), 1u);
    // Ask side untouched by bid quotes.
    book.apply(s.ev(QUOTE, ASK, 108655, 10, 1003));
    book.apply(s.ev(QUOTE, BID, 108648, 9, 1004));
    EXPECT_EQ(*book.best_ask(),
              LE(108655, 10));
    EXPECT_EQ(book.order_count_total(), 2u);
}

// ---------------------------------------------------- sequencing / staleness

TEST(Book, DuplicateSequenceDroppedNoStateChange) {
    OrderBook book(1, 1);
    Stream s;
    book.apply(s.ev(ADD, BID, 100, 10, 1));
    MarketEvent dup = s.ev_seq(1, ADD, BID, 101, 5, 2);  // sequence <= last
    book.apply(dup);
    EXPECT_EQ(book.counters().duplicates_dropped, 1u);
    EXPECT_EQ(book.last_sequence(), 1u);
    EXPECT_EQ(*book.best_bid(), LE(100, 10));
    EXPECT_EQ(book.counters().events_applied, 1u);
}

TEST(Book, GapMarksStaleAndDropsBookEvents) {
    OrderBook book(1, 1);
    Stream s;
    book.apply(s.ev(ADD, BID, 100, 10, 1));  // seq 1
    book.apply(s.ev_seq(5, ADD, BID, 101, 5, 2));  // gap
    EXPECT_TRUE(book.stale());
    EXPECT_EQ(book.counters().gaps_detected, 1u);
    EXPECT_EQ(book.counters().dropped_while_stale, 1u);
    EXPECT_EQ(book.last_sequence(), 5u);  // sequence still advances
    EXPECT_EQ(*book.best_bid(), LE(100, 10));
    // Further book events keep dropping while stale.
    book.apply(s.ev(CANCEL, BID, 0, 0, 1));
    EXPECT_EQ(book.counters().dropped_while_stale, 2u);
    EXPECT_TRUE(book.best_bid().has_value());
}

TEST(Book, FirstEventWithLargeSequenceIsNotAGap) {
    OrderBook book(1, 1);
    Stream s;
    book.apply(s.ev_seq(1000, ADD, BID, 100, 10, 1));  // last_sequence was 0
    EXPECT_FALSE(book.stale());
    EXPECT_EQ(book.counters().gaps_detected, 0u);
    EXPECT_TRUE(book.best_bid().has_value());
}

TEST(Book, TradeStatusHeartbeatApplyWhileStale) {
    OrderBook book(1, 1);
    Stream s;
    book.apply(s.ev(ADD, BID, 100, 10, 1));
    book.apply(s.ev_seq(10, HEARTBEAT, 0, 0, 0, 0));  // gap -> stale
    ASSERT_TRUE(book.stale());
    book.apply(s.ev(TRADE, BID, 100, 50, 0, 9));
    EXPECT_EQ(book.trade_flow(), 50);
    book.apply(s.ev(STATUS, 0, 0, static_cast<std::int64_t>(SessionStatus::HALT), 0));
    EXPECT_EQ(book.status(), static_cast<std::int64_t>(SessionStatus::HALT));
    book.apply(s.ev(HEARTBEAT, 0, 0, 0, 0));
    EXPECT_EQ(book.last_sequence(), 13u);
    EXPECT_TRUE(book.stale());  // only a SNAPSHOT burst clears staleness
}

TEST(Book, SnapshotBurstRebuildsAndClearsStale) {
    OrderBook book(1, 1);
    Stream s;
    book.apply(s.ev(ADD, BID, 100, 10, 1));
    book.apply(s.ev(ADD, ASK, 105, 5, 2));
    book.apply(s.ev_seq(10, ADD, BID, 99, 1, 3));  // gap -> stale, dropped
    ASSERT_TRUE(book.stale());
    // SNAPSHOT burst: bids then asks, best->worst, trade_id = remaining.
    book.apply(s.ev(SNAPSHOT, BID, 101, 7, 11, 2));
    EXPECT_TRUE(book.stale());  // not recovered until the burst completes
    book.apply(s.ev(SNAPSHOT, BID, 100, 3, 12, 1));
    book.apply(s.ev(SNAPSHOT, ASK, 103, 4, 13, 0));  // last record
    EXPECT_FALSE(book.stale());
    // Pre-gap book state was fully replaced by the burst.
    EXPECT_EQ(*book.best_bid(), LE(101, 7));
    EXPECT_EQ(*book.best_ask(), LE(103, 4));
    EXPECT_EQ(book.order_count_total(), 3u);
    // Normal flow resumes.
    book.apply(s.ev(ADD, BID, 102, 2, 14));
    EXPECT_EQ(*book.best_bid(), LE(102, 2));
}

TEST(Book, MidBurstGapMarksBurstBroken) {
    // Broken-SNAPSHOT-burst rule (conventions section 4, pinned): a gap
    // inside an active burst marks it broken; the burst still ends at its
    // trade_id==0 record but does NOT clear stale. Only a later complete
    // gap-free burst recovers.
    OrderBook book(1, 1);
    Stream s;
    book.apply(s.ev(ADD, BID, 100, 10, 1));
    // Gap + burst start in one record: the burst itself is NOT broken (the
    // gap arrived before it became active).
    book.apply(s.ev_seq(5, SNAPSHOT, BID, 101, 7, 11, 1));
    EXPECT_TRUE(book.stale());
    // Gap in the middle of the burst: burst broken; its terminator ends the
    // burst but leaves stale set.
    book.apply(s.ev_seq(9, SNAPSHOT, BID, 100, 3, 12, 0));
    EXPECT_TRUE(book.stale());  // broken burst must NOT clear stale
    EXPECT_EQ(book.counters().gaps_detected, 2u);
    // Burst records were still applied (SNAPSHOT applies while stale).
    EXPECT_EQ(*book.best_bid(), LE(101, 7));
    EXPECT_EQ(book.order_count_total(), 2u);
    // Book events stay blocked until a complete burst arrives.
    book.apply(s.ev(ADD, BID, 102, 2, 13));
    EXPECT_EQ(book.counters().dropped_while_stale, 1u);
    // A subsequent complete burst with no interior gap recovers.
    book.apply(s.ev(SNAPSHOT, BID, 103, 5, 21, 2));
    book.apply(s.ev(SNAPSHOT, BID, 102, 4, 22, 1));
    book.apply(s.ev(SNAPSHOT, ASK, 105, 6, 23, 0));
    EXPECT_FALSE(book.stale());
    EXPECT_EQ(*book.best_bid(), LE(103, 5));
    EXPECT_EQ(*book.best_ask(), LE(105, 6));
    EXPECT_EQ(book.order_count_total(), 3u);
    // Normal flow resumes.
    book.apply(s.ev(ADD, BID, 104, 9, 24));
    EXPECT_EQ(*book.best_bid(), LE(104, 9));
}

TEST(Book, BrokenBurstStateSurvivesCheckpointRestore) {
    OrderBook book(1, 1);
    Stream s;
    book.apply(s.ev(ADD, BID, 100, 10, 1));
    book.apply(s.ev_seq(9, SNAPSHOT, BID, 101, 500, 101, 2));   // gap + start
    book.apply(s.ev_seq(11, SNAPSHOT, BID, 100, 400, 102, 1));  // gap inside
    auto cp = book.checkpoint();
    EXPECT_TRUE(cp.stale);
    EXPECT_TRUE(cp.snapshot_active);
    EXPECT_TRUE(cp.snapshot_broken);
    OrderBook restored = OrderBook::restore(cp);
    MarketEvent term = s.ev(SNAPSHOT, ASK, 102, 600, 103, 0);
    book.apply(term);
    restored.apply(term);
    EXPECT_TRUE(book.stale());  // broken burst: terminator kept stale
    EXPECT_TRUE(restored.stale());
    EXPECT_EQ(book.checkpoint(), restored.checkpoint());
}

// ---------------------------------------------------------- side domain

TEST(Book, InvalidSideDroppedAndCountedNoCorruption) {
    // side > 1 on side-indexed types (ADD/QUOTE/SNAPSHOT/TRADE) => dropped +
    // counted via `invalid_side_dropped`, never raised, after the sequence
    // number is consumed (conventions section 4, pinned).
    OrderBook book(1, 1);
    Stream s;
    book.apply(s.ev(ADD, BID, 100, 10, 1));
    book.apply(s.ev(ADD, ASK, 105, 5, 2));
    const auto cp_before = book.checkpoint();
    book.apply(s.ev(ADD, 9, 99, 100, 77));
    book.apply(s.ev(QUOTE, 5, 101, 10, 78));
    book.apply(s.ev(TRADE, 9, 102, 30, 0, 9));
    book.apply(s.ev(SNAPSHOT, 3, 102, 30, 79, 0));
    EXPECT_EQ(book.counters().invalid_side_dropped, 4u);
    // No corruption: book state unchanged apart from sequence/timestamps
    // and the counter (the OOB side never reaches side-indexed state).
    const auto cp_after = book.checkpoint();
    EXPECT_EQ(cp_after.levels, cp_before.levels);
    EXPECT_EQ(cp_after.arrival_order, cp_before.arrival_order);
    EXPECT_EQ(cp_after.trade_flow, 0);
    EXPECT_EQ(cp_after.status, cp_before.status);
    EXPECT_FALSE(cp_after.stale);
    EXPECT_FALSE(cp_after.snapshot_active);  // dropped SNAPSHOT: no burst
    EXPECT_FALSE(cp_after.snapshot_broken);
    EXPECT_EQ(book.counters().events_applied, 2u);  // drops never applied
    // The sequence numbers were consumed: the next in-order event applies
    // cleanly with no gap.
    book.apply(s.ev(ADD, BID, 100, 50, 80));
    EXPECT_EQ(book.counters().gaps_detected, 0u);
    EXPECT_FALSE(book.stale());
    EXPECT_EQ(*book.best_bid(), LE(100, 60));
    // MODIFY/CANCEL/EXECUTE address by order_id (not side-indexed): a bogus
    // side field does not block them.
    book.apply(s.ev(MODIFY, 9, 100, 75, 80));
    EXPECT_EQ(*book.best_bid(), LE(100, 85));
    EXPECT_EQ(book.counters().invalid_side_dropped, 4u);
}

TEST(Book, InvalidSideCountedBeforeStaleDrop) {
    // The side-domain drop happens on the validation path BEFORE the
    // while-stale drop (mirrors the Python reference's check order).
    OrderBook book(1, 1);
    Stream s;
    book.apply(s.ev(ADD, BID, 100, 10, 1));
    book.apply(s.ev_seq(5, HEARTBEAT, 0, 0, 0, 0));  // gap -> stale
    ASSERT_TRUE(book.stale());
    book.apply(s.ev(ADD, 7, 101, 5, 2));
    EXPECT_EQ(book.counters().invalid_side_dropped, 1u);
    EXPECT_EQ(book.counters().dropped_while_stale, 0u);
}

TEST(Book, StatusStoredFromQty) {
    OrderBook book(1, 1);
    Stream s;
    EXPECT_EQ(book.status(), static_cast<std::int64_t>(SessionStatus::TRADING));
    book.apply(s.ev(STATUS, 0, 0, static_cast<std::int64_t>(SessionStatus::AUCTION), 0));
    EXPECT_EQ(book.status(), static_cast<std::int64_t>(SessionStatus::AUCTION));
}

TEST(Book, HeartbeatUpdatesSequenceAndTimestampsOnly) {
    OrderBook book(1, 1);
    Stream s;
    book.apply(s.ev(ADD, BID, 100, 10, 1));
    MarketEvent hb = s.ev(HEARTBEAT, 0, 0, 0, 0);
    book.apply(hb);
    EXPECT_EQ(book.last_sequence(), hb.sequence);
    EXPECT_EQ(book.exchange_ts(), hb.exchange_ts);
    EXPECT_EQ(book.receive_ts(), hb.receive_ts);
    EXPECT_EQ(*book.best_bid(), LE(100, 10));
}

TEST(Book, WrongRoutingThrows) {
    OrderBook book(1, 1);
    Stream s;
    s.instrument = 2;
    EXPECT_THROW(book.apply(s.ev(ADD, BID, 100, 10, 1)), std::invalid_argument);
    Stream s2;
    s2.venue = 9;
    EXPECT_THROW(book.apply(s2.ev(ADD, BID, 100, 10, 1)), std::invalid_argument);
    // venue_id == 0 books accept any venue.
    OrderBook synth(1, 0);
    Stream s3;
    s3.venue = 42;
    EXPECT_NO_THROW(synth.apply(s3.ev(ADD, BID, 100, 10, 1)));
}

// -------------------------------------------------------------- depth views

TEST(Book, DepthTopNBestFirstAndCapped) {
    OrderBook book(1, 1);
    Stream s;
    for (int i = 0; i < 15; ++i) {
        book.apply(s.ev(ADD, BID, 100 - i, 10 + i, static_cast<std::uint64_t>(i + 1)));
        book.apply(s.ev(ADD, ASK, 110 + i, 20 + i, static_cast<std::uint64_t>(100 + i)));
    }
    auto bids = book.depth(Side::BID);
    auto asks = book.depth(Side::ASK);
    ASSERT_EQ(bids.size(), 10u);  // top-10 default
    ASSERT_EQ(asks.size(), 10u);
    EXPECT_EQ(bids[0].first, 100);
    EXPECT_EQ(bids[9].first, 91);
    EXPECT_EQ(asks[0].first, 110);
    EXPECT_EQ(asks[9].first, 119);
    for (std::size_t i = 1; i < bids.size(); ++i) {
        EXPECT_LT(bids[i].first, bids[i - 1].first);
        EXPECT_GT(asks[i].first, asks[i - 1].first);
    }
    EXPECT_EQ(book.depth(Side::BID, 3).size(), 3u);
    EXPECT_EQ(book.order_count(Side::ASK, 4).size(), 4u);
}

// -------------------------------------------------------------- checkpoints

TEST(Book, CheckpointRestoreRoundTrip) {
    OrderBook book(1, 1);
    Stream s;
    book.apply(s.ev(ADD, BID, 100, 10, 1));
    book.apply(s.ev(ADD, BID, 100, 20, 2));
    book.apply(s.ev(ADD, ASK, 105, 5, 3));
    book.apply(s.ev(TRADE, BID, 100, 40, 0, 5));
    book.apply(s.ev(MODIFY, BID, 100, 30, 1));  // move to tail
    BookCheckpoint cp = book.checkpoint();
    OrderBook restored = OrderBook::restore(cp);
    EXPECT_EQ(restored.checkpoint(), cp);
    EXPECT_EQ(restored.state_summary(), book.state_summary());
    // Subsequent behavior identical: same event applied to both.
    MarketEvent next = s.ev(EXECUTE, BID, 100, 20, 2);
    book.apply(next);
    restored.apply(next);
    EXPECT_EQ(restored.checkpoint(), book.checkpoint());
}

TEST(Book, CheckpointLevelOrderIsSideThenPriceAscending) {
    OrderBook book(1, 1);
    Stream s;
    book.apply(s.ev(ADD, BID, 100, 1, 1));
    book.apply(s.ev(ADD, BID, 98, 1, 2));
    book.apply(s.ev(ADD, ASK, 105, 1, 3));
    book.apply(s.ev(ADD, ASK, 103, 1, 4));
    auto cp = book.checkpoint();
    ASSERT_EQ(cp.levels.size(), 4u);
    EXPECT_EQ(cp.levels[0].side, 0);
    EXPECT_EQ(cp.levels[0].price_ticks, 98);
    EXPECT_EQ(cp.levels[1].price_ticks, 100);
    EXPECT_EQ(cp.levels[2].side, 1);
    EXPECT_EQ(cp.levels[2].price_ticks, 103);
    EXPECT_EQ(cp.levels[3].price_ticks, 105);
}

TEST(Book, CheckpointPreservesCountersAndFlags) {
    OrderBook book(1, 1);
    Stream s;
    book.apply(s.ev(ADD, BID, 100, 10, 1));
    book.apply(s.ev_seq(1, ADD, BID, 99, 1, 2));   // duplicate
    book.apply(s.ev_seq(9, ADD, BID, 99, 1, 3));   // gap -> stale + dropped
    book.apply(s.ev(CANCEL, BID, 0, 0, 77));       // dropped while stale
    book.apply(s.ev(ADD, 6, 99, 1, 78));           // invalid side dropped
    auto cp = book.checkpoint();
    EXPECT_TRUE(cp.stale);
    EXPECT_EQ(cp.counters.duplicates_dropped, 1u);
    EXPECT_EQ(cp.counters.gaps_detected, 1u);
    EXPECT_EQ(cp.counters.dropped_while_stale, 2u);
    EXPECT_EQ(cp.counters.invalid_side_dropped, 1u);
    OrderBook restored = OrderBook::restore(cp);
    EXPECT_TRUE(restored.stale());
    EXPECT_EQ(restored.counters(), book.counters());
}

TEST(Book, CheckpointArrivalOrderRoundTrip) {
    // Global arrival order differs from per-level FIFO reconstruction order
    // (ask order 2 arrived before bid orders 3 and 4): the checkpoint's
    // arrival_order field must pin it and restore() must rebuild it.
    OrderBook book(1, 1);
    Stream s;
    book.apply(s.ev(ADD, BID, 100, 10, 1));
    book.apply(s.ev(ADD, ASK, 105, 5, 2));
    book.apply(s.ev(ADD, BID, 100, 20, 3));
    book.apply(s.ev(CANCEL, BID, 0, 0, 1));
    book.apply(s.ev(ADD, BID, 100, 30, 4));
    auto cp = book.checkpoint();
    EXPECT_EQ(cp.arrival_order, (std::vector<std::uint64_t>{2, 3, 4}));
    OrderBook restored = OrderBook::restore(cp);
    EXPECT_EQ(restored.checkpoint(), cp);
    auto ro = restored.resting_orders();
    ASSERT_EQ(ro.size(), 3u);
    EXPECT_EQ(ro[0], (iap::RestingOrder{2, ASK, 105, 5}));
    EXPECT_EQ(ro[1], (iap::RestingOrder{3, BID, 100, 20}));
    EXPECT_EQ(ro[2], (iap::RestingOrder{4, BID, 100, 30}));
    // Subsequent behavior identical (arrival order is part of book state).
    MarketEvent next = s.ev(ADD, BID, 101, 7, 5);
    book.apply(next);
    restored.apply(next);
    EXPECT_EQ(restored.checkpoint(), book.checkpoint());
    EXPECT_EQ(restored.checkpoint().arrival_order,
              (std::vector<std::uint64_t>{2, 3, 4, 5}));
}

TEST(Book, RestoreRejectsInconsistentArrivalOrder) {
    OrderBook book(1, 1);
    Stream s;
    book.apply(s.ev(ADD, BID, 100, 10, 1));
    book.apply(s.ev(ADD, ASK, 105, 5, 2));
    auto cp = book.checkpoint();
    auto missing = cp;
    missing.arrival_order.pop_back();
    EXPECT_THROW(OrderBook::restore(missing), std::invalid_argument);
    auto unknown = cp;
    unknown.arrival_order.back() = 999;
    EXPECT_THROW(OrderBook::restore(unknown), std::invalid_argument);
    auto duplicated = cp;
    duplicated.arrival_order.back() = duplicated.arrival_order.front();
    EXPECT_THROW(OrderBook::restore(duplicated), std::invalid_argument);
}

// --------------------------------------------------------- ConsolidatedBook

TEST(Consolidated, MergesDepthAcrossVenues) {
    ConsolidatedBook cons(1);
    Stream v1;
    Stream v2;
    v2.venue = 2;
    cons.apply(v1.ev(ADD, BID, 100, 10, 1));
    cons.apply(v1.ev(ADD, BID, 99, 5, 2));
    cons.apply(v2.ev(ADD, BID, 100, 7, 1));   // same price, other venue
    cons.apply(v2.ev(ADD, ASK, 102, 3, 2));
    ASSERT_TRUE(cons.best_bid().has_value());
    EXPECT_EQ(*cons.best_bid(), LE(100, 17));
    auto d = cons.depth(Side::BID);
    ASSERT_EQ(d.size(), 2u);
    EXPECT_EQ(d[0], LE(100, 17));
    EXPECT_EQ(d[1], LE(99, 5));
    auto oc = cons.order_count(Side::BID);
    EXPECT_EQ(oc[0].second, 2);  // one order per venue at 100
    EXPECT_EQ(*cons.best_ask(), LE(102, 3));
}

TEST(Consolidated, PerVenueSequencesIndependent) {
    ConsolidatedBook cons(1);
    Stream v1;
    Stream v2;
    v2.venue = 2;
    cons.apply(v1.ev(ADD, BID, 100, 10, 1));
    cons.apply(v2.ev(ADD, BID, 101, 5, 1));  // sequence 1 on venue 2: no dup
    EXPECT_EQ(cons.books().at(1).counters().duplicates_dropped, 0u);
    EXPECT_EQ(cons.books().at(2).counters().duplicates_dropped, 0u);
    EXPECT_EQ(*cons.best_bid(), LE(101, 5));
}

TEST(Consolidated, TradeFlowSumsVenues) {
    ConsolidatedBook cons(1);
    Stream v1;
    Stream v2;
    v2.venue = 2;
    cons.apply(v1.ev(TRADE, BID, 100, 30, 0, 1));
    cons.apply(v2.ev(TRADE, ASK, 100, 10, 0, 2));
    EXPECT_EQ(cons.trade_flow(), 20);
}

TEST(Consolidated, CheckpointRestoreRoundTrip) {
    ConsolidatedBook cons(1);
    Stream v1;
    Stream v2;
    v2.venue = 2;
    cons.apply(v1.ev(ADD, BID, 100, 10, 1));
    cons.apply(v2.ev(ADD, ASK, 102, 4, 1));
    auto cp = cons.checkpoint();
    ConsolidatedBook restored = ConsolidatedBook::restore(cp);
    EXPECT_EQ(restored.checkpoint(), cp);
    MarketEvent next = v1.ev(ADD, BID, 101, 2, 2);
    cons.apply(next);
    restored.apply(next);
    EXPECT_EQ(restored.checkpoint(), cons.checkpoint());
}

// ------------------------------------------------- OrderIndex (hash) stress

TEST(OrderIndexTest, MatchesReferenceMapUnderRandomOps) {
    iap::OrderIndex idx;
    std::unordered_map<std::uint64_t, std::uint32_t> ref;
    iap::SplitMix64 rng(20260829);
    for (int i = 0; i < 200000; ++i) {
        std::uint64_t key = static_cast<std::uint64_t>(rng.below(5000)) + 1;
        std::int64_t op = rng.below(3);
        if (op == 0) {
            auto val = static_cast<std::uint32_t>(i);
            idx.upsert(key, val);
            ref[key] = val;
        } else if (op == 1) {
            EXPECT_EQ(idx.erase(key), ref.erase(key) > 0);
        } else {
            auto it = ref.find(key);
            std::uint32_t expected =
                it == ref.end() ? iap::OrderIndex::NPOS : it->second;
            EXPECT_EQ(idx.find(key), expected);
        }
        EXPECT_EQ(idx.size(), ref.size());
    }
    for (const auto& [k, v] : ref) {
        EXPECT_EQ(idx.find(k), v);
    }
}
