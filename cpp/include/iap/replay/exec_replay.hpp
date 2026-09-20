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
//
// DECISION TRACE (optional; contracts/trace.hpp). With a sink attached via
// set_trace_sink(), run() emits ONE DecisionTrace per parent order it
// drove, after the stream has been replayed (the trace is built from the
// finished order state, outside the event loop — nothing in on_event /
// schedule allocates or serialises on its behalf; the only per-child work
// while tracing is the SOR candidate table, taken at the routing call).
// C++ owns the EXECUTION stages only; the stages it fills:
//   parent_orders  the parent (strategy_id / alpha_id from TraceOptions;
//                  decision_ts = arrival_ts = start_ts; urgency 0.0 for the
//                  passive TWAP/VWAP schedules, 1.0 for the MARKET-child
//                  POV/IS ones; params = slices / max_child_qty /
//                  risk_aversion / participation as applicable);
//   child_orders   every submitted child (simulator order id, LIMIT price
//                  or 0 for MARKET, expire_ts = parent end_ts, slice_index
//                  = submission ordinal within the parent);
//   routing        one VenueDecision per child: the SOR's candidate table
//                  (SmartOrderRouter::score_*, rank 1 == the routed venue)
//                  when the parent is SOR-routed, or the pinned venue as the
//                  single eligible candidate when parent.venue_id != 0
//                  (reason says which);
//   fills          one ExecutionReport per Fill (execution_id = fill_id,
//                  PARTIAL while the child still has remaining qty after the
//                  fill, FILLED on the fill that completes it; receive_ts =
//                  exchange_ts — the simulator has no capture clock; fees =
//                  signed fee, rebates negative).
// signal / risk / tca are EMPTY and portfolio / attribution NULL: those
// stages belong to the alpha, risk and TCA owners (python / rust / java)
// and are never fabricated here. Identity: instrument_id = the parent's;
// event_ts / sequence = the first replayed event at or after the parent's
// start_ts (the event on which the scheduler first evaluated it; start_ts
// / 0 when the stream ends before the window opens); trace_id =
// make_trace_id(session_id, instrument_id, event_ts, sequence). Versions:
// data_version = sha256 of the IAP1 encoding of the replayed events (the
// codec golden's file hash) unless TraceOptions overrides it;
// config_version = content_hash(config_value(config, sor_options)) unless
// overridden; feature_version / model_version are whatever the caller
// passes — default contracts::kVersionNotApplicable (64 zeros), the
// explicit "this process ran no feature registry / model" marker the
// schema pattern admits. Same events + config + seed => identical traces
// and identical stream digest (tested against expected_replay_fills.json).

#pragma once

#include <cstdint>
#include <map>
#include <string>
#include <vector>

#include "iap/contracts/canonical_json.hpp"
#include "iap/contracts/trace.hpp"
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

// Identity and versions stamped on every emitted trace (header comment).
struct TraceOptions {
    std::string session_id;  // required: ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$
    std::string strategy_id = "REPLAY";
    std::string alpha_id = "REPLAY";
    std::string feature_version = contracts::kVersionNotApplicable;
    std::string model_version = contracts::kVersionNotApplicable;
    std::string data_version;    // empty => sha256(encode_iap1(events))
    std::string config_version;  // empty => content_hash(config_value(...))
};

class ExecutionReplay {
public:
    ExecutionReplay(const ExecConfig& config,
                    const std::vector<ParentOrder>& parents,
                    SorOptions sor_options = SorOptions{});

    // Replay the stream, working every parent. Callable once.
    ExecReplayResult run(const std::vector<MarketEvent>& events);

    // Attach a trace sink (must outlive run()); validates the options
    // (session id alphabet, sha256 hex versions) and throws
    // std::invalid_argument otherwise. Must be called before run().
    void set_trace_sink(contracts::TraceSink* sink, TraceOptions options);

    // Canonical JSON tree of the configuration in force (latency legs, seed,
    // impact coefficient, instruments, venues, SOR options); its
    // content_hash is the default config_version.
    static contracts::Value config_value(const ExecConfig& config,
                                         const SorOptions& sor_options);

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
        // Trace identity: first replayed event at/after start_ts.
        bool triggered = false;
        std::int64_t trigger_ts = 0;
        std::uint64_t trigger_seq = 0;
        std::vector<contracts::VenueDecision> routing;  // tracing only
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
    // Tracing (sink attached only): the routing record of a just-submitted
    // child, and the per-parent trace emitted at the end of run().
    void record_routing(ParentState& ps, const ChildOrder& child,
                        std::uint64_t child_id, bool passive, bool sor_used);
    contracts::DecisionTrace build_trace(const ParentState& ps,
                                         const ExecReplayResult& res) const;

    ExecConfig config_;
    ExecutionSimulator sim_;
    SmartOrderRouter sor_;
    std::vector<std::uint16_t> sor_candidates_;
    std::vector<ParentState> parents_;
    std::size_t fills_booked_ = 0;
    std::uint64_t sor_no_route_ = 0;
    bool ran_ = false;
    SorOptions sor_options_;
    contracts::TraceSink* trace_sink_ = nullptr;
    TraceOptions trace_options_;
    std::vector<SorCandidate> sor_scores_;  // reused per routing call
};

}  // namespace iap
