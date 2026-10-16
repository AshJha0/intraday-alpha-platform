// Dark / midpoint venue model (v1.17 WS-B item B10; F7 part 2 C++ port of
// iap.execution.dark: VENUE_TYPES, sigmoid, dark_features, DarkFillModel,
// DarkControls / dark_pretrade_check, DarkFirstParams, midpoint_ticks; the
// dark-first replay rule lives in replay/exec_replay.hpp).
//
// A venue typed dark / midpoint has no displayed book in the replayed
// stream, so nothing about a fill there can be replayed: every dark fill
// is MODELLED, NOT MEASURED, and every document the model reads carries
// that label (from_value refuses one without it). No free dark-pool data
// exists; the Python proxy is the Nasdaq non-displayed print inside the
// displayed spread, and the FIT (hidden_print_samples / fit_logistic /
// calibrate_dark_model) stays Python: the port reads the fitted document
// (research/execution/dark/dark_fill_model_multi7.json).
//
// Fill model: the probability that a midpoint order of qty shares, posted
// at displayed spread spread_ticks with queue_ahead of our own earlier
// dark shares open, fills within horizon_ns once lit_volume displayed
// shares have traded:
//   p = sigmoid(b0 + b1 * log1p(lit_volume / 100) + b2 * spread_ticks
//               + b3 * log1p((qty + queue_ahead) / 100))
// (plain scalar arithmetic, the Python order: the dot product is the
// left-to-right sum of coef[i] * x[i]; sigmoid is the branch-safe form).
//
// Stateless pre-trade check (dark_pretrade_check): venue_type (the venue's
// type must be in allowed_venue_types), then participation (with
// max_participation > 0: qty <= max_participation * trailing displayed
// EXECUTE volume, float compare, so a zero volume rejects), then notional
// (with max_dark_notional > 0: dark_notional_so_far + child_notional <=
// max). First failure wins. Per plan row F8 these three controls are
// RUST-OWNED (rust/risk, tests/golden/expected_dark_controls.json): this
// function is the replay-path mirror the Python replay evaluates before a
// dark post, ported here only so the C++ replay rule reproduces the
// Python replay byte for byte; it is not a second owner.
//
// midpoint_ticks: the displayed midpoint in ticks, a half tick rounded
// AGAINST us (buy up, sell down).
//
// Python stays the reference (tests/golden/expected_f7_dark_venue.json).

#pragma once

#include <array>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

#include "iap/contracts/canonical_json.hpp"
#include "iap/execution/execution.hpp"
#include "iap/orderbook/book.hpp"

namespace iap {

constexpr const char* kModelledLabel = "modelled, not measured";
constexpr std::uint64_t kDarkIdBase = std::uint64_t{1} << 40;

// dark_pretrade_check results ("" = accepted), in evaluation order.
enum class DarkControlCode : std::uint8_t { NONE = 0, VENUE_TYPE = 1, PARTICIPATION = 2, NOTIONAL = 3 };
const char* dark_control_code_name(DarkControlCode c);

// ChildOrder::dark_outcome values.
enum class DarkOutcome : std::uint8_t { NONE = 0, FILLED = 1, EXPIRED = 2 };
const char* dark_outcome_name(DarkOutcome o);

double sigmoid(double x);

// The model's regressors, intercept first. Throws std::invalid_argument on
// lit_volume / spread / queue < 0 or qty <= 0.
std::array<double, 4> dark_features(std::int64_t lit_volume, std::int64_t spread_ticks,
                                    std::int64_t qty, std::int64_t queue_ahead = 0);

struct DarkFillModel {
    std::array<double, 4> coef{};
    std::int64_t horizon_ns = 0;
    bool fitted = false;
    std::string source;

    // From a calibration document (DarkFillModel.to_dict); throws
    // std::invalid_argument without the modelled label.
    static DarkFillModel from_value(const contracts::Value& doc);
    // The documented prior (not fitted).
    static DarkFillModel prior(std::int64_t horizon_ns = 30'000'000'000);
    double p_fill(std::int64_t lit_volume, std::int64_t spread_ticks, std::int64_t qty,
                  std::int64_t queue_ahead = 0) const;
    void validate() const;  // throws std::invalid_argument
};

struct DarkControls {
    double max_participation = 0.0;                   // 0 = off
    std::int64_t participation_window_ns = 30'000'000'000;
    std::vector<VenueType> allowed_venue_types{VenueType::DARK, VenueType::MIDPOINT};
    double max_dark_notional = 0.0;                   // 0 = off
    void validate() const;  // throws std::invalid_argument
};

// Throws std::invalid_argument on qty <= 0 (header).
DarkControlCode dark_pretrade_check(const DarkControls& controls, VenueType venue_type,
                                    std::int64_t qty, std::int64_t trailing_lit_volume,
                                    double dark_notional_so_far, double child_notional);

// ParentOrder::dark_first (the replay rule, exec_replay.hpp).
struct DarkFirstParams {
    DarkFillModel model;
    std::uint16_t venue_id = 0;  // 0 = the first non-lit venue of the config
    DarkControls controls;
    double max_urgency = 0.5;    // a plain parent is low-urgency when urgency <= this
    std::vector<UrgencyRegime> regimes{UrgencyRegime::AGREE};  // B2 parents (not wired in C++)
    std::int64_t horizon_ns = 0; // 0 = the model's horizon
    std::int64_t rest_ns() const { return horizon_ns > 0 ? horizon_ns : model.horizon_ns; }
    void validate() const;  // throws std::invalid_argument
};

std::int64_t midpoint_ticks(std::int64_t bid_ticks, std::int64_t ask_ticks, std::uint8_t side);

// The consolidated best PRICE on `side` over the non-stale venue books: the
// value ConsolidatedBook::best_bid / best_ask report, in O(venues) instead
// of their O(levels) merge (prices only; the dark rule and the cost-aware
// router need no sizes).
std::optional<std::int64_t> consolidated_touch_price(const ConsolidatedBook& book, Side side);

}  // namespace iap
