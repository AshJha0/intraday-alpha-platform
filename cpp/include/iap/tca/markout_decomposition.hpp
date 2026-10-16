// Markout decomposition by fill type and regime, day-clustered (v1.17 WS-B
// B6; F7 part 1 C++ port of iap.tca.markout_decomposition: fill_type,
// regime_of, fill_rows, cluster_mean, decompose, markout_decomposition).
//
// Fill type, from the fill and the child that produced it (the v1.17
// provenance fields of ChildOrder): TAKER -> "aggressive"; MAKER with a
// pull cancel sent before the fill (cancel_rule set, fill.ts >=
// cancel_rule_ts) -> "faded"; MAKER of a replacement child
// (replaces_order_id != 0) -> "cancelled_replaced"; else "passive".
// Regime: the child's B2 urgency_regime, "plain" when none.
//
// Per cell and horizon: the equal-weight mean of the fills' markout
// (1e4 * s * (m_h - p) / p, bps of the fill price) and price impact
// (1e4 * s * (m_h - m_f) / p), each with the day-clustered CR1 standard
// error: one cluster per label (the fill's day, ts // day_ns, or a caller
// label), se = sqrt(G / (G - 1) * sum_g (sum_{i in g} (x_i - mean))^2 /
// N^2), t = mean / se, df = G - 1, Student-t readings (p_student_t, ci95)
// from util/student_t.hpp (SciPy in Python: compared to a tolerance, every
// other field byte for byte). With fewer than two clusters se / t / df /
// p / ci95 are null; with se == 0 the ci95 collapses to [mean, mean].
//
// Output trees are contracts::Value in the Python key layout
// (tests/golden/expected_f7_markout_decomposition.json). Report-plane.

#pragma once

#include <cstdint>
#include <map>
#include <string>
#include <vector>

#include "iap/contracts/canonical_json.hpp"
#include "iap/execution/execution.hpp"
#include "iap/tca/markout.hpp"

namespace iap {

constexpr std::int64_t kDayNs = 86'400'000'000'000;
const std::vector<std::string>& fill_types();  // passive, aggressive, faded, cancelled_replaced, dark

std::string fill_type(const Fill& fill, const ChildOrder& order);
std::string regime_of(const ChildOrder& order);

// One row per fill (fill_rows); `cluster` nullptr = the fill's day.
contracts::Value fill_rows(const std::vector<Fill>& fills,
                           const std::map<std::uint64_t, ChildOrder>& orders,
                           const MarketTimeline& tl, double tick_size, double qty_unit = 1.0,
                           const Horizons* horizons_ns = nullptr,
                           const std::string* cluster = nullptr, std::int64_t day_ns = kDayNs,
                           const std::string& algo = "");

// Mean with the CR1 day-clustered standard error (header). Throws
// std::invalid_argument when the lengths differ.
contracts::Value cluster_mean(const std::vector<double>& values,
                              const std::vector<std::string>& clusters);

// Aggregate fill_rows output; `horizons` nullptr = the first row's horizons
// IN THE ORDER GIVEN TO fill_rows (callers pass the list: a canonical tree
// holds keys sorted).
contracts::Value decompose(const contracts::Value& rows,
                           const std::vector<std::string>* horizons = nullptr);

contracts::Value markout_decomposition(const std::vector<Fill>& fills,
                                       const std::map<std::uint64_t, ChildOrder>& orders,
                                       const MarketTimeline& tl, double tick_size,
                                       double qty_unit = 1.0,
                                       const Horizons* horizons_ns = nullptr,
                                       std::int64_t day_ns = kDayNs);

}  // namespace iap
