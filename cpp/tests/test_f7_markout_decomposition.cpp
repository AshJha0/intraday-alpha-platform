// v1.17 F7 rule 10: B6 markout decomposition parity with
// tests/golden/expected_f7_markout_decomposition.json (make_golden_f7.py
// --only r10_markout_decomposition): fill_type / regime_of / fill_rows /
// cluster_mean / decompose / markout_decomposition, canonical bytes equal
// on every field the port computes, the SciPy Student-t readings
// (p_student_t, ci95) to the golden's tolerance.

#include <gtest/gtest.h>

#include "f7_util.hpp"
#include "iap/tca/markout.hpp"
#include "iap/tca/markout_decomposition.hpp"

namespace {

using namespace iap_f7;

const std::vector<std::string> kTolKeys = {"p_student_t", "ci95"};

iap::MarketTimeline timeline_from(const Value& d) {
    iap::MarketTimeline tl;
    for (const Value& s : d.at("states").as_array()) {
        tl.append_state_pinned(s.at(0).as_int64(), s.at(1).as_number(), s.at(2).as_number(),
                               s.at(3).as_int64(), s.at(4).as_int64());
    }
    for (const Value& h : d.at("halts").as_array()) tl.add_halt(h.as_int64());
    return tl;
}

std::map<std::uint64_t, iap::ChildOrder> orders_from(const Value& arr) {
    std::map<std::uint64_t, iap::ChildOrder> out;
    for (const Value& r : arr.as_array()) {
        iap::ChildOrder o;
        o.order_id = r.at("order_id").as_uint64();
        o.type = iap::OrderType::LIMIT;
        const auto rule = iap::cancel_rule_from_name(r.at("cancel_rule").as_string());
        if (!rule.has_value()) throw std::runtime_error("unknown cancel_rule in golden");
        o.cancel_rule = static_cast<std::uint8_t>(*rule);
        o.cancel_rule_ts = r.at("cancel_rule_ts").as_int64();
        o.replaces_order_id = r.at("replaces_order_id").as_uint64();
        const auto regime = iap::urgency_regime_from_name(r.at("urgency_regime").as_string());
        if (!regime.has_value()) throw std::runtime_error("unknown urgency_regime in golden");
        o.urgency_regime = static_cast<std::uint8_t>(*regime);
        out[o.order_id] = o;
    }
    return out;
}

std::vector<iap::Fill> fills_from(const Value& arr) {
    std::vector<iap::Fill> out;
    for (const Value& r : arr.as_array()) {
        iap::Fill f;
        f.fill_id = r.at(0).as_uint64();
        f.order_id = r.at(1).as_uint64();
        f.parent_id = r.at(2).as_uint64();
        f.instrument_id = static_cast<std::uint32_t>(r.at(3).as_uint64());
        f.venue_id = static_cast<std::uint16_t>(r.at(4).as_uint64());
        f.side = static_cast<std::uint8_t>(r.at(5).as_int64());
        f.price_ticks = r.at(6).as_int64();
        f.qty = r.at(7).as_int64();
        f.ts = r.at(8).as_int64();
        f.liquidity = r.at(9).as_string() == "TAKER" ? iap::Liquidity::TAKER : iap::Liquidity::MAKER;
        f.fee = r.at(10).as_number();
        f.impact_cost = r.at(11).as_number();
        out.push_back(f);
    }
    return out;
}

iap::Horizons horizons_from(const Value& v, const std::vector<std::string>& order) {
    iap::Horizons out;
    for (const std::string& name : order) out.emplace_back(name, v.at(name).as_int64());
    return out;
}

const std::vector<std::string> kHorizonOrder = {"100ms", "1s", "5s", "30s"};

Value run(const iap::MarketTimeline& tl, const std::map<std::uint64_t, iap::ChildOrder>& orders,
          const std::vector<iap::Fill>& fills, double tick, double qty_unit, const iap::Horizons& hz,
          std::int64_t day_ns, const std::string* cluster, bool default_horizons) {
    Object out;
    Array types, regimes;
    for (const iap::Fill& f : fills) {
        types.push_back(vs(iap::fill_type(f, orders.at(f.order_id))));
        regimes.push_back(vs(iap::regime_of(orders.at(f.order_id))));
    }
    out["types"] = Value(std::move(types));
    out["regimes"] = Value(std::move(regimes));
    const Value rows = iap::fill_rows(fills, orders, tl, tick, qty_unit, &hz, cluster, day_ns);
    out["rows"] = rows;
    std::vector<std::string> names;
    for (const auto& [n, h] : hz) {
        (void)h;
        names.push_back(n);
    }
    // markout_decomposition builds its rows with the fill's day as the
    // cluster; with a caller cluster the Python golden calls it the same way
    // (the cluster argument of fill_rows only), so the two agree only when
    // no caller cluster is given — the golden passes `cluster` to fill_rows
    // and markout_decomposition separately, as here.
    out["markout_decomposition"] = iap::markout_decomposition(fills, orders, tl, tick, qty_unit, &hz, day_ns);
    if (default_horizons) out["decomposition_default_horizons"] = iap::decompose(rows, &names);
    return Value(std::move(out));
}

void check_split(const Value& expected, const Value& expected_tol, const Value& got, double tol,
                 const std::string& what) {
    auto [plain, t] = split_tol(got, kTolKeys);
    expect_canonical_eq(expected, plain, what);
    expect_tree_close(expected_tol, t, tol, what + " (tolerance fields)");
}

TEST(F7MarkoutDecomposition, ClusterMean) {
    const Value g = load_golden("expected_f7_markout_decomposition.json");
    const double tol = g.at("tolerance").as_number();
    Array got;
    for (const Value& c : g.at("inputs").at("cluster_mean").as_array()) {
        std::vector<double> vals;
        std::vector<std::string> cl;
        for (const Value& v : c.at("values").as_array()) vals.push_back(v.as_number());
        for (const Value& v : c.at("clusters").as_array()) cl.push_back(v.as_string());
        try {
            got.push_back(iap::cluster_mean(vals, cl));
        } catch (const std::invalid_argument& e) {
            Object o;
            o["error"] = vs(e.what());
            got.push_back(Value(std::move(o)));
        }
    }
    check_split(g.at("expected").at("cluster_mean"), g.at("expected_tol").at("cluster_mean"),
                Value(std::move(got)), tol, "cluster_mean");
}

TEST(F7MarkoutDecomposition, FixedVectors) {
    const Value g = load_golden("expected_f7_markout_decomposition.json");
    const double tol = g.at("tolerance").as_number();
    const Value& in = g.at("inputs");
    const iap::MarketTimeline tl = timeline_from(in.at("timeline"));
    const auto orders = orders_from(in.at("orders"));
    const auto fills = fills_from(in.at("fills"));
    const iap::Horizons hz = horizons_from(in.at("horizons"), kHorizonOrder);
    const std::string s_cluster = "s";
    check_split(g.at("expected").at("day_clusters"), g.at("expected_tol").at("day_clusters"),
                run(tl, orders, fills, 0.01, 1.0, hz, 100'000'000'000, nullptr, true), tol, "day_clusters");
    check_split(g.at("expected").at("one_cluster"), g.at("expected_tol").at("one_cluster"),
                run(tl, orders, fills, 0.01, 100.0, hz, 86'400'000'000'000, &s_cluster, true), tol,
                "one_cluster");
}

TEST(F7MarkoutDecomposition, RealSlice) {
    const Value g = load_golden("expected_f7_markout_decomposition.json");
    const double tol = g.at("tolerance").as_number();
    const Value& in = g.at("inputs").at("slice");
    const auto events = read_slice();
    iap::ConsolidatedBook book(kSliceInstrument);
    iap::MarketTimeline tl;
    for (std::size_t k = 1; k <= events.size(); ++k) {
        book.apply(events[k - 1]);
        const auto bb = book.books().at(kSliceVenue).best_bid();
        const auto ba = book.books().at(kSliceVenue).best_ask();
        if (!bb.has_value() || !ba.has_value()) continue;
        tl.append_state_pinned(events[k - 1].exchange_ts, static_cast<double>(bb->first) * kSliceTick,
                               static_cast<double>(ba->first) * kSliceTick, bb->second, ba->second);
    }
    const auto orders = orders_from(in.at("orders"));
    const auto fills = fills_from(in.at("fills"));
    const iap::Horizons hz = horizons_from(in.at("horizons"), kHorizonOrder);
    check_split(g.at("expected").at("slice"), g.at("expected_tol").at("slice"),
                run(tl, orders, fills, in.at("tick_size").as_number(), in.at("qty_unit").as_number(), hz,
                    in.at("day_ns").as_int64(), nullptr, false),
                tol, "slice");
}

}  // namespace
