// Decision traces emitted by the execution replay (exec_replay.hpp, trace
// section): the golden replay-fills scenario (test_replay_fills.cpp /
// tests/golden/expected_replay_fills.json) traced end to end.
//
//   - one DecisionTrace per parent order; the fills stage equals the golden
//     fill list fill for fill (ids, ticks, qty, ts, venue, signed fee);
//   - the stream digest is identical across two runs (same events + config +
//     seed => same traces, bit for bit);
//   - every JSONL line parses back with the strict from_value and equals the
//     emitted trace; versions are real hashes (data_version = the codec
//     golden's IAP1 file hash, config_version = content_hash of the config
//     in force), feature/model = the not-applicable marker;
//   - SOR-routed parents carry the router's candidate table with rank 1 ==
//     the routed venue; pinned parents carry the pinned venue.

#include <gtest/gtest.h>

#include <algorithm>
#include <sstream>
#include <string>
#include <vector>

#include "golden_util.hpp"
#include "iap/contracts/canonical_json.hpp"
#include "iap/contracts/trace.hpp"
#include "iap/marketdata/codec.hpp"
#include "iap/replay/exec_replay.hpp"

namespace {

namespace cj = iap::contracts;

constexpr std::int64_t SEC = 1'000'000'000;
constexpr const char* kSession = "replay-golden-eq-mbo";

iap::ExecConfig golden_config() {
    iap::ExecConfig cfg;
    cfg.seed = 20260829;
    cfg.impact_coeff_bps_per_pct_adv = 2.0;
    cfg.venues =
        iap::load_venues(iap_test::golden_dir() + "/../../configs/venues/venues.json");
    iap::InstrumentSpec ins;
    ins.instrument_id = 1;
    ins.tick_size = 0.01;
    ins.qty_unit = 1.0;
    ins.adv = 38000000.0;
    cfg.instruments[1] = ins;
    return cfg;
}

std::vector<iap::ParentOrder> golden_parents(std::int64_t t0, std::uint16_t venue) {
    iap::ParentOrder vwap;
    vwap.parent_id = 1;
    vwap.instrument_id = 1;
    vwap.venue_id = venue;
    vwap.side = 0;
    vwap.qty = 400;
    vwap.algo = iap::AlgoType::VWAP;
    vwap.start_ts = t0 + 60 * SEC;
    vwap.end_ts = t0 + 660 * SEC;
    vwap.slices = 4;
    iap::ParentOrder is;
    is.parent_id = 2;
    is.instrument_id = 1;
    is.venue_id = venue;
    is.side = 1;
    is.qty = 600;
    is.algo = iap::AlgoType::IS;
    is.start_ts = t0 + 120 * SEC;
    is.end_ts = t0 + 720 * SEC;
    is.slices = 3;
    is.risk_aversion = 1.0;
    return {vwap, is};
}

struct Traced {
    iap::ExecReplayResult result;
    std::vector<cj::DecisionTrace> traces;
    std::string digest;
    std::string jsonl;
};

Traced run_traced(const std::vector<iap::MarketEvent>& events, std::uint16_t venue,
                  iap::TraceOptions opts = {}) {
    if (opts.session_id.empty()) opts.session_id = kSession;
    iap::ExecutionReplay replay(golden_config(),
                                golden_parents(events.front().exchange_ts, venue));
    cj::MemoryTraceSink mem;
    std::ostringstream text;
    cj::JsonlTraceSink jsonl(text);
    // Fan out to both sinks through a tiny multi-sink.
    struct Multi : cj::TraceSink {
        cj::TraceSink* a;
        cj::TraceSink* b;
        void emit(const cj::DecisionTrace& t) override {
            a->emit(t);
            b->emit(t);
        }
    } multi{};
    multi.a = &mem;
    multi.b = &jsonl;
    replay.set_trace_sink(&multi, opts);
    Traced out;
    out.result = replay.run(events);
    out.traces = mem.traces();
    out.digest = mem.digest().hexdigest();
    out.jsonl = text.str();
    EXPECT_EQ(jsonl.digest().hexdigest(), out.digest);
    return out;
}

const std::vector<iap::MarketEvent>& golden_events() {
    static const auto events =
        iap::read_jsonl(iap_test::golden_path("events_eq_mbo.jsonl"));
    return events;
}

TEST(ReplayTraceGolden, FillsStageEqualsGoldenFillList) {
    const auto golden = iap_test::load_golden_json("expected_replay_fills.json");
    const auto traced = run_traced(golden_events(), 1);
    ASSERT_EQ(traced.traces.size(), 2u);  // one per parent, in parent order

    // Flatten the fills of both traces back into fill_id order and compare
    // fill for fill with the golden list.
    std::vector<cj::ExecutionReport> reports;
    for (const auto& t : traced.traces) {
        for (const auto& f : t.stages.fills) reports.push_back(f);
    }
    std::sort(reports.begin(), reports.end(),
              [](const cj::ExecutionReport& a, const cj::ExecutionReport& b) {
                  return a.execution_id < b.execution_id;
              });
    const auto& fills = golden["fills"].a();
    ASSERT_EQ(reports.size(), fills.size());
    for (std::size_t i = 0; i < fills.size(); ++i) {
        const auto& want = fills[i];
        const auto& got = reports[i];
        const std::string what = "fill " + std::to_string(i + 1);
        EXPECT_EQ(got.execution_id, want["fill_id"].u64()) << what;
        EXPECT_EQ(got.order_id, want["order_id"].u64()) << what;
        EXPECT_EQ(got.venue_id, want["venue_id"].u64()) << what;
        EXPECT_EQ(got.fill_price_ticks, want["price_ticks"].i64()) << what;
        EXPECT_EQ(got.filled_qty, want["qty"].i64()) << what;
        EXPECT_EQ(got.exchange_ts, want["ts"].i64()) << what;
        EXPECT_EQ(got.receive_ts, want["ts"].i64()) << what;
        EXPECT_NEAR(got.fees, want["fee"].num(), 1e-9) << what;
        EXPECT_TRUE(got.status == cj::ExecStatus::PARTIAL ||
                    got.status == cj::ExecStatus::FILLED)
            << what;
    }
    // Per-parent: the fills stage sums to the golden filled_qty and the
    // child that completes carries FILLED on its last report.
    for (const auto& t : traced.traces) {
        ASSERT_EQ(t.stages.parent_orders.size(), 1u);
        const auto pid = t.stages.parent_orders[0].parent_order_id;
        const auto& want = golden["parents"][std::to_string(pid)];
        std::int64_t filled = 0;
        for (const auto& f : t.stages.fills) filled += f.filled_qty;
        EXPECT_EQ(filled, want["filled_qty"].i64());
        EXPECT_EQ(static_cast<std::int64_t>(t.stages.child_orders.size()),
                  want["children"].i64());
        EXPECT_EQ(t.stages.routing.size(), t.stages.child_orders.size());
    }
    // Parent 2 (IS, MARKET children) fills every child in one print: FILLED.
    for (const auto& f : traced.traces[1].stages.fills) {
        EXPECT_EQ(f.status, cj::ExecStatus::FILLED);
    }
}

TEST(ReplayTraceGolden, DigestIdenticalAcrossRuns) {
    const auto a = run_traced(golden_events(), 1);
    const auto b = run_traced(golden_events(), 1);
    EXPECT_EQ(a.digest, b.digest);
    EXPECT_EQ(a.jsonl, b.jsonl);
    ASSERT_EQ(a.traces.size(), b.traces.size());
    for (std::size_t i = 0; i < a.traces.size(); ++i) EXPECT_EQ(a.traces[i], b.traces[i]);
    EXPECT_NE(a.digest, cj::TraceDigest().hexdigest());
    // A different session id changes every trace id and hence the digest.
    iap::TraceOptions other;
    other.session_id = "replay-golden-eq-mbo-2";
    const auto c = run_traced(golden_events(), 1, other);
    EXPECT_NE(c.digest, a.digest);
    EXPECT_EQ(c.traces[0].stages.fills, a.traces[0].stages.fills);
}

TEST(ReplayTraceGolden, EveryLineParsesStrictlyAndVersionsAreReal) {
    const auto traced = run_traced(golden_events(), 1);
    std::istringstream in(traced.jsonl);
    std::string line;
    std::size_t n = 0;
    cj::TraceDigest reread;
    while (std::getline(in, line)) {
        const cj::DecisionTrace t = cj::DecisionTrace::from_value(cj::parse(line));
        ASSERT_LT(n, traced.traces.size());
        EXPECT_EQ(t, traced.traces[n]);
        EXPECT_EQ(cj::trace_line(t), line);
        reread.update(t);
        ++n;
    }
    EXPECT_EQ(n, traced.traces.size());
    EXPECT_EQ(reread.hexdigest(), traced.digest);

    const auto codec = iap_test::load_golden_json("expected_codec_sha256.json");
    const std::string not_applicable(64, '0');
    const cj::Value cfg = iap::ExecutionReplay::config_value(golden_config(), iap::SorOptions{});
    for (const auto& t : traced.traces) {
        EXPECT_EQ(t.session_id, kSession);
        EXPECT_EQ(t.instrument_id, 1u);
        EXPECT_EQ(t.trace_id, cj::make_trace_id(t.session_id, t.instrument_id,
                                                t.event_ts, t.sequence));
        // data_version = the IAP1 file hash pinned by the codec golden
        EXPECT_EQ(t.data_version, codec["events_eq_mbo.iap1"].s());
        EXPECT_EQ(t.config_version, cj::content_hash(cfg));
        EXPECT_EQ(t.feature_version, not_applicable);
        EXPECT_EQ(t.model_version, not_applicable);
        EXPECT_EQ(t.feature_version, cj::kVersionNotApplicable);
        // C++ owns the execution stages only.
        EXPECT_TRUE(t.stages.signal.empty());
        EXPECT_FALSE(t.stages.portfolio);
        EXPECT_TRUE(t.stages.risk.empty());
        EXPECT_TRUE(t.stages.tca.empty());
        EXPECT_FALSE(t.stages.attribution);
        // Identity: the first replayed event at/after start_ts.
        const auto& po = t.stages.parent_orders[0];
        EXPECT_GE(t.event_ts, po.decision_ts);
        bool found = false;
        for (const auto& ev : golden_events()) {
            if (ev.exchange_ts >= po.decision_ts) {
                EXPECT_EQ(t.event_ts, ev.exchange_ts);
                EXPECT_EQ(t.sequence, ev.sequence);
                found = true;
                break;
            }
        }
        EXPECT_TRUE(found);
        for (const auto& c : t.stages.child_orders) {
            EXPECT_EQ(c.parent_order_id, po.parent_order_id);
            EXPECT_EQ(c.expire_ts, po.end_ts);
        }
    }
    // The parent records reflect the algo mapping.
    const auto& vwap = traced.traces[0].stages.parent_orders[0];
    EXPECT_EQ(vwap.algo, cj::Algo::VWAP);
    EXPECT_EQ(vwap.urgency, 0.0);
    EXPECT_EQ(vwap.params.at("slices"), 4.0);
    const auto& is = traced.traces[1].stages.parent_orders[0];
    EXPECT_EQ(is.algo, cj::Algo::IS);
    EXPECT_EQ(is.urgency, 1.0);
    EXPECT_EQ(is.params.at("risk_aversion"), 1.0);
    // explain() renders without a TCA / attribution stage.
    const std::string text = cj::explain(traced.traces[0], {{1, "XV1"}});
    EXPECT_EQ(text.substr(0, 8), "Order 1\n");
    EXPECT_NE(text.find("Execution:  VWAP"), std::string::npos);
    EXPECT_NE(text.find("SOR:        XV1 = 100%"), std::string::npos);
    EXPECT_NE(text.find("Fills:      329 / 400 (82.2%)"), std::string::npos);
    EXPECT_NE(text.find("TCA:        (none)"), std::string::npos);
}

TEST(ReplayTraceGolden, PinnedVenueRoutingRecordsThePinnedVenue) {
    const auto traced = run_traced(golden_events(), 1);
    for (const auto& t : traced.traces) {
        for (const auto& vd : t.stages.routing) {
            EXPECT_EQ(vd.venue_id, 1);
            ASSERT_EQ(vd.candidates.size(), 1u);
            EXPECT_EQ(vd.candidates[0].venue_id, 1);
            EXPECT_TRUE(vd.candidates[0].eligible);
            EXPECT_EQ(vd.candidates[0].rank, 1);
            EXPECT_EQ(vd.candidates[0].latency_mean_ns, 150000);
            EXPECT_NE(vd.reason.find("pinned"), std::string::npos);
        }
    }
}

TEST(ReplayTraceGolden, SorRoutedCandidateTableRanksTheRoutedVenueFirst) {
    // venue_id 0 => SOR over every configured venue; only XV1 has a book in
    // the EQ golden stream, so the SOR routes there and the other venues are
    // ineligible (no book) with rank 0.
    const auto traced = run_traced(golden_events(), 0);
    const auto plain = run_traced(golden_events(), 1);
    std::size_t routed = 0;
    for (const auto& t : traced.traces) {
        for (const auto& vd : t.stages.routing) {
            ++routed;
            EXPECT_EQ(vd.venue_id, 1);
            EXPECT_EQ(vd.candidates.size(), golden_config().venues.size());
            for (std::size_t i = 0; i < vd.candidates.size(); ++i) {
                const auto& c = vd.candidates[i];
                if (i > 0) {
                    EXPECT_LT(vd.candidates[i - 1].venue_id, c.venue_id);
                }
                if (c.venue_id == vd.venue_id) {
                    EXPECT_TRUE(c.eligible);
                    EXPECT_EQ(c.rank, 1);
                    EXPECT_GT(c.displayed_qty, 0);
                    EXPECT_GT(c.displayed_price_ticks, 0);
                } else {
                    EXPECT_FALSE(c.eligible);
                    EXPECT_EQ(c.rank, 0);
                }
            }
            EXPECT_NE(vd.reason.find("pinned"), 0u);
        }
    }
    EXPECT_GT(routed, 0u);
    // Routing through the SOR to the same venue yields the same fills.
    ASSERT_EQ(traced.traces.size(), plain.traces.size());
    for (std::size_t i = 0; i < traced.traces.size(); ++i) {
        EXPECT_EQ(traced.traces[i].stages.fills, plain.traces[i].stages.fills);
    }
}

TEST(ReplayTraceGolden, OptionsAreValidatedAndSinkIsOptional) {
    const auto& events = golden_events();
    iap::ExecutionReplay replay(golden_config(), golden_parents(events.front().exchange_ts, 1));
    cj::MemoryTraceSink mem;
    iap::TraceOptions bad;
    bad.session_id = "bad|session";
    EXPECT_THROW(replay.set_trace_sink(&mem, bad), std::invalid_argument);
    bad.session_id = "ok";
    bad.feature_version = "not-a-hash";
    EXPECT_THROW(replay.set_trace_sink(&mem, bad), std::invalid_argument);
    bad.feature_version = cj::kVersionNotApplicable;
    bad.data_version = "xyz";
    EXPECT_THROW(replay.set_trace_sink(&mem, bad), std::invalid_argument);
    EXPECT_THROW(replay.set_trace_sink(nullptr, iap::TraceOptions{}), std::invalid_argument);
    // Without a sink the replay is exactly the untraced golden run.
    const auto res = replay.run(events);
    EXPECT_EQ(res.fills.size(), 7u);
    EXPECT_TRUE(mem.traces().empty());
    EXPECT_THROW(replay.set_trace_sink(&mem, iap::TraceOptions{}), std::runtime_error);
}

}  // namespace
