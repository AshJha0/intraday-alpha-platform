// Signal-aware pacing (v1.17 WS-B B3; F7 part 1 C++ port of
// iap.execution.pacing: PacingParams, pacing_scale, apportion,
// tilted_slice_qty, and the POV pacing accumulation of
// ExecutionReplay._schedule_signal).
//
//   scale = clip(1 - gain * alignment(side, z), min_scale, max_scale),
//   1 without a reading (z nullopt or non-finite);
//   POV: p_t = min(participation * scale, max_participation),
//        rate = p_t / (1 - p_t) under pov_exact, else p_t,
//        target_f += rate * dV per TRADE, target = floor(target_f);
//   tilt (TWAP / VWAP / IS): at every slice the REMAINING quantity is
//        re-apportioned over the remaining weights with the current one
//        scaled (largest remainder, never negative, the last slice takes
//        the rest): tilted_slice_qty(remaining, weights[k:], scale).
//
// Python stays the reference (tests/golden/expected_f7_pacing.json).

#pragma once

#include <cstdint>
#include <optional>
#include <vector>

namespace iap {

struct PacingParams {
    double gain = 0.5;
    double min_scale = 0.25;
    double max_scale = 3.0;
    double max_participation = 0.5;
    std::int64_t max_age_ns = 10'000'000'000;
    std::int64_t latency_ns = 0;
    void validate() const;  // throws std::invalid_argument
};

// clip(1 - gain * alignment, min, max); 1 without a reading.
double pacing_scale(std::uint8_t side, std::optional<double> z, const PacingParams& params);

// Largest-remainder apportionment of qty over weights (the pinned rule of
// slice_quantities; weights summing to <= 0 put everything on the last).
std::vector<std::int64_t> apportion(std::int64_t qty, const std::vector<double>& weights);

// Quantity of the CURRENT slice (header).
std::int64_t tilted_slice_qty(std::int64_t remaining_qty,
                              const std::vector<double>& remaining_weights, double scale);

// One POV pacing step (header); returns the new target_f.
struct PovPacingStep {
    double scale = 1.0;
    double p_eff = 0.0;
    double rate = 0.0;
    double target_f = 0.0;
    std::int64_t target = 0;
};
PovPacingStep pov_pacing_step(std::uint8_t side, std::optional<double> z,
                              const PacingParams& params, double participation,
                              bool pov_exact, std::int64_t dv, double target_f);

}  // namespace iap
