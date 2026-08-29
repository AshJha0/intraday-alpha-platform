// Golden suite: cross-language parity against tests/golden/
// (API_CORE.md section 6). All integer comparisons are exact.

#include <gtest/gtest.h>

#include <algorithm>
#include <cstdint>
#include <string>
#include <vector>

#include "golden_util.hpp"
#include "iap/marketdata/codec.hpp"
#include "iap/marketdata/events.hpp"
#include "iap/orderbook/book.hpp"
#include "iap/replay/replay.hpp"
#include "sha256.hpp"

using iap::MarketEvent;
using iap::OrderBook;
using iap::ReplayEngine;
using iap_test::golden_path;
using iap_test::load_golden_json;
using iap_test::read_text_file;
using iap_test::Sha256;

namespace {

const std::vector<MarketEvent>& eq_events() {
    static const std::vector<MarketEvent> events =
        iap::read_jsonl(golden_path("events_eq_mbo.jsonl"));
    return events;
}

const std::vector<MarketEvent>& fx_events() {
    static const std::vector<MarketEvent> events =
        iap::read_jsonl(golden_path("events_fx_quote.jsonl"));
    return events;
}

}  // namespace

TEST(Golden, LoadsEqVector) {
    const auto& events = eq_events();
    ASSERT_EQ(events.size(), 2000u);
    EXPECT_EQ(events.front().event_id, 1u);
    EXPECT_EQ(events.back().event_id, 2000u);
    for (const auto& ev : events) {
        EXPECT_EQ(ev.instrument_id, 1u);
        EXPECT_EQ(ev.venue_id, 1u);
        EXPECT_EQ(iap::validation_error(ev), "");
    }
}

TEST(Golden, LoadsFxVector) {
    const auto& events = fx_events();
    ASSERT_EQ(events.size(), 800u);
    for (const auto& ev : events) {
        EXPECT_EQ(ev.instrument_id, 101u);
        EXPECT_TRUE(ev.venue_id == 10 || ev.venue_id == 11 || ev.venue_id == 12);
        EXPECT_EQ(iap::validation_error(ev), "");
    }
}

TEST(Golden, CodecSha256EqVector) {
    auto expected = load_golden_json("expected_codec_sha256.json");
    auto encoded = iap::encode_iap1(eq_events());
    EXPECT_EQ(Sha256::hash(encoded), expected["events_eq_mbo.iap1"].s());
}

TEST(Golden, CodecSha256FxVector) {
    auto expected = load_golden_json("expected_codec_sha256.json");
    auto encoded = iap::encode_iap1(fx_events());
    EXPECT_EQ(Sha256::hash(encoded), expected["events_fx_quote.iap1"].s());
}

TEST(Golden, Iap1RoundTripBothVectors) {
    for (const auto* vec : {&eq_events(), &fx_events()}) {
        auto decoded = iap::decode_iap1(iap::encode_iap1(*vec));
        ASSERT_EQ(decoded.size(), vec->size());
        for (std::size_t i = 0; i < vec->size(); ++i) {
            ASSERT_EQ(decoded[i], (*vec)[i]) << "record " << i;
        }
    }
}

TEST(Golden, JsonlReencodeIsByteIdentical) {
    // The canonical JSONL encoder must reproduce the golden files exactly.
    EXPECT_EQ(iap::encode_jsonl(eq_events()),
              read_text_file(golden_path("events_eq_mbo.jsonl")));
    EXPECT_EQ(iap::encode_jsonl(fx_events()),
              read_text_file(golden_path("events_fx_quote.jsonl")));
}

TEST(Golden, BookStatesAtPinnedIndices) {
    auto expected = load_golden_json("expected_book_states.json");
    EXPECT_EQ(expected["vector"].s(), "events_eq_mbo.jsonl");
    OrderBook book(
        static_cast<std::uint32_t>(expected["instrument_id"].u64()),
        static_cast<std::uint16_t>(expected["venue_id"].u64()));
    const auto& events = eq_events();
    const auto& states = expected["states"];
    // Sort the pinned indices numerically (JSON object keys are strings).
    std::vector<std::size_t> pins;
    for (const auto& [key, exp] : states.obj) {
        (void)exp;
        pins.push_back(static_cast<std::size_t>(std::stoull(key)));
    }
    std::sort(pins.begin(), pins.end());
    ASSERT_EQ(pins.size(), 5u);
    std::size_t applied = 0;
    for (std::size_t n : pins) {
        ASSERT_LE(n, events.size());
        for (; applied < n; ++applied) {
            book.apply(events[applied]);
        }
        std::string key = std::to_string(n);
        SCOPED_TRACE("after event " + key);
        EXPECT_NO_THROW(iap_test::expect_summary_eq(
            book.state_summary(), states[key], "state[" + key + "]"));
    }
    EXPECT_EQ(applied, 2000u);
}

TEST(Golden, BookStatesViaReplayEngine) {
    auto expected = load_golden_json("expected_book_states.json");
    ReplayEngine engine;
    const auto& events = eq_events();
    for (std::size_t i = 0; i < 2000; ++i) {
        engine.apply(events[i]);
    }
    auto states = engine.book_states();
    const auto& summary = states.at(1).at(1);
    iap_test::expect_summary_eq(summary, expected["states"]["2000"],
                                "replay state[2000]");
}

TEST(Golden, ReplayDeterminism) {
    ReplayEngine a, b;
    a.run(eq_events());
    b.run(eq_events());
    EXPECT_EQ(a.book_states(), b.book_states());
    EXPECT_EQ(a.checkpoint(), b.checkpoint());
    ReplayEngine fa, fb;
    fa.run(fx_events());
    fb.run(fx_events());
    EXPECT_EQ(fa.book_states(), fb.book_states());
    EXPECT_EQ(fa.checkpoint(), fb.checkpoint());
}

TEST(Golden, ReplayCheckpointRestartEquivalenceEq) {
    const auto& events = eq_events();
    ReplayEngine full;
    full.run(events);
    for (std::size_t split : {100u, 999u, 1500u}) {
        ReplayEngine prefix;
        std::vector<MarketEvent> head(events.begin(),
                                      events.begin() + static_cast<long>(split));
        std::vector<MarketEvent> tail(events.begin() + static_cast<long>(split),
                                      events.end());
        prefix.run(head);
        ReplayEngine resumed = ReplayEngine::restore(prefix.checkpoint());
        resumed.run(tail);
        EXPECT_EQ(resumed.checkpoint(), full.checkpoint())
            << "split at " << split;
    }
}

TEST(Golden, ReplayCheckpointRestartEquivalenceFx) {
    const auto& events = fx_events();
    ReplayEngine full;
    full.run(events);
    ReplayEngine prefix;
    std::vector<MarketEvent> head(events.begin(), events.begin() + 400);
    std::vector<MarketEvent> tail(events.begin() + 400, events.end());
    prefix.run(head);
    ReplayEngine resumed = ReplayEngine::restore(prefix.checkpoint());
    resumed.run(tail);
    EXPECT_EQ(resumed.checkpoint(), full.checkpoint());
    EXPECT_EQ(resumed.book_states(), full.book_states());
}

TEST(Golden, FxReplayHasThreeVenuesAndSaneTopOfBook) {
    ReplayEngine engine;
    engine.run(fx_events());
    const auto& cons = engine.books().at(101);
    EXPECT_EQ(cons.books().size(), 3u);  // venues 10, 11, 12
    for (const auto& [vid, book] : cons.books()) {
        (void)vid;
        // QUOTE books keep exactly one order per side once both quoted.
        EXPECT_LE(book.depth(iap::Side::BID).size(), 1u);
        EXPECT_LE(book.depth(iap::Side::ASK).size(), 1u);
        EXPECT_FALSE(book.stale());
    }
    auto bb = cons.best_bid();
    auto ba = cons.best_ask();
    ASSERT_TRUE(bb.has_value());
    ASSERT_TRUE(ba.has_value());
    EXPECT_LT(bb->first, ba->first);  // consolidated book not crossed
}

TEST(Golden, SplitMix64SeedsDocumented) {
    auto g = load_golden_json("splitmix64.json");
    EXPECT_EQ(g["golden_eq_seed"].u64(), 4242424242u);
    EXPECT_EQ(g["golden_fx_seed"].u64(), 8484848484u);
}
