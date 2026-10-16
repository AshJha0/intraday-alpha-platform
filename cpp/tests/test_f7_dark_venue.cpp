// v1.17 F7 part 2, rule 11: the B10 dark / midpoint venue model and the
// dark-first replay rule — parity with tests/golden/expected_f7_dark_venue.json
// (make_golden_f7.py --only r11_dark_venue): sigmoid, dark_features, p_fill
// per model document, midpoint_ticks, the stateless pre-trade check (the
// replay-path mirror of the Rust-owned F8 controls), the B6 "dark" fill
// type, and ExecutionReplay with dark_first parents over the golden stream
// and the real slice. Every dark fill is MODELLED, not measured.

#include <gtest/gtest.h>

#include "f7_replay_util.hpp"
#include "iap/execution/dark.hpp"
#include "iap/tca/markout_decomposition.hpp"

namespace {

using namespace iap_f7;

std::map<std::string, iap::DarkFillModel> models_from(const Value& docs) {
    std::map<std::string, iap::DarkFillModel> out;
    for (const auto& [name, doc] : docs.as_object()) out[name] = iap::DarkFillModel::from_value(doc);
    return out;
}

iap::DarkControls controls_from(const Value& v) {
    iap::DarkControls c;
    c.max_participation = v.at("max_participation").as_number();
    c.participation_window_ns = v.at("participation_window_ns").as_int64();
    c.allowed_venue_types.clear();
    for (const Value& t : v.at("allowed_venue_types").as_array()) {
        const auto vt = iap::venue_type_from_name(t.as_string());
        if (!vt.has_value()) throw std::runtime_error("unknown venue type in golden");
        c.allowed_venue_types.push_back(*vt);
    }
    c.max_dark_notional = v.at("max_dark_notional").as_number();
    return c;
}

iap::ExecConfig config_v2_from(const Value& v) {
    iap::ExecConfig c = exec_config_from(v);
    for (const Value& x : v.at("venues").as_array()) {
        const auto vid = static_cast<std::uint16_t>(x.at("venue_id").as_uint64());
        const auto vt = iap::venue_type_from_name(x.at("venue_type").as_string());
        if (!vt.has_value()) throw std::runtime_error("unknown venue type in golden");
        c.venues.at(vid).venue_type = *vt;
    }
    return c;
}

iap::ParentOrder parent_v2_from(const Value& v, const std::map<std::string, iap::DarkFillModel>& models) {
    iap::ParentOrder p = parent_from(v);
    const Value& d = v.at("dark_first");
    if (d.is_null()) return p;
    iap::DarkFirstParams dp;
    dp.model = models.at(d.at("model").as_string());
    dp.venue_id = static_cast<std::uint16_t>(d.at("venue_id").as_uint64());
    dp.controls = controls_from(d.at("controls"));
    dp.max_urgency = d.at("max_urgency").as_number();
    dp.regimes.clear();
    for (const Value& r : d.at("regimes").as_array()) {
        const auto reg = iap::urgency_regime_from_name(r.as_string());
        if (!reg.has_value()) throw std::runtime_error("unknown regime in golden");
        dp.regimes.push_back(*reg);
    }
    dp.horizon_ns = d.at("horizon_ns").as_int64();
    p.dark_first = dp;
    return p;
}

Value fill_row_v2(const iap::Fill& f) {
    Value row = fill_row(f);
    row.as_array().push_back(Value(f.dark_midpoint));
    return row;
}

Value child_row_v2(const iap::ChildOrder& o) {
    Value row = child_row(o);
    Array& a = row.as_array();
    a.push_back(vs(o.venue_type == 0 ? "" : iap::venue_type_name(static_cast<iap::VenueType>(o.venue_type))));
    a.push_back(vs(iap::dark_outcome_name(static_cast<iap::DarkOutcome>(o.dark_outcome))));
    a.push_back(vd(o.dark_u));
    a.push_back(vd(o.dark_p_fill));
    return row;
}

Value report_row_v2(const iap::ParentReport& r) {
    Value row = report_row(r);
    Object& o = row.as_object();
    o["dark_children"] = vi(r.dark_children);
    o["dark_posted_qty"] = vi(r.dark_posted_qty);
    o["dark_filled_qty"] = vi(r.dark_filled_qty);
    o["dark_expired_qty"] = vi(r.dark_expired_qty);
    o["dark_unsent_qty"] = vi(r.dark_unsent_qty);
    o["dark_notional"] = vd(r.dark_notional);
    Object rej;
    for (const auto& [code, n] : r.dark_rejected) rej[code] = vi(n);
    o["dark_rejected"] = Value(std::move(rej));
    return row;
}

Value run_dark_scenario(const Value& s, const std::map<std::string, iap::DarkFillModel>& models,
                        const std::vector<iap::MarketEvent>* slice) {
    const iap::ExecConfig cfg = config_v2_from(s.at("config"));
    std::vector<iap::ParentOrder> parents;
    for (const Value& p : s.at("parents").as_array()) parents.push_back(parent_v2_from(p, models));
    iap::ExecutionReplay replay(cfg, parents);
    const auto events = events_for(s.at("events").as_string(), slice);
    const iap::ExecReplayResult res = replay.run(events);
    Object out;
    out["events_processed"] = vu(res.events_processed);
    out["sor_no_route"] = vu(res.sor_no_route);
    Array fills;
    for (const auto& f : res.fills) fills.push_back(fill_row_v2(f));
    Object reports;
    for (const auto& [pid, r] : res.parents) reports[std::to_string(pid)] = report_row_v2(r);
    out["parents"] = Value(std::move(reports));
    Array children;
    for (const auto& [pid, kids] : res.children) {
        (void)pid;
        for (const auto& o : kids) children.push_back(child_row_v2(o));
    }
    Object stats;
    for (const auto& [pid, st] : res.passive) stats[std::to_string(pid)] = stats_row(st);
    out["passive"] = Value(std::move(stats));
    if (s.at("digest").as_bool()) {
        out["fills_count"] = vi(static_cast<std::int64_t>(fills.size()));
        out["fills_sha256"] = vs(iap::contracts::content_hash(Value(std::move(fills))));
        out["children_count"] = vi(static_cast<std::int64_t>(children.size()));
        out["children_sha256"] = vs(iap::contracts::content_hash(Value(std::move(children))));
    } else {
        out["fills"] = Value(std::move(fills));
        out["children"] = Value(std::move(children));
    }
    return Value(std::move(out));
}

TEST(F7DarkVenue, ScalarRules) {
    const Value g = load_golden("expected_f7_dark_venue.json");
    const Value& in = g.at("inputs");
    const Value& exp = g.at("expected");
    const auto models = models_from(in.at("models"));
    Array sig;
    for (const Value& x : in.at("sigmoid").as_array()) sig.push_back(vd(iap::sigmoid(x.as_number())));
    expect_canonical_eq(exp.at("sigmoid"), Value(std::move(sig)), "sigmoid");
    Array feats;
    Object pf;
    for (const auto& [name, m] : models) pf[name] = Value(Array{});
    for (const Value& c : in.at("features").as_array()) {
        const auto x = iap::dark_features(c.at(0).as_int64(), c.at(1).as_int64(), c.at(2).as_int64(),
                                          c.at(3).as_int64());
        feats.push_back(Value(Array{vd(x[0]), vd(x[1]), vd(x[2]), vd(x[3])}));
        for (const auto& [name, m] : models) {
            pf[name].as_array().push_back(vd(m.p_fill(c.at(0).as_int64(), c.at(1).as_int64(),
                                                      c.at(2).as_int64(), c.at(3).as_int64())));
        }
    }
    expect_canonical_eq(exp.at("features"), Value(std::move(feats)), "features");
    expect_canonical_eq(exp.at("p_fill"), Value(std::move(pf)), "p_fill");
    Array errs;
    for (const Value& c : in.at("feature_errors").as_array()) {
        try {
            iap::dark_features(c.at(0).as_int64(), c.at(1).as_int64(), c.at(2).as_int64(), c.at(3).as_int64());
            errs.push_back(Value(nullptr));
        } catch (const std::invalid_argument& e) {
            errs.push_back(vs(e.what()));
        }
    }
    expect_canonical_eq(exp.at("feature_errors"), Value(std::move(errs)), "feature_errors");
    Array mid;
    for (const Value& c : in.at("midpoint").as_array()) {
        mid.push_back(vi(iap::midpoint_ticks(c.at(0).as_int64(), c.at(1).as_int64(),
                                             static_cast<std::uint8_t>(c.at(2).as_int64()))));
    }
    expect_canonical_eq(exp.at("midpoint"), Value(std::move(mid)), "midpoint");
    Array ctl;
    for (const Value& c : in.at("controls").as_array()) {
        try {
            const auto vt = iap::venue_type_from_name(c.at("venue_type").as_string());
            ASSERT_TRUE(vt.has_value());
            ctl.push_back(vs(iap::dark_control_code_name(iap::dark_pretrade_check(
                controls_from(c.at("controls")), *vt, c.at("qty").as_int64(),
                c.at("trailing_lit_volume").as_int64(), c.at("dark_notional_so_far").as_number(),
                c.at("child_notional").as_number()))));
        } catch (const std::invalid_argument& e) {
            Object o;
            o["error"] = vs(e.what());
            ctl.push_back(Value(std::move(o)));
        }
    }
    expect_canonical_eq(exp.at("controls"), Value(std::move(ctl)), "controls");
    // the B6 fill type of a modelled dark fill, before the liquidity split
    iap::ChildOrder o;
    o.order_id = 1;
    o.replaces_order_id = 7;
    o.cancel_rule = static_cast<std::uint8_t>(iap::CancelRule::FADE_SIGNAL);
    o.cancel_rule_ts = 0;
    iap::Fill f;
    f.liquidity = iap::Liquidity::MAKER;
    f.ts = 10;
    f.dark_midpoint = true;
    iap::Fill lit = f;
    lit.dark_midpoint = false;
    iap::Fill tak = f;
    tak.liquidity = iap::Liquidity::TAKER;
    expect_canonical_eq(exp.at("fill_type"),
                        Value(Array{vs(iap::fill_type(f, o)), vs(iap::fill_type(lit, o)), vs(iap::fill_type(tak, o))}),
                        "fill_type");
    // a document without the modelled label is refused
    Object bad;
    bad["coef"] = Value(Array{Value(0.0), Value(0.0), Value(0.0), Value(0.0)});
    bad["horizon_ns"] = Value(1LL);
    EXPECT_THROW(iap::DarkFillModel::from_value(Value(std::move(bad))), std::invalid_argument);
}

TEST(F7DarkVenue, ReplayScenarios) {
    const Value g = load_golden("expected_f7_dark_venue.json");
    const auto models = models_from(g.at("inputs").at("models"));
    const Array& scen = g.at("inputs").at("scenarios").as_array();
    const Array& exp = g.at("expected").at("scenarios").as_array();
    ASSERT_EQ(scen.size(), exp.size());
    std::vector<iap::MarketEvent> slice;
    for (std::size_t i = 0; i < scen.size(); ++i) {
        if (scen[i].at("events").as_string() == "slice" && slice.empty()) slice = read_slice();
        const Value got = run_dark_scenario(scen[i], models, &slice);
        expect_canonical_eq(exp[i], got, "scenario " + std::to_string(i) + " (" +
                                             scen[i].at("label").as_string() + ")");
    }
}

TEST(F7DarkVenue, NeedsADarkVenue) {
    iap::ExecConfig cfg;
    iap::VenueSpec v;
    v.venue_id = 1;
    cfg.venues[1] = v;
    iap::InstrumentSpec ins;
    ins.instrument_id = 1;
    ins.tick_size = 0.01;
    cfg.instruments[1] = ins;
    iap::ParentOrder p;
    p.parent_id = 1;
    p.instrument_id = 1;
    p.venue_id = 1;
    p.qty = 100;
    p.start_ts = 0;
    p.end_ts = 10;
    iap::DarkFirstParams d;
    d.model = iap::DarkFillModel::prior();
    p.dark_first = d;
    EXPECT_THROW(static_cast<void>(iap::ExecutionReplay(cfg, {p})), std::invalid_argument);
}

}  // namespace
