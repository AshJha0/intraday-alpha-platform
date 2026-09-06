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

}  // namespace iap
