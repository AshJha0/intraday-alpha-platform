// Replay-scenario helpers of the F7 parity tests (rules 3 and 4): the
// ExecConfig / ParentOrder rows of python/tools/f7_golden/_replay.py, the
// scenario runner and its result tree (fills, children, parent reports,
// passive stats; digested when the scenario says so).

#pragma once

#include <map>
#include <string>
#include <vector>

#include "f7_util.hpp"
#include "iap/execution/algos.hpp"
#include "iap/replay/exec_replay.hpp"

namespace iap_f7 {

inline iap::ExecConfig exec_config_from(const Value& v) {
    iap::ExecConfig c;
    c.latency.decision_ns = v.at("latency").at("decision_ns").as_int64();
    c.latency.risk_ns = v.at("latency").at("risk_ns").as_int64();
    c.latency.wire_ns = v.at("latency").at("wire_ns").as_int64();
    c.seed = v.at("seed").as_uint64();
    c.impact_coeff_bps_per_pct_adv = v.at("impact_coeff_bps_per_pct_adv").as_number();
    c.impact_model = v.at("impact_model").as_string() == "parent" ? iap::ImpactModel::PARENT
                                                                   : iap::ImpactModel::CHILD;
    c.impact_exponent = v.at("impact_exponent").as_number();
    c.permanent_impact_bps_per_pct_adv = v.at("permanent_impact_bps_per_pct_adv").as_number();
    for (const Value& i : v.at("instruments").as_array()) {
        iap::InstrumentSpec s = instrument_from(i);
        c.instruments[s.instrument_id] = s;
    }
    for (const Value& x : v.at("venues").as_array()) {
        iap::VenueSpec s = venue_spec_from(x);
        c.venues[s.venue_id] = s;
    }
    return c;
}

inline iap::ParentOrder parent_from(const Value& v) {
    iap::ParentOrder p;
    p.parent_id = v.at("parent_id").as_uint64();
    p.instrument_id = static_cast<std::uint32_t>(v.at("instrument_id").as_uint64());
    p.venue_id = static_cast<std::uint16_t>(v.at("venue_id").as_uint64());
    p.side = static_cast<std::uint8_t>(v.at("side").as_int64());
    p.qty = v.at("qty").as_int64();
    p.algo = static_cast<iap::AlgoType>(v.at("algo").as_int64());
    p.start_ts = v.at("start_ts").as_int64();
    p.end_ts = v.at("end_ts").as_int64();
    p.slices = static_cast<int>(v.at("slices").as_int64());
    p.participation = v.at("participation").as_number();
    p.risk_aversion = v.at("risk_aversion").as_number();
    p.max_child_qty = v.at("max_child_qty").as_int64();
    p.policy = static_cast<iap::ExecPolicy>(v.at("policy").as_int64());
    p.urgency = v.at("urgency").as_number();
    const Value& ps = v.at("passive");
    p.passive.max_rest_ns = ps.at("max_rest_ns").as_int64();
    p.passive.max_reprices = static_cast<int>(ps.at("max_reprices").as_int64());
    p.passive.max_behind_fraction = ps.at("max_behind_fraction").as_number();
    p.passive.improve_min_spread_ticks = ps.at("improve_min_spread_ticks").as_int64();
    p.passive.end_margin_ns = ps.at("end_margin_ns").as_int64();
    p.pov_exact = v.at("pov_exact").as_bool();
    return p;
}

inline std::vector<iap::MarketEvent> events_for(const std::string& spec,
                                                const std::vector<iap::MarketEvent>* slice) {
    if (spec == "golden:events_eq_mbo.jsonl") {
        return iap::read_jsonl(iap::golden_file("events_eq_mbo.jsonl"));
    }
    if (spec == "slice") {
        if (slice == nullptr) throw std::runtime_error("slice events not loaded");
        return *slice;
    }
    throw std::runtime_error("unknown events spec: " + spec);
}

inline Value fill_row(const iap::Fill& f) {
    return Value(Array{vu(f.fill_id), vu(f.order_id), vu(f.parent_id), vu(f.instrument_id),
                       vu(f.venue_id), vi(f.side), vi(f.price_ticks), vi(f.qty), vi(f.ts),
                       vs(f.liquidity == iap::Liquidity::TAKER ? "TAKER" : "MAKER"),
                       vd(f.fee), vd(f.impact_cost)});
}

inline Value child_row(const iap::ChildOrder& o) {
    return Value(Array{vu(o.order_id), vu(o.parent_id), vu(o.venue_id), vi(o.side),
                       vi(static_cast<int>(o.type)), vi(o.limit_ticks), vi(o.qty),
                       vi(o.decision_ts), vi(o.expire_ts), vi(static_cast<int>(o.state)),
                       vi(o.remaining), vi(static_cast<int>(o.cancel_reason))});
}

inline Value report_row(const iap::ParentReport& r) {
    Object o;
    o["filled_qty"] = vi(r.filled_qty);
    o["unfilled_qty"] = vi(r.unfilled_qty);
    o["children"] = vi(r.children);
    o["notional"] = vd(r.notional);
    o["avg_price"] = vd(r.avg_price);
    o["fees"] = vd(r.fees);
    o["rebates"] = vd(r.rebates);
    o["impact"] = vd(r.impact);
    o["total_cost"] = vd(r.total_cost);
    return Value(std::move(o));
}

inline Value stats_row(const iap::PassiveStats& s) {
    Object o;
    o["posts"] = vi(s.posts);
    o["reprices"] = vi(s.reprices);
    o["rest_extensions"] = vi(s.rest_extensions);
    o["crosses_timeout"] = vi(s.crosses_timeout);
    o["crosses_behind"] = vi(s.crosses_behind);
    o["crosses_immediate"] = vi(s.crosses_immediate);
    return Value(std::move(o));
}

// One scenario of the replay goldens (python/tools/f7_golden/_replay.py
// run_scenario), as the golden records it.
inline Value run_scenario(const Value& s, bool with_passive,
                          const std::vector<iap::MarketEvent>* slice) {
    const iap::ExecConfig cfg = exec_config_from(s.at("config"));
    std::vector<iap::ParentOrder> parents;
    for (const Value& p : s.at("parents").as_array()) parents.push_back(parent_from(p));
    iap::ExecutionReplay replay(cfg, parents);
    const auto events = events_for(s.at("events").as_string(), slice);
    const iap::ExecReplayResult res = replay.run(events);
    Object out;
    out["events_processed"] = vu(res.events_processed);
    out["sor_no_route"] = vu(res.sor_no_route);
    Array fills;
    for (const auto& f : res.fills) fills.push_back(fill_row(f));
    Object reports;
    for (const auto& [pid, r] : res.parents) reports[std::to_string(pid)] = report_row(r);
    out["parents"] = Value(std::move(reports));
    // children: by parent_id ascending, submission order within a parent
    // (the Python res.children layout). The simulator's order map is in
    // order-id (= submission) order; group it by parent.
    std::map<std::uint64_t, std::vector<const iap::ChildOrder*>> by_parent;
    for (const auto& [oid, o] : replay.simulator().orders()) {
        (void)oid;
        by_parent[o.parent_id].push_back(&o);
    }
    Array children;
    for (const auto& [pid, list] : by_parent) {
        (void)pid;
        for (const iap::ChildOrder* o : list) children.push_back(child_row(*o));
    }
    if (with_passive) {
        Object stats;
        for (const auto& [pid, st] : res.passive) stats[std::to_string(pid)] = stats_row(st);
        out["passive"] = Value(std::move(stats));
    }
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

}  // namespace iap_f7
