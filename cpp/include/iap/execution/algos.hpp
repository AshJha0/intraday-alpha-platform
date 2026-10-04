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
//
// EXECUTION POLICIES (ParentOrder::policy; the schedules above never change).
// The child styles above are the NATIVE policy — the default, and the only
// behaviour before v1.5.0 (tests/golden/expected_replay_fills.json).
// AGGRESSIVE sends every child as a MARKET order, whatever the algo.
// PASSIVE works every schedule step through the pinned state machine
// POST -> REST -> REPRICE / CROSS (this port is the reference for
// tests/golden/expected_replay_fills_passive.json), evaluated by the replay
// scheduler after every market event against the post-event books:
//
// - POST. The step's quantity (split at max_child_qty) is posted as a LIMIT
//   at post_price(): the same-side best of the routed venue, improved by ONE
//   tick when the venue's spread is at least improve_min_spread_ticks ticks
//   (0 disables the improvement), then clamped so it never reaches the
//   opposite touch (a buy posts at most at best_ask - 1, a sell at least at
//   best_bid + 1). With no same-side quote, with patience_ns == 0, or at or
//   after end_ts - end_margin_ns, the child is sent as a MARKET order
//   instead. The rule is evaluated on the book at decision time: there is no
//   post-only order type, so a limit the market moved through while the
//   order was in flight executes as a taker up to its limit (execution.hpp
//   rule 3).
// - REST. The child rests until deadline = min(decision_ts + patience_ns,
//   end_ts - end_margin_ns) or until the parent's schedule is BEHIND.
//     patience_ns = floor(max_rest_ns * (1 - urgency) * k),
//   urgency clamped to [0, 1], k = exp(-risk_aversion) for IS and 1
//   otherwise. backlog(t) = scheduled(t) - filled(t) - q_cur, where
//   scheduled is the sum of the slice quantities that have come due
//   (TWAP/VWAP/IS; q_cur = the most recent of them, because a slice is by
//   construction a whole slice behind the instant it is due) or
//   min(floor(participation * V(t)), qty) (POV; q_cur = 0). The schedule is
//   BEHIND when backlog(t) > floor(max_behind_fraction * qty).
// - REPRICE. At the deadline, while reprices < max_reprices and the schedule
//   is not behind: if post_price() still equals the child's limit, the child
//   keeps its queue position and gets a fresh deadline (rest_extensions);
//   otherwise it is cancelled and, once the cancel has taken effect, its
//   unfilled remainder is posted again at the new post_price() (reprices).
//   Both use up one reprice.
// - CROSS. At the deadline with no reprice left, or as soon as the schedule
//   is behind, the child is cancelled and, once the cancel has taken effect,
//   its unfilled remainder is sent as a MARKET order (crosses_timeout /
//   crosses_behind).
//
// A cancel travels the simulator's latency path (rule 7), so a child can
// still fill while its cancel is in flight; only the quantity that was
// actually cancelled is re-sent — quantity is never duplicated. Nothing is
// re-sent at or after end_ts (the remainder is opportunity cost). Per event
// and per PASSIVE parent the scheduler runs, in pinned order: (a) the
// schedule state (slices come due / POV volume), (b) the state machine of
// every posted child in posting order, (c) the new schedule steps.

#pragma once

#include <cstdint>
#include <optional>
#include <vector>

#include "iap/execution/execution.hpp"

namespace iap {

enum class AlgoType : std::uint8_t { TWAP = 0, VWAP = 1, POV = 2, IS = 3 };

// How a parent order's children are sent (header comment).
enum class ExecPolicy : std::uint8_t { NATIVE = 0, PASSIVE = 1, AGGRESSIVE = 2 };

// Parameters of the PASSIVE policy (defaults = the pinned ones).
struct PassiveParams {
    std::int64_t max_rest_ns = 30'000'000'000;       // rest time at urgency 0
    int max_reprices = 1;                            // before crossing
    double max_behind_fraction = 0.1;                // of the parent qty
    std::int64_t improve_min_spread_ticks = 3;       // 0 = never post inside
    std::int64_t end_margin_ns = 1'000'000'000;      // no resting this close to end_ts
};

// Per-parent transition counters of the PASSIVE state machine.
struct PassiveStats {
    std::int64_t posts = 0;              // LIMIT children posted
    std::int64_t reprices = 0;           // cancel + re-post at a new price
    std::int64_t rest_extensions = 0;    // deadline renewed, price unchanged
    std::int64_t crosses_timeout = 0;    // MARKET after the last reprice
    std::int64_t crosses_behind = 0;     // MARKET, schedule behind
    std::int64_t crosses_immediate = 0;  // step sent as MARKET without posting
};

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
    ExecPolicy policy = ExecPolicy::NATIVE;
    double urgency = 0.5;            // PASSIVE patience, in [0, 1]; 1 = cross at once
    PassiveParams passive;           // PASSIVE parameters
};

// Slice weights for TWAP/VWAP/IS (POV is event-driven; throws for POV).
std::vector<double> slice_weights(const ParentOrder& parent);

// Integer child quantities per slice (largest-remainder; sums to qty).
std::vector<std::int64_t> slice_quantities(const ParentOrder& parent);

// Due time of each slice (see header comment).
std::vector<std::int64_t> slice_times(const ParentOrder& parent);

// Throws std::invalid_argument on a negative duration / count or a
// max_behind_fraction outside [0, 1].
void validate_passive_params(const PassiveParams& params);

// Rest time of one posted child (header comment, REST).
std::int64_t patience_ns(const PassiveParams& params, double urgency,
                         bool is_algo, double risk_aversion);

// Limit price of a posted child from the routed venue's touches at decision
// time, or nullopt when it cannot be posted (no same-side quote). Never at
// or through the opposite touch.
std::optional<std::int64_t> post_price(
    std::uint8_t side, const std::optional<LevelEntry>& best_bid,
    const std::optional<LevelEntry>& best_ask,
    std::int64_t improve_min_spread_ticks);

// floor(max_behind_fraction * qty): the BEHIND tolerance.
std::int64_t max_behind_qty(const PassiveParams& params, std::int64_t parent_qty);

}  // namespace iap
