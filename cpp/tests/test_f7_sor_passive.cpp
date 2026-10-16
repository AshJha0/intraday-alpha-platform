// v1.17 F7 rule 2: CostAwareRouter passive routing with fill probability
// (passive_costs / route_passive with the model's p_fill_touch and with
// hazard_fill_prob over the X-4 depletion read), aggressive_costs /
// route_aggressive and allocate_passive — parity with
// tests/golden/expected_f7_sor_passive.json (make_golden_f7.py --only
// r02_sor_passive).

#include <gtest/gtest.h>

#include "f7_util.hpp"
#include "iap/execution/queue_depletion.hpp"
#include "iap/sor/sor_v2.hpp"

namespace {

using namespace iap_f7;

using Tables = std::map<std::uint16_t, iap::QueueDepletionTable>;

Tables tables_from(const Value& docs) {
    Tables out;
    for (const auto& [vid, doc] : docs.as_object()) {
        out[static_cast<std::uint16_t>(std::stoul(vid))] = iap::QueueDepletionTable::from_value(doc);
    }
    return out;
}

iap::FillProb fill_prob_from(const Tables& tables, const std::string& source, double horizon_s) {
    iap::HazardRate rate = [&tables, source](std::uint16_t vid, std::uint8_t side,
                                             std::int64_t q) -> std::optional<double> {
        auto it = tables.find(vid);
        if (it == tables.end()) return std::nullopt;
        if (source == "exec") return it->second.exec_hazard(side, q);
        if (source == "cancel") return it->second.cancel_hazard(side, q);
        return it->second.hazard(side, q);
    };
    return iap::hazard_fill_prob(rate, horizon_s);
}

Value costs_value(const std::map<std::uint16_t, double>& costs) {
    Object o;
    for (const auto& [vid, c] : costs) o[std::to_string(vid)] = vd(c);
    return Value(std::move(o));
}

Value alloc_value(const std::map<std::uint16_t, std::int64_t>& a) {
    Object o;
    for (const auto& [vid, q] : a) o[std::to_string(vid)] = vi(q);
    return Value(std::move(o));
}

Value run_queries(iap::CostAwareRouter& r, const iap::ConsolidatedBook& book,
                  const Value& queries, const Tables& tables) {
    Array out;
    for (const Value& q : queries.as_array()) {
        r.options() = router_options_from(q.at("options"));
        const auto cands = candidates_from(q.at("candidates"));
        const auto* cp = cands.has_value() ? &*cands : nullptr;
        const auto side = static_cast<std::uint8_t>(q.at("side").as_int64());
        const std::int64_t ts = q.at("ts").as_int64();
        const double tick = q.at("tick").as_number();
        std::optional<iap::FillProb> fp;
        if (!q.at("fill_prob").is_null()) {
            fp = fill_prob_from(tables, q.at("fill_prob").at("source").as_string(),
                                q.at("fill_prob").at("horizon_s").as_number());
        }
        const std::string kind = q.at("kind").as_string();
        Object res;
        try {
            if (kind == "passive") {
                res["costs"] = costs_value(
                    r.passive_costs(book, side, ts, tick, cp, fp.has_value() ? &*fp : nullptr));
                res["route"] = vi(r.route_passive(book, side, ts, tick, cp,
                                                  fp.has_value() ? &*fp : nullptr));
            } else if (kind == "aggressive") {
                res["costs"] = costs_value(r.aggressive_costs(book, side, ts, tick, cp));
                res["route"] = vi(r.route_aggressive(book, side, ts, tick, cp));
            } else {
                res["allocation"] =
                    alloc_value(r.allocate_passive(book, side, q.at("qty").as_int64(), cp));
            }
        } catch (const std::invalid_argument& e) {
            res["error"] = vs(e.what());
        }
        out.push_back(Value(std::move(res)));
    }
    return Value(std::move(out));
}

TEST(F7SorPassive, FixedVectors) {
    const Value g = load_golden("expected_f7_sor_passive.json");
    const Tables tables = tables_from(g.at("inputs").at("calibration_docs"));
    Array got;
    for (const Value& s : g.at("inputs").at("fixed").as_array()) {
        const auto models = venue_models_from(s.at("venues"));
        iap::ConsolidatedBook book(7);
        for (const auto& ev : events_from_rows(s.at("events"))) book.apply(ev);
        iap::FeeLedger ledger(models);
        for (const Value& f : s.at("ledger_fills").as_array()) {
            ledger.record(static_cast<std::uint16_t>(f.at(0).as_uint64()), f.at(1).as_int64(),
                          f.at(2).as_int64(), f.at(3).as_bool());
        }
        iap::CostAwareRouter r(models, iap::RouterOptions{},
                               sor_options_from(s.at("sor_options")), &ledger);
        got.push_back(run_queries(r, book, s.at("queries"), tables));
    }
    expect_canonical_eq(g.at("expected").at("fixed"), Value(std::move(got)), "fixed");
}

TEST(F7SorPassive, RealSliceSamples) {
    const Value g = load_golden("expected_f7_sor_passive.json");
    const Tables tables = tables_from(g.at("inputs").at("calibration_docs"));
    const Value& in = g.at("inputs").at("slice");
    const auto models = venue_models_from(in.at("venues"));
    iap::FeeLedger ledger(models);
    iap::CostAwareRouter r(models, iap::RouterOptions{}, sor_options_from(in.at("sor_options")),
                           &ledger);
    const auto events = read_slice();
    iap::ConsolidatedBook book(kSliceInstrument);
    iap::RouterOptions base, pen, alloc, alloc1;
    pen.latency_penalty_bps_per_ms = 0.5;
    pen.miss_cost_bps = 1.25;
    pen.tox_horizon = "100ms";
    alloc.max_toxicity_bps = 1.0;
    alloc.tox_scale_bps = 0.5;
    alloc.lot = 100;
    alloc1.tox_scale_bps = 2.0;
    alloc1.lot = 1;
    const iap::FillProb exec1 = fill_prob_from(tables, "exec", 1.0);
    const iap::FillProb exec5 = fill_prob_from(tables, "exec", 5.0);
    Array rows;
    for (std::size_t k = 1; k <= events.size(); ++k) {
        book.apply(events[k - 1]);
        if (!is_sample(k)) continue;
        const std::int64_t t = events[k - 1].exchange_ts;
        Array res;
        auto passive = [&](std::uint8_t side, const iap::RouterOptions& o, const iap::FillProb* fp) {
            r.options() = o;
            Object e;
            e["costs"] = costs_value(r.passive_costs(book, side, t, kSliceTick, nullptr, fp));
            e["route"] = vi(r.route_passive(book, side, t, kSliceTick, nullptr, fp));
            res.push_back(Value(std::move(e)));
        };
        auto aggressive = [&](std::uint8_t side, const iap::RouterOptions& o) {
            r.options() = o;
            Object e;
            e["costs"] = costs_value(r.aggressive_costs(book, side, t, kSliceTick));
            e["route"] = vi(r.route_aggressive(book, side, t, kSliceTick));
            res.push_back(Value(std::move(e)));
        };
        auto allocate = [&](std::uint8_t side, const iap::RouterOptions& o, std::int64_t qty) {
            r.options() = o;
            Object e;
            e["allocation"] = alloc_value(r.allocate_passive(book, side, qty));
            res.push_back(Value(std::move(e)));
        };
        passive(0, base, nullptr);
        passive(1, base, nullptr);
        passive(0, pen, &exec1);
        passive(1, base, &exec5);
        aggressive(0, base);
        aggressive(1, pen);
        allocate(0, alloc, 1050);
        allocate(1, alloc1, 777);
        Object row;
        row["k"] = vi(static_cast<std::int64_t>(k));
        row["ts"] = vi(t);
        row["results"] = Value(std::move(res));
        rows.push_back(Value(std::move(row)));
    }
    expect_canonical_eq(g.at("expected").at("slice"), Value(std::move(rows)), "slice");
}

}  // namespace
