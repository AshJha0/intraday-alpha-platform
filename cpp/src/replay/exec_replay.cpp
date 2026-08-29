// Execution replay driver implementation (pinned order in exec_replay.hpp).

#include "iap/replay/exec_replay.hpp"

#include <algorithm>
#include <cmath>
#include <optional>
#include <stdexcept>

namespace iap {

ExecutionReplay::ExecutionReplay(const ExecConfig& config,
                                 const std::vector<ParentOrder>& parents)
    : config_(config), sim_(config), sor_(config.venues) {
    for (const auto& [vid, spec] : config.venues) {
        (void)spec;
        sor_candidates_.push_back(vid);
    }
    for (const auto& p : parents) {
        if (p.qty <= 0) throw std::invalid_argument("parent qty must be > 0");
        ParentState ps;
        ps.order = p;
        if (p.algo != AlgoType::POV) {
            ps.slice_qty = slice_quantities(p);
            ps.slice_due = slice_times(p);
        }
        parents_.push_back(std::move(ps));
    }
}

void ExecutionReplay::issue_child(ParentState& ps, std::int64_t child_qty,
                                  std::int64_t decision_ts, bool passive) {
    if (child_qty <= 0) return;
    const ParentOrder& p = ps.order;
    ChildOrder c;
    c.parent_id = p.parent_id;
    c.instrument_id = p.instrument_id;
    c.side = p.side;
    c.qty = child_qty;
    c.decision_ts = decision_ts;
    const ConsolidatedBook& book = sim_.instrument_book(p.instrument_id);
    if (p.venue_id != 0) {
        c.venue_id = p.venue_id;
    } else if (passive) {
        c.venue_id = sor_.route_passive(book, p.side, sor_candidates_);
    } else {
        c.venue_id = sor_.route_aggressive(book, p.side, sor_candidates_);
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
    ps.child_ids.push_back(sim_.submit(c));
    ps.sent_qty += child_qty;
}

void ExecutionReplay::schedule(ParentState& ps, const MarketEvent& ev) {
    const ParentOrder& p = ps.order;
    const std::int64_t t = ev.exchange_ts;
    if (p.algo == AlgoType::POV) {
        if (ev.instrument_id != p.instrument_id ||
            static_cast<EventType>(ev.event_type) != EventType::TRADE ||
            t < p.start_ts || t >= p.end_ts) {
            return;
        }
        ps.pov_volume += ev.qty;
        const auto target = static_cast<std::int64_t>(
            std::floor(p.participation * static_cast<double>(ps.pov_volume)));
        std::int64_t deficit =
            std::min(target, p.qty) - ps.sent_qty;  // never exceed parent qty
        if (deficit > 0) {
            issue_child(ps, std::min(deficit, p.max_child_qty), t, false);
        }
        return;
    }
    // TWAP / VWAP / IS: issue every slice that has come due.
    while (ps.next_slice < ps.slice_due.size() &&
           t >= ps.slice_due[ps.next_slice]) {
        const std::int64_t q = ps.slice_qty[ps.next_slice];
        ++ps.next_slice;
        const bool passive =
            p.algo == AlgoType::TWAP || p.algo == AlgoType::VWAP;
        issue_child(ps, std::min(q, p.max_child_qty), t, passive);
    }
}

ExecReplayResult ExecutionReplay::run(const std::vector<MarketEvent>& events) {
    if (ran_) throw std::runtime_error("ExecutionReplay::run is one-shot");
    ran_ = true;
    ExecReplayResult res;
    for (const auto& ev : events) {
        sim_.on_event(ev);
        for (auto& ps : parents_) schedule(ps, ev);
        ++res.events_processed;
    }
    sim_.cancel_all();

    res.fills = sim_.fills();
    for (const auto& ps : parents_) {
        ParentReport r;
        r.parent_id = ps.order.parent_id;
        r.children = static_cast<std::int64_t>(ps.child_ids.size());
        const auto iit = config_.instruments.find(ps.order.instrument_id);
        const double lot =
            iit == config_.instruments.end() ? 1.0 : iit->second.lot_size;
        const double tick =
            iit == config_.instruments.end() ? 1.0 : iit->second.tick_size;
        for (const auto& f : res.fills) {
            if (f.parent_id != ps.order.parent_id) continue;
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
    return res;
}

}  // namespace iap
