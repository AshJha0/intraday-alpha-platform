// Smart order router implementation (rules pinned in sor.hpp).

#include "iap/sor/sor.hpp"

#include <algorithm>
#include <stdexcept>

namespace iap {

namespace {

std::uint16_t fallback(const std::vector<std::uint16_t>& candidates) {
    if (candidates.empty()) {
        throw std::invalid_argument("SOR: empty candidate venue list");
    }
    return *std::min_element(candidates.begin(), candidates.end());
}

}  // namespace

std::uint16_t SmartOrderRouter::route_aggressive(
    const ConsolidatedBook& book, std::uint8_t side,
    const std::vector<std::uint16_t>& candidates) const {
    std::vector<std::uint16_t> sorted = candidates;
    std::sort(sorted.begin(), sorted.end());
    bool have = false;
    std::uint16_t best_vid = 0;
    std::int64_t best_price = 0;
    double best_fee = 0.0;
    for (std::uint16_t vid : sorted) {
        auto bit = book.books().find(vid);
        if (bit == book.books().end() || bit->second.stale()) continue;
        const auto quote =
            side == 0 ? bit->second.best_ask() : bit->second.best_bid();
        if (!quote.has_value()) continue;
        auto fit = venues_.find(vid);
        const double fee =
            fit == venues_.end() ? 0.0 : fit->second.taker_fee_per_share;
        const bool better =
            !have ||
            (side == 0 ? quote->first < best_price
                       : quote->first > best_price) ||
            (quote->first == best_price && fee < best_fee);
        if (better) {
            have = true;
            best_vid = vid;
            best_price = quote->first;
            best_fee = fee;
        }
    }
    return have ? best_vid : fallback(candidates);
}

std::uint16_t SmartOrderRouter::route_passive(
    const ConsolidatedBook& book, std::uint8_t side,
    const std::vector<std::uint16_t>& candidates) const {
    std::vector<std::uint16_t> sorted = candidates;
    std::sort(sorted.begin(), sorted.end());
    bool have = false;
    std::uint16_t best_vid = 0;
    double best_rebate = 0.0;
    for (std::uint16_t vid : sorted) {
        auto bit = book.books().find(vid);
        if (bit == book.books().end() || bit->second.stale()) continue;
        const auto quote =
            side == 0 ? bit->second.best_bid() : bit->second.best_ask();
        if (!quote.has_value()) continue;
        auto fit = venues_.find(vid);
        const double rebate =
            fit == venues_.end() ? 0.0 : fit->second.maker_rebate_per_share;
        if (!have || rebate > best_rebate) {
            have = true;
            best_vid = vid;
            best_rebate = rebate;
        }
    }
    return have ? best_vid : fallback(candidates);
}

}  // namespace iap
