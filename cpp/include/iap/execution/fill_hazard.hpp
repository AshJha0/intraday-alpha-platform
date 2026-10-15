// Fill-hazard model, decision side (v1.12 X4; v1.17 F7 part 1 C++ port of
// the prediction path of iap.execution.fill_hazard.FillHazardModel:
// features, bucket_hazards, p_fill_within, and time_of_day on the UTC
// clock; the fit (Newton / IRLS over pandas frames) is research-plane and
// stays in Python, the port LOADS a fitted document).
//
// Grouped-time hazard: per time bucket b (edges_ns), with the standardised
// features z(x) = (design(x) - mean) / std and design = (log1p(max(queue,
// 0)), log1p(max(depletion, 0)), spread_ticks, imbalance, sin(2 pi tod),
// cos(2 pi tod)):
//   eta_b = alpha_b + beta . z(x), clipped to [-30, 30];
//   cloglog: h_b = -expm1(-exp(eta_b));  logit: h_b = 1 / (1 + exp(-eta_b));
//   h_b clipped to [1e-12, 1 - 1e-12];
//   P(fill within t) = -expm1( sum_b log1p(-h_b) * overlap_b / width_b ),
//   overlap_b = clip(t - edge_b, 0, width_b), the last bucket's overlap
//   extended by max(t - last edge, 0) (its hazard continues).
// tod (X-8 clock "utc", the pinned v1.12 rule) = ((ts - capture_offset_s *
// 1e9) mod DAY_NS) / DAY_NS with Python's floored modulo. The IANA-zone
// clocks of X-8 are NOT ported here (no tz database in the C++ core): a
// document with another clock is refused by from_value.
//
// Python stays the reference (tests/golden/expected_f7_queue_cancel.json):
// the same libm calls (log1p, expm1, exp, sin, cos) in the same order; the
// beta . z dot product is the plain left-to-right sum.

#pragma once

#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <vector>

#include "iap/contracts/canonical_json.hpp"

namespace iap {

constexpr std::int64_t kHazardDayNs = 86'400'000'000'000;

// Fraction of the UTC day of an epoch-ns timestamp (the "utc" clock).
double time_of_day_utc(std::int64_t ts_ns, std::int64_t capture_offset_s = 0);

// One decision-time feature row (iap.execution.fill_hazard.hazard_features).
struct HazardFeatures {
    double queue_ahead = 0.0;
    double depletion_per_s = 0.0;
    double spread_ticks = 0.0;
    double imbalance = 0.0;
    double tod = 0.0;
};

class FillHazardModel {
public:
    // From a `iap.fill_hazard/1` document (FillHazardModel.to_dict); throws
    // std::invalid_argument on another schema, columns, shapes or a clock
    // other than "utc".
    static FillHazardModel from_value(const contracts::Value& doc);

    HazardFeatures features(double queue_ahead, double spread_ticks, double imbalance = 0.0,
                            std::optional<double> depletion_per_s = std::nullopt,
                            std::int64_t ts_ns = 0) const;
    // h_b(x) per bucket.
    std::vector<double> bucket_hazards(const HazardFeatures& x) const;
    // P(fill within t_ns | x).
    double p_fill_within(const HazardFeatures& x, std::int64_t t_ns) const;

    int n_buckets() const { return static_cast<int>(edges_ns_.size()) - 1; }
    const std::vector<std::int64_t>& edges_ns() const { return edges_ns_; }
    const std::string& link() const { return link_; }

private:
    std::vector<std::int64_t> edges_ns_;
    std::string link_ = "cloglog";
    std::vector<double> alpha_;  // per bucket
    std::vector<double> beta_;   // per column (6)
    std::vector<double> mean_;
    std::vector<double> std_;
    std::int64_t capture_offset_s_ = 0;
};

}  // namespace iap
