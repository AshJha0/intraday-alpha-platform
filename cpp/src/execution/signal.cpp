// Signal units and B2 urgency: line-for-line port (signal.hpp).

// GCC (12/13, -O2 and up) reports maybe-uninitialized false positives on
// std::optional / std::variant temporaries; no sanitizer build sees an
// uninitialised read. Silenced for this translation unit.
#if defined(__GNUC__) && !defined(__clang__)
#pragma GCC diagnostic ignored "-Wmaybe-uninitialized"
#pragma GCC diagnostic ignored "-Warray-bounds"  // GCC 12: initializer-list Array{...} false positive
#endif

#include "iap/execution/signal.hpp"

#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace iap {

ZScore zscore(const std::vector<double>& values) {
    ZScore out;
    std::vector<double> finite;
    for (const double v : values) {
        if (std::isfinite(v)) finite.push_back(v);
    }
    if (finite.empty()) {
        out.z.assign(values.size(), 0.0);
        return out;
    }
    double total = 0.0;  // Python sum(): 0 + v_1 + v_2 ... in order
    for (const double v : finite) total += v;
    const double mean = total / static_cast<double>(finite.size());
    double ss = 0.0;
    for (const double v : finite) ss += std::pow(v - mean, 2.0);
    const double var = ss / static_cast<double>(finite.size());
    const double sd = std::sqrt(var);
    out.mean = mean;
    if (sd <= 0.0) {
        out.z.assign(values.size(), 0.0);
        out.std = 0.0;
        return out;
    }
    out.std = sd;
    out.z.reserve(values.size());
    for (const double v : values) {
        out.z.push_back(std::isfinite(v) ? (v - mean) / sd : 0.0);
    }
    return out;
}

void ArraySignalSource::add(std::uint32_t instrument_id, const std::vector<Row>& rows) {
    for (std::size_t i = 1; i < rows.size(); ++i) {
        if (rows[i].ts <= rows[i - 1].ts) {
            throw std::invalid_argument("signal readings of instrument " +
                                        std::to_string(instrument_id) +
                                        " must be strictly increasing");
        }
    }
    std::vector<SignalReading>& out = rows_[instrument_id];
    out.clear();
    out.reserve(rows.size());
    for (const Row& r : rows) out.push_back({r.ts, r.z, r.expected_return, horizon_s_});
}

std::optional<SignalReading> ArraySignalSource::at(std::uint32_t instrument_id,
                                                   std::int64_t t) const {
    auto it = rows_.find(instrument_id);
    if (it == rows_.end() || it->second.empty()) return std::nullopt;
    const auto& rows = it->second;
    // bisect_right(ts, t) - 1: the last row with ts <= t.
    auto ub = std::upper_bound(rows.begin(), rows.end(), t,
                               [](std::int64_t v, const SignalReading& r) { return v < r.ts; });
    if (ub == rows.begin()) return std::nullopt;
    return *(ub - 1);
}

double alignment(std::uint8_t side, double signal) {
    return side == 0 ? -signal : signal;
}

UrgencyRegime regime_from_z(std::uint8_t side, double z, double threshold_z) {
    const double a = alignment(side, z);
    if (threshold_z == 0.0) {
        if (a > 0.0) return UrgencyRegime::AGREE;
        if (a < 0.0) return UrgencyRegime::ADVERSE;
        return UrgencyRegime::NEUTRAL;
    }
    if (a >= threshold_z) return UrgencyRegime::AGREE;
    if (a <= -threshold_z) return UrgencyRegime::ADVERSE;
    return UrgencyRegime::NEUTRAL;
}

void AlphaUrgencyPolicy::validate() const {
    if (threshold_z < 0.0 || max_age_ns < 0 || latency_ns < 0) {
        throw std::invalid_argument("need threshold_z >= 0, max_age_ns >= 0, latency_ns >= 0");
    }
    if (!(passive_urgency >= 0.0 && passive_urgency <= 1.0)) {
        throw std::invalid_argument("passive_urgency must be in [0, 1]");
    }
}

const char* signal_status_name(SignalStatus s) {
    switch (s) {
        case SignalStatus::OK: return "ok";
        case SignalStatus::NO_SIGNAL: return "no_signal";
        case SignalStatus::STALE: return "stale";
    }
    return "";
}

SignalDecision read_signal(const ArraySignalSource& src, std::uint32_t instrument_id,
                           std::uint8_t side, std::int64_t t, double threshold_z,
                           std::int64_t max_age_ns, std::int64_t latency_ns) {
    SignalDecision d;
    const std::int64_t at = t - latency_ns;
    std::optional<SignalReading> r = src.at(instrument_id, at);
    if (!r.has_value()) {
        d.status = SignalStatus::NO_SIGNAL;
        d.regime = UrgencyRegime::NO_SIGNAL;
        return d;
    }
    if (max_age_ns > 0 && at - r->ts > max_age_ns) {
        d.status = SignalStatus::STALE;
        d.regime = UrgencyRegime::STALE;
        return d;
    }
    d.status = SignalStatus::OK;
    d.reading = r;
    d.z = r->z;
    d.regime = regime_from_z(side, d.z, threshold_z);
    return d;
}

}  // namespace iap
