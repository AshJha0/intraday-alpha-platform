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
      dark_rng_(config.seed ^ 0xB10),
      sor_options_(sor_options) {
    for (const auto& [vid, spec] : config.venues) {
        // v1.17 B10: a dark / midpoint venue has no displayed book and is
        // never a candidate of the pinned router.
        if (spec.is_lit()) sor_candidates_.push_back(vid);
    }
    for (const auto& p : parents) {
        if (p.qty <= 0) throw std::invalid_argument("parent qty must be > 0");
        if (p.max_child_qty <= 0) {
            throw std::invalid_argument("max_child_qty must be > 0");
        }
        if (p.end_ts <= p.start_ts) {
            throw std::invalid_argument("parent window must have end_ts > start_ts");
        }
        if (p.algo == AlgoType::POV && p.pov_exact &&
            !(p.participation > 0.0 && p.participation < 1.0)) {
            throw std::invalid_argument("pov_exact needs 0 < participation < 1");
        }
        ParentState ps;
        ps.order = p;
        if (p.algo != AlgoType::POV) {
            ps.slice_qty = slice_quantities(p);
            ps.slice_due = slice_times(p);
        }
        if (p.policy == ExecPolicy::PASSIVE) {
            validate_passive_params(p.passive);
            ps.patience_ns = patience_ns(p.passive, p.urgency,
                                         p.algo == AlgoType::IS, p.risk_aversion);
            ps.behind_qty = max_behind_qty(p.passive, p.qty);
        }
        if (p.dark_first.has_value()) {
            // B10: ExecutionReplay._init_dark_state
            p.dark_first->validate();
            std::uint16_t vid = p.dark_first->venue_id;
            if (vid == 0) {
                for (const auto& [v, s] : config.venues) {
                    if (!s.is_lit()) {
                        vid = v;
                        break;
                    }
                }
                if (vid == 0) {
                    throw std::invalid_argument(
                        "parent " + std::to_string(p.parent_id) +
                        ": dark_first needs a dark / midpoint venue in the config");
                }
            }
            auto sit = config.venues.find(vid);
            if (sit == config.venues.end() || sit->second.is_lit()) {
                throw std::invalid_argument("parent " + std::to_string(p.parent_id) +
                                            ": venue " + std::to_string(vid) +
                                            " is not a dark / midpoint venue");
            }
            ps.dark_venue_id = vid;
            lit_volume_.emplace(p.instrument_id, 0);
            lit_trail_.emplace(p.instrument_id, std::vector<std::pair<std::int64_t, std::int64_t>>{});
            lit_trail_head_.emplace(p.instrument_id, 0);
        }
        parents_.push_back(std::move(ps));
    }
}

bool ExecutionReplay::issue_child(ParentState& ps, std::int64_t child_qty,
                                  std::int64_t decision_ts, bool passive,
                                  bool post) {
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
        std::optional<std::int64_t> price;
        if (vb != nullptr && post) {
            price = post_price(p.side, vb->best_bid(), vb->best_ask(),
                               p.passive.improve_min_spread_ticks);
        } else if (vb != nullptr) {
            const std::optional<LevelEntry> best =
                p.side == 0 ? vb->best_bid() : vb->best_ask();
            if (best.has_value()) price = best->first;
        }
        if (price.has_value()) {
            c.type = OrderType::LIMIT;
            c.limit_ticks = *price;
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
        if (!dark_try(ps, q, decision_ts)) issue_child(ps, q, decision_ts, passive);
        left -= q;
    }
}

std::int64_t ExecutionReplay::committed_qty(const ParentState& ps) const {
    std::int64_t open = 0;
    const auto& orders = sim_.orders();
    for (std::uint64_t id : ps.child_ids) {
        if (id >= kDarkIdBase) continue;  // dark children: counted as dark_open_qty
        const ChildOrder& o = orders.at(id);
        if (o.state == OrderState::PENDING || o.state == OrderState::ACTIVE) {
            open += o.remaining;
        }
    }
    return ps.filled_qty + open + ps.dark_open_qty;
}

// --------------------------------------------------- v1.17 B10 dark-first

const ChildOrder& ExecutionReplay::order(std::uint64_t oid) const {
    auto it = dark_orders_.find(oid);
    return it == dark_orders_.end() ? sim_.orders().at(oid) : it->second;
}

void ExecutionReplay::track_lit_volume(const MarketEvent& ev) {
    auto it = lit_volume_.find(ev.instrument_id);
    if (it == lit_volume_.end() ||
        static_cast<EventType>(ev.event_type) != EventType::EXECUTE || ev.qty <= 0) {
        return;
    }
    auto vit = config_.venues.find(ev.venue_id);
    if (vit != config_.venues.end() && !vit->second.is_lit()) return;
    it->second += ev.qty;
    lit_trail_[ev.instrument_id].emplace_back(ev.exchange_ts, ev.qty);
}

std::int64_t ExecutionReplay::trailing_lit_volume(std::uint32_t iid, std::int64_t t,
                                                  std::int64_t window_ns) {
    auto& trail = lit_trail_[iid];
    std::size_t head = lit_trail_head_[iid];
    while (head < trail.size() && trail[head].first < t - window_ns) ++head;
    if (head > 4096 && head * 2 > trail.size()) {
        trail.erase(trail.begin(), trail.begin() + static_cast<std::ptrdiff_t>(head));
        head = 0;
    }
    lit_trail_head_[iid] = head;
    std::int64_t total = 0;
    for (std::size_t i = head; i < trail.size(); ++i) total += trail[i].second;
    return total;
}

bool ExecutionReplay::dark_try(ParentState& ps, std::int64_t qty, std::int64_t t) {
    const ParentOrder& p = ps.order;
    // eligibility: a plain parent is low-urgency when urgency <= max_urgency
    // (the B2 regime rule of an alpha-urgency parent is not wired in C++)
    if (qty <= 0 || !p.dark_first.has_value() || !(p.urgency <= p.dark_first->max_urgency)) {
        return false;
    }
    const DarkFirstParams& d = *p.dark_first;
    const std::int64_t cap = p.end_ts - p.passive.end_margin_ns;
    if (t >= cap) return false;  // no time left to rest dark: lit at once
    const ConsolidatedBook& book = sim_.instrument_book(p.instrument_id);
    const auto bb = consolidated_touch_price(book, Side::BID);
    const auto ba = consolidated_touch_price(book, Side::ASK);
    if (!bb.has_value() || !ba.has_value() || *ba <= *bb) return false;  // no midpoint to peg to
    const auto iit = config_.instruments.find(p.instrument_id);
    const double lot = iit == config_.instruments.end() ? 1.0 : iit->second.qty_unit;
    const double tick = iit == config_.instruments.end() ? 1.0 : iit->second.tick_size;
    const std::int64_t mid = midpoint_ticks(*bb, *ba, p.side);
    const VenueSpec& spec = config_.venues.at(ps.dark_venue_id);
    const DarkControlCode code = dark_pretrade_check(
        d.controls, spec.venue_type, qty,
        trailing_lit_volume(p.instrument_id, t, d.controls.participation_window_ns),
        ps.dark_notional,
        static_cast<double>(qty) * lot * static_cast<double>(mid) * tick);
    if (code != DarkControlCode::NONE) {
        ps.dark_rejected[dark_control_code_name(code)] += 1;
        return false;
    }
    const std::uint64_t oid = next_dark_id_++;
    ChildOrder child;
    child.parent_id = p.parent_id;
    child.instrument_id = p.instrument_id;
    child.venue_id = ps.dark_venue_id;
    child.side = p.side;
    child.type = OrderType::LIMIT;
    child.limit_ticks = 0;  // midpoint peg: no limit of our own
    child.qty = qty;
    child.decision_ts = t;
    child.expire_ts = p.end_ts;
    child.order_id = oid;
    child.arrival_ts = t;
    child.state = OrderState::ACTIVE;
    child.remaining = qty;
    child.resting = true;
    child.venue_type = static_cast<std::uint8_t>(spec.venue_type);
    child.dark_u = dark_rng_.uniform();
    dark_orders_[oid] = child;
    ps.child_ids.push_back(oid);
    ParentState::DarkChild dc;
    dc.order_id = oid;
    dc.qty = qty;
    dc.u = child.dark_u;
    dc.post_ts = t;
    dc.deadline = std::min(t + d.rest_ns(), cap);
    dc.lit_volume_at_post = lit_volume_[p.instrument_id];
    dc.spread_ticks = *ba - *bb;
    dc.queue_ahead = ps.dark_open_qty;
    ps.dark_open.push_back(dc);
    ps.dark_open_qty += qty;
    ps.dark_children += 1;
    ps.dark_posted_qty += qty;
    return true;
}

void ExecutionReplay::dark_fill(ParentState& ps, const ParentState::DarkChild& dc,
                                ChildOrder& o, std::int64_t t, std::int64_t px) {
    const ParentOrder& p = ps.order;
    const auto iit = config_.instruments.find(p.instrument_id);
    const double lot = iit == config_.instruments.end() ? 1.0 : iit->second.qty_unit;
    const double tick = iit == config_.instruments.end() ? 1.0 : iit->second.tick_size;
    const VenueSpec& spec = config_.venues.at(ps.dark_venue_id);
    Fill f;
    f.fill_id = kDarkIdBase + dark_fills_.size();
    f.order_id = o.order_id;
    f.parent_id = p.parent_id;
    f.instrument_id = p.instrument_id;
    f.venue_id = ps.dark_venue_id;
    f.side = p.side;
    f.price_ticks = px;
    f.qty = dc.qty;
    f.ts = t;
    f.liquidity = Liquidity::MAKER;
    f.fee = spec.taker_fee_per_share * static_cast<double>(dc.qty);
    f.impact_cost = 0.0;
    f.dark_midpoint = true;
    dark_fills_.push_back(f);
    o.state = OrderState::FILLED;
    o.remaining = 0;
    o.dark_outcome = static_cast<std::uint8_t>(DarkOutcome::FILLED);
    ps.filled_qty += dc.qty;
    ps.dark_filled_qty += dc.qty;
    ps.dark_notional += static_cast<double>(dc.qty) * lot * static_cast<double>(px) * tick;
}

void ExecutionReplay::plain_child(ParentState& ps, std::int64_t qty, std::int64_t t) {
    const ParentOrder& p = ps.order;
    if (p.policy == ExecPolicy::PASSIVE) {
        post(ps, qty, t, 0);
    } else if (p.policy == ExecPolicy::AGGRESSIVE) {
        issue_child(ps, qty, t, false);
    } else {
        issue_child(ps, qty, t, p.algo == AlgoType::TWAP || p.algo == AlgoType::VWAP);
    }
}

void ExecutionReplay::work_dark(ParentState& ps, const MarketEvent& ev) {
    const ParentOrder& p = ps.order;
    const DarkFirstParams& d = *p.dark_first;
    if (ev.instrument_id != p.instrument_id) return;
    const std::int64_t t = ev.exchange_ts;
    const ConsolidatedBook& book = sim_.instrument_book(p.instrument_id);
    const auto bb = consolidated_touch_price(book, Side::BID);
    const auto ba = consolidated_touch_price(book, Side::ASK);
    const bool two_sided = bb.has_value() && ba.has_value() && *ba > *bb;
    const std::int64_t lit_now = lit_volume_[p.instrument_id];
    std::vector<std::int64_t> expired;
    std::vector<ParentState::DarkChild> keep;
    for (const ParentState::DarkChild& dc : ps.dark_open) {
        ChildOrder& o = dark_orders_.at(dc.order_id);
        if (t >= dc.deadline) {
            o.state = OrderState::CANCELLED;
            o.cancel_reason = CancelReason::EXPIRED;
            o.dark_outcome = static_cast<std::uint8_t>(DarkOutcome::EXPIRED);
            ps.dark_open_qty -= dc.qty;
            ps.dark_expired_qty += dc.qty;
            expired.push_back(dc.qty);
            continue;
        }
        const double pf = d.model.p_fill(lit_now - dc.lit_volume_at_post, dc.spread_ticks,
                                         dc.qty, dc.queue_ahead);
        o.dark_p_fill = pf;
        if (two_sided && pf >= dc.u) {
            ps.dark_open_qty -= dc.qty;
            dark_fill(ps, dc, o, t, midpoint_ticks(*bb, *ba, p.side));
            continue;
        }
        keep.push_back(dc);
    }
    ps.dark_open = std::move(keep);
    for (const std::int64_t q : expired) {
        if (t >= p.end_ts) {
            ps.dark_unsent_qty += q;  // the window closed on it
            continue;
        }
        plain_child(ps, q, t);
    }
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

void ExecutionReplay::post(ParentState& ps, std::int64_t qty, std::int64_t t,
                           int reprices) {
    const ParentOrder& p = ps.order;
    const std::int64_t cap = p.end_ts - p.passive.end_margin_ns;
    if (ps.patience_ns <= 0 || t >= cap) {
        ++ps.stats.crosses_immediate;
        issue_child(ps, qty, t, false);
        return;
    }
    if (!issue_child(ps, qty, t, true, true)) return;
    const std::uint64_t oid = ps.child_ids.back();
    if (sim_.orders().at(oid).type != OrderType::LIMIT) {
        ++ps.stats.crosses_immediate;  // no same-side quote: MARKET fallback
        return;
    }
    ++ps.stats.posts;
    Working w;
    w.order_id = oid;
    w.deadline = std::min(t + ps.patience_ns, cap);
    w.reprices = reprices;
    ps.working.push_back(w);
}

void ExecutionReplay::post_step(ParentState& ps, std::int64_t step_qty,
                                std::int64_t t) {
    const std::int64_t cap = ps.order.max_child_qty;
    std::int64_t left = step_qty;
    while (left > 0) {
        const std::int64_t q = std::min(left, cap);
        if (!dark_try(ps, q, t)) post(ps, q, t, 0);
        left -= q;
    }
}

void ExecutionReplay::work_passive(ParentState& ps, std::int64_t t) {
    const ParentOrder& p = ps.order;
    const auto& orders = sim_.orders();
    const bool behind =
        ps.scheduled_qty - ps.filled_qty - ps.current_step_qty > ps.behind_qty;
    // Children posted below are evaluated from the next event on. post()
    // appends to ps.working, so entries are addressed by index and never
    // touched again after a call that may reallocate the vector.
    const std::size_t n = ps.working.size();
    for (std::size_t k = 0; k < n; ++k) {
        const WorkState state = ps.working[k].state;
        if (state == WorkState::DONE) continue;
        const std::uint64_t oid = ps.working[k].order_id;
        const ChildOrder& o = orders.at(oid);
        const bool terminal = o.state == OrderState::FILLED ||
                              o.state == OrderState::CANCELLED;
        if (state == WorkState::REST) {
            if (terminal) {
                ps.working[k].state = WorkState::DONE;
            } else if (t >= p.end_ts) {
                continue;  // expires with the window (rule 7)
            } else if (behind) {
                sim_.cancel(oid, t);
                ps.working[k].state = WorkState::CANCEL_CROSS_BEHIND;
            } else if (t >= ps.working[k].deadline) {
                if (ps.working[k].reprices >= p.passive.max_reprices) {
                    sim_.cancel(oid, t);
                    ps.working[k].state = WorkState::CANCEL_CROSS_TIMEOUT;
                    continue;
                }
                const OrderBook* vb = sim_.venue_book(p.instrument_id, o.venue_id);
                std::optional<std::int64_t> price;
                if (vb != nullptr) {
                    price = post_price(p.side, vb->best_bid(), vb->best_ask(),
                                       p.passive.improve_min_spread_ticks);
                }
                if (price.has_value() && *price == o.limit_ticks) {
                    // Still at the target price: keep the queue position.
                    ++ps.working[k].reprices;
                    ps.working[k].deadline =
                        std::min(t + ps.patience_ns,
                                 p.end_ts - p.passive.end_margin_ns);
                    ++ps.stats.rest_extensions;
                } else {
                    sim_.cancel(oid, t);
                    ps.working[k].state = WorkState::CANCEL_REPRICE;
                }
            }
            continue;
        }
        // A cancel is in flight: act once it has taken effect.
        if (!terminal) continue;
        const int reprices = ps.working[k].reprices;
        const std::int64_t remaining = o.remaining;
        ps.working[k].state = WorkState::DONE;
        if (o.state != OrderState::CANCELLED ||
            o.cancel_reason != CancelReason::USER || remaining <= 0 ||
            t >= p.end_ts) {
            continue;  // filled, expired or the window closed: nothing to re-send
        }
        if (state == WorkState::CANCEL_REPRICE) {
            ++ps.stats.reprices;
            post(ps, remaining, t, reprices + 1);
        } else {
            if (state == WorkState::CANCEL_CROSS_BEHIND) {
                ++ps.stats.crosses_behind;
            } else {
                ++ps.stats.crosses_timeout;
            }
            issue_child(ps, remaining, t, false);
        }
    }
}

void ExecutionReplay::schedule_passive(ParentState& ps, const MarketEvent& ev) {
    const ParentOrder& p = ps.order;
    const std::int64_t t = ev.exchange_ts;
    if (p.algo == AlgoType::POV) {
        const bool in_window =
            ev.instrument_id == p.instrument_id &&
            static_cast<EventType>(ev.event_type) == EventType::TRADE &&
            t >= p.start_ts && t < p.end_ts;
        if (in_window) {
            ps.pov_volume += ev.qty;
            ps.scheduled_qty = std::min(pov_target(p, ps.pov_volume), p.qty);
        }
        work_passive(ps, t);
        if (in_window) {
            // pov_exact: the whole deficit at once (rule 4 of the F7 ports);
            // pinned: one child per TRADE.
            std::int64_t deficit = ps.scheduled_qty - committed_qty(ps);
            while (deficit > 0) {
                const std::int64_t q = std::min(deficit, p.max_child_qty);
                if (!dark_try(ps, q, t)) post(ps, q, t, 0);
                deficit = p.pov_exact ? deficit - q : 0;
            }
        }
        return;
    }
    std::vector<std::int64_t> steps;
    while (ps.next_slice < ps.slice_due.size() &&
           t >= ps.slice_due[ps.next_slice]) {
        const std::int64_t q = ps.slice_qty[ps.next_slice];
        ++ps.next_slice;
        ps.scheduled_qty += q;
        ps.current_step_qty = q;
        if (t >= p.end_ts) continue;
        steps.push_back(q);
    }
    work_passive(ps, t);
    for (const std::int64_t q : steps) post_step(ps, q, t);
}

void ExecutionReplay::schedule(ParentState& ps, const MarketEvent& ev) {
    const ParentOrder& p = ps.order;
    const std::int64_t t = ev.exchange_ts;
    if (!ps.triggered && t >= p.start_ts) {
        ps.triggered = true;
        ps.trigger_ts = t;
        ps.trigger_seq = ev.sequence;
    }
    if (!ps.dark_open.empty()) work_dark(ps, ev);  // B10, before the schedule
    if (p.policy == ExecPolicy::PASSIVE) {
        schedule_passive(ps, ev);
        return;
    }
    if (p.algo == AlgoType::POV) {
        if (ev.instrument_id != p.instrument_id ||
            static_cast<EventType>(ev.event_type) != EventType::TRADE ||
            t < p.start_ts || t >= p.end_ts) {
            return;
        }
        ps.pov_volume += ev.qty;
        // Deficit against FILLED + in-flight qty, never sent qty (pinned).
        std::int64_t deficit =
            std::min(pov_target(p, ps.pov_volume), p.qty) - committed_qty(ps);
        while (deficit > 0) {
            const std::int64_t q = std::min(deficit, p.max_child_qty);
            if (!dark_try(ps, q, t)) issue_child(ps, q, t, false);
            deficit = p.pov_exact ? deficit - q : 0;
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
            p.policy != ExecPolicy::AGGRESSIVE &&
            (p.algo == AlgoType::TWAP || p.algo == AlgoType::VWAP);
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
        if (!lit_volume_.empty()) track_lit_volume(ev);
        for (auto& ps : parents_) schedule(ps, ev);
        ++res.events_processed;
    }
    sim_.cancel_all();
    book_new_fills();
    for (auto& ps : parents_) {
        // B10: open dark children at the end of the stream expire unsent
        for (const auto& dc : ps.dark_open) {
            ChildOrder& o = dark_orders_.at(dc.order_id);
            o.state = OrderState::CANCELLED;
            o.cancel_reason = CancelReason::END_OF_STREAM;
            o.dark_outcome = static_cast<std::uint8_t>(DarkOutcome::EXPIRED);
            ps.dark_open_qty -= dc.qty;
            ps.dark_expired_qty += dc.qty;
            ps.dark_unsent_qty += dc.qty;
        }
        ps.dark_open.clear();
    }

    res.fills = sim_.fills();
    if (!dark_fills_.empty()) {
        // modelled dark fills merged by time; the lit order is unchanged
        res.fills.insert(res.fills.end(), dark_fills_.begin(), dark_fills_.end());
        std::stable_sort(res.fills.begin(), res.fills.end(), [](const Fill& a, const Fill& b) {
            return a.ts != b.ts ? a.ts < b.ts : a.fill_id < b.fill_id;
        });
    }
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
        if (ps.order.dark_first.has_value()) {
            r.dark_children = ps.dark_children;
            r.dark_posted_qty = ps.dark_posted_qty;
            r.dark_filled_qty = ps.dark_filled_qty;
            r.dark_expired_qty = ps.dark_expired_qty;
            r.dark_unsent_qty = ps.dark_unsent_qty;
            r.dark_notional = ps.dark_notional;
            r.dark_rejected = ps.dark_rejected;
        }
        res.parents[r.parent_id] = r;
        if (ps.order.policy == ExecPolicy::PASSIVE) {
            res.passive[r.parent_id] = ps.stats;
        }
        std::vector<ChildOrder>& kids = res.children[ps.order.parent_id];
        for (const std::uint64_t oid : ps.child_ids) kids.push_back(order(oid));
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
    if (p.policy == ExecPolicy::AGGRESSIVE) {
        po.urgency = 1.0;
    } else if (p.policy == ExecPolicy::PASSIVE) {
        po.urgency = std::min(std::max(p.urgency, 0.0), 1.0);
    }
    po.limit_price_ticks = 0;
    po.params["max_child_qty"] = static_cast<double>(p.max_child_qty);
    if (p.policy != ExecPolicy::NATIVE) {
        // 1 = PASSIVE, 2 = AGGRESSIVE; a NATIVE parent's params are unchanged.
        po.params["exec_policy"] = static_cast<double>(static_cast<int>(p.policy));
    }
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
        if (ps.child_ids[i] >= kDarkIdBase) continue;  // B10: never in the simulator
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
        const ChildOrder& o = order(f.order_id);
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
