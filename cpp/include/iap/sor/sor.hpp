// Smart order routing (spec section 17: multi-venue routing research).
//
// Deterministic venue selection over the per-venue books of one instrument:
//
// - route_aggressive: the venue whose displayed opposite-side best price is
//   most favorable (lowest ask for a buy / highest bid for a sell) among the
//   candidates quoting that side. Ties break to the lower taker fee, then to
//   the lower venue_id. Falls back to the lowest candidate venue_id when no
//   candidate quotes the opposite side.
// - route_passive: among candidates quoting OUR side, the venue with the
//   highest maker rebate (prefer_rebate, configs/execution.json sor block);
//   ties break to the lower venue_id; same fallback.
//
// Stale venue books are never routed to. All iteration is in ascending
// venue_id order — same candidates + same books => same route.

#pragma once

#include <cstdint>
#include <map>
#include <vector>

#include "iap/execution/execution.hpp"
#include "iap/orderbook/book.hpp"

namespace iap {

class SmartOrderRouter {
public:
    explicit SmartOrderRouter(const std::map<std::uint16_t, VenueSpec>& venues)
        : venues_(venues) {}

    std::uint16_t route_aggressive(
        const ConsolidatedBook& book, std::uint8_t side,
        const std::vector<std::uint16_t>& candidates) const;

    std::uint16_t route_passive(
        const ConsolidatedBook& book, std::uint8_t side,
        const std::vector<std::uint16_t>& candidates) const;

private:
    std::map<std::uint16_t, VenueSpec> venues_;
};

}  // namespace iap
