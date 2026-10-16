// Signal units and the B2 alpha-urgency rule (v1.17 WS-B B2; F7 part 1 C++
// port of iap.execution.signal (SignalReading, SignalSource,
// ArraySignalSource, zscore) and iap.execution.urgency (alignment,
// regime_from_z, AlphaUrgencyPolicy), plus the replay's causal reading
// (ExecutionReplay._reading) and the per-child classification of
// ExecutionReplay._signal_child).
//
// A signal source answers, at a decision time t, the instrument's expected
// return in SIGNAL UNITS: z = (expected_return - mean) / std over the rows
// the alpha scored (per instrument), so a threshold of 1 is "one standard
// deviation of this alpha's scores". The sign is the alpha's (positive =
// price expected to rise). Every reading is causal: at(iid, t) is the last
// row with ts <= t, never a later one.
//
// alignment(side, z) = -z for a buy, +z for a sell (> 0: waiting is
// expected to pay). regime_from_z(side, z, threshold_z): with threshold 0,
// agree / adverse / neutral by the sign of the alignment; otherwise agree
// when alignment >= threshold_z, adverse when <= -threshold_z, else neutral.
//
// B2 reading at a child decision (read_signal): the reading as of
// t - latency_ns; no row => status NO_SIGNAL; a row older than max_age_ns
// (> 0) => STALE; else OK and the regime is regime_from_z. The child's
// regime is the status itself (no_signal / stale) when there is no usable
// reading. Python stays the reference (tests/golden/expected_f7_urgency.json).

#pragma once

#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <vector>

#include "iap/execution/execution.hpp"

namespace iap {

struct SignalReading {
    std::int64_t ts = 0;           // exchange_ts of the row the reading comes from
    double z = 0.0;                // signal units
    double expected_return = 0.0;  // the alpha's expected return (dimensionless)
    double horizon_s = 1.0;        // the alpha's prediction horizon, seconds
};

// (z, mean, std) of `values` ignoring non-finite entries; a zero std gives
// all-zero z (iap.execution.signal.zscore).
struct ZScore {
    std::vector<double> z;
    double mean = 0.0;
    double std = 0.0;
};
ZScore zscore(const std::vector<double>& values);

// Explicit per-instrument readings, ts strictly increasing per instrument.
class ArraySignalSource {
public:
    struct Row {
        std::int64_t ts;
        double z;
        double expected_return;
    };
    explicit ArraySignalSource(double horizon_s = 1.0) : horizon_s_(horizon_s) {}
    // Throws std::invalid_argument when the timestamps are not strictly increasing.
    void add(std::uint32_t instrument_id, const std::vector<Row>& rows);
    // The last reading with ts <= t, or nullopt.
    std::optional<SignalReading> at(std::uint32_t instrument_id, std::int64_t t) const;
    double horizon_s() const { return horizon_s_; }

private:
    double horizon_s_;
    std::map<std::uint32_t, std::vector<SignalReading>> rows_;
};

// iap.execution.urgency.alignment / regime_from_z.
double alignment(std::uint8_t side, double signal);
UrgencyRegime regime_from_z(std::uint8_t side, double z, double threshold_z);

// ParentOrder(urgency_policy="alpha", alpha_urgency=...) (B2).
struct AlphaUrgencyPolicy {
    double threshold_z = 1.0;
    double passive_urgency = 0.1;
    std::int64_t max_age_ns = 10'000'000'000;  // 0 = never stale
    std::int64_t latency_ns = 0;
    void validate() const;  // throws std::invalid_argument
};

enum class SignalStatus : std::uint8_t { OK = 0, NO_SIGNAL = 1, STALE = 2 };
const char* signal_status_name(SignalStatus s);

// The replay's causal reading at t (ExecutionReplay._reading) and the
// _signal_child classification: `regime` is the status when there is no
// usable reading, `z` is 0 then.
struct SignalDecision {
    SignalStatus status = SignalStatus::NO_SIGNAL;
    UrgencyRegime regime = UrgencyRegime::NO_SIGNAL;
    double z = 0.0;
    std::optional<SignalReading> reading;
};
SignalDecision read_signal(const ArraySignalSource& src, std::uint32_t instrument_id,
                           std::uint8_t side, std::int64_t t, double threshold_z,
                           std::int64_t max_age_ns, std::int64_t latency_ns);

}  // namespace iap
