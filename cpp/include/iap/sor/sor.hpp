// Smart order routing (spec section 17: multi-venue routing research).
//
// Deterministic venue selection over the per-venue books of one instrument
// (PINNED; the Java port mirrors it exactly):
//
// - Eligible venue: a candidate whose book exists, is not stale, whose
//   status is TRADING and whose configured latency_mean_ns is within
//   SorOptions::max_venue_latency_ns. A stale, halted or missing venue book
//   is NEVER routed to, whatever the alternative.
// - route_aggressive: among eligible venues quoting the opposite side, the
//   most favorable displayed best price (lowest ask for a buy / highest bid
//   for a sell). Ties break to the lower taker_fee_per_share, then to the
//   lower commission_per_million (FX venues charge per notional, their
//   per-share fee is 0), then to the lower venue_id.
// - route_passive: among eligible venues quoting OUR side, the highest
//   maker rebate when SorOptions::prefer_rebate (ties: lower
//   commission_per_million, then lower venue_id); with prefer_rebate off,
//   the lowest venue_id quoting our side.
// - No route (0): no eligible venue quotes the needed side. The caller
//   MUST NOT submit (BacktestEngine / ExecutionReplay skip the child and
//   count sor_no_route_total). An empty candidate list throws.
//
// All iteration is in ascending venue_id order — same candidates + same
// books => same route.

#pragma once

#include <cstdint>
#include <map>
#include <vector>

#include "iap/execution/execution.hpp"
#include "iap/orderbook/book.hpp"

namespace iap {

// configs/execution.json `sor` block.
struct SorOptions {
    bool prefer_rebate = true;
    std::int64_t max_venue_latency_ns = INT64_MAX;
};

class SmartOrderRouter {
public:
    explicit SmartOrderRouter(const std::map<std::uint16_t, VenueSpec>& venues,
                              SorOptions options = SorOptions{})
        : venues_(venues), options_(options) {}

    // 0 = no route (see header).
    std::uint16_t route_aggressive(
        const ConsolidatedBook& book, std::uint8_t side,
        const std::vector<std::uint16_t>& candidates) const;

    // 0 = no route (see header).
    std::uint16_t route_passive(
        const ConsolidatedBook& book, std::uint8_t side,
        const std::vector<std::uint16_t>& candidates) const;

    const SorOptions& options() const { return options_; }

private:
    // The venue book when the venue is eligible, else nullptr.
    const OrderBook* eligible(const ConsolidatedBook& book,
                              std::uint16_t vid) const;

    std::map<std::uint16_t, VenueSpec> venues_;
    SorOptions options_;
};

}  // namespace iap
