// Quote fading and queue-aware cancels (v1.17 WS-B B4 / B5; F7 part 1 C++
// port of iap.execution.fading: FadeParams, QueueCancelParams,
// book_pressure, fade_rule, queue_cancel_rule, PullRecord, and the B4
// counterfactual would_have_filled / pull_rows / pull_summary /
// pull_outcomes).
//
// Rules (plain arithmetic on scalars, the shape of the Python):
//   book_pressure(own, opp) = opp / (own + opp), 0.5 when both are empty;
//   fade_rule: the signal leg first — z given, threshold_z > 0 and
//     regime_from_z(side, z, threshold_z) == ADVERSE => FADE_SIGNAL; then
//     the pressure leg — pressure given, pressure_threshold > 0 and
//     pressure >= pressure_threshold => FADE_PRESSURE; else NONE;
//   queue_cancel_rule: max_ahead_qty > 0 and ahead_qty > max_ahead_qty =>
//     QUEUE_AHEAD; then p_fill given, min_p_fill > 0 and p_fill < min_p_fill
//     => QUEUE_P_FILL; else NONE.
// The replay wiring (which child is tested when, the cancel through rule 7,
// hold / repost / cross of the remainder, the pull accounting) lives in
// Python's ExecutionReplay; its C++ port is F7 part 2.
//
// Counterfactual ("fades saved vs fills missed", labelled as such):
// would_have_filled = the opposite best reached the pulled price in any
// state with start_ts <= ts <= end_ts (touch-through proxy); pull_rows
// prices every resolved pull with pulled_qty > 0 under the wanted rules at
// each horizon h: counterfactual[h] = null (undefined markout) or
// {would_fill, markout_bps = 1e4 * s * (m_h - price) / price} with m_h the
// markout mid at effect_ts + h; pull_summary splits the would-fill pulls
// by the sign of the markout (saved: < 0, |bps| equal-weighted and ccy =
// sum |bps| * price * qty * qty_unit / 1e4; missed: >= 0), net_ccy and
// net_bps over the would-fill notional. The dictionaries are produced as
// contracts::Value trees in the Python key layout, so the golden
// (tests/golden/expected_f7_fading.json) compares bytes.

#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

#include "iap/contracts/canonical_json.hpp"
#include "iap/execution/execution.hpp"
#include "iap/tca/markout.hpp"

namespace iap {

enum class FadeAction : std::uint8_t { HOLD = 0, REPOST = 1, CROSS = 2 };
enum class QueueAction : std::uint8_t { CROSS = 0, REPOST = 1 };

struct FadeParams {
    double threshold_z = 1.0;        // signal leg, signal units; 0 = off
    double pressure_threshold = 0.0; // book-pressure leg, (0.5, 1]; 0 = off
    FadeAction action = FadeAction::HOLD;
    int max_fades = 3;
    std::int64_t max_age_ns = 10'000'000'000;
    std::int64_t latency_ns = 0;
    void validate() const;  // throws std::invalid_argument
};

struct QueueCancelParams {
    std::int64_t max_ahead_qty = 0;  // 0 = off
    double min_p_fill = 0.0;         // 0 = off; needs a hazard model
    std::int64_t horizon_ns = 1'000'000'000;
    QueueAction action = QueueAction::CROSS;
    bool has_hazard = false;         // a FillHazardModel is attached
    std::int64_t min_rest_ns = 0;
    void validate() const;  // throws std::invalid_argument
};

double book_pressure(std::int64_t own_qty, std::int64_t opp_qty);
CancelRule fade_rule(std::uint8_t side, std::optional<double> z, double threshold_z,
                     std::optional<double> pressure, double pressure_threshold);
CancelRule queue_cancel_rule(std::int64_t ahead_qty, std::int64_t max_ahead_qty,
                             std::optional<double> p_fill, double min_p_fill);

// One B4 / B5 pull of a resting child (PullRecord.to_dict field names).
struct PullRecord {
    std::uint64_t parent_id = 0;
    std::uint64_t order_id = 0;
    std::uint8_t side = 0;
    std::int64_t price_ticks = 0;
    std::int64_t qty_at_decision = 0;
    std::int64_t decision_ts = 0;
    std::string rule;  // one of the CANCEL_RULES strings
    std::int64_t ahead_qty = 0;
    double signal_z = 0.0;
    double pressure = 0.0;
    double p_fill = -1.0;
    std::int64_t effect_ts = 0;
    std::int64_t pulled_qty = 0;
    std::int64_t filled_in_flight = 0;
    std::string outcome;  // "" while in flight, else pulled / partial / lost_race

    contracts::Value to_value() const;
    static PullRecord from_value(const contracts::Value& v);
};

bool would_have_filled(const MarketTimeline& tl, std::uint8_t side, double price,
                       std::int64_t start_ts, std::int64_t end_ts);

// The two fade rules (the default `rules`).
const std::vector<std::string>& fade_rules();

contracts::Value pull_rows(const std::vector<PullRecord>& pulls, const MarketTimeline& tl,
                           double tick_size, const Horizons* horizons_ns = nullptr,
                           double qty_unit = 1.0,
                           const std::vector<std::string>* rules = nullptr,
                           const std::string& cluster = "");
// `horizons` nullptr: the horizons of the first row (none when no rows).
contracts::Value pull_summary(const contracts::Value& rows,
                              const std::vector<std::string>* horizons = nullptr,
                              std::int64_t lost_race = 0,
                              const std::vector<std::string>* rules = nullptr);
contracts::Value pull_outcomes(const std::vector<PullRecord>& pulls, const MarketTimeline& tl,
                               double tick_size, const Horizons* horizons_ns = nullptr,
                               double qty_unit = 1.0,
                               const std::vector<std::string>* rules = nullptr);

}  // namespace iap
