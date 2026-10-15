// Almgren-Chriss: line-for-line port of iap.execution.optimal (header).

// GCC (12/13, -O2 and up) reports maybe-uninitialized false positives on
// std::optional / std::variant temporaries; no sanitizer build sees an
// uninitialised read. Silenced for this translation unit.
#if defined(__GNUC__) && !defined(__clang__)
#pragma GCC diagnostic ignored "-Wmaybe-uninitialized"
#endif

#include "iap/execution/optimal.hpp"

#include <cmath>
#include <stdexcept>

#include "iap/execution/signal.hpp"

namespace iap {

void ACParams::validate() const {
    if (sigma < 0.0 || eta <= 0.0 || gamma < 0.0 || horizon <= 0.0) {
        throw std::invalid_argument("need sigma >= 0, eta > 0, gamma >= 0, horizon > 0");
    }
    if (risk_aversion < 0.0) throw std::invalid_argument("risk_aversion must be >= 0");
}

ACParams ac_params_from_calibration(const ACCalibrationInputs& in) {
    std::optional<double> adv = in.adv;
    std::optional<double> horizon = in.horizon;
    if (in.adv_per_day.has_value() || in.horizon_s.has_value()) {
        if (adv.has_value() || horizon.has_value()) {
            throw std::invalid_argument(
                "ambiguous units: pass either (adv, horizon) in one time unit or "
                "(adv_per_day, horizon_s), not a mix");
        }
        if (!in.adv_per_day.has_value() || !in.horizon_s.has_value()) {
            throw std::invalid_argument("adv_per_day and horizon_s must be given together");
        }
        if (*in.horizon_s <= 0.0 || in.session_s <= 0.0) {
            throw std::invalid_argument("horizon_s and session_s must be > 0");
        }
        adv = *in.adv_per_day;
        horizon = *in.horizon_s / in.session_s;
    }
    if (!adv.has_value()) {
        throw std::invalid_argument("adv (or adv_per_day with horizon_s) is required");
    }
    if (!horizon.has_value()) horizon = 1.0;
    if (*adv <= 0.0 || in.price <= 0.0) {
        throw std::invalid_argument("adv and price must be > 0");
    }
    const double eta = in.coeff * 1e-2 * in.price / *adv;
    ACParams p;
    p.sigma = in.sigma;
    p.eta = eta;
    p.gamma = in.permanent_fraction * eta;
    p.eps = in.eps;
    p.horizon = *horizon;
    p.risk_aversion = in.risk_aversion;
    p.validate();
    return p;
}

namespace {

double eta_tilde(const ACParams& p, double tau) {
    const double et = p.eta - p.gamma * tau / 2.0;
    if (et <= 0.0) {
        throw std::invalid_argument("eta - gamma * tau / 2 must be > 0 (use more slices)");
    }
    return et;
}

}  // namespace

double ac_kappa(const ACParams& p, int n) {
    if (n <= 0) throw std::invalid_argument("n must be > 0");
    const double tau = p.horizon / static_cast<double>(n);
    const double k2 = p.risk_aversion * std::pow(p.sigma, 2.0) / eta_tilde(p, tau);
    if (k2 == 0.0) return 0.0;
    return std::acosh(1.0 + k2 * tau * tau / 2.0) / tau;
}

std::vector<double> ac_holdings(double qty, const ACParams& p, int n, double drift) {
    const double kappa = ac_kappa(p, n);
    const double t_end = p.horizon;
    const double tau = t_end / static_cast<double>(n);
    std::vector<double> out;
    out.reserve(static_cast<std::size_t>(n) + 1);
    const double risk = p.risk_aversion * std::pow(p.sigma, 2.0);
    for (int j = 0; j <= n; ++j) {
        const double t = static_cast<double>(j) * tau;
        double x;
        if (kappa * t_end < 1e-12) {
            x = qty * (t_end - t) / t_end;
            if (drift != 0.0) {
                x += drift * t * (t_end - t) / (4.0 * eta_tilde(p, tau));
            }
        } else {
            const double s_t = std::sinh(kappa * t_end);
            x = qty * std::sinh(kappa * (t_end - t)) / s_t;
            if (drift != 0.0 && risk > 0.0) {
                const double xb = drift / (2.0 * risk);
                x += xb * (1.0 - (std::sinh(kappa * (t_end - t)) + std::sinh(kappa * t)) / s_t);
            }
        }
        out.push_back(x);
    }
    out.back() = 0.0;
    return out;
}

std::vector<double> ac_trajectory(double qty, const ACParams& p, int n, double drift) {
    const std::vector<double> x = ac_holdings(qty, p, n, drift);
    std::vector<double> out;
    out.reserve(static_cast<std::size_t>(n));
    for (int j = 1; j <= n; ++j) {
        out.push_back(x[static_cast<std::size_t>(j) - 1] - x[static_cast<std::size_t>(j)]);
    }
    return out;
}

double ac_drift_from_signal(std::uint8_t side, double expected_return, double price,
                            double remaining_horizon) {
    if (remaining_horizon <= 0.0 || price <= 0.0) {
        throw std::invalid_argument("remaining_horizon and price must be > 0");
    }
    return alignment(side, expected_return) * price / remaining_horizon;
}

std::pair<double, double> ac_cost(double qty, const ACParams& p, int n,
                                  const std::vector<double>* trades) {
    const std::vector<double> tr = trades == nullptr ? ac_trajectory(qty, p, n) : *trades;
    const double tau = p.horizon / static_cast<double>(tr.size());
    const double et = eta_tilde(p, tau);
    std::vector<double> x{qty};
    for (const double v : tr) x.push_back(x.back() - v);
    double sum_abs = 0.0;  // Python sum(): 0 + |n_1| + ...
    double sum_sq = 0.0;
    for (const double v : tr) {
        sum_abs += std::fabs(v);
        sum_sq += v * v;
    }
    const double e = 0.5 * p.gamma * qty * qty + p.eps * sum_abs + et / tau * sum_sq;
    double sum_h = 0.0;
    for (std::size_t j = 1; j < x.size(); ++j) sum_h += x[j] * x[j];
    const double var = std::pow(p.sigma, 2.0) * tau * sum_h;
    return {e, var};
}

}  // namespace iap
