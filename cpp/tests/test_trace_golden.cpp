// Golden decision-trace contract: the DecisionTrace example of
// tests/golden/expected_contracts_examples.json parsed strictly, re-emitted
// as the canonical line pinned in expected_canonical_json.json
// (trace_digest: line length, line sha256, the one-trace / twice / empty
// stream digests), the pinned explain() block, and to_value/from_value
// round-trip equality.

#include <gtest/gtest.h>

#include <sstream>
#include <string>
#include <vector>

#include "golden_util.hpp"
#include "iap/contracts/canonical_json.hpp"
#include "iap/contracts/trace.hpp"

namespace {

namespace cj = iap::contracts;

cj::Value load_examples() {
    return cj::parse(iap_test::read_text_file(
        iap_test::golden_path("expected_contracts_examples.json")));
}

cj::Value load_canonical() {
    return cj::parse(iap_test::read_text_file(
        iap_test::golden_path("expected_canonical_json.json")));
}

cj::DecisionTrace example_trace() {
    const auto ex = load_examples();
    const auto& dt = ex.at("examples").at("DecisionTrace");
    EXPECT_EQ(dt.at("schema").as_string(), "trace/decision_trace.schema.json");
    EXPECT_EQ(dt.at("x_version").as_int64(), 1);
    return cj::DecisionTrace::from_value(dt.at("value"));
}

TEST(TraceGolden, ExampleParsesStrictlyAndCanonicalLineIsPinned) {
    const auto canon = load_canonical();
    const auto& pin = canon.at("trace_digest");
    const cj::DecisionTrace t = example_trace();
    const std::string line = cj::trace_line(t);
    EXPECT_EQ(line.size(), pin.at("line_length").as_uint64());
    EXPECT_EQ(cj::sha256_hex(line), pin.at("line_sha256").as_string());
    // The line is exactly the canonical JSON of the example's raw tree too
    // (to_value() loses nothing the schema carries).
    const auto ex = load_examples();
    EXPECT_EQ(line, cj::canonical_json(ex.at("examples").at("DecisionTrace").at("value")));
}

TEST(TraceGolden, DigestsPinned) {
    const auto canon = load_canonical();
    const auto& pin = canon.at("trace_digest");
    const cj::DecisionTrace t = example_trace();

    cj::TraceDigest empty;
    EXPECT_EQ(empty.hexdigest(), pin.at("digest_empty_stream").as_string());
    EXPECT_EQ(empty.count(), 0u);

    cj::TraceDigest one;
    one.update(t);
    EXPECT_EQ(one.hexdigest(), pin.at("digest_one_trace").as_string());
    EXPECT_EQ(one.count(), 1u);

    cj::TraceDigest twice;
    twice.update(t).update(t);
    EXPECT_EQ(twice.hexdigest(), pin.at("digest_same_trace_twice").as_string());
    EXPECT_EQ(twice.count(), 2u);

    // The JSONL sink writes line + '\n' and keeps the same digest; re-reading
    // the stream with update_line reproduces it.
    std::ostringstream out;
    cj::JsonlTraceSink sink(out);
    sink.emit(t);
    sink.emit(t);
    EXPECT_EQ(sink.digest().hexdigest(), pin.at("digest_same_trace_twice").as_string());
    const std::string text = out.str();
    EXPECT_EQ(text.size(), 2 * (pin.at("line_length").as_uint64() + 1));
    cj::TraceDigest reread;
    std::istringstream in(text);
    std::string l;
    while (std::getline(in, l)) {
        // re-canonicalise through the parser, as TraceDigest.of_jsonl does
        reread.update(cj::DecisionTrace::from_value(cj::parse(l)));
    }
    EXPECT_EQ(reread.hexdigest(), pin.at("digest_same_trace_twice").as_string());

    cj::MemoryTraceSink mem;
    mem.emit(t);
    EXPECT_EQ(mem.digest().hexdigest(), pin.at("digest_one_trace").as_string());
    ASSERT_EQ(mem.traces().size(), 1u);
    EXPECT_EQ(mem.traces()[0], t);
}

TEST(TraceGolden, ExplainPinned) {
    const auto ex = load_examples();
    const auto& pin = ex.at("explain");
    std::map<std::uint16_t, std::string> names;
    for (const auto& [k, v] : pin.at("venue_names").as_object()) {
        names[static_cast<std::uint16_t>(std::stoul(k))] = v.as_string();
    }
    const cj::DecisionTrace t = example_trace();
    EXPECT_EQ(cj::explain(t, names), pin.at("text").as_string());
    // Unnamed venues render as their decimal id.
    names.erase(3);
    const std::string unnamed = cj::explain(t, names);
    EXPECT_NE(unnamed.find("XV2 = 35%  3 = 20%"), std::string::npos);
}

TEST(TraceGolden, ExplainLabelsTheActingSignalAndItsComponents) {
    // signal[0] is the acting signal (the order's alpha id); every further
    // signal is a component labelled by its own model_version.
    const cj::DecisionTrace t = example_trace();
    cj::DecisionTrace multi = t;
    cj::AlphaSignal member = t.stages.signal.at(0);
    member.model_version = "EQ01";
    member.expected_return = 0.0001;
    member.confidence = 0.5;
    multi.stages.signal.push_back(member);
    const std::map<std::uint16_t, std::string> names;
    const auto split = [](const std::string& text) {
        std::vector<std::string> out;
        std::string cur;
        for (char c : text) {
            if (c == '\n') { out.push_back(cur); cur.clear(); } else { cur.push_back(c); }
        }
        out.push_back(cur);
        return out;
    };
    const auto pinned = split(cj::explain(t, names));
    const auto lines = split(cj::explain(multi, names));
    ASSERT_EQ(lines.size(), pinned.size() + 1);
    EXPECT_EQ(lines[1], pinned[1]);
    EXPECT_EQ(lines[2], "Alpha:      EQ01  expected return = +1.0 bps  confidence = 0.50");
    for (std::size_t i = 2; i < pinned.size(); ++i) EXPECT_EQ(lines[i + 1], pinned[i]);
}

TEST(TraceGolden, RoundTripEquality) {
    const cj::DecisionTrace t = example_trace();
    const cj::DecisionTrace back = cj::DecisionTrace::from_value(t.to_value());
    EXPECT_EQ(back, t);
    EXPECT_EQ(cj::trace_line(back), cj::trace_line(t));
    // Every nested record round-trips on its own.
    for (const auto& s : t.stages.signal) EXPECT_EQ(cj::AlphaSignal::from_value(s.to_value()), s);
    ASSERT_TRUE(t.stages.portfolio);
    EXPECT_EQ(cj::PortfolioTarget::from_value(t.stages.portfolio->to_value()),
              *t.stages.portfolio);
    for (const auto& r : t.stages.risk) EXPECT_EQ(cj::RiskDecision::from_value(r.to_value()), r);
    for (const auto& p : t.stages.parent_orders) {
        EXPECT_EQ(cj::ParentOrder::from_value(p.to_value()), p);
    }
    for (const auto& c : t.stages.child_orders) EXPECT_EQ(cj::ChildOrder::from_value(c.to_value()), c);
    for (const auto& v : t.stages.routing) EXPECT_EQ(cj::VenueDecision::from_value(v.to_value()), v);
    for (const auto& f : t.stages.fills) EXPECT_EQ(cj::ExecutionReport::from_value(f.to_value()), f);
    for (const auto& x : t.stages.tca) EXPECT_EQ(cj::TCAResult::from_value(x.to_value()), x);
    ASSERT_TRUE(t.stages.attribution);
    EXPECT_EQ(cj::Attribution::from_value(t.stages.attribution->to_value()),
              *t.stages.attribution);
    // Copy semantics of the optional stages.
    cj::DecisionTrace copy = t;
    EXPECT_EQ(copy, t);
    copy.stages.attribution.reset();
    EXPECT_NE(copy, t);
    EXPECT_TRUE(t.stages.attribution);
}

TEST(TraceGolden, EveryExampleRecordParsesAndReserialises) {
    const auto ex = load_examples();
    const auto& all = ex.at("examples").as_object();
    auto check = [&](const char* name, auto parse) {
        const auto& v = all.at(name).at("value");
        const auto rec = parse(v);
        EXPECT_EQ(cj::canonical_json(rec.to_value()), cj::canonical_json(v)) << name;
    };
    check("AlphaSignal", [](const cj::Value& v) { return cj::AlphaSignal::from_value(v); });
    check("PortfolioLeg", [](const cj::Value& v) { return cj::PortfolioLeg::from_value(v); });
    check("PortfolioTarget", [](const cj::Value& v) { return cj::PortfolioTarget::from_value(v); });
    check("RiskDecision", [](const cj::Value& v) { return cj::RiskDecision::from_value(v); });
    check("ParentOrder", [](const cj::Value& v) { return cj::ParentOrder::from_value(v); });
    check("ChildOrder", [](const cj::Value& v) { return cj::ChildOrder::from_value(v); });
    check("VenueScore", [](const cj::Value& v) { return cj::VenueScore::from_value(v); });
    check("VenueDecision", [](const cj::Value& v) { return cj::VenueDecision::from_value(v); });
    check("ExecutionReport", [](const cj::Value& v) { return cj::ExecutionReport::from_value(v); });
    check("LatencyStats", [](const cj::Value& v) { return cj::LatencyStats::from_value(v); });
    check("TCAResult", [](const cj::Value& v) { return cj::TCAResult::from_value(v); });
    check("Attribution", [](const cj::Value& v) { return cj::Attribution::from_value(v); });
    check("TraceStages", [](const cj::Value& v) { return cj::TraceStages::from_value(v); });
    check("DecisionTrace", [](const cj::Value& v) { return cj::DecisionTrace::from_value(v); });
}

TEST(TraceGolden, FromValueIsStrict) {
    const auto ex = load_examples();
    const cj::Value base = ex.at("examples").at("DecisionTrace").at("value");
    {   // unknown key
        cj::Value v = base;
        v.as_object()["extra"] = cj::Value(1);
        EXPECT_THROW(cj::DecisionTrace::from_value(v), std::invalid_argument);
    }
    {   // missing key
        cj::Value v = base;
        v.as_object().erase("sequence");
        EXPECT_THROW(cj::DecisionTrace::from_value(v), std::invalid_argument);
    }
    {   // double where an integer is wanted
        cj::Value v = base;
        v.as_object()["sequence"] = cj::Value(500.0);
        EXPECT_THROW(cj::DecisionTrace::from_value(v), std::invalid_argument);
    }
    {   // bool where a number is wanted
        cj::Value v = base;
        v.as_object()["stages"].as_object()["attribution"].as_object()["alpha_bps"] =
            cj::Value(true);
        EXPECT_THROW(cj::DecisionTrace::from_value(v), std::invalid_argument);
    }
    {   // bad version hash
        cj::Value v = base;
        v.as_object()["data_version"] = cj::Value("abc");
        EXPECT_THROW(cj::DecisionTrace::from_value(v), std::invalid_argument);
    }
    {   // the not-applicable marker is a legal version
        cj::Value v = base;
        v.as_object()["feature_version"] = cj::Value(cj::kVersionNotApplicable);
        EXPECT_NO_THROW(cj::DecisionTrace::from_value(v));
    }
    {   // routed venue must be an eligible candidate
        cj::Value v = base;
        auto& routing = v.as_object()["stages"].as_object()["routing"].as_array();
        routing[0].as_object()["venue_id"] = cj::Value(9);
        EXPECT_THROW(cj::DecisionTrace::from_value(v), std::invalid_argument);
    }
    {   // an int is accepted for a double field (python float(value))
        cj::Value v = base;
        v.as_object()["stages"].as_object()["fills"].as_array()[0].as_object()["fees"] =
            cj::Value(27);
        const auto t = cj::DecisionTrace::from_value(v);
        EXPECT_EQ(t.stages.fills[0].fees, 27.0);
        EXPECT_NE(cj::trace_line(t).find("\"fees\":27.0"), std::string::npos);
    }
}

}  // namespace
