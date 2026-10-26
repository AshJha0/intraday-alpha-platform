// Markout primitives (API_PORTFOLIO_TCA.md section 2.9; v1.17 F7 part 1 C++
// port of the parts of iap.tca.fills (MarketTimeline) and iap.tca.markout
// (reference_mid, markout_mid, fill_measures) that the B4 counterfactual
// (fading.hpp) and the B6 markout decomposition (markout_decomposition.hpp)
// read. Report-plane code: it allocates, it is never on the event loop.
//
// MarketTimeline: parallel arrays of BBO states (ts non-decreasing; a
// crossed state is skipped and counted by append_state_pinned, a locked one
// is legal), HALT start timestamps. prevailing(t) = the index of the latest
// state with ts <= t (bisect_right - 1), mid(i) = 0.5 * (bid + ask).
// reference_mid(ts, liquidity): m_f = the mid prevailing at ts (TAKER) or
// ts - 1 (MAKER); undefined when no state prevails or a halt started inside
// (prevailing ts, ref ts]. markout_mid(fill_ts, h): m_h = the mid prevailing
// at fill_ts + h; undefined before the first state, past the last state, or
// when a halt started inside (min(fill_ts, prevailing ts), fill_ts + h].
// fill_measures: markout = s (m_h - p), effective = s (p - m_f), realised =
// s (p - m_h), price_impact = s (m_h - m_f), s = +1 buy / -1 sell.

#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace iap {

constexpr const char* kTaker = "TAKER";
constexpr const char* kMaker = "MAKER";

class MarketTimeline {
public:
    std::vector<std::int64_t> ts;
    std::vector<double> bid;
    std::vector<double> ask;
    std::vector<std::int64_t> bid_sz;
    std::vector<std::int64_t> ask_sz;
    std::vector<std::int64_t> halts;
    std::int64_t crossed_states_skipped = 0;

    // Throws std::invalid_argument on a decreasing ts or a crossed state.
    void append(std::int64_t t, double b, double a, std::int64_t bsz, std::int64_t asz);
    // Builder rule (pinned 2.1): a crossed state is skipped (false).
    bool append_state_pinned(std::int64_t t, double b, double a, std::int64_t bsz,
                             std::int64_t asz);
    void add_halt(std::int64_t t) { halts.push_back(t); }
    std::optional<std::size_t> prevailing(std::int64_t t) const;
    double mid(std::size_t i) const { return 0.5 * (bid[i] + ask[i]); }
    std::size_t size() const { return ts.size(); }
};

struct MarkoutMeasures {
    double markout;
    double effective_half_spread;
    double realised_half_spread;
    double price_impact;
};

std::optional<double> reference_mid(const MarketTimeline& tl, std::int64_t ts, bool taker);
// Throws std::invalid_argument on horizon_ns < 0.
std::optional<double> markout_mid(const MarketTimeline& tl, std::int64_t fill_ts,
                                  std::int64_t horizon_ns);
std::optional<MarkoutMeasures> fill_measures(const MarketTimeline& tl, std::int64_t fill_ts,
                                             double price, std::uint8_t side, bool taker,
                                             std::int64_t horizon_ns);

// A named markout horizon list in report order (Python dict order).
using Horizons = std::vector<std::pair<std::string, std::int64_t>>;
const Horizons& default_horizons_ns();

}  // namespace iap
