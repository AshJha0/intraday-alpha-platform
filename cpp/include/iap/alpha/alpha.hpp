// Production alpha scoring — the 6 golden flagship alphas (API_ALPHA.md).
//
// Model linear_z_v1 (all 6): with params {mu, sigma, beta, z_clip,
// conf_scale} loaded from configs/strategies/alpha_params.json (ports NEVER
// fit — Python research owns fitting; every number is treated as opaque f64):
//
//   z    = clip((raw - mu) / (sigma + EPS), -z_clip, +z_clip)
//   er   = beta * z
//   conf = min(1, |z| / conf_scale)
//   invalid raw (NaN)  =>  er = 0.0, conf = 0.0 exactly
//
// Raw signals (API_ALPHA.md section 4; inputs are registry features produced
// by the native feature engine — a NaN/invalid input makes raw NaN):
//
//   EQ01  micro_mid_dev_bps_v1                                  (h = 1s)
//   EQ03  0.5*ofi_norm_l1_w1s + 0.3*ofi_norm_l5_w1s + 0.2*ofi_norm_l5_w5s
//   EQ06  ret_vol_adj_10s_v1                                    (h = 10s)
//   FX01  micro_mid_dev_bps_v1                                  (h = 500ms)
//   FX09  -ret_vol_adj_10s_v1 * vol_regime_ratio_v1             (h = 1m)
//   FX05  -residual of the currency-factor solve (section 5)    (h = 5m)
//
// FX05 currency-exposure machinery (pinned): currencies sorted {AUD, CAD,
// CHF, EUR, GBP, JPY, NZD, USD}, numeraire USD dropped from the solve;
// exposure matrix rows = pairs 101..108 (+1 base, -1 quote); factor returns
// f = pinv(A_free[valid]) @ r[valid] (minimum-norm least squares — matches
// numpy.linalg.pinv); residual_i = r_i - (A_free f)_i; raw_i = -residual_i.
// Fewer than 2 valid pairs => no signal. No RNG anywhere on the scoring path.

#pragma once

#include <array>
#include <cstdint>
#include <map>
#include <string>
#include <vector>

#include "iap/features/feature_engine.hpp"

namespace iap {

constexpr double ALPHA_EPS = 1e-12;

// AlphaSignal contract (schemas/alpha_signal.schema.json).
struct AlphaSignal {
    std::string alpha_id;
    std::uint32_t instrument_id = 0;
    std::int64_t timestamp = 0;
    double expected_return = 0.0;  // finite always; 0.0 when confidence == 0
    double confidence = 0.0;       // in [0, 1]
    std::string horizon;
};

// Fitted linear_z_v1 parameters for one alpha (opaque, from JSON).
struct LinearZParams {
    std::string alpha_id;
    std::string horizon;
    double mu = 0.0;
    double sigma = 0.0;
    double beta = 0.0;
    double z_clip = 4.0;
    double conf_scale = 2.0;
    std::vector<std::string> features;
};

// The 6 production alpha ids, pinned order.
extern const std::array<const char*, 6> GOLDEN_ALPHA_IDS;

// Load configs/strategies/alpha_params.json (throws std::runtime_error on
// missing file / malformed JSON, std::invalid_argument on missing alphas).
std::map<std::string, LinearZParams> load_alpha_params(const std::string& path);

// Core scoring: raw -> (expected_return, confidence). NaN raw => (0, 0).
void score_linear_z(double raw, const LinearZParams& p, double& er,
                    double& conf);

// Raw signal of a single-frame alpha (EQ01/EQ03/EQ06/FX01/FX09) from a
// native feature vector; NaN when any input feature is invalid.
// Throws std::invalid_argument for FX05 (cross-pair; use fx05_raw_signals).
double raw_signal(const std::string& alpha_id, const FeatureVector& vec);

// Score one instrument row of a single-frame alpha.
AlphaSignal score_row(const LinearZParams& p, const FeatureVector& vec);

// ---------------------------------------------------------------- FX05 ----

// Pinned FX pair universe (instrument_id order 101..108).
extern const std::array<std::uint32_t, 8> FX_PAIR_IDS;
constexpr std::size_t FX_NUM_PAIRS = 8;
constexpr std::size_t FX_NUM_FREE_CCY = 7;  // AUD CAD CHF EUR GBP JPY NZD (USD = 0)
constexpr std::int64_t FX05_GRID_STEP_NS = 30'000'000'000;
constexpr std::int64_t FX05_MAX_AGE_NS = 120'000'000'000;

// Exposure matrix restricted to non-numeraire currencies:
// A[i][j] = exposure of pair FX_PAIR_IDS[i] to free currency j.
std::array<std::array<double, FX_NUM_FREE_CCY>, FX_NUM_PAIRS>
fx_free_exposure_matrix();

// Minimum-norm least-squares currency factor solve for one cross-section.
// `returns[i]` is pair FX_PAIR_IDS[i]'s ret_log_1m sample (NaN = missing).
// Outputs the fitted A@f per pair; returns false when < 1 valid pair
// (fitted then all NaN). Matches numpy.linalg.pinv semantics.
bool fx_solve_factors(const std::array<double, FX_NUM_PAIRS>& returns,
                      std::array<double, FX_NUM_FREE_CCY>& factors,
                      std::array<double, FX_NUM_PAIRS>& fitted);

// FX05 raw signals for one grid cross-section: raw_i = -(r_i - fitted_i)
// for pairs with finite r_i, NaN otherwise; all-NaN when < 2 valid pairs.
std::array<double, FX_NUM_PAIRS> fx05_raw_signals(
    const std::array<double, FX_NUM_PAIRS>& returns);

}  // namespace iap
