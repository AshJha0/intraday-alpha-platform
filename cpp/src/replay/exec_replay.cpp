// Execution replay driver implementation (pinned order in exec_replay.hpp).

#include "iap/replay/exec_replay.hpp"

#include <algorithm>
#include <cmath>
#include <optional>
#include <stdexcept>

namespace iap {

ExecutionReplay::ExecutionReplay(const ExecConfig& config,
                                 const std::vector<ParentOrder>& parents,
                                 SorOptions sor_options)
    : config_(config), sim_(config), sor_(config.venues, sor_options) {
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
    ps.child_ids.push_back(sim_.submit(c));
    return true;
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
    return res;
}

}  // namespace iap
