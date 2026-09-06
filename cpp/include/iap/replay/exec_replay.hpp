// Execution replay — the event-driven backtest driver (spec section 18).
//
// Extends the deterministic replay layer: consumes the normalized event
// stream in file order (event time), drives the ExecutionSimulator's books
// and order lifecycle, and works a set of parent orders through their
// VWAP/TWAP/POV/IS schedules (algos.hpp). Same events + config + seed =>
// identical fills, bit for bit.
//
// Per event, in pinned order:
//   1. the simulator processes the event (child activation, passive queue
//      tracking, book application, crossing checks — execution.hpp rules);
//   2. the scheduler then evaluates every parent against the post-event
//      state: due TWAP/VWAP/IS slices are issued (decision_ts = the event's
//      exchange_ts, limit prices read from the just-updated book), and POV
//      targets are re-evaluated after TRADE events of the parent's
//      instrument inside its window.
// Children expire at their parent's end_ts (execution.hpp rule 7); after
// the last event every still-unfinished child is cancelled (unfilled
// residual = opportunity cost, reported per parent). Every fill attributed
// to a parent lies inside [start_ts, end_ts] by construction.
//
// Parent accounting identity (tested): with buy notional > 0,
//   total_cost = fees - rebates + impact
// and fees/rebates/impact are exact sums over the parent's fills.

#pragma once

#include <cstdint>
#include <map>
#include <vector>

#include "iap/execution/algos.hpp"
#include "iap/execution/execution.hpp"
#include "iap/sor/sor.hpp"

namespace iap {

struct ParentReport {
    std::uint64_t parent_id = 0;
    std::int64_t filled_qty = 0;
    std::int64_t unfilled_qty = 0;
    std::int64_t children = 0;
    double notional = 0.0;      // sum of fill qty * price * tick * lot
    double avg_price = 0.0;     // notional / (filled qty * lot); 0 if unfilled
    double fees = 0.0;          // taker fees (>= 0)
    double rebates = 0.0;       // maker rebates (>= 0)
    double impact = 0.0;        // linear impact charges (>= 0)
    double total_cost = 0.0;    // fees - rebates + impact
};

struct ExecReplayResult {
    std::vector<Fill> fills;
    std::map<std::uint64_t, ParentReport> parents;
    std::uint64_t events_processed = 0;
    std::uint64_t sor_no_route = 0;  // children not submitted: no eligible venue
};

class ExecutionReplay {
public:
    ExecutionReplay(const ExecConfig& config,
                    const std::vector<ParentOrder>& parents,
                    SorOptions sor_options = SorOptions{});

    // Replay the stream, working every parent. Callable once.
    ExecReplayResult run(const std::vector<MarketEvent>& events);

    const ExecutionSimulator& simulator() const { return sim_; }

private:
    struct ParentState {
        ParentOrder order;
        std::vector<std::int64_t> slice_qty;   // TWAP/VWAP/IS
        std::vector<std::int64_t> slice_due;   // TWAP/VWAP/IS
        std::size_t next_slice = 0;
        std::int64_t filled_qty = 0;           // fills booked so far
        std::int64_t pov_volume = 0;           // window TRADE volume (POV)
        std::vector<std::uint64_t> child_ids;
    };

    void schedule(ParentState& ps, const MarketEvent& ev);
    // Issue one child of at most max_child_qty; returns false when unroutable.
    bool issue_child(ParentState& ps, std::int64_t child_qty,
                     std::int64_t decision_ts, bool passive);
    // Split a slice into children of at most max_child_qty (pinned).
    void issue_slice(ParentState& ps, std::int64_t slice_qty,
                     std::int64_t decision_ts, bool passive);
    // Filled + still open/in-flight qty of the parent's children.
    std::int64_t committed_qty(const ParentState& ps) const;
    void book_new_fills();

    ExecConfig config_;
    ExecutionSimulator sim_;
    SmartOrderRouter sor_;
    std::vector<std::uint16_t> sor_candidates_;
    std::vector<ParentState> parents_;
    std::size_t fills_booked_ = 0;
    std::uint64_t sor_no_route_ = 0;
    bool ran_ = false;
};

}  // namespace iap
