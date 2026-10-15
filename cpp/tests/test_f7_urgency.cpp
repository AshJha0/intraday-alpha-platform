// v1.17 F7 rule 6: B2 alpha urgency parity with
// tests/golden/expected_f7_urgency.json (make_golden_f7.py --only
// r06_urgency): zscore, regime_from_z, the causal reading with latency and
// staleness under each AlphaUrgencyPolicy, on fixed sources and on readings
// derived from the real slice's book.

#include <gtest/gtest.h>

#include "f7_util.hpp"
#include "iap/execution/signal.hpp"

namespace {

using namespace iap_f7;

struct Policy {
    double threshold_z;
    double passive_urgency;
    std::int64_t max_age_ns;
    std::int64_t latency_ns;
};

Policy policy_from(const Value& v) {
    return {v.at("threshold_z").as_number(), v.at("passive_urgency").as_number(),
            v.at("max_age_ns").as_int64(), v.at("latency_ns").as_int64()};
}

Value decision_row(const iap::ArraySignalSource& src, const Policy& p, std::uint32_t iid,
                   std::uint8_t side, std::int64_t t) {
    const iap::SignalDecision d =
        iap::read_signal(src, iid, side, t, p.threshold_z, p.max_age_ns, p.latency_ns);
    Object o;
    o["status"] = vs(iap::signal_status_name(d.status));
    o["regime"] = vs(iap::urgency_regime_name(d.regime));
    o["z"] = vd(d.z);
    o["reading_ts"] = d.reading.has_value() ? vi(d.reading->ts) : Value(nullptr);
    o["expected_return"] = d.reading.has_value() ? vd(d.reading->expected_return) : Value(nullptr);
    o["alignment"] = vd(iap::alignment(side, d.z));
    return Value(std::move(o));
}

TEST(F7Urgency, FixedVectors) {
    const Value g = load_golden("expected_f7_urgency.json");
    const Value& in = g.at("inputs");
    Object got;
    Array zs;
    for (const Value& c : in.at("zscore").as_array()) {
        std::vector<double> vals;
        for (const Value& x : c.as_array()) vals.push_back(x.as_number());
        const iap::ZScore z = iap::zscore(vals);
        Object o;
        Array za;
        for (const double x : z.z) za.push_back(vd(x));
        o["z"] = Value(std::move(za));
        o["mean"] = vd(z.mean);
        o["std"] = vd(z.std);
        zs.push_back(Value(std::move(o)));
    }
    got["zscore"] = Value(std::move(zs));
    Array regimes;
    for (const Value& c : in.at("regimes").as_array()) {
        regimes.push_back(vs(iap::urgency_regime_name(iap::regime_from_z(
            static_cast<std::uint8_t>(c.at("side").as_int64()), c.at("z").as_number(),
            c.at("threshold_z").as_number()))));
    }
    got["regimes"] = Value(std::move(regimes));
    iap::ArraySignalSource src(in.at("source_horizon_s").as_number());
    for (const auto& [iid, rows] : in.at("sources").as_object()) {
        std::vector<iap::ArraySignalSource::Row> r;
        for (const Value& x : rows.as_array()) {
            r.push_back({x.at(0).as_int64(), x.at(1).as_number(), x.at(2).as_number()});
        }
        src.add(static_cast<std::uint32_t>(std::stoul(iid)), r);
    }
    std::vector<Policy> pols;
    for (const Value& p : in.at("policies").as_array()) pols.push_back(policy_from(p));
    Array queries;
    for (const Value& q : in.at("queries").as_array()) {
        queries.push_back(decision_row(src, pols[static_cast<std::size_t>(q.at("policy").as_int64())],
                                       static_cast<std::uint32_t>(q.at("instrument_id").as_uint64()),
                                       static_cast<std::uint8_t>(q.at("side").as_int64()),
                                       q.at("t").as_int64()));
    }
    got["queries"] = Value(std::move(queries));
    const Value& exp = g.at("expected");
    expect_canonical_eq(exp.at("zscore"), got.at("zscore"), "zscore");
    expect_canonical_eq(exp.at("regimes"), got.at("regimes"), "regimes");
    expect_canonical_eq(exp.at("queries"), got.at("queries"), "queries");
    // strictly increasing readings are enforced
    iap::ArraySignalSource bad;
    EXPECT_THROW(bad.add(1, {{10, 0.0, 0.0}, {10, 1.0, 0.0}}), std::invalid_argument);
}

TEST(F7Urgency, RealSliceReadingsAndQueries) {
    const Value g = load_golden("expected_f7_urgency.json");
    const Value& in = g.at("inputs");
    std::vector<Policy> pols;
    for (const Value& p : in.at("slice").at("policies").as_array()) pols.push_back(policy_from(p));
    const auto events = read_slice();
    iap::ConsolidatedBook book(kSliceInstrument);
    // readings: every 50th event past the prefix with a two-sided book
    struct Raw {
        std::int64_t ts;
        double er;
    };
    std::vector<Raw> raw;
    std::vector<std::pair<std::size_t, std::int64_t>> samples;  // (k, ts)
    for (std::size_t k = 1; k <= events.size(); ++k) {
        book.apply(events[k - 1]);
        const auto bb = book.books().at(kSliceVenue).best_bid();
        const auto ba = book.books().at(kSliceVenue).best_ask();
        const bool two_sided = bb.has_value() && ba.has_value();
        if (k > static_cast<std::size_t>(kSlicePrefix) && k % 50 == 0 && two_sided) {
            const double er = static_cast<double>(bb->second - ba->second) /
                              static_cast<double>(bb->second + ba->second) * 1e-4;
            raw.push_back({events[k - 1].exchange_ts, er});
        }
        if (is_sample(k) && two_sided) samples.emplace_back(k, events[k - 1].exchange_ts);
    }
    std::vector<double> ers;
    for (const Raw& r : raw) ers.push_back(r.er);
    const iap::ZScore z = iap::zscore(ers);
    std::vector<iap::ArraySignalSource::Row> rows;
    for (std::size_t i = 0; i < raw.size(); ++i) {
        if (!rows.empty() && rows.back().ts == raw[i].ts) {
            rows.back() = {raw[i].ts, z.z[i], raw[i].er};  // the last of equal ts wins
        } else {
            rows.push_back({raw[i].ts, z.z[i], raw[i].er});
        }
    }
    Array readings;
    for (const auto& r : rows) readings.push_back(Value(Array{vi(r.ts), vd(r.z), vd(r.expected_return)}));
    expect_canonical_eq(g.at("expected").at("slice").at("readings"), Value(std::move(readings)),
                        "slice readings");
    iap::ArraySignalSource src;
    src.add(kSliceInstrument, rows);
    Array queries;
    for (const auto& [k, ts] : samples) {
        for (std::size_t pi = 0; pi < pols.size(); ++pi) {
            for (std::uint8_t side = 0; side < 2; ++side) {
                Value d = decision_row(src, pols[pi], kSliceInstrument, side, ts);
                Object o = d.as_object();
                o["k"] = vi(static_cast<std::int64_t>(k));
                o["policy"] = vi(static_cast<std::int64_t>(pi));
                o["side"] = vi(side);
                queries.push_back(Value(std::move(o)));
            }
        }
    }
    expect_canonical_eq(g.at("expected").at("slice").at("queries"), Value(std::move(queries)),
                        "slice queries");
}

}  // namespace
