// Smart order router implementation (rules pinned in sor.hpp).

#include "iap/sor/sor.hpp"

#include <algorithm>
#include <stdexcept>

namespace iap {

namespace {

std::vector<std::uint16_t> sorted_candidates(
    const std::vector<std::uint16_t>& candidates) {
    if (candidates.empty()) {
        throw std::invalid_argument("SOR: empty candidate venue list");
    }
    std::vector<std::uint16_t> sorted = candidates;
    std::sort(sorted.begin(), sorted.end());
    return sorted;
}

}  // namespace

const OrderBook* SmartOrderRouter::eligible(const ConsolidatedBook& book,
                                            std::uint16_t vid) const {
    auto bit = book.books().find(vid);
    if (bit == book.books().end()) return nullptr;
    if (!ExecutionSimulator::venue_open(&bit->second)) return nullptr;
    auto fit = venues_.find(vid);
    if (fit != venues_.end() &&
        fit->second.latency_mean_ns > options_.max_venue_latency_ns) {
        return nullptr;
    }
    return &bit->second;
}

std::uint16_t SmartOrderRouter::route_aggressive(
    const ConsolidatedBook& book, std::uint8_t side,
    const std::vector<std::uint16_t>& candidates) const {
    bool have = false;
    std::uint16_t best_vid = 0;
    std::int64_t best_price = 0;
    double best_fee = 0.0;
    double best_comm = 0.0;
    for (std::uint16_t vid : sorted_candidates(candidates)) {
        const OrderBook* vb = eligible(book, vid);
        if (vb == nullptr) continue;
        const auto quote = side == 0 ? vb->best_ask() : vb->best_bid();
        if (!quote.has_value()) continue;
        auto fit = venues_.find(vid);
        const double fee =
            fit == venues_.end() ? 0.0 : fit->second.taker_fee_per_share;
        const double comm =
            fit == venues_.end() ? 0.0 : fit->second.commission_per_million;
        const bool better =
            !have ||
            (side == 0 ? quote->first < best_price
                       : quote->first > best_price) ||
            (quote->first == best_price &&
             (fee < best_fee || (fee == best_fee && comm < best_comm)));
        if (better) {
            have = true;
            best_vid = vid;
            best_price = quote->first;
            best_fee = fee;
            best_comm = comm;
        }
    }
    return have ? best_vid : 0;
}

std::uint16_t SmartOrderRouter::route_passive(
    const ConsolidatedBook& book, std::uint8_t side,
    const std::vector<std::uint16_t>& candidates) const {
    bool have = false;
    std::uint16_t best_vid = 0;
    double best_rebate = 0.0;
    double best_comm = 0.0;
    for (std::uint16_t vid : sorted_candidates(candidates)) {
        const OrderBook* vb = eligible(book, vid);
        if (vb == nullptr) continue;
        const auto quote = side == 0 ? vb->best_bid() : vb->best_ask();
        if (!quote.has_value()) continue;
        if (!options_.prefer_rebate) {
            return vid;  // lowest eligible venue id quoting our side
        }
        auto fit = venues_.find(vid);
        const double rebate =
            fit == venues_.end() ? 0.0 : fit->second.maker_rebate_per_share;
        const double comm =
            fit == venues_.end() ? 0.0 : fit->second.commission_per_million;
        if (!have || rebate > best_rebate ||
            (rebate == best_rebate && comm < best_comm)) {
            have = true;
            best_vid = vid;
            best_rebate = rebate;
            best_comm = comm;
        }
    }
    return have ? best_vid : 0;
}

void SmartOrderRouter::score(const ConsolidatedBook& book, std::uint8_t side,
                             const std::vector<std::uint16_t>& candidates,
                             bool passive,
                             std::vector<SorCandidate>& out) const {
    out.clear();
    for (std::uint16_t vid : sorted_candidates(candidates)) {
        SorCandidate c;
        c.venue_id = vid;
        auto fit = venues_.find(vid);
        if (fit != venues_.end()) {
            c.taker_fee = fit->second.taker_fee_per_share;
            c.maker_rebate = fit->second.maker_rebate_per_share;
            c.commission_per_million = fit->second.commission_per_million;
            c.latency_mean_ns = fit->second.latency_mean_ns;
        }
        const OrderBook* vb = eligible(book, vid);
        if (vb != nullptr) {
            // Aggressive routing looks at the opposite side, passive at ours.
            const bool want_ask = passive ? side == 1 : side == 0;
            const auto quote = want_ask ? vb->best_ask() : vb->best_bid();
            if (quote.has_value()) {
                c.eligible = true;
                c.displayed_price_ticks = quote->first;
                c.displayed_qty = quote->second;
            }
        }
        out.push_back(c);
    }
    // Rank the eligible rows with the route_* preference order; the input
    // is already in ascending venue id, so a stable sort keeps "lower venue
    // id" as the final tie-break for free.
    std::vector<std::size_t> order;
    for (std::size_t i = 0; i < out.size(); ++i) {
        if (out[i].eligible) order.push_back(i);
    }
    const bool prefer_rebate = options_.prefer_rebate;
    auto better = [&](std::size_t ia, std::size_t ib) {
        const SorCandidate& a = out[ia];
        const SorCandidate& b = out[ib];
        if (passive) {
            if (!prefer_rebate) return false;  // venue id only
            if (a.maker_rebate != b.maker_rebate) {
                return a.maker_rebate > b.maker_rebate;
            }
            return a.commission_per_million < b.commission_per_million;
        }
        if (a.displayed_price_ticks != b.displayed_price_ticks) {
            return side == 0 ? a.displayed_price_ticks < b.displayed_price_ticks
                             : a.displayed_price_ticks > b.displayed_price_ticks;
        }
        if (a.taker_fee != b.taker_fee) return a.taker_fee < b.taker_fee;
        return a.commission_per_million < b.commission_per_million;
    };
    std::stable_sort(order.begin(), order.end(), better);
    for (std::size_t r = 0; r < order.size(); ++r) {
        out[order[r]].rank = static_cast<std::uint16_t>(r + 1);
    }
}

void SmartOrderRouter::score_aggressive(
    const ConsolidatedBook& book, std::uint8_t side,
    const std::vector<std::uint16_t>& candidates,
    std::vector<SorCandidate>& out) const {
    score(book, side, candidates, false, out);
}

void SmartOrderRouter::score_passive(
    const ConsolidatedBook& book, std::uint8_t side,
    const std::vector<std::uint16_t>& candidates,
    std::vector<SorCandidate>& out) const {
    score(book, side, candidates, true, out);
}

}  // namespace iap
