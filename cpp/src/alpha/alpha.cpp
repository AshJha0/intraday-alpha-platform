// Alpha scoring implementation (see alpha.hpp; python/src/iap/alpha is the
// reference). The FX05 factor solve uses Eigen's SVD to reproduce
// numpy.linalg.pinv (minimum-norm least squares) deterministically.

#include "iap/alpha/alpha.hpp"

#include <Eigen/Dense>
#include <Eigen/SVD>

#include <cmath>
#include <limits>
#include <stdexcept>

#include "iap/util/json.hpp"

namespace iap {

namespace {
constexpr double kNaN = std::numeric_limits<double>::quiet_NaN();

double feature_or_nan(const FeatureVector& vec, int slot) {
    return vec.valid[static_cast<std::size_t>(slot)]
               ? vec.values[static_cast<std::size_t>(slot)]
               : kNaN;
}
}  // namespace

const std::array<const char*, 6> GOLDEN_ALPHA_IDS = {
    "EQ01", "EQ03", "EQ06", "FX01", "FX05", "FX09"};

const std::array<std::uint32_t, 8> FX_PAIR_IDS = {101, 102, 103, 104,
                                                  105, 106, 107, 108};

std::map<std::string, LinearZParams> load_alpha_params(
    const std::string& path) {
    const Json root = read_json_file(path);
    const Json& params = root["params"];
    std::map<std::string, LinearZParams> out;
    for (const char* aid : GOLDEN_ALPHA_IDS) {
        if (!params.has(aid)) {
            throw std::invalid_argument(std::string("alpha_params.json lacks ") +
                                        aid);
        }
        const Json& p = params[aid];
        if (p["model"].s() != "linear_z_v1") {
            throw std::invalid_argument(std::string(aid) +
                                        ": unsupported model " + p["model"].s());
        }
        LinearZParams lp;
        lp.alpha_id = p["alpha_id"].s();
        lp.horizon = p["horizon"].s();
        lp.mu = p["mu"].num();
        lp.sigma = p["sigma"].num();
        lp.beta = p["beta"].num();
        lp.z_clip = p["z_clip"].num();
        lp.conf_scale = p["conf_scale"].num();
        for (const auto& f : p["features"].a()) lp.features.push_back(f.s());
        out[aid] = lp;
    }
    return out;
}

void score_linear_z(double raw, const LinearZParams& p, double& er,
                    double& conf) {
    if (!std::isfinite(raw)) {
        er = 0.0;
        conf = 0.0;
        return;
    }
    double z = (raw - p.mu) / (p.sigma + ALPHA_EPS);
    if (z > p.z_clip) z = p.z_clip;
    if (z < -p.z_clip) z = -p.z_clip;
    er = p.beta * z;
    conf = std::min(1.0, std::fabs(z) / p.conf_scale);
}

double raw_signal(const std::string& alpha_id, const FeatureVector& vec) {
    if (alpha_id == "EQ01" || alpha_id == "FX01") {
        return feature_or_nan(vec, F_MICRO_MID_DEV_BPS);
    }
    if (alpha_id == "EQ03") {
        // pinned weights 0.5 / 0.3 / 0.2 (API_ALPHA.md section 4)
        return 0.5 * feature_or_nan(vec, F_OFI_NORM_L1_W1S) +
               0.3 * feature_or_nan(vec, F_OFI_NORM_L5_W1S) +
               0.2 * feature_or_nan(vec, F_OFI_NORM_L5_W5S);
    }
    if (alpha_id == "EQ06") {
        return feature_or_nan(vec, F_RET_VOL_ADJ_10S);
    }
    if (alpha_id == "FX09") {
        return -feature_or_nan(vec, F_RET_VOL_ADJ_10S) *
               feature_or_nan(vec, F_VOL_REGIME_RATIO);
    }
    throw std::invalid_argument("raw_signal: not a single-frame alpha: " +
                                alpha_id);
}

AlphaSignal score_row(const LinearZParams& p, const FeatureVector& vec) {
    AlphaSignal sig;
    sig.alpha_id = p.alpha_id;
    sig.instrument_id = vec.instrument_id;
    sig.timestamp = vec.timestamp;
    sig.horizon = p.horizon;
    score_linear_z(raw_signal(p.alpha_id, vec), p, sig.expected_return,
                   sig.confidence);
    return sig;
}

std::array<std::array<double, FX_NUM_FREE_CCY>, FX_NUM_PAIRS>
fx_free_exposure_matrix() {
    // Currencies sorted: AUD CAD CHF EUR GBP JPY NZD (USD dropped).
    // Pair -> (base, quote): 101 EUR/USD, 102 GBP/USD, 103 USD/JPY,
    // 104 AUD/USD, 105 USD/CAD, 106 USD/CHF, 107 NZD/USD, 108 EUR/GBP.
    enum { AUD, CAD, CHF, EUR, GBP, JPY, NZD };
    std::array<std::array<double, FX_NUM_FREE_CCY>, FX_NUM_PAIRS> a{};
    a[0][EUR] = 1.0;                    // EUR/USD
    a[1][GBP] = 1.0;                    // GBP/USD
    a[2][JPY] = -1.0;                   // USD/JPY
    a[3][AUD] = 1.0;                    // AUD/USD
    a[4][CAD] = -1.0;                   // USD/CAD
    a[5][CHF] = -1.0;                   // USD/CHF
    a[6][NZD] = 1.0;                    // NZD/USD
    a[7][EUR] = 1.0;                    // EUR/GBP
    a[7][GBP] = -1.0;
    return a;
}

bool fx_solve_factors(const std::array<double, FX_NUM_PAIRS>& returns,
                      std::array<double, FX_NUM_FREE_CCY>& factors,
                      std::array<double, FX_NUM_PAIRS>& fitted) {
    const auto a_all = fx_free_exposure_matrix();
    Eigen::Index nvalid = 0;
    for (double r : returns) {
        if (std::isfinite(r)) ++nvalid;
    }
    if (nvalid == 0) {
        factors.fill(kNaN);
        fitted.fill(kNaN);
        return false;
    }
    Eigen::MatrixXd a(nvalid, static_cast<Eigen::Index>(FX_NUM_FREE_CCY));
    Eigen::VectorXd r(nvalid);
    Eigen::Index row = 0;
    for (std::size_t i = 0; i < FX_NUM_PAIRS; ++i) {
        if (!std::isfinite(returns[i])) continue;
        for (std::size_t j = 0; j < FX_NUM_FREE_CCY; ++j) {
            a(row, static_cast<Eigen::Index>(j)) = a_all[i][j];
        }
        r(row) = returns[i];
        ++row;
    }
    // pinv(a) @ r — minimum-norm least squares via SVD with numpy's default
    // cutoff (rcond = 1e-15 relative to the largest singular value).
    Eigen::JacobiSVD<Eigen::MatrixXd> svd(
        a, Eigen::ComputeThinU | Eigen::ComputeThinV);
    const auto& sv = svd.singularValues();
    const double cutoff =
        sv.size() > 0 ? 1e-15 * static_cast<double>(
                                    std::max(a.rows(), a.cols())) *
                            sv(0)
                      : 0.0;
    Eigen::VectorXd inv_sv(sv.size());
    for (Eigen::Index i = 0; i < sv.size(); ++i) {
        inv_sv(i) = sv(i) > cutoff ? 1.0 / sv(i) : 0.0;
    }
    const Eigen::VectorXd f =
        svd.matrixV() * inv_sv.asDiagonal() * svd.matrixU().transpose() * r;
    for (std::size_t j = 0; j < FX_NUM_FREE_CCY; ++j) {
        factors[j] = f(static_cast<Eigen::Index>(j));
    }
    for (std::size_t i = 0; i < FX_NUM_PAIRS; ++i) {
        double v = 0.0;
        for (std::size_t j = 0; j < FX_NUM_FREE_CCY; ++j) {
            v += a_all[i][j] * f(static_cast<Eigen::Index>(j));
        }
        fitted[i] = v;
    }
    return true;
}

std::array<double, FX_NUM_PAIRS> fx05_raw_signals(
    const std::array<double, FX_NUM_PAIRS>& returns) {
    std::array<double, FX_NUM_PAIRS> raw;
    raw.fill(kNaN);
    int nvalid = 0;
    for (double r : returns) {
        if (std::isfinite(r)) ++nvalid;
    }
    if (nvalid < 2) return raw;  // no cross-pair information
    std::array<double, FX_NUM_FREE_CCY> factors;
    std::array<double, FX_NUM_PAIRS> fitted;
    fx_solve_factors(returns, factors, fitted);
    for (std::size_t i = 0; i < FX_NUM_PAIRS; ++i) {
        if (std::isfinite(returns[i])) {
            raw[i] = -(returns[i] - fitted[i]);
        }
    }
    return raw;
}

}  // namespace iap
