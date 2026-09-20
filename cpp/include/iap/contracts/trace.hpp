// Decision trace — the auditable chain for one decision
// (schemas/trace/decision_trace.schema.json and the sibling schemas it
// $refs; python reference: iap.contracts.types / iap.trace).
//
// Every record here mirrors one schema exactly: field == property, same
// name, same wire domain (u16/u32/u64/i64 integers, finite doubles, enums
// as their wire value — ints for side/direction/decision/order_type/status,
// names for algo/solver_status). `to_value()` builds the canonical JSON tree
// (key order is irrelevant on the wire because canonical JSON sorts keys;
// the code keeps the schema property order for readability) and
// `from_value()` is the STRICT inverse: unknown keys, missing keys, a bool
// where a number is wanted, a double where an integer is wanted, integers
// outside the field's domain, unknown enum values and the cross-field
// invariants the Python types enforce all throw std::invalid_argument with
// the dotted path. `from_value(x.to_value()) == x` always.
//
// A trace line is canonical_json(trace.to_value()); the digest of a stream
// is sha256 over every line followed by one 0x0A byte, in emission order
// (empty stream = sha256 of nothing). JsonlTraceSink writes exactly those
// lines. explain() renders the pinned human-readable block.
//
// Hot-path note: the trace is OFF the latency loop. The book / feature /
// alpha / execution code makes its decision without touching any of this;
// a trace is built and serialised AFTER the decision (ExecutionReplay emits
// one per parent order once the stream has been replayed). The writer
// appends into a reusable std::string (reserve once, clear per trace), so
// the per-trace cost is the tree plus one buffer append — measured by
// bench_all ("canonical trace serialisation").

#pragma once

#include <cstdint>
#include <map>
#include <memory>
#include <ostream>
#include <string>
#include <vector>

#include "iap/contracts/canonical_json.hpp"
#include "iap/util/sha256.hpp"

namespace iap {
namespace contracts {

// 64 zero hex chars: the explicit "not applicable" version marker. The
// schema pattern ^[0-9a-f]{64}$ admits it; a stage that never ran in this
// process (no feature registry / no model on the C++ execution path) records
// it instead of inventing a hash.
extern const char* const kVersionNotApplicable;

// ---- enumerations (wire values pinned by the schemas) ------------------
enum class Direction : int { DOWN = -1, FLAT = 0, UP = 1 };
enum class SolverStatus : std::uint8_t { OPTIMAL, INFEASIBLE, MAX_ITER };
enum class Decision : std::uint8_t { ALLOW = 1, REJECT = 2, KILL = 3 };
enum class Algo : std::uint8_t { TWAP, VWAP, POV, IS };
enum class OrderType : std::uint8_t {
    MARKET = 1, LIMIT = 2, IOC = 3, FOK = 4, PEG = 5, MID = 6
};
enum class ExecStatus : std::uint8_t {
    NEW = 1, PARTIAL = 2, FILLED = 3, CANCELED = 4, REJECTED = 5, EXPIRED = 6
};

const char* to_string(SolverStatus s);
const char* to_string(Algo a);
const char* to_string(Decision d);  // "ALLOW" / "REJECT" / "KILL"
SolverStatus solver_status_from_string(const std::string& s);
Algo algo_from_string(const std::string& s);

// ---- records ------------------------------------------------------------
struct AlphaSignal {
    std::int64_t timestamp = 0;
    std::uint32_t instrument_id = 0;
    double expected_return = 0.0;  // dimensionless (1e-4 = 1 bp)
    double confidence = 0.0;       // [0, 1]
    std::int64_t horizon_ns = 0;
    Direction direction = Direction::FLAT;
    std::string model_version;

    Value to_value() const;
    static AlphaSignal from_value(const Value& v, const std::string& path = "AlphaSignal");
    friend bool operator==(const AlphaSignal& a, const AlphaSignal& b);
};

struct PortfolioLeg {
    std::uint32_t instrument_id = 0;
    std::int64_t target_qty = 0;
    double target_weight = 0.0;
    double expected_return_bps = 0.0;
    std::int64_t prev_qty = 0;

    Value to_value() const;
    static PortfolioLeg from_value(const Value& v, const std::string& path = "PortfolioLeg");
    friend bool operator==(const PortfolioLeg& a, const PortfolioLeg& b);
};

struct PortfolioTarget {
    std::string strategy_id;
    std::int64_t timestamp_ns = 0;
    std::string portfolio_version;
    std::string feature_version;
    std::string model_version;
    SolverStatus solver_status = SolverStatus::OPTIMAL;
    double objective_value = 0.0;
    double turnover = 0.0;
    std::vector<PortfolioLeg> targets;  // sorted by unique instrument_id

    Value to_value() const;
    static PortfolioTarget from_value(const Value& v, const std::string& path = "PortfolioTarget");
    friend bool operator==(const PortfolioTarget& a, const PortfolioTarget& b);
};

struct RiskDecision {
    std::uint64_t order_id = 0;
    std::string strategy_id;
    std::uint32_t instrument_id = 0;
    std::int64_t timestamp_ns = 0;
    Decision decision = Decision::ALLOW;
    std::string rule_id;   // "" for ALLOW
    int rule_index = -1;   // -1 for ALLOW, else the pinned check index
    std::string reason;

    Value to_value() const;
    static RiskDecision from_value(const Value& v, const std::string& path = "RiskDecision");
    friend bool operator==(const RiskDecision& a, const RiskDecision& b);
};

struct ParentOrder {
    std::uint64_t parent_order_id = 0;
    std::string strategy_id;
    std::string alpha_id;
    std::uint32_t instrument_id = 0;
    std::uint8_t side = 0;  // BID=0 (buy) ASK=1 (sell)
    std::int64_t qty = 0;   // > 0
    Algo algo = Algo::TWAP;
    std::int64_t decision_ts = 0;
    std::int64_t arrival_ts = 0;
    std::int64_t end_ts = 0;  // decision <= arrival <= end
    double urgency = 0.0;     // [0, 1]
    std::int64_t limit_price_ticks = 0;  // 0 = unpriced
    std::map<std::string, double> params;  // keys ^[A-Za-z_][A-Za-z0-9_]*$

    Value to_value() const;
    static ParentOrder from_value(const Value& v, const std::string& path = "ParentOrder");
    friend bool operator==(const ParentOrder& a, const ParentOrder& b);
};

struct ChildOrder {
    std::uint64_t child_order_id = 0;
    std::uint64_t parent_order_id = 0;
    std::uint32_t instrument_id = 0;
    std::uint16_t venue_id = 0;  // 0 = route via SOR
    std::uint8_t side = 0;
    std::int64_t qty = 0;          // > 0
    std::int64_t price_ticks = 0;  // 0 for MARKET
    OrderType order_type = OrderType::LIMIT;
    std::int64_t submit_ts = 0;
    std::int64_t expire_ts = 0;  // 0 = parent end_ts, else >= submit_ts
    std::uint32_t slice_index = 0;

    Value to_value() const;
    static ChildOrder from_value(const Value& v, const std::string& path = "ChildOrder");
    friend bool operator==(const ChildOrder& a, const ChildOrder& b);
};

struct VenueScore {
    std::uint16_t venue_id = 0;
    bool eligible = false;  // eligible <=> rank >= 1
    std::int64_t displayed_price_ticks = 0;  // 0 = none
    std::int64_t displayed_qty = 0;
    double taker_fee = 0.0;
    double maker_rebate = 0.0;
    double commission_per_million = 0.0;
    std::int64_t latency_mean_ns = 0;
    std::uint16_t rank = 0;  // 1-based among eligible; 0 when ineligible

    Value to_value() const;
    static VenueScore from_value(const Value& v, const std::string& path = "VenueScore");
    friend bool operator==(const VenueScore& a, const VenueScore& b);
};

struct VenueDecision {
    std::uint64_t child_order_id = 0;
    std::uint16_t venue_id = 0;  // 0 = NO_ROUTE
    std::string reason;
    std::vector<VenueScore> candidates;  // sorted by unique venue_id

    Value to_value() const;
    static VenueDecision from_value(const Value& v, const std::string& path = "VenueDecision");
    friend bool operator==(const VenueDecision& a, const VenueDecision& b);
};

struct ExecutionReport {
    std::uint64_t order_id = 0;
    std::uint64_t execution_id = 0;
    ExecStatus status = ExecStatus::NEW;
    std::int64_t filled_qty = 0;  // THIS report's fill; > 0 iff PARTIAL/FILLED
    std::int64_t fill_price_ticks = 0;
    std::uint16_t venue_id = 0;
    std::int64_t exchange_ts = 0;
    std::int64_t receive_ts = 0;  // >= exchange_ts
    double fees = 0.0;            // negative = rebate

    Value to_value() const;
    static ExecutionReport from_value(const Value& v, const std::string& path = "ExecutionReport");
    friend bool operator==(const ExecutionReport& a, const ExecutionReport& b);
};

struct LatencyStats {
    std::int64_t min = 0;
    double mean = 0.0;
    std::int64_t max = 0;
    std::int64_t p50 = 0;
    std::int64_t p99 = 0;

    Value to_value() const;
    static LatencyStats from_value(const Value& v, const std::string& path = "LatencyStats");
    friend bool operator==(const LatencyStats& a, const LatencyStats& b);
};

struct TCAResult {
    std::uint64_t parent_order_id = 0;
    std::uint32_t instrument_id = 0;
    std::uint8_t side = 0;
    std::int64_t qty = 0;
    std::int64_t filled_qty = 0;
    double fill_rate = 0.0;
    std::int64_t arrival_price_ticks = 0;
    double avg_fill_price = 0.0;
    double interval_vwap = 0.0;
    double interval_twap = 0.0;
    double implementation_shortfall_bps = 0.0;
    double delay_cost_bps = 0.0;
    double trading_cost_bps = 0.0;
    double opportunity_cost_bps = 0.0;
    double spread_cost_bps = 0.0;
    double impact_bps = 0.0;
    double fees_bps = 0.0;
    double timing_cost_bps = 0.0;
    double slippage_bps = 0.0;
    double participation_rate = 0.0;
    std::uint32_t n_fills = 0;
    std::map<std::string, double> venue_contribution_bps;  // decimal venue id keys
    Algo algo = Algo::TWAP;
    LatencyStats latency_ns;

    Value to_value() const;
    static TCAResult from_value(const Value& v, const std::string& path = "TCAResult");
    friend bool operator==(const TCAResult& a, const TCAResult& b);
};

struct Attribution {
    double alpha_bps = 0.0;
    double spread_bps = 0.0;
    double impact_bps = 0.0;
    double fees_bps = 0.0;
    double timing_bps = 0.0;
    double total_bps = 0.0;  // sum of the five (1e-9)

    Value to_value() const;
    static Attribution from_value(const Value& v, const std::string& path = "Attribution");
    friend bool operator==(const Attribution& a, const Attribution& b);
};

// Stage outputs; empty vectors / absent optionals = the stage did not run.
struct TraceStages {
    std::vector<AlphaSignal> signal;
    std::unique_ptr<PortfolioTarget> portfolio;  // null = did not run
    std::vector<RiskDecision> risk;
    std::vector<ParentOrder> parent_orders;
    std::vector<ChildOrder> child_orders;
    std::vector<VenueDecision> routing;
    std::vector<ExecutionReport> fills;
    std::vector<TCAResult> tca;
    std::unique_ptr<Attribution> attribution;  // null = did not run

    TraceStages() = default;
    TraceStages(const TraceStages& o);
    TraceStages& operator=(const TraceStages& o);
    TraceStages(TraceStages&&) noexcept = default;
    TraceStages& operator=(TraceStages&&) noexcept = default;

    Value to_value() const;
    static TraceStages from_value(const Value& v, const std::string& path = "TraceStages");
    friend bool operator==(const TraceStages& a, const TraceStages& b);
};

struct DecisionTrace {
    std::string trace_id;  // 32 lowercase hex = make_trace_id(...)
    std::string session_id;
    std::uint32_t instrument_id = 0;
    std::int64_t event_ts = 0;
    std::uint64_t sequence = 0;
    std::string data_version;     // sha256 hex
    std::string feature_version;  // sha256 hex
    std::string model_version;    // sha256 hex
    std::string config_version;   // sha256 hex
    TraceStages stages;

    Value to_value() const;
    static DecisionTrace from_value(const Value& v, const std::string& path = "DecisionTrace");
    friend bool operator==(const DecisionTrace& a, const DecisionTrace& b);
    friend bool operator!=(const DecisionTrace& a, const DecisionTrace& b) { return !(a == b); }
};

// Append the canonical line of `t` (no newline) to `out`.
void write_trace_line(const DecisionTrace& t, std::string& out);
std::string trace_line(const DecisionTrace& t);

// Stream digest: sha256 over canonical_line + "\n" per trace, in order.
class TraceDigest {
public:
    TraceDigest();
    TraceDigest& update(const DecisionTrace& t);
    TraceDigest& update_line(std::string_view canonical_line);
    std::string hexdigest() const { return hash_.hexdigest(); }
    std::uint64_t count() const { return count_; }

private:
    Sha256 hash_;
    std::uint64_t count_ = 0;
    std::string buf_;  // reused across traces
};

// Sink interface (python: iap.contracts.protocols.TraceSink).
class TraceSink {
public:
    virtual ~TraceSink() = default;
    virtual void emit(const DecisionTrace& t) = 0;
};

// Writes one canonical line + '\n' per trace to a std::ostream and keeps the
// stream digest. The ostream must outlive the sink.
class JsonlTraceSink : public TraceSink {
public:
    explicit JsonlTraceSink(std::ostream& out);
    void emit(const DecisionTrace& t) override;
    const TraceDigest& digest() const { return digest_; }
    std::uint64_t count() const { return digest_.count(); }

private:
    std::ostream& out_;
    TraceDigest digest_;
    std::string buf_;
};

// Keeps every emitted trace in memory (tests, explain after the run).
class MemoryTraceSink : public TraceSink {
public:
    void emit(const DecisionTrace& t) override;
    const std::vector<DecisionTrace>& traces() const { return traces_; }
    const TraceDigest& digest() const { return digest_; }

private:
    std::vector<DecisionTrace> traces_;
    TraceDigest digest_;
};

// Pinned human-readable rendering (python iap.contracts.types.explain):
//   Order 12345
//   Alpha:      EQ03  expected return = +4.2 bps  confidence = 0.81
//   Portfolio:  target = +20,000 shares
//   Risk:       ALLOW
//   Execution:  POV 15%
//   SOR:        XV1 = 45%  XV2 = 35%  XV3 = 20%
//   Fills:      18,000 / 20,000 (90.0%)
//   TCA:        IS = 2.1 bps
//   Attribution: alpha = +6.2 bps  spread = -0.8 bps  impact = -2.1 bps  fees = -0.4 bps
// venue_names maps venue id -> display name; unnamed venues render as the
// decimal id. Stages that did not run render "(none)".
std::string explain(const DecisionTrace& t,
                    const std::map<std::uint16_t, std::string>& venue_names = {});

}  // namespace contracts
}  // namespace iap
