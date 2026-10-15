// v1.17 F7 rule 9: B5 queue-aware cancels parity with
// tests/golden/expected_f7_queue_cancel.json (make_golden_f7.py --only
// r09_queue_cancel): queue_cancel_rule, queue_bucket, the X-4 depletion
// split read, hazard_fill_prob, the hazard model's features /
// bucket_hazards / p_fill_within (UTC clock), on fixed cases and per sample
// of the real slice.

#include <gtest/gtest.h>

#include "f7_util.hpp"
#include "iap/execution/fading.hpp"
#include "iap/execution/fill_hazard.hpp"
#include "iap/execution/queue_depletion.hpp"
#include "iap/sor/sor_v2.hpp"

namespace {

using namespace iap_f7;

Value features_row(const iap::HazardFeatures& x) {
    Object o;
    o["queue_ahead"] = vd(x.queue_ahead);
    o["depletion_per_s"] = vd(x.depletion_per_s);
    o["spread_ticks"] = vd(x.spread_ticks);
    o["imbalance"] = vd(x.imbalance);
    o["tod"] = vd(x.tod);
    return Value(std::move(o));
}

std::map<std::string, iap::QueueDepletionTable> tables_from(const Value& docs) {
    std::map<std::string, iap::QueueDepletionTable> out;
    for (const auto& [vid, doc] : docs.as_object()) out[vid] = iap::QueueDepletionTable::from_value(doc);
    return out;
}

std::optional<double> depletion(const std::map<std::string, iap::QueueDepletionTable>& t,
                                const std::string& venue, std::uint8_t side, std::int64_t q,
                                const std::string& source) {
    auto it = t.find(venue);
    if (it == t.end()) return std::nullopt;
    if (source == "exec") return it->second.exec_hazard(side, q);
    if (source == "cancel") return it->second.cancel_hazard(side, q);
    return it->second.hazard(side, q);
}

TEST(F7QueueCancel, FixedVectors) {
    const Value g = load_golden("expected_f7_queue_cancel.json");
    const Value& in = g.at("inputs");
    const Value& exp = g.at("expected");
    Array rules;
    for (const Value& c : in.at("rules").as_array()) {
        rules.push_back(vs(iap::cancel_rule_name(iap::queue_cancel_rule(
            c.at("ahead_qty").as_int64(), c.at("max_ahead_qty").as_int64(), opt_num(c.at("p_fill")),
            c.at("min_p_fill").as_number()))));
    }
    expect_canonical_eq(exp.at("rules"), Value(std::move(rules)), "rules");
    Array buckets;
    for (const Value& c : in.at("buckets").as_array()) {
        std::vector<std::int64_t> edges;
        for (const Value& e : c.at("edges").as_array()) edges.push_back(e.as_int64());
        buckets.push_back(vs(iap::queue_bucket(c.at("q").as_int64(), edges)));
    }
    expect_canonical_eq(exp.at("buckets"), Value(std::move(buckets)), "buckets");
    const auto tables = tables_from(in.at("calibration_docs"));
    Array dep;
    for (const Value& c : in.at("depletion").as_array()) {
        dep.push_back(vod(depletion(tables, c.at("venue").as_string(),
                                    static_cast<std::uint8_t>(c.at("side").as_int64()),
                                    c.at("q").as_int64(), c.at("source").as_string())));
    }
    expect_canonical_eq(exp.at("depletion"), Value(std::move(dep)), "depletion");
    Array hp;
    for (const Value& c : in.at("hazard_fill_prob").as_array()) {
        const std::optional<double> lam = opt_num(c.at("lam"));
        const iap::FillProb fp = iap::hazard_fill_prob(
            [lam](std::uint16_t, std::uint8_t, std::int64_t) { return lam; }, c.at("horizon_s").as_number());
        hp.push_back(vod(fp(1, 0, 0)));
    }
    expect_canonical_eq(exp.at("hazard_fill_prob"), Value(std::move(hp)), "hazard_fill_prob");
    EXPECT_THROW(iap::hazard_fill_prob([](std::uint16_t, std::uint8_t, std::int64_t) { return 1.0; }, 0.0),
                 std::invalid_argument);
    const iap::FillHazardModel m = iap::FillHazardModel::from_value(in.at("model"));
    std::vector<std::int64_t> t_ns;
    for (const Value& t : in.at("p_fill_t_ns").as_array()) t_ns.push_back(t.as_int64());
    Array feats;
    for (const Value& c : in.at("features").as_array()) {
        const iap::HazardFeatures x = m.features(c.at("queue_ahead").as_number(), c.at("spread_ticks").as_number(),
                                                 c.at("imbalance").as_number(), opt_num(c.at("depletion_per_s")),
                                                 c.at("ts_ns").as_int64());
        Object o;
        o["features"] = features_row(x);
        Array h;
        for (const double v : m.bucket_hazards(x)) h.push_back(vd(v));
        o["bucket_hazards"] = Value(std::move(h));
        Array p;
        for (const std::int64_t t : t_ns) p.push_back(vd(m.p_fill_within(x, t)));
        o["p_fill"] = Value(std::move(p));
        feats.push_back(Value(std::move(o)));
    }
    expect_canonical_eq(exp.at("features"), Value(std::move(feats)), "features");
    Array tod;
    for (const std::int64_t ts : {std::int64_t{0}, std::int64_t{1}, std::int64_t{86'399'999'999'999},
                                  std::int64_t{86'400'000'000'000}, std::int64_t{1'548'858'600'000'000'000},
                                  std::int64_t{-1}}) {
        tod.push_back(vd(iap::time_of_day_utc(ts)));
    }
    expect_canonical_eq(exp.at("tod_utc"), Value(std::move(tod)), "tod_utc");
}

TEST(F7QueueCancel, RealSliceSamples) {
    const Value g = load_golden("expected_f7_queue_cancel.json");
    const Value& in = g.at("inputs");
    const Value& sl = in.at("slice");
    const iap::FillHazardModel m = iap::FillHazardModel::from_value(in.at("model"));
    const iap::QueueDepletionTable cal = iap::QueueDepletionTable::from_value(in.at("calibration_docs").at("101"));
    const std::int64_t max_ahead = sl.at("max_ahead_qty").as_int64();
    const double min_p = sl.at("min_p_fill").as_number();
    const std::int64_t horizon = sl.at("horizon_ns").as_int64();
    const auto events = read_slice();
    iap::ConsolidatedBook book(kSliceInstrument);
    Array rows;
    for (std::size_t k = 1; k <= events.size(); ++k) {
        book.apply(events[k - 1]);
        if (!is_sample(k)) continue;
        const auto bb = book.books().at(kSliceVenue).best_bid();
        const auto ba = book.books().at(kSliceVenue).best_ask();
        if (!bb.has_value() || !ba.has_value()) continue;
        for (std::uint8_t side = 0; side < 2; ++side) {
            const std::int64_t ahead = side == 0 ? bb->second : ba->second;
            const std::int64_t own = ahead;
            const std::int64_t opp = side == 0 ? ba->second : bb->second;
            const double spread = static_cast<double>(ba->first - bb->first);
            const double imb = 1.0 - iap::book_pressure(own, opp);
            const std::optional<double> dep = cal.exec_hazard(side, ahead);
            const iap::HazardFeatures x = m.features(static_cast<double>(ahead), spread, imb, dep,
                                                     events[k - 1].exchange_ts);
            const double p = m.p_fill_within(x, horizon);
            Object o;
            o["k"] = vi(static_cast<std::int64_t>(k));
            o["side"] = vi(side);
            o["ahead_qty"] = vi(ahead);
            o["features"] = features_row(x);
            o["depletion_per_s"] = vod(dep);
            o["p_fill"] = vd(p);
            o["rule"] = vs(iap::cancel_rule_name(iap::queue_cancel_rule(ahead, max_ahead, p, min_p)));
            o["rule_ahead_only"] = vs(iap::cancel_rule_name(iap::queue_cancel_rule(ahead, max_ahead, std::nullopt, 0.0)));
            rows.push_back(Value(std::move(o)));
        }
    }
    expect_canonical_eq(g.at("expected").at("slice"), Value(std::move(rows)), "slice");
}

}  // namespace
