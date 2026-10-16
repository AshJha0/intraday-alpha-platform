// Student-t survival function and its inverse (v1.17 F7 part 1).
//
// The Python reference of the B6 cell statistic reads its two Student-t
// columns (p_student_t, the 95 % critical value behind ci95) from SciPy
// (iap.validation.metrics.day_cluster_inference -> scipy.stats.t.sf / isf,
// i.e. Cephes stdtr / stdtri). Those are report-only readings (no verdict
// rule consumes them); this header gives the C++ port the same functions
// from the regularised incomplete beta function (continued fraction,
// Numerical Recipes betacf) and a bisection for the inverse, to the
// tolerance tests/golden/expected_f7_markout_decomposition.json states
// (1e-9), not byte for byte: the C++ B6 port is byte-exact on every field
// it computes itself (mean, CR1 SE, t, df) and tolerance-exact on these.

#pragma once

#include <cmath>
#include <limits>

namespace iap {

// Regularised incomplete beta I_x(a, b).
inline double regularised_incomplete_beta(double a, double b, double x) {
    if (x <= 0.0) return 0.0;
    if (x >= 1.0) return 1.0;
    auto betacf = [](double a_, double b_, double x_) {
        const int max_it = 500;
        const double eps = 1e-16;
        const double fpmin = 1e-300;
        const double qab = a_ + b_;
        const double qap = a_ + 1.0;
        const double qam = a_ - 1.0;
        double c = 1.0;
        double d = 1.0 - qab * x_ / qap;
        if (std::fabs(d) < fpmin) d = fpmin;
        d = 1.0 / d;
        double h = d;
        for (int m = 1; m <= max_it; ++m) {
            const int m2 = 2 * m;
            double aa = m * (b_ - m) * x_ / ((qam + m2) * (a_ + m2));
            d = 1.0 + aa * d;
            if (std::fabs(d) < fpmin) d = fpmin;
            c = 1.0 + aa / c;
            if (std::fabs(c) < fpmin) c = fpmin;
            d = 1.0 / d;
            h *= d * c;
            aa = -(a_ + m) * (qab + m) * x_ / ((a_ + m2) * (qap + m2));
            d = 1.0 + aa * d;
            if (std::fabs(d) < fpmin) d = fpmin;
            c = 1.0 + aa / c;
            if (std::fabs(c) < fpmin) c = fpmin;
            d = 1.0 / d;
            const double del = d * c;
            h *= del;
            if (std::fabs(del - 1.0) < eps) break;
        }
        return h;
    };
    const double lbeta = std::lgamma(a + b) - std::lgamma(a) - std::lgamma(b);
    const double front = std::exp(lbeta + a * std::log(x) + b * std::log(1.0 - x));
    if (x < (a + 1.0) / (a + b + 2.0)) return front * betacf(a, b, x) / a;
    return 1.0 - front * betacf(b, a, 1.0 - x) / b;
}

// P(T > t) for Student-t with df degrees of freedom (one-sided).
inline double student_t_sf(double t, double df) {
    if (!std::isfinite(t)) return t > 0 ? 0.0 : 1.0;
    const double x = df / (df + t * t);
    const double tail = 0.5 * regularised_incomplete_beta(df / 2.0, 0.5, x);
    return t >= 0.0 ? tail : 1.0 - tail;
}

// The t with P(T > t) = q (q in (0, 1)): bisection to full double precision.
inline double student_t_isf(double q, double df) {
    if (q <= 0.0) return std::numeric_limits<double>::infinity();
    if (q >= 1.0) return -std::numeric_limits<double>::infinity();
    double lo = -1e3;
    double hi = 1e3;
    while (student_t_sf(hi, df) > q) hi *= 2.0;
    while (student_t_sf(lo, df) < q) lo *= 2.0;
    for (int i = 0; i < 400; ++i) {
        const double mid = 0.5 * (lo + hi);
        if (mid == lo || mid == hi) break;
        if (student_t_sf(mid, df) > q) {
            lo = mid;
        } else {
            hi = mid;
        }
    }
    return 0.5 * (lo + hi);
}

}  // namespace iap
