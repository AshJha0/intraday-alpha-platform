// Parent-order execution algorithms: VWAP / TWAP / POV / IS (spec section 17).
//
// A parent order is worked over an event-time window [start_ts, end_ts) by
// scheduling child orders. All schedules are pure functions of the parent
// config and the replayed event stream — deterministic, no wall clock.
//
// PINNED SCHEDULES (this port is the reference for
// tests/golden/expected_replay_fills.json):
//
// - Slice decision times (TWAP/VWAP/IS): slice i of N is due at
//     due_i = start_ts + i * (end_ts - start_ts) / N          (integer div)
//   and is issued while processing the first event with exchange_ts >= due_i
//   (decision_ts = that event's exchange_ts). Slice weights map to integer
//   child quantities by largest-remainder apportionment (floor each target,
//   hand remaining shares to the largest fractional parts, ties to the
//   earlier slice) — quantities sum exactly to the parent qty.
// - TWAP: equal weights (w_i = 1).
// - VWAP: pinned U-shaped session volume curve ("time_of_day_default"):
//     w_i = 1 + x_i^2,  x_i = (2i - (N-1)) / (N-1)   (N >= 2; N = 1 -> all)
//   (weight 2 at the window edges, 1 in the middle).
// - IS (implementation shortfall): front-loaded exponential decay,
//     w_i = exp(-risk_aversion * i / max(1, N-1))
//   — urgency (risk_aversion) shifts quantity toward the earliest slices.
// - POV: no precomputed slices. Tracks cumulative TRADE volume V(t) of the
//   parent's instrument inside the window; after each TRADE event, target =
//   floor(participation * V(t)); whenever target exceeds the quantity
//   COMMITTED (filled + still open/in-flight of the parent's children — a
//   cancelled MARKET remainder frees its qty and is re-sent), a child covers
//   the deficit (capped at max_child_qty and the parent's remainder).
//
// Child sizing (pinned): a slice larger than max_child_qty is split into
// ceil(slice / max_child_qty) children of max_child_qty (the last one the
// remainder), all decided at the same event, in order — no quantity is ever
// silently dropped. Every child carries expire_ts = end_ts (venue-side
// time-in-force, execution.hpp rule 7): no child outlives its parent's
// window, and an unfilled slice at end_ts is reported as unfilled_qty
// (opportunity cost), never filled later.
//
// Child order styles (pinned): TWAP and VWAP children are passive LIMIT
// orders joining the same-side best price at decision time (falling back to
// MARKET when that side is empty); POV and IS children are MARKET orders.
// Venue: parent.venue_id, or SOR-routed when venue_id == 0 (aggressive
// children via route_aggressive, passive via route_passive); when the SOR
// finds no eligible venue (0) the child is NOT submitted and counted in
// ExecReplayResult::sor_no_route (a TWAP/VWAP/IS slice is then lost to
// opportunity cost; POV re-evaluates on the next trade).

#pragma once

#include <cstdint>
#include <vector>

#include "iap/execution/execution.hpp"

namespace iap {

enum class AlgoType : std::uint8_t { TWAP = 0, VWAP = 1, POV = 2, IS = 3 };

struct ParentOrder {
    std::uint64_t parent_id = 0;
    std::uint32_t instrument_id = 0;
    std::uint16_t venue_id = 0;  // 0 => SOR-routed
    std::uint8_t side = 0;       // 0 = buy, 1 = sell
    std::int64_t qty = 0;
    AlgoType algo = AlgoType::TWAP;
    std::int64_t start_ts = 0;
    std::int64_t end_ts = 0;
    int slices = 8;                  // TWAP / VWAP / IS
    double participation = 0.05;     // POV
    double risk_aversion = 1.0;      // IS
    std::int64_t max_child_qty = 1000;
};

// Slice weights for TWAP/VWAP/IS (POV is event-driven; throws for POV).
std::vector<double> slice_weights(const ParentOrder& parent);

// Integer child quantities per slice (largest-remainder; sums to qty).
std::vector<std::int64_t> slice_quantities(const ParentOrder& parent);

// Due time of each slice (see header comment).
std::vector<std::int64_t> slice_times(const ParentOrder& parent);

}  // namespace iap
