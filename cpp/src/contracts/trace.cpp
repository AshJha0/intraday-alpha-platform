// Decision-trace records: canonical tree construction, strict parsing,
// digest, sinks and explain() (contract pinned in trace.hpp).

#include "iap/contracts/trace.hpp"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <limits>
#include <set>
#include <stdexcept>

namespace iap {
namespace contracts {

const char* const kVersionNotApplicable =
    "0000000000000000000000000000000000000000000000000000000000000000";

namespace {

constexpr std::int64_t kI64Max = std::numeric_limits<std::int64_t>::max();
constexpr std::int64_t kI64Min = std::numeric_limits<std::int64_t>::min();
constexpr std::uint64_t kU64Max = std::numeric_limits<std::uint64_t>::max();
constexpr std::uint64_t kU32Max = 4294967295ull;
constexpr std::uint64_t kU16Max = 65535ull;

[[noreturn]] void contract_error(const std::string& path, const std::string& msg) {
    throw std::invalid_argument("contract: " + path + ": " + msg);
}

// Strict object reader: every key read is ticked off; done() rejects the
// rest (python Contract.from_dict: unknown keys, missing keys).
class Fields {
public:
    Fields(const Value& v, const std::string& path) : path_(path) {
        if (!v.is_object()) {
            contract_error(path, "expected object, got " +
                                     std::string(v.is_array() ? "array" : "scalar"));
        }
        obj_ = &v.as_object();
    }

    const Value& get(const char* key) {
        auto it = obj_->find(key);
        if (it == obj_->end()) contract_error(path_, std::string("missing key '") + key + "'");
        seen_.insert(key);
        return it->second;
    }

    void done() const {
        std::string unknown;
        for (const auto& [k, v] : *obj_) {
            (void)v;
            if (!seen_.count(k)) unknown += (unknown.empty() ? "" : ", ") + k;
        }
        if (!unknown.empty()) contract_error(path_, "unknown keys: " + unknown);
    }

    std::string sub(const char* key) const { return path_ + "." + key; }

    std::int64_t i64(const char* key, std::int64_t lo = kI64Min,
                     std::int64_t hi = kI64Max) {
        const Value& v = get(key);
        if (!v.is_integer()) contract_error(sub(key), "expected integer");
        if (v.kind() == Value::Kind::UInt) {
            contract_error(sub(key), "integer exceeds the int64 domain");
        }
        const std::int64_t x = v.as_int64();
        if (x < lo || x > hi) {
            contract_error(sub(key), std::to_string(x) + " outside [" +
                                         std::to_string(lo) + ", " +
                                         std::to_string(hi) + "]");
        }
        return x;
    }
    std::uint64_t u64(const char* key, std::uint64_t hi = kU64Max) {
        const Value& v = get(key);
        if (!v.is_integer()) contract_error(sub(key), "expected integer");
        if (v.kind() == Value::Kind::Int && v.as_int64() < 0) {
            contract_error(sub(key), std::to_string(v.as_int64()) + " is negative");
        }
        const std::uint64_t x = v.as_uint64();
        if (x > hi) {
            contract_error(sub(key), std::to_string(x) + " exceeds " + std::to_string(hi));
        }
        return x;
    }
    std::uint32_t u32(const char* key) { return static_cast<std::uint32_t>(u64(key, kU32Max)); }
    std::uint16_t u16(const char* key) { return static_cast<std::uint16_t>(u64(key, kU16Max)); }
    std::uint8_t side(const char* key) { return static_cast<std::uint8_t>(u64(key, 1)); }

    double number(const char* key, double lo = -std::numeric_limits<double>::infinity(),
                  double hi = std::numeric_limits<double>::infinity()) {
        const Value& v = get(key);
        if (v.is_bool() || !(v.is_double() || v.is_integer())) {
            contract_error(sub(key), "expected number");
        }
        const double x = v.as_number();
        if (!std::isfinite(x)) contract_error(sub(key), "non-finite number");
        if (x < lo || x > hi) {
            contract_error(sub(key), float_repr(x) + " outside [" + float_repr(lo) +
                                         ", " + float_repr(hi) + "]");
        }
        return x;
    }
    bool boolean(const char* key) {
        const Value& v = get(key);
        if (!v.is_bool()) contract_error(sub(key), "expected bool");
        return v.as_bool();
    }
    const std::string& str(const char* key) {
        const Value& v = get(key);
        if (!v.is_string()) contract_error(sub(key), "expected string");
        return v.as_string();
    }
    const std::string& ident(const char* key) {
        const std::string& s = str(key);
        if (!is_generic_id(s)) {
            contract_error(sub(key), "'" + s + "' is not a valid identifier");
        }
        return s;
    }
    const std::string& sha256(const char* key) {
        const std::string& s = str(key);
        if (!is_sha256_hex(s)) contract_error(sub(key), "not a lowercase sha256 hex");
        return s;
    }
    const Array& array(const char* key) {
        const Value& v = get(key);
        if (!v.is_array()) contract_error(sub(key), "expected list");
        return v.as_array();
    }
    std::map<std::string, double> number_map(const char* key, bool decimal_keys) {
        const Value& v = get(key);
        if (!v.is_object()) contract_error(sub(key), "expected object");
        std::map<std::string, double> out;
        for (const auto& [k, e] : v.as_object()) {
            if (decimal_keys) {
                bool ok = !k.empty() && (k == "0" || k[0] != '0');
                for (char c : k) ok = ok && c >= '0' && c <= '9';
                if (!ok) contract_error(sub(key), "key '" + k + "' is not a decimal id");
            }
            if (e.is_bool() || !(e.is_double() || e.is_integer())) {
                contract_error(sub(key) + "." + k, "expected number");
            }
            const double x = e.as_number();
            if (!std::isfinite(x)) contract_error(sub(key) + "." + k, "non-finite number");
            out[k] = x;
        }
        return out;
    }

private:
    const Object* obj_ = nullptr;
    std::string path_;
    std::set<std::string> seen_;
};

template <typename T>
std::vector<T> parse_list(const Array& a, const std::string& path) {
    std::vector<T> out;
    out.reserve(a.size());
    for (std::size_t i = 0; i < a.size(); ++i) {
        out.push_back(T::from_value(a[i], path + "[" + std::to_string(i) + "]"));
    }
    return out;
}

template <typename T>
Array to_list(const std::vector<T>& xs) {
    Array a;
    a.reserve(xs.size());
    for (const auto& x : xs) a.push_back(x.to_value());
    return a;
}

Value number_map_value(const std::map<std::string, double>& m) {
    Object o;
    for (const auto& [k, v] : m) o.emplace(k, Value(v));
    return Value(std::move(o));
}

}  // namespace

// ------------------------------------------------------------------ enums
const char* to_string(SolverStatus s) {
    switch (s) {
        case SolverStatus::OPTIMAL: return "OPTIMAL";
        case SolverStatus::INFEASIBLE: return "INFEASIBLE";
        case SolverStatus::MAX_ITER: return "MAX_ITER";
    }
    throw std::invalid_argument("contract: unknown SolverStatus");
}

const char* to_string(Algo a) {
    switch (a) {
        case Algo::TWAP: return "TWAP";
        case Algo::VWAP: return "VWAP";
        case Algo::POV: return "POV";
        case Algo::IS: return "IS";
    }
    throw std::invalid_argument("contract: unknown Algo");
}

const char* to_string(Decision d) {
    switch (d) {
        case Decision::ALLOW: return "ALLOW";
        case Decision::REJECT: return "REJECT";
        case Decision::KILL: return "KILL";
    }
    throw std::invalid_argument("contract: unknown Decision");
}

SolverStatus solver_status_from_string(const std::string& s) {
    if (s == "OPTIMAL") return SolverStatus::OPTIMAL;
    if (s == "INFEASIBLE") return SolverStatus::INFEASIBLE;
    if (s == "MAX_ITER") return SolverStatus::MAX_ITER;
    throw std::invalid_argument("contract: '" + s + "' is not a SolverStatus");
}

Algo algo_from_string(const std::string& s) {
    if (s == "TWAP") return Algo::TWAP;
    if (s == "VWAP") return Algo::VWAP;
    if (s == "POV") return Algo::POV;
    if (s == "IS") return Algo::IS;
    throw std::invalid_argument("contract: '" + s + "' is not an Algo");
}

// ------------------------------------------------------------ AlphaSignal
Value AlphaSignal::to_value() const {
    Object o;
    o["timestamp"] = Value(timestamp);
    o["instrument_id"] = Value(instrument_id);
    o["expected_return"] = Value(expected_return);
    o["confidence"] = Value(confidence);
    o["horizon_ns"] = Value(horizon_ns);
    o["direction"] = Value(static_cast<int>(direction));
    o["model_version"] = Value(model_version);
    return Value(std::move(o));
}

AlphaSignal AlphaSignal::from_value(const Value& v, const std::string& path) {
    Fields f(v, path);
    AlphaSignal s;
    s.timestamp = f.i64("timestamp");
    s.instrument_id = f.u32("instrument_id");
    s.expected_return = f.number("expected_return");
    s.confidence = f.number("confidence", 0.0, 1.0);
    s.horizon_ns = f.i64("horizon_ns");
    const std::int64_t d = f.i64("direction", -1, 1);
    s.direction = static_cast<Direction>(static_cast<int>(d));
    s.model_version = f.str("model_version");
    f.done();
    if (s.confidence == 0.0 && s.expected_return != 0.0) {
        contract_error(path, "expected_return must be 0 when confidence is 0");
    }
    return s;
}

bool operator==(const AlphaSignal& a, const AlphaSignal& b) {
    return a.timestamp == b.timestamp && a.instrument_id == b.instrument_id &&
           a.expected_return == b.expected_return && a.confidence == b.confidence &&
           a.horizon_ns == b.horizon_ns && a.direction == b.direction &&
           a.model_version == b.model_version;
}

// ----------------------------------------------------------- PortfolioLeg
Value PortfolioLeg::to_value() const {
    Object o;
    o["instrument_id"] = Value(instrument_id);
    o["target_qty"] = Value(target_qty);
    o["target_weight"] = Value(target_weight);
    o["expected_return_bps"] = Value(expected_return_bps);
    o["prev_qty"] = Value(prev_qty);
    return Value(std::move(o));
}

PortfolioLeg PortfolioLeg::from_value(const Value& v, const std::string& path) {
    Fields f(v, path);
    PortfolioLeg l;
    l.instrument_id = f.u32("instrument_id");
    l.target_qty = f.i64("target_qty");
    l.target_weight = f.number("target_weight");
    l.expected_return_bps = f.number("expected_return_bps");
    l.prev_qty = f.i64("prev_qty");
    f.done();
    return l;
}

bool operator==(const PortfolioLeg& a, const PortfolioLeg& b) {
    return a.instrument_id == b.instrument_id && a.target_qty == b.target_qty &&
           a.target_weight == b.target_weight &&
           a.expected_return_bps == b.expected_return_bps && a.prev_qty == b.prev_qty;
}

// -------------------------------------------------------- PortfolioTarget
Value PortfolioTarget::to_value() const {
    Object o;
    o["strategy_id"] = Value(strategy_id);
    o["timestamp_ns"] = Value(timestamp_ns);
    o["portfolio_version"] = Value(portfolio_version);
    o["feature_version"] = Value(feature_version);
    o["model_version"] = Value(model_version);
    o["solver_status"] = Value(to_string(solver_status));
    o["objective_value"] = Value(objective_value);
    o["turnover"] = Value(turnover);
    o["targets"] = Value(to_list(targets));
    return Value(std::move(o));
}

PortfolioTarget PortfolioTarget::from_value(const Value& v, const std::string& path) {
    Fields f(v, path);
    PortfolioTarget p;
    p.strategy_id = f.ident("strategy_id");
    p.timestamp_ns = f.i64("timestamp_ns");
    p.portfolio_version = f.sha256("portfolio_version");
    p.feature_version = f.sha256("feature_version");
    p.model_version = f.sha256("model_version");
    p.solver_status = solver_status_from_string(f.str("solver_status"));
    p.objective_value = f.number("objective_value");
    p.turnover = f.number("turnover", 0.0);
    p.targets = parse_list<PortfolioLeg>(f.array("targets"), path + ".targets");
    f.done();
    for (std::size_t i = 1; i < p.targets.size(); ++i) {
        if (p.targets[i - 1].instrument_id >= p.targets[i].instrument_id) {
            contract_error(path, "targets must be sorted by unique instrument_id");
        }
    }
    return p;
}

bool operator==(const PortfolioTarget& a, const PortfolioTarget& b) {
    return a.strategy_id == b.strategy_id && a.timestamp_ns == b.timestamp_ns &&
           a.portfolio_version == b.portfolio_version &&
           a.feature_version == b.feature_version && a.model_version == b.model_version &&
           a.solver_status == b.solver_status && a.objective_value == b.objective_value &&
           a.turnover == b.turnover && a.targets == b.targets;
}

// ----------------------------------------------------------- RiskDecision
Value RiskDecision::to_value() const {
    Object o;
    o["order_id"] = Value(order_id);
    o["strategy_id"] = Value(strategy_id);
    o["instrument_id"] = Value(instrument_id);
    o["timestamp_ns"] = Value(timestamp_ns);
    o["decision"] = Value(static_cast<int>(decision));
    o["rule_id"] = Value(rule_id);
    o["rule_index"] = Value(rule_index);
    o["reason"] = Value(reason);
    return Value(std::move(o));
}

RiskDecision RiskDecision::from_value(const Value& v, const std::string& path) {
    Fields f(v, path);
    RiskDecision r;
    r.order_id = f.u64("order_id");
    r.strategy_id = f.ident("strategy_id");
    r.instrument_id = f.u32("instrument_id");
    r.timestamp_ns = f.i64("timestamp_ns");
    r.decision = static_cast<Decision>(f.i64("decision", 1, 3));
    r.rule_id = f.str("rule_id");
    r.rule_index = static_cast<int>(f.i64("rule_index", -1, 65535));
    r.reason = f.str("reason");
    f.done();
    if (r.decision == Decision::ALLOW && r.rule_index != -1) {
        contract_error(path, "ALLOW requires rule_index == -1");
    }
    if (r.decision != Decision::ALLOW && r.rule_index < 0) {
        contract_error(path, "REJECT/KILL require a pinned rule_index >= 0");
    }
    return r;
}

bool operator==(const RiskDecision& a, const RiskDecision& b) {
    return a.order_id == b.order_id && a.strategy_id == b.strategy_id &&
           a.instrument_id == b.instrument_id && a.timestamp_ns == b.timestamp_ns &&
           a.decision == b.decision && a.rule_id == b.rule_id &&
           a.rule_index == b.rule_index && a.reason == b.reason;
}

// ------------------------------------------------------------ ParentOrder
Value ParentOrder::to_value() const {
    Object o;
    o["parent_order_id"] = Value(parent_order_id);
    o["strategy_id"] = Value(strategy_id);
    o["alpha_id"] = Value(alpha_id);
    o["instrument_id"] = Value(instrument_id);
    o["side"] = Value(static_cast<int>(side));
    o["qty"] = Value(qty);
    o["algo"] = Value(to_string(algo));
    o["decision_ts"] = Value(decision_ts);
    o["arrival_ts"] = Value(arrival_ts);
    o["end_ts"] = Value(end_ts);
    o["urgency"] = Value(urgency);
    o["limit_price_ticks"] = Value(limit_price_ticks);
    o["params"] = number_map_value(params);
    return Value(std::move(o));
}

ParentOrder ParentOrder::from_value(const Value& v, const std::string& path) {
    Fields f(v, path);
    ParentOrder p;
    p.parent_order_id = f.u64("parent_order_id");
    p.strategy_id = f.ident("strategy_id");
    p.alpha_id = f.ident("alpha_id");
    p.instrument_id = f.u32("instrument_id");
    p.side = f.side("side");
    p.qty = f.i64("qty", 1);
    p.algo = algo_from_string(f.str("algo"));
    p.decision_ts = f.i64("decision_ts");
    p.arrival_ts = f.i64("arrival_ts");
    p.end_ts = f.i64("end_ts");
    p.urgency = f.number("urgency", 0.0, 1.0);
    p.limit_price_ticks = f.i64("limit_price_ticks", 0);
    p.params = f.number_map("params", false);
    f.done();
    if (p.decision_ts > p.arrival_ts) contract_error(path, "decision_ts > arrival_ts");
    if (p.arrival_ts > p.end_ts) contract_error(path, "arrival_ts > end_ts");
    return p;
}

bool operator==(const ParentOrder& a, const ParentOrder& b) {
    return a.parent_order_id == b.parent_order_id && a.strategy_id == b.strategy_id &&
           a.alpha_id == b.alpha_id && a.instrument_id == b.instrument_id &&
           a.side == b.side && a.qty == b.qty && a.algo == b.algo &&
           a.decision_ts == b.decision_ts && a.arrival_ts == b.arrival_ts &&
           a.end_ts == b.end_ts && a.urgency == b.urgency &&
           a.limit_price_ticks == b.limit_price_ticks && a.params == b.params;
}

// ------------------------------------------------------------- ChildOrder
Value ChildOrder::to_value() const {
    Object o;
    o["child_order_id"] = Value(child_order_id);
    o["parent_order_id"] = Value(parent_order_id);
    o["instrument_id"] = Value(instrument_id);
    o["venue_id"] = Value(venue_id);
    o["side"] = Value(static_cast<int>(side));
    o["qty"] = Value(qty);
    o["price_ticks"] = Value(price_ticks);
    o["order_type"] = Value(static_cast<int>(order_type));
    o["submit_ts"] = Value(submit_ts);
    o["expire_ts"] = Value(expire_ts);
    o["slice_index"] = Value(slice_index);
    return Value(std::move(o));
}

ChildOrder ChildOrder::from_value(const Value& v, const std::string& path) {
    Fields f(v, path);
    ChildOrder c;
    c.child_order_id = f.u64("child_order_id");
    c.parent_order_id = f.u64("parent_order_id");
    c.instrument_id = f.u32("instrument_id");
    c.venue_id = f.u16("venue_id");
    c.side = f.side("side");
    c.qty = f.i64("qty", 1);
    c.price_ticks = f.i64("price_ticks", 0);
    c.order_type = static_cast<OrderType>(f.i64("order_type", 1, 6));
    c.submit_ts = f.i64("submit_ts");
    c.expire_ts = f.i64("expire_ts");
    c.slice_index = f.u32("slice_index");
    f.done();
    if (c.expire_ts != 0 && c.expire_ts < c.submit_ts) {
        contract_error(path, "expire_ts < submit_ts");
    }
    if (c.order_type == OrderType::MARKET && c.price_ticks != 0) {
        contract_error(path, "MARKET orders are unpriced");
    }
    return c;
}

bool operator==(const ChildOrder& a, const ChildOrder& b) {
    return a.child_order_id == b.child_order_id && a.parent_order_id == b.parent_order_id &&
           a.instrument_id == b.instrument_id && a.venue_id == b.venue_id &&
           a.side == b.side && a.qty == b.qty && a.price_ticks == b.price_ticks &&
           a.order_type == b.order_type && a.submit_ts == b.submit_ts &&
           a.expire_ts == b.expire_ts && a.slice_index == b.slice_index;
}

// ------------------------------------------------------------- VenueScore
Value VenueScore::to_value() const {
    Object o;
    o["venue_id"] = Value(venue_id);
    o["eligible"] = Value(eligible);
    o["displayed_price_ticks"] = Value(displayed_price_ticks);
    o["displayed_qty"] = Value(displayed_qty);
    o["taker_fee"] = Value(taker_fee);
    o["maker_rebate"] = Value(maker_rebate);
    o["commission_per_million"] = Value(commission_per_million);
    o["latency_mean_ns"] = Value(latency_mean_ns);
    o["rank"] = Value(rank);
    return Value(std::move(o));
}

VenueScore VenueScore::from_value(const Value& v, const std::string& path) {
    Fields f(v, path);
    VenueScore s;
    s.venue_id = f.u16("venue_id");
    s.eligible = f.boolean("eligible");
    s.displayed_price_ticks = f.i64("displayed_price_ticks", 0);
    s.displayed_qty = f.i64("displayed_qty", 0);
    s.taker_fee = f.number("taker_fee");
    s.maker_rebate = f.number("maker_rebate");
    s.commission_per_million = f.number("commission_per_million");
    s.latency_mean_ns = f.i64("latency_mean_ns", 0);
    s.rank = f.u16("rank");
    f.done();
    if (s.eligible != (s.rank > 0)) {
        contract_error(path, "eligible venues carry rank >= 1, ineligible ones rank 0");
    }
    return s;
}

bool operator==(const VenueScore& a, const VenueScore& b) {
    return a.venue_id == b.venue_id && a.eligible == b.eligible &&
           a.displayed_price_ticks == b.displayed_price_ticks &&
           a.displayed_qty == b.displayed_qty && a.taker_fee == b.taker_fee &&
           a.maker_rebate == b.maker_rebate &&
           a.commission_per_million == b.commission_per_million &&
           a.latency_mean_ns == b.latency_mean_ns && a.rank == b.rank;
}

// ---------------------------------------------------------- VenueDecision
Value VenueDecision::to_value() const {
    Object o;
    o["child_order_id"] = Value(child_order_id);
    o["venue_id"] = Value(venue_id);
    o["reason"] = Value(reason);
    o["candidates"] = Value(to_list(candidates));
    return Value(std::move(o));
}

VenueDecision VenueDecision::from_value(const Value& v, const std::string& path) {
    Fields f(v, path);
    VenueDecision d;
    d.child_order_id = f.u64("child_order_id");
    d.venue_id = f.u16("venue_id");
    d.reason = f.str("reason");
    d.candidates = parse_list<VenueScore>(f.array("candidates"), path + ".candidates");
    f.done();
    bool routed_ok = d.venue_id == 0;
    for (std::size_t i = 0; i < d.candidates.size(); ++i) {
        if (i > 0 && d.candidates[i - 1].venue_id >= d.candidates[i].venue_id) {
            contract_error(path, "candidates must be sorted by unique venue_id");
        }
        if (d.candidates[i].venue_id == d.venue_id && d.candidates[i].eligible) {
            routed_ok = true;
        }
    }
    if (!routed_ok) contract_error(path, "routed venue must be an eligible candidate");
    return d;
}

bool operator==(const VenueDecision& a, const VenueDecision& b) {
    return a.child_order_id == b.child_order_id && a.venue_id == b.venue_id &&
           a.reason == b.reason && a.candidates == b.candidates;
}

// -------------------------------------------------------- ExecutionReport
Value ExecutionReport::to_value() const {
    Object o;
    o["order_id"] = Value(order_id);
    o["execution_id"] = Value(execution_id);
    o["status"] = Value(static_cast<int>(status));
    o["filled_qty"] = Value(filled_qty);
    o["fill_price_ticks"] = Value(fill_price_ticks);
    o["venue_id"] = Value(venue_id);
    o["exchange_ts"] = Value(exchange_ts);
    o["receive_ts"] = Value(receive_ts);
    o["fees"] = Value(fees);
    return Value(std::move(o));
}

ExecutionReport ExecutionReport::from_value(const Value& v, const std::string& path) {
    Fields f(v, path);
    ExecutionReport r;
    r.order_id = f.u64("order_id");
    r.execution_id = f.u64("execution_id");
    r.status = static_cast<ExecStatus>(f.i64("status", 1, 6));
    r.filled_qty = f.i64("filled_qty", 0);
    r.fill_price_ticks = f.i64("fill_price_ticks", 0);
    r.venue_id = f.u16("venue_id");
    r.exchange_ts = f.i64("exchange_ts");
    r.receive_ts = f.i64("receive_ts");
    r.fees = f.number("fees");
    f.done();
    if (r.receive_ts < r.exchange_ts) contract_error(path, "receive_ts < exchange_ts");
    const bool is_fill = r.status == ExecStatus::PARTIAL || r.status == ExecStatus::FILLED;
    if (is_fill && r.filled_qty <= 0) contract_error(path, "fill status needs filled_qty > 0");
    if (!is_fill && r.filled_qty != 0) {
        contract_error(path, "non-fill status carries filled_qty 0");
    }
    return r;
}

bool operator==(const ExecutionReport& a, const ExecutionReport& b) {
    return a.order_id == b.order_id && a.execution_id == b.execution_id &&
           a.status == b.status && a.filled_qty == b.filled_qty &&
           a.fill_price_ticks == b.fill_price_ticks && a.venue_id == b.venue_id &&
           a.exchange_ts == b.exchange_ts && a.receive_ts == b.receive_ts &&
           a.fees == b.fees;
}

// ----------------------------------------------------------- LatencyStats
Value LatencyStats::to_value() const {
    Object o;
    o["min"] = Value(min);
    o["mean"] = Value(mean);
    o["max"] = Value(max);
    o["p50"] = Value(p50);
    o["p99"] = Value(p99);
    return Value(std::move(o));
}

LatencyStats LatencyStats::from_value(const Value& v, const std::string& path) {
    Fields f(v, path);
    LatencyStats s;
    s.min = f.i64("min", 0);
    s.mean = f.number("mean", 0.0);
    s.max = f.i64("max", 0);
    s.p50 = f.i64("p50", 0);
    s.p99 = f.i64("p99", 0);
    f.done();
    if (!(s.min <= s.p50 && s.p50 <= s.p99 && s.p99 <= s.max)) {
        contract_error(path, "need min <= p50 <= p99 <= max");
    }
    return s;
}

bool operator==(const LatencyStats& a, const LatencyStats& b) {
    return a.min == b.min && a.mean == b.mean && a.max == b.max && a.p50 == b.p50 &&
           a.p99 == b.p99;
}

// -------------------------------------------------------------- TCAResult
Value TCAResult::to_value() const {
    Object o;
    o["parent_order_id"] = Value(parent_order_id);
    o["instrument_id"] = Value(instrument_id);
    o["side"] = Value(static_cast<int>(side));
    o["qty"] = Value(qty);
    o["filled_qty"] = Value(filled_qty);
    o["fill_rate"] = Value(fill_rate);
    o["arrival_price_ticks"] = Value(arrival_price_ticks);
    o["avg_fill_price"] = Value(avg_fill_price);
    o["interval_vwap"] = Value(interval_vwap);
    o["interval_twap"] = Value(interval_twap);
    o["implementation_shortfall_bps"] = Value(implementation_shortfall_bps);
    o["delay_cost_bps"] = Value(delay_cost_bps);
    o["trading_cost_bps"] = Value(trading_cost_bps);
    o["opportunity_cost_bps"] = Value(opportunity_cost_bps);
    o["spread_cost_bps"] = Value(spread_cost_bps);
    o["impact_bps"] = Value(impact_bps);
    o["fees_bps"] = Value(fees_bps);
    o["timing_cost_bps"] = Value(timing_cost_bps);
    o["slippage_bps"] = Value(slippage_bps);
    o["participation_rate"] = Value(participation_rate);
    o["n_fills"] = Value(n_fills);
    o["venue_contribution_bps"] = number_map_value(venue_contribution_bps);
    o["algo"] = Value(to_string(algo));
    o["latency_ns"] = latency_ns.to_value();
    return Value(std::move(o));
}

TCAResult TCAResult::from_value(const Value& v, const std::string& path) {
    Fields f(v, path);
    TCAResult t;
    t.parent_order_id = f.u64("parent_order_id");
    t.instrument_id = f.u32("instrument_id");
    t.side = f.side("side");
    t.qty = f.i64("qty", 1);
    t.filled_qty = f.i64("filled_qty", 0);
    t.fill_rate = f.number("fill_rate", 0.0, 1.0);
    t.arrival_price_ticks = f.i64("arrival_price_ticks", 0);
    t.avg_fill_price = f.number("avg_fill_price", 0.0);
    t.interval_vwap = f.number("interval_vwap", 0.0);
    t.interval_twap = f.number("interval_twap", 0.0);
    t.implementation_shortfall_bps = f.number("implementation_shortfall_bps");
    t.delay_cost_bps = f.number("delay_cost_bps");
    t.trading_cost_bps = f.number("trading_cost_bps");
    t.opportunity_cost_bps = f.number("opportunity_cost_bps");
    t.spread_cost_bps = f.number("spread_cost_bps");
    t.impact_bps = f.number("impact_bps");
    t.fees_bps = f.number("fees_bps");
    t.timing_cost_bps = f.number("timing_cost_bps");
    t.slippage_bps = f.number("slippage_bps");
    t.participation_rate = f.number("participation_rate", 0.0, 1.0);
    t.n_fills = f.u32("n_fills");
    t.venue_contribution_bps = f.number_map("venue_contribution_bps", true);
    t.algo = algo_from_string(f.str("algo"));
    t.latency_ns = LatencyStats::from_value(f.get("latency_ns"), path + ".latency_ns");
    f.done();
    if (t.filled_qty > t.qty) contract_error(path, "filled_qty > qty");
    if (std::fabs(t.fill_rate - static_cast<double>(t.filled_qty) /
                                    static_cast<double>(t.qty)) > 1e-9) {
        contract_error(path, "fill_rate != filled_qty / qty");
    }
    const double perold = t.delay_cost_bps + t.trading_cost_bps + t.opportunity_cost_bps;
    if (std::fabs(perold - t.implementation_shortfall_bps) > 1e-9) {
        contract_error(path, "Perold identity violated");
    }
    const double split = t.spread_cost_bps + t.impact_bps + t.timing_cost_bps;
    if (std::fabs(split - t.trading_cost_bps) > 1e-9) {
        contract_error(path, "trading != spread + impact + timing");
    }
    return t;
}

bool operator==(const TCAResult& a, const TCAResult& b) {
    return a.parent_order_id == b.parent_order_id && a.instrument_id == b.instrument_id &&
           a.side == b.side && a.qty == b.qty && a.filled_qty == b.filled_qty &&
           a.fill_rate == b.fill_rate && a.arrival_price_ticks == b.arrival_price_ticks &&
           a.avg_fill_price == b.avg_fill_price && a.interval_vwap == b.interval_vwap &&
           a.interval_twap == b.interval_twap &&
           a.implementation_shortfall_bps == b.implementation_shortfall_bps &&
           a.delay_cost_bps == b.delay_cost_bps && a.trading_cost_bps == b.trading_cost_bps &&
           a.opportunity_cost_bps == b.opportunity_cost_bps &&
           a.spread_cost_bps == b.spread_cost_bps && a.impact_bps == b.impact_bps &&
           a.fees_bps == b.fees_bps && a.timing_cost_bps == b.timing_cost_bps &&
           a.slippage_bps == b.slippage_bps && a.participation_rate == b.participation_rate &&
           a.n_fills == b.n_fills && a.venue_contribution_bps == b.venue_contribution_bps &&
           a.algo == b.algo && a.latency_ns == b.latency_ns;
}

// ------------------------------------------------------------ Attribution
Value Attribution::to_value() const {
    Object o;
    o["alpha_bps"] = Value(alpha_bps);
    o["spread_bps"] = Value(spread_bps);
    o["impact_bps"] = Value(impact_bps);
    o["fees_bps"] = Value(fees_bps);
    o["timing_bps"] = Value(timing_bps);
    o["total_bps"] = Value(total_bps);
    return Value(std::move(o));
}

Attribution Attribution::from_value(const Value& v, const std::string& path) {
    Fields f(v, path);
    Attribution a;
    a.alpha_bps = f.number("alpha_bps");
    a.spread_bps = f.number("spread_bps");
    a.impact_bps = f.number("impact_bps");
    a.fees_bps = f.number("fees_bps");
    a.timing_bps = f.number("timing_bps");
    a.total_bps = f.number("total_bps");
    f.done();
    const double total = a.alpha_bps + a.spread_bps + a.impact_bps + a.fees_bps + a.timing_bps;
    if (std::fabs(total - a.total_bps) > 1e-9) {
        contract_error(path, "total_bps != sum of components");
    }
    return a;
}

bool operator==(const Attribution& a, const Attribution& b) {
    return a.alpha_bps == b.alpha_bps && a.spread_bps == b.spread_bps &&
           a.impact_bps == b.impact_bps && a.fees_bps == b.fees_bps &&
           a.timing_bps == b.timing_bps && a.total_bps == b.total_bps;
}

// ------------------------------------------------------------ TraceStages
TraceStages::TraceStages(const TraceStages& o)
    : signal(o.signal),
      portfolio(o.portfolio ? std::make_unique<PortfolioTarget>(*o.portfolio) : nullptr),
      risk(o.risk),
      parent_orders(o.parent_orders),
      child_orders(o.child_orders),
      routing(o.routing),
      fills(o.fills),
      tca(o.tca),
      attribution(o.attribution ? std::make_unique<Attribution>(*o.attribution) : nullptr) {}

TraceStages& TraceStages::operator=(const TraceStages& o) {
    if (this != &o) {
        TraceStages copy(o);
        *this = std::move(copy);
    }
    return *this;
}

Value TraceStages::to_value() const {
    Object o;
    o["signal"] = Value(to_list(signal));
    o["portfolio"] = portfolio ? portfolio->to_value() : Value(nullptr);
    o["risk"] = Value(to_list(risk));
    o["parent_orders"] = Value(to_list(parent_orders));
    o["child_orders"] = Value(to_list(child_orders));
    o["routing"] = Value(to_list(routing));
    o["fills"] = Value(to_list(fills));
    o["tca"] = Value(to_list(tca));
    o["attribution"] = attribution ? attribution->to_value() : Value(nullptr);
    return Value(std::move(o));
}

TraceStages TraceStages::from_value(const Value& v, const std::string& path) {
    Fields f(v, path);
    TraceStages s;
    s.signal = parse_list<AlphaSignal>(f.array("signal"), path + ".signal");
    {
        const Value& p = f.get("portfolio");
        if (!p.is_null()) {
            s.portfolio = std::make_unique<PortfolioTarget>(
                PortfolioTarget::from_value(p, path + ".portfolio"));
        }
    }
    s.risk = parse_list<RiskDecision>(f.array("risk"), path + ".risk");
    s.parent_orders = parse_list<ParentOrder>(f.array("parent_orders"), path + ".parent_orders");
    s.child_orders = parse_list<ChildOrder>(f.array("child_orders"), path + ".child_orders");
    s.routing = parse_list<VenueDecision>(f.array("routing"), path + ".routing");
    s.fills = parse_list<ExecutionReport>(f.array("fills"), path + ".fills");
    s.tca = parse_list<TCAResult>(f.array("tca"), path + ".tca");
    {
        const Value& a = f.get("attribution");
        if (!a.is_null()) {
            s.attribution = std::make_unique<Attribution>(
                Attribution::from_value(a, path + ".attribution"));
        }
    }
    f.done();
    return s;
}

bool operator==(const TraceStages& a, const TraceStages& b) {
    const bool pf = (a.portfolio == nullptr) == (b.portfolio == nullptr) &&
                    (a.portfolio == nullptr || *a.portfolio == *b.portfolio);
    const bool at = (a.attribution == nullptr) == (b.attribution == nullptr) &&
                    (a.attribution == nullptr || *a.attribution == *b.attribution);
    return pf && at && a.signal == b.signal && a.risk == b.risk &&
           a.parent_orders == b.parent_orders && a.child_orders == b.child_orders &&
           a.routing == b.routing && a.fills == b.fills && a.tca == b.tca;
}

// ---------------------------------------------------------- DecisionTrace
Value DecisionTrace::to_value() const {
    Object o;
    o["trace_id"] = Value(trace_id);
    o["session_id"] = Value(session_id);
    o["instrument_id"] = Value(instrument_id);
    o["event_ts"] = Value(event_ts);
    o["sequence"] = Value(sequence);
    o["data_version"] = Value(data_version);
    o["feature_version"] = Value(feature_version);
    o["model_version"] = Value(model_version);
    o["config_version"] = Value(config_version);
    o["stages"] = stages.to_value();
    return Value(std::move(o));
}

DecisionTrace DecisionTrace::from_value(const Value& v, const std::string& path) {
    Fields f(v, path);
    DecisionTrace t;
    t.trace_id = f.str("trace_id");
    if (!is_trace_id(t.trace_id)) contract_error(path + ".trace_id", "not a 32-hex trace id");
    t.session_id = f.ident("session_id");
    t.instrument_id = f.u32("instrument_id");
    t.event_ts = f.i64("event_ts");
    t.sequence = f.u64("sequence");
    t.data_version = f.sha256("data_version");
    t.feature_version = f.sha256("feature_version");
    t.model_version = f.sha256("model_version");
    t.config_version = f.sha256("config_version");
    t.stages = TraceStages::from_value(f.get("stages"), path + ".stages");
    f.done();
    return t;
}

bool operator==(const DecisionTrace& a, const DecisionTrace& b) {
    return a.trace_id == b.trace_id && a.session_id == b.session_id &&
           a.instrument_id == b.instrument_id && a.event_ts == b.event_ts &&
           a.sequence == b.sequence && a.data_version == b.data_version &&
           a.feature_version == b.feature_version && a.model_version == b.model_version &&
           a.config_version == b.config_version && a.stages == b.stages;
}

// --------------------------------------------------------- line / digest
void write_trace_line(const DecisionTrace& t, std::string& out) {
    write_canonical(t.to_value(), out);
}

std::string trace_line(const DecisionTrace& t) {
    std::string out;
    out.reserve(8192);
    write_trace_line(t, out);
    return out;
}

TraceDigest::TraceDigest() { buf_.reserve(8192); }

TraceDigest& TraceDigest::update(const DecisionTrace& t) {
    buf_.clear();
    write_trace_line(t, buf_);
    return update_line(buf_);
}

TraceDigest& TraceDigest::update_line(std::string_view canonical_line) {
    hash_.update(canonical_line.data(), canonical_line.size());
    const char nl = '\n';
    hash_.update(&nl, 1);
    ++count_;
    return *this;
}

JsonlTraceSink::JsonlTraceSink(std::ostream& out) : out_(out) { buf_.reserve(8192); }

void JsonlTraceSink::emit(const DecisionTrace& t) {
    buf_.clear();
    write_trace_line(t, buf_);
    buf_.push_back('\n');
    out_.write(buf_.data(), static_cast<std::streamsize>(buf_.size()));
    if (!out_) throw std::runtime_error("JsonlTraceSink: write failed");
    digest_.update_line(std::string_view(buf_.data(), buf_.size() - 1));
}

void MemoryTraceSink::emit(const DecisionTrace& t) {
    traces_.push_back(t);
    digest_.update(t);
}

// ---------------------------------------------------------------- explain
namespace {

constexpr std::size_t kLabelWidth = 12;

std::string line(const char* label, const std::string& body) {
    std::string s = std::string(label) + ": ";
    if (s.size() < kLabelWidth) s.append(kLabelWidth - s.size(), ' ');
    return s + body;
}

// Python f"{v:+.1f}" (C printf is correctly rounded on the exact binary
// value, as is Python's formatter).
std::string fixed(double v, int decimals, bool sign) {
    char buf[64];
    std::snprintf(buf, sizeof(buf), sign ? "%+.*f" : "%.*f", decimals, v);
    return buf;
}

std::string bps(double v) { return fixed(v, 1, true) + " bps"; }

// Python f"{n:,d}" / f"{n:+,d}".
std::string grouped(std::int64_t n, bool sign) {
    const bool neg = n < 0;
    std::uint64_t mag = neg ? std::uint64_t{0} - static_cast<std::uint64_t>(n)
                            : static_cast<std::uint64_t>(n);
    std::string digits = std::to_string(mag);
    std::string out;
    for (std::size_t i = 0; i < digits.size(); ++i) {
        if (i != 0 && (digits.size() - i) % 3 == 0) out.push_back(',');
        out.push_back(digits[i]);
    }
    if (neg) return "-" + out;
    if (sign) return "+" + out;
    return out;
}

}  // namespace

std::string explain(const DecisionTrace& t,
                    const std::map<std::uint16_t, std::string>& venue_names) {
    const TraceStages& st = t.stages;
    const auto& parents = st.parent_orders;
    std::vector<std::string> lines;
    lines.push_back(parents.empty() ? "Trace " + t.trace_id
                                    : "Order " + std::to_string(parents[0].parent_order_id));

    if (!st.signal.empty()) {
        // signal[0] is the acting signal (labelled by the order's alpha id);
        // every further signal is one of its components (its own model_version).
        for (std::size_t i = 0; i < st.signal.size(); ++i) {
            const auto& sig = st.signal[i];
            const std::string label =
                (i == 0 && !parents.empty()) ? parents[0].alpha_id : sig.model_version;
            lines.push_back(line("Alpha", label + "  expected return = " +
                                              bps(sig.expected_return * 1e4) +
                                              "  confidence = " + fixed(sig.confidence, 2, false)));
        }
    } else {
        lines.push_back(line("Alpha", "(none)"));
    }

    if (st.portfolio) {
        const PortfolioLeg* leg = nullptr;
        for (const auto& l : st.portfolio->targets) {
            if (l.instrument_id == t.instrument_id) {
                leg = &l;
                break;
            }
        }
        if (leg == nullptr && !st.portfolio->targets.empty()) leg = &st.portfolio->targets[0];
        lines.push_back(line("Portfolio",
                             leg ? "target = " + grouped(leg->target_qty, true) + " shares"
                                 : std::string(to_string(st.portfolio->solver_status)) +
                                       "  no target"));
    } else {
        lines.push_back(line("Portfolio", "(none)"));
    }

    if (!st.risk.empty()) {
        for (const auto& rd : st.risk) {
            std::string body = to_string(rd.decision);
            if (rd.decision != Decision::ALLOW) {
                body += "  rule = " + rd.rule_id + "  reason = " + rd.reason;
            }
            lines.push_back(line("Risk", body));
        }
    } else {
        lines.push_back(line("Risk", "(none)"));
    }

    if (!parents.empty()) {
        for (const auto& po : parents) {
            std::string body = to_string(po.algo);
            auto it = po.params.find("participation");
            if (it != po.params.end()) body += " " + fixed(it->second * 100.0, 0, false) + "%";
            lines.push_back(line("Execution", body));
        }
    } else {
        lines.push_back(line("Execution", "(none)"));
    }

    std::map<std::uint64_t, std::int64_t> child_qty;
    for (const auto& c : st.child_orders) child_qty[c.child_order_id] = c.qty;
    std::map<std::uint16_t, std::int64_t> routed;
    for (const auto& vd : st.routing) {
        auto it = child_qty.find(vd.child_order_id);
        routed[vd.venue_id] += it == child_qty.end() ? 0 : it->second;
    }
    std::int64_t total_routed = 0;
    for (const auto& [v, q] : routed) total_routed += q;
    if (total_routed != 0) {
        std::string body;
        for (const auto& [v, q] : routed) {
            if (!body.empty()) body += "  ";
            auto nit = venue_names.find(v);
            body += (nit == venue_names.end() ? std::to_string(v) : nit->second) + " = " +
                    fixed(100.0 * static_cast<double>(q) / static_cast<double>(total_routed), 0,
                          false) +
                    "%";
        }
        lines.push_back(line("SOR", body));
    } else {
        lines.push_back(line("SOR", "(none)"));
    }

    std::int64_t target_qty = 0;
    for (const auto& po : parents) target_qty += po.qty;
    std::int64_t filled = 0;
    for (const auto& er : st.fills) filled += er.filled_qty;
    if (target_qty != 0) {
        lines.push_back(line("Fills", grouped(filled, false) + " / " + grouped(target_qty, false) +
                                          " (" +
                                          fixed(100.0 * static_cast<double>(filled) /
                                                    static_cast<double>(target_qty),
                                                1, false) +
                                          "%)"));
    } else {
        lines.push_back(line("Fills", "(none)"));
    }

    if (!st.tca.empty()) {
        for (const auto& tc : st.tca) {
            lines.push_back(
                line("TCA", "IS = " + fixed(tc.implementation_shortfall_bps, 1, false) + " bps"));
        }
    } else {
        lines.push_back(line("TCA", "(none)"));
    }

    if (st.attribution) {
        const Attribution& a = *st.attribution;
        lines.push_back(line("Attribution", "alpha = " + bps(a.alpha_bps) +
                                                "  spread = " + bps(a.spread_bps) +
                                                "  impact = " + bps(a.impact_bps) +
                                                "  fees = " + bps(a.fees_bps)));
    } else {
        lines.push_back(line("Attribution", "(none)"));
    }

    std::string out;
    for (std::size_t i = 0; i < lines.size(); ++i) {
        if (i) out.push_back('\n');
        out += lines[i];
    }
    return out;
}

}  // namespace contracts
}  // namespace iap
