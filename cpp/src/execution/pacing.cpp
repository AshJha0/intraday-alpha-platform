// B3 pacing: line-for-line port of iap.execution.pacing (pacing.hpp).

// GCC (12/13, -O2 and up) reports maybe-uninitialized false positives on
// std::optional / std::variant temporaries; no sanitizer build sees an
// uninitialised read. Silenced for this translation unit.
#if defined(__GNUC__) && !defined(__clang__)
#pragma GCC diagnostic ignored "-Wmaybe-uninitialized"
#endif

#include "iap/execution/pacing.hpp"

#include <algorithm>
#include <cmath>
#include <stdexcept>

#include "iap/execution/signal.hpp"

namespace iap {

void PacingParams::validate() const {
    if (gain < 0.0 || min_scale < 0.0 || max_scale < min_scale) {
        throw std::invalid_argument("need gain >= 0 and 0 <= min_scale <= max_scale");
    }
    if (!(max_participation > 0.0 && max_participation < 1.0)) {
        throw std::invalid_argument("max_participation must be in (0, 1)");
    }
    if (max_age_ns < 0 || latency_ns < 0) {
        throw std::invalid_argument("max_age_ns and latency_ns must be >= 0");
    }
}

double pacing_scale(std::uint8_t side, std::optional<double> z, const PacingParams& params) {
    if (!z.has_value() || !std::isfinite(*z)) return 1.0;
    const double s = 1.0 - params.gain * alignment(side, *z);
    return std::min(std::max(s, params.min_scale), params.max_scale);
}

std::vector<std::int64_t> apportion(std::int64_t qty, const std::vector<double>& weights) {
    double wsum = 0.0;  // Python float(sum(weights)): 0 + w_1 + ...
    for (const double w : weights) wsum += w;
    const std::size_t n = weights.size();
    if (n == 0) return {};
    if (wsum <= 0.0) {
        std::vector<std::int64_t> out(n, 0);
        out.back() = qty;
        return out;
    }
    std::vector<std::int64_t> q(n, 0);
    std::vector<std::pair<double, std::size_t>> frac;
    frac.reserve(n);
    std::int64_t assigned = 0;
    for (std::size_t i = 0; i < n; ++i) {
        const double target = static_cast<double>(qty) * weights[i] / wsum;
        const double fl = std::floor(target);
        q[i] = static_cast<std::int64_t>(fl);
        assigned += q[i];
        frac.emplace_back(-(target - fl), i);
    }
    std::sort(frac.begin(), frac.end());  // (-fraction, index): ties to the earlier slice
    std::int64_t left = qty - assigned;
    for (std::size_t k = 0; k < n; ++k) {
        if (left <= 0) break;
        q[frac[k].second] += 1;
        --left;
    }
    return q;
}

std::int64_t tilted_slice_qty(std::int64_t remaining_qty,
                              const std::vector<double>& remaining_weights, double scale) {
    if (remaining_qty <= 0) return 0;
    if (remaining_weights.size() <= 1) return remaining_qty;
    std::vector<double> w = remaining_weights;
    w[0] = std::max(0.0, scale) * remaining_weights[0];
    return apportion(remaining_qty, w)[0];
}

PovPacingStep pov_pacing_step(std::uint8_t side, std::optional<double> z,
                              const PacingParams& params, double participation,
                              bool pov_exact, std::int64_t dv, double target_f) {
    PovPacingStep s;
    s.scale = pacing_scale(side, z, params);
    s.p_eff = std::min(participation * s.scale, params.max_participation);
    s.rate = pov_exact ? s.p_eff / (1.0 - s.p_eff) : s.p_eff;
    s.target_f = target_f + s.rate * static_cast<double>(dv);
    s.target = static_cast<std::int64_t>(std::floor(s.target_f));
    return s;
}

}  // namespace iap
