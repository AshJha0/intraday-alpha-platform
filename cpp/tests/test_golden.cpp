// Golden suite: cross-language parity against tests/golden/
// (API_CORE.md section 6). All integer comparisons are exact.

#include <gtest/gtest.h>

#include <algorithm>
#include <cstdint>
#include <map>
#include <optional>
#include <stdexcept>
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

// ------------------------------------------------------- anomaly goldens (round 3)

namespace {

const std::vector<MarketEvent>& anomaly_events(const std::string& name) {
    static std::map<std::string, std::vector<MarketEvent>> cache;
    auto it = cache.find(name);
    if (it == cache.end()) {
        it = cache.emplace(name, iap::read_jsonl(golden_path(name))).first;
    }
    return it->second;
}

void expect_counters_eq(const iap::BookCounters& c, const iap_test::Json& exp,
                        const std::string& what) {
    auto check = [&](const char* key, std::uint64_t got) {
        if (got != exp[key].u64()) {
            throw std::runtime_error(what + "." + key + ": got " +
                                     std::to_string(got) + " expected " +
                                     std::to_string(exp[key].u64()));
        }
    };
    check("duplicates_dropped", c.duplicates_dropped);
    check("gaps_detected", c.gaps_detected);
    check("dropped_while_stale", c.dropped_while_stale);
    check("unknown_order_events", c.unknown_order_events);
    check("invalid_side_dropped", c.invalid_side_dropped);
    check("invalid_payload_dropped", c.invalid_payload_dropped);
    check("unknown_type_dropped", c.unknown_type_dropped);
    check("modify_price_mismatch", c.modify_price_mismatch);
    check("snapshot_restarts", c.snapshot_restarts);
    check("sequence_resets", c.sequence_resets);
    check("late_recovered", c.late_recovered);
    check("events_applied", c.events_applied);
}

void expect_optional_level_eq(const std::optional<iap::LevelEntry>& got,
                              const iap_test::Json& exp, const std::string& what) {
    if (exp.type == iap_test::Json::Type::Null) {
        if (got.has_value()) throw std::runtime_error(what + ": expected none");
        return;
    }
    const auto& row = exp.a();
    if (!got || got->first != row[0].i64() || got->second != row[1].i64()) {
        throw std::runtime_error(what + ": mismatch");
    }
}

void expect_venues_eq(const std::vector<std::uint16_t>& got, const iap_test::Json& exp,
                      const std::string& what) {
    const auto& rows = exp.a();
    if (rows.size() != got.size()) throw std::runtime_error(what + ": size mismatch");
    for (std::size_t i = 0; i < rows.size(); ++i) {
        if (rows[i].u64() != got[i]) throw std::runtime_error(what + ": venue mismatch");
    }
}

void check_anomaly_vector(const std::string& name) {
    auto expected = load_golden_json("expected_anomaly_states.json");
    const iap_test::Json spec = expected["vectors"][name];
    const auto& events = anomaly_events(name);
    ASSERT_EQ(events.size(), spec["events"].u64());
    for (const auto& run : spec["runs"].a()) {
        const std::size_t window = static_cast<std::size_t>(run["reorder_window"].u64());
        iap::ConsolidatedBook cons(
            static_cast<std::uint32_t>(spec["instrument_id"].u64()), window);
        const auto& states = run["states"];
        for (std::size_t i = 1; i <= events.size(); ++i) {
            cons.apply(events[i - 1]);
            const std::string key = std::to_string(i);
            if (!states.has(key)) continue;
            SCOPED_TRACE(name + " window=" + std::to_string(window) + " index " + key);
            const auto& exp = states[key];
            const auto& venues = exp["venues"];
            ASSERT_EQ(venues.obj.size(), cons.books().size());
            for (const auto& [vid, book] : cons.books()) {
                const auto& vexp = venues[std::to_string(vid)];
                const std::string what = "venue " + std::to_string(vid);
                EXPECT_NO_THROW(iap_test::expect_summary_eq(
                    book.state_summary(), vexp["summary"], what + ".summary"));
                EXPECT_NO_THROW(expect_counters_eq(book.counters(), vexp["counters"],
                                                   what + ".counters"));
                EXPECT_EQ(book.stale(), vexp["stale"].boolean) << what;
                EXPECT_EQ(book.status(), vexp["status"].i64()) << what;
                EXPECT_EQ(book.has_sequence(), vexp["has_sequence"].boolean) << what;
                EXPECT_EQ(book.sequence_epoch(), vexp["sequence_epoch"].u64()) << what;
                EXPECT_EQ(book.pending_count(), vexp["pending_count"].u64()) << what;
            }
            const auto& cexp = exp["consolidated"];
            EXPECT_NO_THROW(expect_optional_level_eq(cons.best_bid(), cexp["best_bid"], "best_bid"));
            EXPECT_NO_THROW(expect_optional_level_eq(cons.best_ask(), cexp["best_ask"], "best_ask"));
            EXPECT_NO_THROW(iap_test::expect_levels_eq(cons.depth(iap::Side::BID, 5),
                                                       cexp["depth_bid_top5"], "depth_bid_top5"));
            EXPECT_NO_THROW(iap_test::expect_levels_eq(cons.depth(iap::Side::ASK, 5),
                                                       cexp["depth_ask_top5"], "depth_ask_top5"));
            EXPECT_EQ(cons.is_crossed(), cexp["is_crossed"].boolean);
            EXPECT_EQ(cons.is_locked(), cexp["is_locked"].boolean);
            EXPECT_NO_THROW(expect_venues_eq(cons.active_venues(), cexp["active_venues"],
                                             "active_venues"));
            EXPECT_EQ(cons.trade_flow(), cexp["trade_flow"].i64());
        }
        // Accounting invariant: applied + drops + pending == events fed.
        std::map<std::uint16_t, std::uint64_t> fed;
        for (const auto& ev : events) ++fed[ev.venue_id];
        for (const auto& [vid, book] : cons.books()) {
            EXPECT_EQ(book.counters().events_applied + book.counters().drops() +
                          book.pending_count(),
                      fed[vid]) << "venue " << vid;
        }
    }
}

}  // namespace

TEST(Golden, AnomalyEqVectorStatesAndCounters) {
    check_anomaly_vector("events_eq_anomalies.jsonl");
}

TEST(Golden, AnomalyFxVectorStatesAndCounters) {
    check_anomaly_vector("events_fx_anomalies.jsonl");
}

TEST(Golden, AnomalyVectorsByteStableAndCheckpointRoundTrip) {
    for (const std::string name : {"events_eq_anomalies.jsonl", "events_fx_anomalies.jsonl"}) {
        const std::vector<MarketEvent> events = anomaly_events(name);
        EXPECT_EQ(iap::encode_jsonl(events), read_text_file(golden_path(name)));
        for (std::size_t window : {0u, 4u}) {
            iap::ConsolidatedBook full(events[0].instrument_id, window);
            for (const auto& ev : events) full.apply(ev);
            for (std::size_t split : std::vector<std::size_t>{137, 500, events.size() - 20}) {
                iap::ConsolidatedBook part(events[0].instrument_id, window);
                for (std::size_t i = 0; i < split; ++i) part.apply(events[i]);
                // Round-trip through the JSON codec via a one-instrument engine.
                iap::ReplayCheckpoint ecp;
                ecp.books.emplace(events[0].instrument_id, part.checkpoint());
                auto back = iap::checkpoint_from_json(iap::checkpoint_to_json(ecp));
                iap::ConsolidatedBook resumed = iap::ConsolidatedBook::restore(
                    back.books.at(events[0].instrument_id));
                for (std::size_t i = split; i < events.size(); ++i) resumed.apply(events[i]);
                EXPECT_EQ(resumed.checkpoint(), full.checkpoint())
                    << name << " window " << window << " split " << split;
            }
        }
    }
}

TEST(Golden, CheckpointEq1000InterchangeWithPython) {
    const auto& events = eq_events();
    auto expected = load_golden_json("expected_book_states.json");
    const std::string text = read_text_file(golden_path("expected_checkpoint_eq_1000.json"));
    iap::ReplayCheckpoint cp = iap::checkpoint_from_json(text);
    // Restore Python's checkpoint, replay the rest, reach the golden state.
    ReplayEngine resumed = ReplayEngine::restore(cp);
    EXPECT_EQ(resumed.keep_checkpoints(), 4u);
    std::vector<MarketEvent> tail(events.begin() + 1000, events.end());
    resumed.run(tail);
    iap_test::expect_summary_eq(resumed.book_states().at(1).at(1),
                                expected["states"]["2000"], "restored state[2000]");
    ReplayEngine full;
    full.run(events);
    EXPECT_EQ(resumed.checkpoint(), full.checkpoint());
    // Our own checkpoint at 1000 is structurally identical to Python's.
    ReplayEngine own;
    std::vector<MarketEvent> head(events.begin(), events.begin() + 1000);
    own.run(head);
    EXPECT_EQ(own.checkpoint(), cp);
    EXPECT_EQ(iap::checkpoint_from_json(iap::checkpoint_to_json(own.checkpoint())), cp);
}

TEST(Golden, JsonlRejectCasesFixture) {
    const std::string text = read_text_file(golden_path("jsonl_reject_cases.txt"));
    std::size_t rejected = 0, accepted = 0;
    bool accept_block = false;
    std::size_t start = 0;
    while (start < text.size()) {
        std::size_t nl = text.find('\n', start);
        if (nl == std::string::npos) nl = text.size();
        std::string line = text.substr(start, nl - start);
        start = nl + 1;
        if (line == "# ACCEPT") {
            accept_block = true;
            continue;
        }
        if (line.empty() || line[0] == '#') continue;
        if (accept_block) {
            MarketEvent ev;
            EXPECT_NO_THROW(ev = iap::decode_jsonl_line(line)) << line;
            EXPECT_EQ(iap::decode_jsonl_line(iap::encode_jsonl_line(ev)), ev);
            ++accepted;
        } else {
            EXPECT_THROW(iap::decode_jsonl_line(line), std::invalid_argument) << line;
            ++rejected;
        }
    }
    EXPECT_GE(rejected, 25u);
    EXPECT_GE(accepted, 5u);
}
