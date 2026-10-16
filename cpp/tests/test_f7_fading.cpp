// v1.17 F7 rule 8: B4 quote fading parity with
// tests/golden/expected_f7_fading.json (make_golden_f7.py --only
// r08_fading): book_pressure, fade_rule, would_have_filled, pull_rows /
// pull_summary / pull_outcomes on fixed vectors and on the real slice.

#include <gtest/gtest.h>

#include "f7_util.hpp"
#include "iap/execution/fading.hpp"
#include "iap/execution/signal.hpp"
#include "iap/tca/markout.hpp"

namespace {

using namespace iap_f7;

iap::MarketTimeline timeline_from(const Value& d) {
    iap::MarketTimeline tl;
    for (const Value& s : d.at("states").as_array()) {
        tl.append_state_pinned(s.at(0).as_int64(), s.at(1).as_number(), s.at(2).as_number(),
                               s.at(3).as_int64(), s.at(4).as_int64());
    }
    for (const Value& h : d.at("halts").as_array()) tl.add_halt(h.as_int64());
    return tl;
}

iap::Horizons horizons_from(const Value& v, const std::vector<std::string>& order) {
    iap::Horizons out;
    for (const std::string& name : order) out.emplace_back(name, v.at(name).as_int64());
    return out;
}

std::vector<std::string> names(std::initializer_list<const char*> l) {
    return std::vector<std::string>(l.begin(), l.end());
}

std::vector<iap::PullRecord> pulls_from(const Value& arr) {
    std::vector<iap::PullRecord> out;
    for (const Value& r : arr.as_array()) out.push_back(iap::PullRecord::from_value(r));
    return out;
}

TEST(F7Fading, FixedVectors) {
    const Value g = load_golden("expected_f7_fading.json");
    const Value& in = g.at("inputs");
    const Value& exp = g.at("expected");
    Array pressure;
    for (const Value& c : in.at("pressure").as_array()) {
        pressure.push_back(vd(iap::book_pressure(c.at(0).as_int64(), c.at(1).as_int64())));
    }
    expect_canonical_eq(exp.at("pressure"), Value(std::move(pressure)), "pressure");
    Array fade;
    for (const Value& c : in.at("fade").as_array()) {
        fade.push_back(vs(iap::cancel_rule_name(iap::fade_rule(
            static_cast<std::uint8_t>(c.at("side").as_int64()), opt_num(c.at("z")),
            c.at("threshold_z").as_number(), opt_num(c.at("pressure")),
            c.at("pressure_threshold").as_number()))));
    }
    expect_canonical_eq(exp.at("fade"), Value(std::move(fade)), "fade");
    const iap::MarketTimeline tl = timeline_from(in.at("timeline"));
    Array whf;
    for (const Value& c : in.at("would_have_filled").as_array()) {
        whf.push_back(Value(iap::would_have_filled(tl, static_cast<std::uint8_t>(c.at("side").as_int64()),
                                                   c.at("price").as_number(), c.at("start_ts").as_int64(),
                                                   c.at("end_ts").as_int64())));
    }
    expect_canonical_eq(exp.at("would_have_filled"), Value(std::move(whf)), "would_have_filled");
    const auto pulls = pulls_from(in.at("pulls"));
    // a PullRecord round-trips through its dict form
    for (const Value& r : in.at("pulls").as_array()) {
        expect_canonical_eq(r, iap::PullRecord::from_value(r).to_value(), "pull record");
    }
    const std::vector<std::string> hnames = names({"100ms", "1s", "5s"});
    const iap::Horizons hz = horizons_from(in.at("horizons"), hnames);
    const Value rows = iap::pull_rows(pulls, tl, 0.01, &hz, 1.0, nullptr, "s1");
    expect_canonical_eq(exp.at("rows"), rows, "rows");
    expect_canonical_eq(exp.at("summary"), iap::pull_summary(rows, &hnames, 1), "summary");
    const std::vector<std::string> qrules = names({"queue:ahead", "queue:p_fill"});
    const Value rows_q = iap::pull_rows(pulls, tl, 0.01, &hz, 100.0, &qrules, "");
    expect_canonical_eq(exp.at("rows_queue"), rows_q, "rows_queue");
    const std::vector<std::string> two = names({"1s", "5s"});
    expect_canonical_eq(exp.at("summary_queue"), iap::pull_summary(rows_q, &two, 0, &qrules),
                        "summary_queue");
    expect_canonical_eq(exp.at("outcomes"), iap::pull_outcomes(pulls, tl, 0.01, &hz), "outcomes");
    expect_canonical_eq(exp.at("summary_empty"), iap::pull_summary(Value(Array{})), "summary_empty");
}

TEST(F7Fading, RealSlice) {
    const Value g = load_golden("expected_f7_fading.json");
    const Value& in = g.at("inputs").at("slice");
    const Value& exp = g.at("expected").at("slice");
    const auto events = read_slice();
    iap::ConsolidatedBook book(kSliceInstrument);
    iap::MarketTimeline tl;
    struct Raw {
        std::int64_t ts;
        double er;
    };
    std::vector<Raw> raw;
    struct Sample {
        std::size_t k;
        std::int64_t ts;
        std::int64_t bid, ask, bsz, asz;
    };
    std::vector<Sample> samples;
    for (std::size_t k = 1; k <= events.size(); ++k) {
        book.apply(events[k - 1]);
        const auto bb = book.books().at(kSliceVenue).best_bid();
        const auto ba = book.books().at(kSliceVenue).best_ask();
        if (!bb.has_value() || !ba.has_value()) continue;
        const std::int64_t ts = events[k - 1].exchange_ts;
        tl.append_state_pinned(ts, static_cast<double>(bb->first) * kSliceTick,
                               static_cast<double>(ba->first) * kSliceTick, bb->second, ba->second);
        if (k > static_cast<std::size_t>(kSlicePrefix) && k % 50 == 0) {
            raw.push_back({ts, static_cast<double>(bb->second - ba->second) /
                                   static_cast<double>(bb->second + ba->second) * 1e-4});
        }
        if (is_sample(k)) samples.push_back({k, ts, bb->first, ba->first, bb->second, ba->second});
    }
    std::vector<double> ers;
    for (const Raw& r : raw) ers.push_back(r.er);
    const iap::ZScore z = iap::zscore(ers);
    std::vector<iap::ArraySignalSource::Row> rows;
    for (std::size_t i = 0; i < raw.size(); ++i) {
        if (!rows.empty() && rows.back().ts == raw[i].ts) {
            rows.back() = {raw[i].ts, z.z[i], raw[i].er};
        } else {
            rows.push_back({raw[i].ts, z.z[i], raw[i].er});
        }
    }
    iap::ArraySignalSource src;
    src.add(kSliceInstrument, rows);
    Array rules;
    for (const Sample& s : samples) {
        const auto rd = src.at(kSliceInstrument, s.ts);
        const std::optional<double> zz = rd.has_value() ? std::optional<double>(rd->z) : std::nullopt;
        for (std::uint8_t side = 0; side < 2; ++side) {
            const std::int64_t own = side == 0 ? s.bsz : s.asz;
            const std::int64_t opp = side == 0 ? s.asz : s.bsz;
            const double pr = iap::book_pressure(own, opp);
            Object o;
            o["k"] = vi(static_cast<std::int64_t>(s.k));
            o["side"] = vi(side);
            o["pressure"] = vd(pr);
            o["z"] = vod(zz);
            o["rule_signal"] = vs(iap::cancel_rule_name(iap::fade_rule(side, zz, 1.0, pr, 0.0)));
            o["rule_pressure"] = vs(iap::cancel_rule_name(iap::fade_rule(side, std::nullopt, 0.0, pr, 0.6)));
            o["rule_both"] = vs(iap::cancel_rule_name(iap::fade_rule(side, zz, 0.5, pr, 0.75)));
            rules.push_back(Value(std::move(o)));
        }
    }
    expect_canonical_eq(exp.at("rules"), Value(std::move(rules)), "slice rules");
    const auto pulls = pulls_from(in.at("pulls"));
    const std::vector<std::string> hnames = names({"100ms", "1s", "5s", "30s"});
    const iap::Horizons hz = horizons_from(in.at("horizons"), hnames);
    const Value prow = iap::pull_rows(pulls, tl, kSliceTick, &hz, 1.0, nullptr, "2019-01-30");
    expect_canonical_eq(exp.at("rows"), prow, "slice rows");
    std::int64_t lost = 0;
    for (const auto& p : pulls) lost += p.outcome == "lost_race" ? 1 : 0;
    expect_canonical_eq(exp.at("summary"), iap::pull_summary(prow, &hnames, lost), "slice summary");
    expect_canonical_eq(exp.at("outcomes"), iap::pull_outcomes(pulls, tl, kSliceTick, &hz), "slice outcomes");
}

}  // namespace
