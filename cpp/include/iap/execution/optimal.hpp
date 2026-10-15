// Almgren-Chriss optimal execution (v1.10 X1, v1.13 X-P2-15, v1.16 P0-5,
// v1.17 B3 drift; v1.17 F7 part 1 C++ port of iap.execution.optimal).
//
// Closed-form implementation-shortfall trajectory of Almgren & Chriss
// (2000) for working X shares in N equal intervals of length tau = T / N:
//   eta_tilde = eta - gamma * tau / 2,
//   kappa solves 2 (cosh(kappa tau) - 1) / tau^2 = lam sigma^2 / eta_tilde,
//   holdings x_j = X sinh(kappa (T - t_j)) / sinh(kappa T), trades
//   n_j = x_{j-1} - x_j, E = gamma X^2 / 2 + eps sum|n_j| + (eta_tilde /
//   tau) sum n_j^2, V = sigma^2 tau sum x_j^2 (j = 1..N).
// The eta path (ac_params_from_calibration): the simulator's linear impact
// coefficient (bps per percent of ADV, read as a rate per time unit) gives
// eta = coeff * 1e-2 * price / adv, gamma = permanent_fraction * eta; the
// explicit (adv_per_day, horizon_s) form makes the time unit one trading
// day of session_s seconds. The coefficient itself is resolved by the
// caller (Python: the calibration's slope, the residual linear fit, or the
// config default; a dictionary read, not arithmetic).
// B3 drift (ac_holdings drift, favours-waiting sign): the Euler-Lagrange
// solution of 2 eta_tilde x'' = 2 lam sigma^2 x - drift, x(0) = X, x(T) =
// 0, is x(t) = (X - xb) sinh(k (T - t)) / sinh(k T) - xb sinh(k t) /
// sinh(k T) + xb with xb = drift / (2 lam sigma^2); for k T < 1e-12 the
// limit X (T - t) / T + drift t (T - t) / (4 eta_tilde). ac_drift_from_signal
// = alignment(side, expected_return) * price / remaining_horizon.
//
// Python stays the reference; this port reproduces
// tests/golden/expected_f7_almgren_chriss.json byte for byte: the same
// libm calls (pow(sigma, 2.0) for sigma**2, acosh, sinh, exp) in the same
// evaluation order. Not the pinned AlgoType::IS schedule (algos.hpp).

#pragma once

#include <cstdint>
#include <optional>
#include <utility>
#include <vector>

namespace iap {

struct ACParams {
    double sigma = 0.0;          // volatility, price per sqrt(time unit)
    double eta = 0.0;            // temporary impact, price per (share / time unit)
    double gamma = 0.0;          // permanent impact, price per share
    double eps = 0.0;            // fixed cost per share (half spread + fees)
    double horizon = 1.0;        // T, in time units
    double risk_aversion = 1e-6; // lambda

    // Throws std::invalid_argument like the Python __post_init__.
    void validate() const;
};

// Inputs of ac_params_from_calibration (nullopt = the Python None).
struct ACCalibrationInputs {
    double coeff = 2.0;  // bps per % of ADV, already resolved (header)
    double sigma = 0.0;
    std::optional<double> adv;
    double price = 0.0;
    std::optional<double> horizon;
    double risk_aversion = 1e-6;
    double permanent_fraction = 0.0;
    double eps = 0.0;
    std::optional<double> adv_per_day;
    std::optional<double> horizon_s;
    double session_s = 23'400.0;
};

// Throws std::invalid_argument with the Python messages.
ACParams ac_params_from_calibration(const ACCalibrationInputs& in);

// The discrete urgency kappa for n intervals (throws on n <= 0, or when
// eta - gamma * tau / 2 <= 0).
double ac_kappa(const ACParams& p, int n);

// Holdings x_0 = qty, ..., x_n = 0 of the optimal trajectory.
std::vector<double> ac_holdings(double qty, const ACParams& p, int n,
                                double drift = 0.0);

// Trade list n_1..n_N (sums to qty).
std::vector<double> ac_trajectory(double qty, const ACParams& p, int n,
                                  double drift = 0.0);

// The AC drift of a signal expecting `expected_return` over the parent's
// remaining window (B3). Throws on remaining_horizon <= 0 or price <= 0.
double ac_drift_from_signal(std::uint8_t side, double expected_return,
                            double price, double remaining_horizon);

// (E, V): expected shortfall and its variance of `trades` (nullptr: the
// optimal trajectory over n intervals).
std::pair<double, double> ac_cost(double qty, const ACParams& p, int n,
                                  const std::vector<double>* trades = nullptr);

}  // namespace iap
