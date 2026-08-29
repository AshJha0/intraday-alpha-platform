// Parent-algo schedule construction (pinned rules in algos.hpp).

#include "iap/execution/algos.hpp"

#include <algorithm>
#include <cmath>
#include <numeric>
#include <stdexcept>

namespace iap {

std::vector<double> slice_weights(const ParentOrder& parent) {
    if (parent.algo == AlgoType::POV) {
        throw std::invalid_argument("POV has no precomputed slice weights");
    }
    const int n = parent.slices;
    if (n <= 0) throw std::invalid_argument("slices must be > 0");
    std::vector<double> w(static_cast<std::size_t>(n), 1.0);
    if (n == 1) return w;
    for (int i = 0; i < n; ++i) {
        const auto ui = static_cast<std::size_t>(i);
        switch (parent.algo) {
            case AlgoType::TWAP:
                w[ui] = 1.0;
                break;
            case AlgoType::VWAP: {
                const double x = (2.0 * i - (n - 1)) / (n - 1);
                w[ui] = 1.0 + x * x;
                break;
            }
            case AlgoType::IS:
                w[ui] = std::exp(-parent.risk_aversion * i /
                                 static_cast<double>(n - 1));
                break;
            case AlgoType::POV:
                break;  // unreachable
        }
    }
    return w;
}

std::vector<std::int64_t> slice_quantities(const ParentOrder& parent) {
    if (parent.qty <= 0) throw std::invalid_argument("parent qty must be > 0");
    const std::vector<double> w = slice_weights(parent);
    const double wsum = std::accumulate(w.begin(), w.end(), 0.0);
    const std::size_t n = w.size();
    std::vector<std::int64_t> q(n, 0);
    std::vector<std::pair<double, std::size_t>> frac(n);
    std::int64_t assigned = 0;
    for (std::size_t i = 0; i < n; ++i) {
        const double target = static_cast<double>(parent.qty) * w[i] / wsum;
        q[i] = static_cast<std::int64_t>(std::floor(target));
        assigned += q[i];
        // Largest remainder; ties resolved toward the earlier slice.
        frac[i] = {-(target - std::floor(target)), i};
    }
    std::sort(frac.begin(), frac.end());
    std::int64_t left = parent.qty - assigned;
    for (std::size_t i = 0; left > 0 && i < n; ++i) {
        ++q[frac[i].second];
        --left;
    }
    return q;
}

std::vector<std::int64_t> slice_times(const ParentOrder& parent) {
    if (parent.end_ts <= parent.start_ts) {
        throw std::invalid_argument("parent window must have end_ts > start_ts");
    }
    const int n = parent.slices;
    if (n <= 0) throw std::invalid_argument("slices must be > 0");
    std::vector<std::int64_t> out(static_cast<std::size_t>(n));
    const std::int64_t span = parent.end_ts - parent.start_ts;
    for (int i = 0; i < n; ++i) {
        out[static_cast<std::size_t>(i)] =
            parent.start_ts + static_cast<std::int64_t>(i) * span / n;
    }
    return out;
}

}  // namespace iap
