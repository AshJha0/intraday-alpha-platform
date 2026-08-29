// ReplayEngine unit tests (API_CORE.md section 5).

#include <gtest/gtest.h>

#include <cstdint>
#include <vector>

#include "iap/marketdata/events.hpp"
#include "iap/replay/replay.hpp"

using iap::EventType;
using iap::MarketEvent;
using iap::ReplayEngine;

namespace {

constexpr std::uint8_t ADD = static_cast<std::uint8_t>(EventType::ADD);
constexpr std::uint8_t TRADE = static_cast<std::uint8_t>(EventType::TRADE);

// Small multi-instrument, multi-venue stream (event-time ordered).
std::vector<MarketEvent> sample_stream(int n) {
    std::vector<MarketEvent> out;
    std::uint64_t eid = 0;
    std::uint64_t seq[2][2] = {{0, 0}, {0, 0}};
    for (int i = 0; i < n; ++i) {
        std::uint32_t iid = (i % 3 == 0) ? 2u : 1u;
        std::uint16_t vid = (i % 2 == 0) ? 1u : 2u;
        std::uint64_t s = ++seq[iid - 1][vid - 1];
        ++eid;
        std::int64_t ts = 1000 + i;
        if (i % 5 == 4) {
            out.push_back(MarketEvent::of(eid, iid, vid, ts, ts + 10, s, TRADE,
                                          static_cast<std::uint8_t>(i % 2),
                                          100, 10 + i, 0, eid));
        } else {
            out.push_back(MarketEvent::of(eid, iid, vid, ts, ts + 10, s, ADD,
                                          static_cast<std::uint8_t>(i % 2),
                                          100 + (i % 7) - 3, 10 + i, 1000 + eid,
                                          0));
        }
    }
    return out;
}

}  // namespace

TEST(Replay, RoutesByInstrumentAndVenue) {
    ReplayEngine engine;
    auto events = sample_stream(30);
    auto summary = engine.run(events);
    EXPECT_EQ(summary.events_processed, 30u);
    EXPECT_EQ(summary.instruments, 2u);
    EXPECT_EQ(summary.time_regressions, 0u);
    const auto& books = engine.books();
    ASSERT_TRUE(books.count(1));
    ASSERT_TRUE(books.count(2));
    EXPECT_EQ(books.at(1).books().size(), 2u);  // venues 1 and 2
}

TEST(Replay, SnapshotEveryEmitsWithIndex) {
    ReplayEngine engine(0, 10);
    auto events = sample_stream(35);
    std::vector<std::uint64_t> seen;
    auto summary = engine.run(
        events, [&](std::uint64_t idx, const iap::ReplaySnapshot& snap) {
            seen.push_back(idx);
            EXPECT_EQ(snap.index, idx);
            EXPECT_FALSE(snap.states.empty());
        });
    EXPECT_EQ(summary.snapshots, 3u);
    ASSERT_EQ(engine.snapshots().size(), 3u);
    EXPECT_EQ(seen, (std::vector<std::uint64_t>{10, 20, 30}));
    EXPECT_EQ(engine.snapshots()[2].index, 30u);
}

TEST(Replay, CheckpointEveryKeepsLatestN) {
    ReplayEngine engine(5, 0, 3);
    auto events = sample_stream(40);
    engine.run(events);
    ASSERT_EQ(engine.checkpoints().size(), 3u);  // 8 taken, latest 3 kept
    EXPECT_EQ(engine.checkpoints()[0].events_processed, 30u);
    EXPECT_EQ(engine.checkpoints()[1].events_processed, 35u);
    EXPECT_EQ(engine.checkpoints()[2].events_processed, 40u);
}

TEST(Replay, TimeRegressionsCounted) {
    ReplayEngine engine;
    auto events = sample_stream(4);
    events[2].exchange_ts = events[1].exchange_ts - 100;  // regression
    events[2].receive_ts = events[2].exchange_ts + 1;
    engine.run(events);
    EXPECT_EQ(engine.time_regressions(), 1u);
}

TEST(Replay, DeterministicAcrossRuns) {
    auto events = sample_stream(60);
    ReplayEngine a, b;
    a.run(events);
    b.run(events);
    EXPECT_EQ(a.book_states(), b.book_states());
    EXPECT_EQ(a.checkpoint(), b.checkpoint());
}

TEST(Replay, CheckpointRestartEquivalence) {
    auto events = sample_stream(50);
    // One-pass run.
    ReplayEngine full;
    full.run(events);
    // Prefix -> checkpoint -> restore -> remainder.
    ReplayEngine prefix;
    std::vector<MarketEvent> head(events.begin(), events.begin() + 23);
    std::vector<MarketEvent> tail(events.begin() + 23, events.end());
    prefix.run(head);
    ReplayEngine resumed = ReplayEngine::restore(prefix.checkpoint());
    resumed.run(tail);
    EXPECT_EQ(resumed.events_processed(), full.events_processed());
    EXPECT_EQ(resumed.book_states(), full.book_states());
    EXPECT_EQ(resumed.checkpoint(), full.checkpoint());
}

TEST(Replay, RestorePreservesConfigAndCounters) {
    ReplayEngine engine(7, 9);
    auto events = sample_stream(20);
    engine.run(events);
    auto cp = engine.checkpoint();
    EXPECT_EQ(cp.checkpoint_every, 7u);
    EXPECT_EQ(cp.snapshot_every, 9u);
    ReplayEngine restored = ReplayEngine::restore(cp);
    EXPECT_EQ(restored.events_processed(), engine.events_processed());
    EXPECT_EQ(restored.checkpoint(), cp);
}

TEST(Replay, BookStatesSortedAndComplete) {
    ReplayEngine engine;
    engine.run(sample_stream(30));
    auto states = engine.book_states();
    ASSERT_EQ(states.size(), 2u);
    auto it = states.begin();
    EXPECT_EQ(it->first, 1u);  // instrument keys ascending
    ++it;
    EXPECT_EQ(it->first, 2u);
    for (const auto& [iid, venues] : states) {
        (void)iid;
        EXPECT_FALSE(venues.empty());
    }
}
