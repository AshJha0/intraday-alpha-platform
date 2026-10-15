// Markout primitives: port of iap.tca.fills / iap.tca.markout (header).

// GCC (12/13, -O2 and up) reports maybe-uninitialized false positives on
// std::optional / std::variant temporaries; no sanitizer build sees an
// uninitialised read. Silenced for this translation unit.
#if defined(__GNUC__) && !defined(__clang__)
#pragma GCC diagnostic ignored "-Wmaybe-uninitialized"
#endif

#include "iap/tca/markout.hpp"

#include <algorithm>
#include <stdexcept>

namespace iap {

void MarketTimeline::append(std::int64_t t, double b, double a, std::int64_t bsz,
                            std::int64_t asz) {
    if (!ts.empty() && t < ts.back()) {
        throw std::invalid_argument("timeline timestamps must be non-decreasing");
    }
    if (a < b) throw std::invalid_argument("crossed timeline state");
    ts.push_back(t);
    bid.push_back(b);
    ask.push_back(a);
    bid_sz.push_back(bsz);
    ask_sz.push_back(asz);
}

bool MarketTimeline::append_state_pinned(std::int64_t t, double b, double a,
                                         std::int64_t bsz, std::int64_t asz) {
    if (a < b) {
        ++crossed_states_skipped;
        return false;
    }
    append(t, b, a, bsz, asz);
    return true;
}

std::optional<std::size_t> MarketTimeline::prevailing(std::int64_t t) const {
    auto ub = std::upper_bound(ts.begin(), ts.end(), t);
    if (ub == ts.begin()) return std::nullopt;
    return static_cast<std::size_t>(ub - ts.begin()) - 1;
}

std::optional<double> reference_mid(const MarketTimeline& tl, std::int64_t ts, bool taker) {
    const std::int64_t ref_ts = taker ? ts : ts - 1;
    const auto i = tl.prevailing(ref_ts);
    if (!i.has_value()) return std::nullopt;
    for (const std::int64_t g : tl.halts) {
        if (tl.ts[*i] < g && g <= ref_ts) return std::nullopt;
    }
    return tl.mid(*i);
}

std::optional<double> markout_mid(const MarketTimeline& tl, std::int64_t fill_ts,
                                  std::int64_t horizon_ns) {
    if (horizon_ns < 0) throw std::invalid_argument("horizon_ns must be >= 0");
    const std::int64_t t = fill_ts + horizon_ns;
    const auto i = tl.prevailing(t);
    if (!i.has_value() || tl.ts.back() < t) return std::nullopt;
    const std::int64_t lo = std::min(fill_ts, tl.ts[*i]);
    for (const std::int64_t g : tl.halts) {
        if (lo < g && g <= t) return std::nullopt;
    }
    return tl.mid(*i);
}

std::optional<MarkoutMeasures> fill_measures(const MarketTimeline& tl, std::int64_t fill_ts,
                                             double price, std::uint8_t side, bool taker,
                                             std::int64_t horizon_ns) {
    const auto m_h = markout_mid(tl, fill_ts, horizon_ns);
    const auto m_f = reference_mid(tl, fill_ts, taker);
    if (!m_h.has_value() || !m_f.has_value()) return std::nullopt;
    const double s = side == 0 ? 1.0 : -1.0;
    return MarkoutMeasures{s * (*m_h - price), s * (price - *m_f), s * (price - *m_h),
                           s * (*m_h - *m_f)};
}

const Horizons& default_horizons_ns() {
    static const Horizons h = {{"100ms", 100'000'000},      {"1s", 1'000'000'000},
                               {"5s", 5'000'000'000},       {"30s", 30'000'000'000},
                               {"60s", 60'000'000'000},     {"5min", 300'000'000'000}};
    return h;
}

}  // namespace iap
