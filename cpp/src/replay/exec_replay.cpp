// Execution replay driver implementation (pinned order in exec_replay.hpp).

#include "iap/replay/exec_replay.hpp"

#include <algorithm>
#include <cmath>
#include <optional>
#include <stdexcept>

#include "iap/marketdata/codec.hpp"
#include "iap/util/sha256.hpp"

namespace iap {

namespace {

const char* algo_name(AlgoType a) {
    switch (a) {
        case AlgoType::TWAP: return "TWAP";
        case AlgoType::VWAP: return "VWAP";
        case AlgoType::POV: return "POV";
        case AlgoType::IS: return "IS";
    }
    throw std::invalid_argument("unknown AlgoType");
}

contracts::OrderType wire_order_type(OrderType t) {
    switch (t) {
        case OrderType::MARKET: return contracts::OrderType::MARKET;
        case OrderType::LIMIT: return contracts::OrderType::LIMIT;
        case OrderType::IOC: return contracts::OrderType::IOC;
        case OrderType::FOK: return contracts::OrderType::FOK;
    }
    throw std::invalid_argument("unknown OrderType");
}

contracts::VenueScore to_score(const SorCandidate& c) {
    contracts::VenueScore s;
    s.venue_id = c.venue_id;
    s.eligible = c.eligible;
    s.displayed_price_ticks = c.displayed_price_ticks;
    s.displayed_qty = c.displayed_qty;
    s.taker_fee = c.taker_fee;
    s.maker_rebate = c.maker_rebate;
    s.commission_per_million = c.commission_per_million;
    s.latency_mean_ns = c.latency_mean_ns;
    s.rank = c.rank;
    return s;
}

}  // namespace

ExecutionReplay::ExecutionReplay(const ExecConfig& config,
                                 const std::vector<ParentOrder>& parents,
                                 SorOptions sor_options)
    : config_(config),
      sim_(config),
      sor_(config.venues, sor_options),
      sor_options_(sor_options) {
    for (const auto& [vid, spec] : config.venues) {
        (void)spec;
        sor_candidates_.push_back(vid);
    }
    for (const auto& p : parents) {
        if (p.qty <= 0) throw std::invalid_argument("parent qty must be > 0");
        if (p.max_child_qty <= 0) {
            throw std::invalid_argument("max_child_qty must be > 0");
        }
        if (p.end_ts <= p.start_ts) {
            throw std::invalid_argument("parent window must have end_ts > start_ts");
        }
        ParentState ps;
        ps.order = p;
        if (p.algo != AlgoType::POV) {
            ps.slice_qty = slice_quantities(p);
            ps.slice_due = slice_times(p);
        }
        parents_.push_back(std::move(ps));
    }
}

bool ExecutionReplay::issue_child(ParentState& ps, std::int64_t child_qty,
                                  std::int64_t decision_ts, bool passive) {
    if (child_qty <= 0) return true;
    const ParentOrder& p = ps.order;
    ChildOrder c;
    c.parent_id = p.parent_id;
    c.instrument_id = p.instrument_id;
    c.side = p.side;
    c.qty = child_qty;
    c.decision_ts = decision_ts;
    c.expire_ts = p.end_ts;  // pinned: no child outlives the window
    const ConsolidatedBook& book = sim_.instrument_book(p.instrument_id);
    if (p.venue_id != 0) {
        c.venue_id = p.venue_id;
    } else if (passive) {
        c.venue_id = sor_.route_passive(book, p.side, sor_candidates_);
    } else {
        c.venue_id = sor_.route_aggressive(book, p.side, sor_candidates_);
    }
    if (c.venue_id == 0) {
        ++sor_no_route_;  // no eligible venue: do not submit (pinned)
        return false;
    }
    if (passive) {
        // Join the same-side best on the routed venue; MARKET fallback.
        const OrderBook* vb = sim_.venue_book(p.instrument_id, c.venue_id);
        std::optional<LevelEntry> best;
        if (vb != nullptr) {
            best = p.side == 0 ? vb->best_bid() : vb->best_ask();
        }
        if (best.has_value()) {
            c.type = OrderType::LIMIT;
            c.limit_ticks = best->first;
        } else {
            c.type = OrderType::MARKET;
        }
    } else {
        c.type = OrderType::MARKET;
    }
    const std::uint64_t child_id = sim_.submit(c);
    ps.child_ids.push_back(child_id);
    if (trace_sink_ != nullptr) {
        record_routing(ps, c, child_id, passive, p.venue_id == 0);
    }
    return true;
}

void ExecutionReplay::record_routing(ParentState& ps, const ChildOrder& child,
                                     std::uint64_t child_id, bool passive,
                                     bool sor_used) {
    contracts::VenueDecision vd;
    vd.child_order_id = child_id;
    vd.venue_id = child.venue_id;
    const ConsolidatedBook& book = sim_.instrument_book(child.instrument_id);
    if (sor_used) {
        if (passive) {
            sor_.score_passive(book, child.side, sor_candidates_, sor_scores_);
            vd.reason = sor_options_.prefer_rebate
                            ? "passive: highest maker rebate quoting our side "
                              "(ties: commission, venue id)"
                            : "passive: lowest venue id quoting our side";
        } else {
            sor_.score_aggressive(book, child.side, sor_candidates_, sor_scores_);
            vd.reason = "aggressive: best displayed opposite price (ties: taker "
                        "fee, commission, venue id)";
        }
        vd.candidates.reserve(sor_scores_.size());
        for (const auto& c : sor_scores_) vd.candidates.push_back(to_score(c));
    } else {
        // The parent pins its venue: the router was not consulted and the
        // pinned venue is the only one considered, so it is recorded as the
        // single eligible candidate whatever its state. Its displayed best is
        // read through the router's view at decision time (0 while the venue
        // is gated — the simulator's rule-8 gate decides whether it fills).
        const std::vector<std::uint16_t> only{child.venue_id};
        if (passive) {
            sor_.score_passive(book, child.side, only, sor_scores_);
        } else {
            sor_.score_aggressive(book, child.side, only, sor_scores_);
        }
        contracts::VenueScore s = to_score(sor_scores_.front());
        s.eligible = true;
        s.rank = 1;
        vd.candidates.push_back(s);
        vd.reason = "venue pinned by parent order (SOR bypassed)";
    }
    ps.routing.push_back(std::move(vd));
}

void ExecutionReplay::issue_slice(ParentState& ps, std::int64_t slice_qty,
                                  std::int64_t decision_ts, bool passive) {
    const std::int64_t cap = ps.order.max_child_qty;
    std::int64_t left = slice_qty;
    while (left > 0) {
        const std::int64_t q = std::min(left, cap);
        issue_child(ps, q, decision_ts, passive);
        left -= q;
    }
}

std::int64_t ExecutionReplay::committed_qty(const ParentState& ps) const {
    std::int64_t open = 0;
    const auto& orders = sim_.orders();
    for (std::uint64_t id : ps.child_ids) {
        const ChildOrder& o = orders.at(id);
        if (o.state == OrderState::PENDING || o.state == OrderState::ACTIVE) {
            open += o.remaining;
        }
    }
    return ps.filled_qty + open;
}

void ExecutionReplay::book_new_fills() {
    const auto& fills = sim_.fills();
    while (fills_booked_ < fills.size()) {
        const Fill& f = fills[fills_booked_++];
        for (auto& ps : parents_) {
            if (ps.order.parent_id == f.parent_id) ps.filled_qty += f.qty;
        }
    }
}

void ExecutionReplay::schedule(ParentState& ps, const MarketEvent& ev) {
    const ParentOrder& p = ps.order;
    const std::int64_t t = ev.exchange_ts;
    if (!ps.triggered && t >= p.start_ts) {
        ps.triggered = true;
        ps.trigger_ts = t;
        ps.trigger_seq = ev.sequence;
    }
    if (p.algo == AlgoType::POV) {
        if (ev.instrument_id != p.instrument_id ||
            static_cast<EventType>(ev.event_type) != EventType::TRADE ||
            t < p.start_ts || t >= p.end_ts) {
            return;
        }
        ps.pov_volume += ev.qty;
        const auto target = static_cast<std::int64_t>(
            std::floor(p.participation * static_cast<double>(ps.pov_volume)));
        // Deficit against FILLED + in-flight qty, never sent qty (pinned).
        std::int64_t deficit = std::min(target, p.qty) - committed_qty(ps);
        if (deficit > 0) {
            issue_child(ps, std::min(deficit, p.max_child_qty), t, false);
        }
        return;
    }
    // TWAP / VWAP / IS: issue every slice that has come due (inside the
    // window only — a slice due at/after end_ts would expire on arrival).
    while (ps.next_slice < ps.slice_due.size() &&
           t >= ps.slice_due[ps.next_slice]) {
        const std::int64_t q = ps.slice_qty[ps.next_slice];
        ++ps.next_slice;
        if (t >= p.end_ts) continue;
        const bool passive =
            p.algo == AlgoType::TWAP || p.algo == AlgoType::VWAP;
        issue_slice(ps, q, t, passive);
    }
}

ExecReplayResult ExecutionReplay::run(const std::vector<MarketEvent>& events) {
    if (ran_) throw std::runtime_error("ExecutionReplay::run is one-shot");
    ran_ = true;
    if (trace_sink_ != nullptr) {
        // Versions are fixed before the loop; nothing below allocates for
        // the trace until the stream has been replayed.
        if (trace_options_.data_version.empty()) {
            trace_options_.data_version = Sha256::hash(encode_iap1(events));
        }
        if (trace_options_.config_version.empty()) {
            trace_options_.config_version =
                contracts::content_hash(config_value(config_, sor_options_));
        }
    }
    ExecReplayResult res;
    for (const auto& ev : events) {
        sim_.on_event(ev);
        book_new_fills();
        for (auto& ps : parents_) schedule(ps, ev);
        ++res.events_processed;
    }
    sim_.cancel_all();
    book_new_fills();

    res.fills = sim_.fills();
    res.sor_no_route = sor_no_route_;
    for (const auto& ps : parents_) {
        ParentReport r;
        r.parent_id = ps.order.parent_id;
        r.children = static_cast<std::int64_t>(ps.child_ids.size());
        const auto iit = config_.instruments.find(ps.order.instrument_id);
        const double lot =
            iit == config_.instruments.end() ? 1.0 : iit->second.qty_unit;
        const double tick =
            iit == config_.instruments.end() ? 1.0 : iit->second.tick_size;
        for (const auto& f : res.fills) {
            if (f.parent_id != ps.order.parent_id) continue;
            if (f.ts < ps.order.start_ts || f.ts > ps.order.end_ts) {
                throw std::runtime_error(
                    "fill outside the parent window (time-in-force broken)");
            }
            r.filled_qty += f.qty;
            r.notional += static_cast<double>(f.qty) * lot *
                          static_cast<double>(f.price_ticks) * tick;
            if (f.fee >= 0.0) {
                r.fees += f.fee;
            } else {
                r.rebates += -f.fee;
            }
            r.impact += f.impact_cost;
        }
        r.unfilled_qty = ps.order.qty - r.filled_qty;
        r.avg_price = r.filled_qty > 0
                          ? r.notional / (static_cast<double>(r.filled_qty) * lot)
                          : 0.0;
        r.total_cost = r.fees - r.rebates + r.impact;
        res.parents[r.parent_id] = r;
    }
    if (trace_sink_ != nullptr) {
        for (const auto& ps : parents_) trace_sink_->emit(build_trace(ps, res));
    }
    return res;
}

void ExecutionReplay::set_trace_sink(contracts::TraceSink* sink,
                                     TraceOptions options) {
    if (ran_) throw std::runtime_error("set_trace_sink must precede run()");
    if (sink == nullptr) throw std::invalid_argument("trace sink is null");
    if (!contracts::is_generic_id(options.session_id)) {
        throw std::invalid_argument(
            "TraceOptions.session_id '" + options.session_id +
            "' is not in the id alphabet [A-Za-z0-9][A-Za-z0-9._:-]{0,127}");
    }
    if (!contracts::is_generic_id(options.strategy_id) ||
        !contracts::is_generic_id(options.alpha_id)) {
        throw std::invalid_argument(
            "TraceOptions.strategy_id / alpha_id must be valid identifiers");
    }
    for (const std::string* v : {&options.feature_version, &options.model_version}) {
        if (!contracts::is_sha256_hex(*v)) {
            throw std::invalid_argument(
                "TraceOptions feature/model version must be 64 lowercase hex "
                "chars (contracts::kVersionNotApplicable when none applies)");
        }
    }
    for (const std::string* v : {&options.data_version, &options.config_version}) {
        if (!v->empty() && !contracts::is_sha256_hex(*v)) {
            throw std::invalid_argument(
                "TraceOptions data/config version override must be 64 "
                "lowercase hex chars or empty (computed)");
        }
    }
    trace_sink_ = sink;
    trace_options_ = std::move(options);
}

contracts::Value ExecutionReplay::config_value(const ExecConfig& config,
                                               const SorOptions& sor_options) {
    using contracts::Object;
    using contracts::Value;
    Object latency;
    latency["decision_ns"] = Value(config.latency.decision_ns);
    latency["risk_ns"] = Value(config.latency.risk_ns);
    latency["wire_ns"] = Value(config.latency.wire_ns);
    Object instruments;
    for (const auto& [iid, ins] : config.instruments) {
        Object o;
        o["instrument_id"] = Value(ins.instrument_id);
        o["tick_size"] = Value(ins.tick_size);
        o["qty_unit"] = Value(ins.qty_unit);
        o["adv"] = Value(ins.adv);
        o["quote_ccy"] = Value(ins.quote_ccy);
        instruments[std::to_string(iid)] = Value(std::move(o));
    }
    Object venues;
    for (const auto& [vid, v] : config.venues) {
        Object o;
        o["venue_id"] = Value(v.venue_id);
        o["name"] = Value(v.name);
        o["is_fx"] = Value(v.is_fx);
        o["taker_fee_per_share"] = Value(v.taker_fee_per_share);
        o["maker_rebate_per_share"] = Value(v.maker_rebate_per_share);
        o["commission_per_million"] = Value(v.commission_per_million);
        o["latency_mean_ns"] = Value(v.latency_mean_ns);
        o["latency_jitter_ns"] = Value(v.latency_jitter_ns);
        venues[std::to_string(vid)] = Value(std::move(o));
    }
    Object sor;
    sor["prefer_rebate"] = Value(sor_options.prefer_rebate);
    sor["max_venue_latency_ns"] = Value(sor_options.max_venue_latency_ns);
    Object root;
    root["latency"] = Value(std::move(latency));
    root["seed"] = Value(config.seed);
    root["impact_coeff_bps_per_pct_adv"] = Value(config.impact_coeff_bps_per_pct_adv);
    root["instruments"] = Value(std::move(instruments));
    root["venues"] = Value(std::move(venues));
    root["sor"] = Value(std::move(sor));
    return Value(std::move(root));
}

contracts::DecisionTrace ExecutionReplay::build_trace(
    const ParentState& ps, const ExecReplayResult& res) const {
    const ParentOrder& p = ps.order;
    contracts::DecisionTrace t;
    t.session_id = trace_options_.session_id;
    t.instrument_id = p.instrument_id;
    t.event_ts = ps.triggered ? ps.trigger_ts : p.start_ts;
    t.sequence = ps.triggered ? ps.trigger_seq : 0;
    t.trace_id = contracts::make_trace_id(t.session_id, t.instrument_id,
                                          t.event_ts, t.sequence);
    t.data_version = trace_options_.data_version;
    t.feature_version = trace_options_.feature_version;
    t.model_version = trace_options_.model_version;
    t.config_version = trace_options_.config_version;

    contracts::ParentOrder po;
    po.parent_order_id = p.parent_id;
    po.strategy_id = trace_options_.strategy_id;
    po.alpha_id = trace_options_.alpha_id;
    po.instrument_id = p.instrument_id;
    po.side = p.side;
    po.qty = p.qty;
    po.algo = contracts::algo_from_string(algo_name(p.algo));
    po.decision_ts = p.start_ts;
    po.arrival_ts = p.start_ts;
    po.end_ts = p.end_ts;
    const bool passive_algo = p.algo == AlgoType::TWAP || p.algo == AlgoType::VWAP;
    po.urgency = passive_algo ? 0.0 : 1.0;
    po.limit_price_ticks = 0;
    po.params["max_child_qty"] = static_cast<double>(p.max_child_qty);
    if (p.algo == AlgoType::POV) {
        po.params["participation"] = p.participation;
    } else {
        po.params["slices"] = static_cast<double>(p.slices);
        if (p.algo == AlgoType::IS) po.params["risk_aversion"] = p.risk_aversion;
    }
    t.stages.parent_orders.push_back(std::move(po));

    const auto& orders = sim_.orders();
    t.stages.child_orders.reserve(ps.child_ids.size());
    for (std::size_t i = 0; i < ps.child_ids.size(); ++i) {
        const ChildOrder& o = orders.at(ps.child_ids[i]);
        contracts::ChildOrder c;
        c.child_order_id = o.order_id;
        c.parent_order_id = o.parent_id;
        c.instrument_id = o.instrument_id;
        c.venue_id = o.venue_id;
        c.side = o.side;
        c.qty = o.qty;
        c.price_ticks = o.type == OrderType::MARKET ? 0 : o.limit_ticks;
        c.order_type = wire_order_type(o.type);
        c.submit_ts = o.decision_ts;
        c.expire_ts = o.expire_ts;
        c.slice_index = static_cast<std::uint32_t>(i);
        t.stages.child_orders.push_back(std::move(c));
    }
    t.stages.routing = ps.routing;

    std::map<std::uint64_t, std::int64_t> cum_filled;
    for (const auto& f : res.fills) {
        if (f.parent_id != p.parent_id) continue;
        const std::int64_t done = cum_filled[f.order_id] += f.qty;
        const ChildOrder& o = orders.at(f.order_id);
        contracts::ExecutionReport er;
        er.order_id = f.order_id;
        er.execution_id = f.fill_id;
        er.status = done >= o.qty ? contracts::ExecStatus::FILLED
                                  : contracts::ExecStatus::PARTIAL;
        er.filled_qty = f.qty;
        er.fill_price_ticks = f.price_ticks;
        er.venue_id = f.venue_id;
        er.exchange_ts = f.ts;
        er.receive_ts = f.ts;
        er.fees = f.fee;
        t.stages.fills.push_back(std::move(er));
    }
    return t;
}

}  // namespace iap
