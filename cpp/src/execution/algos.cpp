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

void validate_passive_params(const PassiveParams& params) {
    if (params.max_rest_ns < 0 || params.end_margin_ns < 0) {
        throw std::invalid_argument(
            "max_rest_ns and end_margin_ns must be >= 0");
    }
    if (params.max_reprices < 0) {
        throw std::invalid_argument("max_reprices must be >= 0");
    }
    if (!(params.max_behind_fraction >= 0.0 &&
          params.max_behind_fraction <= 1.0)) {
        throw std::invalid_argument("max_behind_fraction must be in [0, 1]");
    }
    if (params.improve_min_spread_ticks < 0) {
        throw std::invalid_argument("improve_min_spread_ticks must be >= 0");
    }
}

std::int64_t patience_ns(const PassiveParams& params, double urgency,
                         bool is_algo, double risk_aversion) {
    const double u = std::min(std::max(urgency, 0.0), 1.0);
    double x = static_cast<double>(params.max_rest_ns) * (1.0 - u);
    if (is_algo) x *= std::exp(-risk_aversion);
    return static_cast<std::int64_t>(std::floor(x));
}

std::optional<std::int64_t> post_price(
    std::uint8_t side, const std::optional<LevelEntry>& best_bid,
    const std::optional<LevelEntry>& best_ask,
    std::int64_t improve_min_spread_ticks) {
    const std::optional<LevelEntry>& own = side == 0 ? best_bid : best_ask;
    if (!own.has_value()) return std::nullopt;
    const std::optional<LevelEntry>& opp = side == 0 ? best_ask : best_bid;
    std::int64_t price = own->first;
    if (opp.has_value()) {
        const std::int64_t spread =
            side == 0 ? opp->first - own->first : own->first - opp->first;
        if (improve_min_spread_ticks > 0 && spread >= improve_min_spread_ticks) {
            price += side == 0 ? 1 : -1;
        }
        // Never post at or through the opposite touch.
        if (side == 0 && price >= opp->first) {
            price = opp->first - 1;
        } else if (side == 1 && price <= opp->first) {
            price = opp->first + 1;
        }
    }
    if (price <= 0) return std::nullopt;
    return price;
}

std::int64_t max_behind_qty(const PassiveParams& params, std::int64_t parent_qty) {
    return static_cast<std::int64_t>(std::floor(
        params.max_behind_fraction * static_cast<double>(parent_qty)));
}

}  // namespace iap
