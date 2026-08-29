// Execution simulator implementation (pinned rules in execution.hpp).

#include "iap/execution/execution.hpp"

#include <algorithm>
#include <stdexcept>

#include "iap/util/json.hpp"

namespace iap {

std::map<std::uint16_t, VenueSpec> load_venues(const std::string& path) {
    const Json root = read_json_file(path);
    std::map<std::uint16_t, VenueSpec> out;
    for (const auto& v : root["venues"].a()) {
        VenueSpec spec;
        spec.venue_id = static_cast<std::uint16_t>(v["venue_id"].u64());
        spec.name = v["venue"].s();
        spec.is_fx = v["asset_class"].s() == "FX";
        if (v.has("taker_fee_per_share")) {
            spec.taker_fee_per_share = v["taker_fee_per_share"].num();
        }
        if (v.has("maker_rebate_per_share")) {
            spec.maker_rebate_per_share = v["maker_rebate_per_share"].num();
        }
        if (v.has("commission_per_million")) {
            spec.commission_per_million = v["commission_per_million"].num();
        }
        spec.latency_mean_ns = v["latency"]["mean_ns"].i64();
        spec.latency_jitter_ns = v["latency"]["jitter_ns"].i64();
        out[spec.venue_id] = spec;
    }
    if (out.empty()) throw std::runtime_error("no venues in " + path);
    return out;
}

ExecutionSimulator::ExecutionSimulator(const ExecConfig& config)
    : config_(config), rng_(config.seed) {
    fills_.reserve(256);
    pending_.reserve(64);
    resting_.reserve(64);
}

const VenueSpec& ExecutionSimulator::venue(std::uint16_t venue_id) const {
    auto it = config_.venues.find(venue_id);
    if (it == config_.venues.end()) {
        throw std::invalid_argument("unknown venue_id " +
                                    std::to_string(venue_id));
    }
    return it->second;
}

const InstrumentSpec& ExecutionSimulator::instrument(
    std::uint32_t instrument_id) const {
    auto it = config_.instruments.find(instrument_id);
    if (it == config_.instruments.end()) {
        throw std::invalid_argument("unknown instrument_id " +
                                    std::to_string(instrument_id));
    }
    return it->second;
}

ConsolidatedBook& ExecutionSimulator::instrument_book(
    std::uint32_t instrument_id) {
    auto it = books_.find(instrument_id);
    if (it == books_.end()) {
        it = books_.emplace(instrument_id, ConsolidatedBook(instrument_id))
                 .first;
    }
    return it->second;
}

const OrderBook* ExecutionSimulator::venue_book(std::uint32_t instrument_id,
                                                std::uint16_t venue_id) const {
    auto it = books_.find(instrument_id);
    if (it == books_.end()) return nullptr;
    auto vit = it->second.books().find(venue_id);
    return vit == it->second.books().end() ? nullptr : &vit->second;
}

std::uint64_t ExecutionSimulator::submit(const ChildOrder& child) {
    if (child.qty <= 0) throw std::invalid_argument("child qty must be > 0");
    if (child.side > 1) throw std::invalid_argument("child side must be 0/1");
    if (child.type != OrderType::MARKET && child.limit_ticks <= 0) {
        throw std::invalid_argument("non-MARKET child needs a limit price");
    }
    ChildOrder o = child;
    o.order_id = next_order_id_++;
    const VenueSpec& v = venue(o.venue_id);
    // Pinned rule 1: one jitter draw per order, in submission order.
    const std::int64_t jitter =
        v.latency_jitter_ns > 0 ? rng_.below(v.latency_jitter_ns + 1) : 0;
    o.arrival_ts = o.decision_ts + config_.latency.decision_ns +
                   config_.latency.risk_ns + config_.latency.wire_ns +
                   v.latency_mean_ns + jitter;
    o.state = OrderState::PENDING;
    o.remaining = o.qty;
    const std::uint64_t id = o.order_id;
    orders_[id] = o;
    // Keep pending_ sorted by (arrival_ts, order_id).
    auto pos = std::upper_bound(
        pending_.begin(), pending_.end(), id,
        [&](std::uint64_t a, std::uint64_t b) {
            const ChildOrder& oa = orders_.at(a);
            const ChildOrder& ob = orders_.at(b);
            if (oa.arrival_ts != ob.arrival_ts) {
                return oa.arrival_ts < ob.arrival_ts;
            }
            return oa.order_id < ob.order_id;
        });
    pending_.insert(pos, id);
    return id;
}

void ExecutionSimulator::cancel(std::uint64_t order_id) {
    auto it = orders_.find(order_id);
    if (it == orders_.end()) {
        throw std::invalid_argument("unknown order_id " +
                                    std::to_string(order_id));
    }
    ChildOrder& o = it->second;
    if (o.state == OrderState::FILLED || o.state == OrderState::CANCELLED) {
        return;
    }
    o.state = OrderState::CANCELLED;
    o.resting = false;
    auto drop = [&](std::vector<std::uint64_t>& v) {
        v.erase(std::remove(v.begin(), v.end(), order_id), v.end());
    };
    drop(pending_);
    drop(resting_);
}

void ExecutionSimulator::cancel_all() {
    while (!pending_.empty()) cancel(pending_.front());
    while (!resting_.empty()) cancel(resting_.front());
}

double ExecutionSimulator::fill_fee(const ChildOrder& o,
                                    std::int64_t price_ticks, std::int64_t qty,
                                    Liquidity liq) const {
    const VenueSpec& v = venue(o.venue_id);
    if (v.is_fx) {
        const InstrumentSpec& ins = instrument(o.instrument_id);
        const double notional = static_cast<double>(qty) * ins.lot_size *
                                static_cast<double>(price_ticks) *
                                ins.tick_size;
        return v.commission_per_million * notional / 1e6;
    }
    if (liq == Liquidity::TAKER) {
        return v.taker_fee_per_share * static_cast<double>(qty);
    }
    return -v.maker_rebate_per_share * static_cast<double>(qty);
}

void ExecutionSimulator::emit_fill(ChildOrder& o, std::int64_t price_ticks,
                                   std::int64_t qty, std::int64_t ts,
                                   Liquidity liq) {
    Fill f;
    f.fill_id = next_fill_id_++;
    f.order_id = o.order_id;
    f.parent_id = o.parent_id;
    f.instrument_id = o.instrument_id;
    f.venue_id = o.venue_id;
    f.side = o.side;
    f.price_ticks = price_ticks;
    f.qty = qty;
    f.ts = ts;
    f.liquidity = liq;
    f.fee = fill_fee(o, price_ticks, qty, liq);
    if (liq == Liquidity::TAKER) {
        // Pinned rule 6: linear impact from the child's total size.
        const InstrumentSpec& ins = instrument(o.instrument_id);
        const double impact_bps = config_.impact_coeff_bps_per_pct_adv *
                                  (static_cast<double>(o.qty) / ins.adv * 100.0);
        const double notional = static_cast<double>(qty) * ins.lot_size *
                                static_cast<double>(price_ticks) *
                                ins.tick_size;
        f.impact_cost = impact_bps * 1e-4 * notional;
    }
    fills_.push_back(f);
    o.remaining -= qty;
    if (o.remaining == 0) {
        o.state = OrderState::FILLED;
        o.resting = false;
    }
}

void ExecutionSimulator::aggressive_fill(ChildOrder& o, const OrderBook& book) {
    const Side opp = o.side == 0 ? Side::ASK : Side::BID;
    // Displayed opposite depth, best first (pinned rule 3).
    const auto depth = book.depth(opp, DEPTH_LEVELS);
    auto within_limit = [&](std::int64_t price) {
        if (o.type == OrderType::MARKET) return true;
        return o.side == 0 ? price <= o.limit_ticks : price >= o.limit_ticks;
    };
    if (o.type == OrderType::FOK) {
        std::int64_t avail = 0;
        for (const auto& [p, q] : depth) {
            if (within_limit(p)) avail += q;
        }
        if (avail < o.remaining) return;  // all-or-none: no fills at all
    }
    for (const auto& [p, q] : depth) {
        if (o.remaining == 0) break;
        if (!within_limit(p)) break;  // levels are sorted best-first
        const std::int64_t take = std::min(o.remaining, q);
        emit_fill(o, p, take, o.arrival_ts, Liquidity::TAKER);
    }
}

void ExecutionSimulator::activate(ChildOrder& o) {
    const OrderBook* book = venue_book(o.instrument_id, o.venue_id);
    if (book != nullptr) {
        aggressive_fill(o, *book);
    }
    if (o.remaining == 0) return;  // fully filled aggressively
    switch (o.type) {
        case OrderType::LIMIT: {
            // Rest passively: queue position = displayed qty at our level.
            o.state = OrderState::ACTIVE;
            o.resting = true;
            const Side side = o.side == 0 ? Side::BID : Side::ASK;
            o.ahead_qty =
                book != nullptr ? book->level_qty(side, o.limit_ticks) : 0;
            // Crossing exemption: the display may still show the liquidity
            // our aggressive leg just consumed (pinned rule 4).
            if (book != nullptr) {
                const auto opp =
                    o.side == 0 ? book->best_ask() : book->best_bid();
                o.cross_exempt =
                    opp.has_value() &&
                    (o.side == 0 ? opp->first <= o.limit_ticks
                                 : opp->first >= o.limit_ticks);
            }
            resting_.push_back(o.order_id);
            break;
        }
        case OrderType::MARKET:
        case OrderType::IOC:
        case OrderType::FOK:
            // Unfilled remainder is cancelled (pinned rule 3).
            o.state = OrderState::CANCELLED;
            break;
    }
}

void ExecutionSimulator::track_consumption(std::uint32_t instrument_id,
                                           std::uint16_t venue_id,
                                           std::uint8_t side,
                                           std::int64_t price_ticks,
                                           std::int64_t qty, std::int64_t ts) {
    for (std::size_t i = 0; i < resting_.size();) {
        ChildOrder& o = orders_.at(resting_[i]);
        if (o.instrument_id == instrument_id && o.venue_id == venue_id &&
            o.side == side && o.state == OrderState::ACTIVE) {
            if (price_ticks == o.limit_ticks) {
                const std::int64_t dec = std::min(o.ahead_qty, qty);
                o.ahead_qty -= dec;
                const std::int64_t leftover = qty - dec;
                if (leftover > 0) {
                    emit_fill(o, o.limit_ticks, std::min(leftover, o.remaining),
                              ts, Liquidity::MAKER);
                }
            } else {
                // Consumption strictly worse than our price: the market
                // traded through our level — full fill at our limit.
                const bool through = o.side == 0
                                         ? price_ticks < o.limit_ticks
                                         : price_ticks > o.limit_ticks;
                if (through) {
                    emit_fill(o, o.limit_ticks, o.remaining, ts,
                              Liquidity::MAKER);
                }
            }
        }
        if (o.state == OrderState::FILLED) {
            resting_.erase(resting_.begin() + static_cast<std::ptrdiff_t>(i));
        } else {
            ++i;
        }
    }
}

void ExecutionSimulator::on_event(const MarketEvent& ev) {
    const std::int64_t t = ev.exchange_ts;

    // 1. Activate due orders against the pre-event book state (rule 2).
    while (!pending_.empty()) {
        const std::uint64_t id = pending_.front();
        ChildOrder& o = orders_.at(id);
        if (o.arrival_ts > t) break;
        pending_.erase(pending_.begin());
        activate(o);
    }

    // 2. Passive queue tracking on the raw event (rule 4), before the book
    //    is mutated.
    const auto et = static_cast<EventType>(ev.event_type);
    if (!resting_.empty()) {
        if (et == EventType::EXECUTE) {
            track_consumption(ev.instrument_id, ev.venue_id, ev.side,
                              ev.price_ticks, ev.qty, t);
        } else if (et == EventType::CANCEL) {
            for (std::uint64_t id : resting_) {
                ChildOrder& o = orders_.at(id);
                if (o.instrument_id == ev.instrument_id &&
                    o.venue_id == ev.venue_id && o.side == ev.side &&
                    ev.price_ticks == o.limit_ticks &&
                    o.state == OrderState::ACTIVE) {
                    o.ahead_qty -= std::min(o.ahead_qty, ev.qty);
                }
            }
        } else if (et == EventType::ADD) {
            // Marketable-ADD expansion (rule 4): the replayed book matches a
            // crossing ADD internally without EXECUTE events; walk the
            // pre-event displayed opposite depth and track the consumption.
            const OrderBook* vb = venue_book(ev.instrument_id, ev.venue_id);
            if (vb != nullptr) {
                const std::uint8_t consumed_side = ev.side == 0 ? 1 : 0;
                const auto depth = vb->depth(
                    consumed_side == 0 ? Side::BID : Side::ASK, DEPTH_LEVELS);
                std::int64_t incoming = ev.qty;
                for (const auto& [p, q] : depth) {
                    if (incoming <= 0) break;
                    const bool crosses = ev.side == 0 ? p <= ev.price_ticks
                                                      : p >= ev.price_ticks;
                    if (!crosses) break;
                    const std::int64_t consumed = std::min(incoming, q);
                    track_consumption(ev.instrument_id, ev.venue_id,
                                      consumed_side, p, consumed, t);
                    incoming -= consumed;
                }
            }
        }
    }

    // 3. Apply the event to the replayed books.
    instrument_book(ev.instrument_id).apply(ev);

    // 4. Post-apply crossing check (rule 4, last bullet).
    const OrderBook* book = venue_book(ev.instrument_id, ev.venue_id);
    if (book != nullptr) {
        for (std::size_t i = 0; i < resting_.size();) {
            ChildOrder& o = orders_.at(resting_[i]);
            bool filled = false;
            if (o.instrument_id == ev.instrument_id &&
                o.venue_id == ev.venue_id && o.state == OrderState::ACTIVE) {
                const auto opp =
                    o.side == 0 ? book->best_ask() : book->best_bid();
                const bool crossed =
                    opp.has_value() &&
                    (o.side == 0 ? opp->first <= o.limit_ticks
                                 : opp->first >= o.limit_ticks);
                if (!crossed) {
                    o.cross_exempt = false;  // display uncrossed: exemption ends
                } else if (!o.cross_exempt) {
                    emit_fill(o, o.limit_ticks, o.remaining, t,
                              Liquidity::MAKER);
                    filled = true;
                }
            }
            if (filled) {
                resting_.erase(resting_.begin() +
                               static_cast<std::ptrdiff_t>(i));
            } else {
                ++i;
            }
        }
    }
}

}  // namespace iap
