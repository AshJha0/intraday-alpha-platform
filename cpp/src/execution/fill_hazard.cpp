// Fill hazard, decision side: port of iap.execution.fill_hazard (header).

// GCC (12/13, -O2 and up) reports maybe-uninitialized false positives on
// std::optional / std::variant temporaries; no sanitizer build sees an
// uninitialised read. Silenced for this translation unit.
#if defined(__GNUC__) && !defined(__clang__)
#pragma GCC diagnostic ignored "-Wmaybe-uninitialized"
#pragma GCC diagnostic ignored "-Warray-bounds"  // GCC 12: initializer-list Array{...} false positive
#endif

#include "iap/execution/fill_hazard.hpp"

#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace iap {

namespace {

constexpr const char* kSchema = "iap.fill_hazard/1";
constexpr double kPi = 3.141592653589793;  // math.pi
const std::vector<std::string> kColumns = {"log_queue_ahead", "log_depletion", "spread_ticks",
                                           "imbalance",       "tod_sin",       "tod_cos"};

std::vector<double> doubles(const contracts::Value& a) {
    std::vector<double> out;
    for (const auto& v : a.as_array()) out.push_back(v.as_number());
    return out;
}

}  // namespace

double time_of_day_utc(std::int64_t ts_ns, std::int64_t capture_offset_s) {
    const std::int64_t ts = ts_ns - capture_offset_s * 1'000'000'000;
    std::int64_t r = ts % kHazardDayNs;  // Python: floored modulo (never negative)
    if (r < 0) r += kHazardDayNs;
    return static_cast<double>(r) / static_cast<double>(kHazardDayNs);
}

FillHazardModel FillHazardModel::from_value(const contracts::Value& doc) {
    if (!doc.has("schema") || !doc.at("schema").is_string() ||
        doc.at("schema").as_string() != kSchema) {
        throw std::invalid_argument("not a iap.fill_hazard/1 document");
    }
    std::vector<std::string> cols;
    if (doc.has("columns")) {
        for (const auto& c : doc.at("columns").as_array()) cols.push_back(c.as_string());
    }
    if (cols != kColumns) {
        throw std::invalid_argument("fill hazard document has different feature columns");
    }
    FillHazardModel m;
    for (const auto& e : doc.at("edges_ns").as_array()) m.edges_ns_.push_back(e.as_int64());
    if (m.edges_ns_.size() < 2 || m.edges_ns_[0] != 0) {
        throw std::invalid_argument("edges_ns must start at 0 and increase strictly (>= 2 edges)");
    }
    for (std::size_t i = 1; i < m.edges_ns_.size(); ++i) {
        if (m.edges_ns_[i] <= m.edges_ns_[i - 1]) {
            throw std::invalid_argument("edges_ns must start at 0 and increase strictly (>= 2 edges)");
        }
    }
    m.link_ = doc.at("link").as_string();
    if (m.link_ != "cloglog" && m.link_ != "logit") {
        throw std::invalid_argument("link must be one of ('cloglog', 'logit')");
    }
    const std::string clock = doc.has("clock") ? doc.at("clock").as_string() : "utc";
    if (clock != "utc") {
        throw std::invalid_argument("C++ fill hazard: only the 'utc' clock is ported (X-8 zones are Python-only)");
    }
    m.capture_offset_s_ = doc.has("capture_offset_s") ? doc.at("capture_offset_s").as_int64() : 0;
    m.alpha_ = doubles(doc.at("alpha"));
    m.beta_ = doubles(doc.at("beta"));
    m.mean_ = doubles(doc.at("mean"));
    m.std_ = doubles(doc.at("std"));
    if (m.alpha_.size() != static_cast<std::size_t>(m.n_buckets()) ||
        m.beta_.size() != kColumns.size() || m.mean_.size() != kColumns.size() ||
        m.std_.size() != kColumns.size()) {
        throw std::invalid_argument("fill hazard document has inconsistent shapes");
    }
    return m;
}

HazardFeatures FillHazardModel::features(double queue_ahead, double spread_ticks,
                                         double imbalance,
                                         std::optional<double> depletion_per_s,
                                         std::int64_t ts_ns) const {
    HazardFeatures x;
    x.queue_ahead = queue_ahead;
    // Python: float(depletion_per_s or 0.0) — None AND 0.0 both give 0.0.
    x.depletion_per_s = depletion_per_s.has_value() ? *depletion_per_s : 0.0;
    x.spread_ticks = spread_ticks;
    x.imbalance = imbalance;
    x.tod = time_of_day_utc(ts_ns, capture_offset_s_);
    return x;
}

std::vector<double> FillHazardModel::bucket_hazards(const HazardFeatures& x) const {
    const double q = std::max(x.queue_ahead, 0.0);
    const double d = std::max(x.depletion_per_s, 0.0);
    const double design[6] = {std::log1p(q),
                              std::log1p(d),
                              x.spread_ticks,
                              x.imbalance,
                              std::sin(2 * kPi * x.tod),
                              std::cos(2 * kPi * x.tod)};
    double dot = 0.0;  // z @ beta: plain left-to-right accumulation
    for (std::size_t i = 0; i < 6; ++i) {
        const double z = (design[i] - mean_[i]) / std_[i];
        dot += z * beta_[i];
    }
    std::vector<double> mu;
    mu.reserve(alpha_.size());
    const double eps = 1e-12;
    for (const double a : alpha_) {
        double eta = a + dot;
        eta = std::min(std::max(eta, -30.0), 30.0);  // np.clip(eta, -30, 30)
        double m;
        if (link_ == "cloglog") {
            const double ee = std::exp(eta);
            m = -std::expm1(-ee);
        } else {
            m = 1.0 / (1.0 + std::exp(-eta));
        }
        mu.push_back(std::min(std::max(m, eps), 1 - eps));
    }
    return mu;
}

double FillHazardModel::p_fill_within(const HazardFeatures& x, std::int64_t t_ns) const {
    const std::vector<double> h = bucket_hazards(x);
    const double t = static_cast<double>(t_ns);
    const std::size_t nb = h.size();
    double log_s = 0.0;  // np.sum over <= 7 buckets: sequential
    const double extra = std::max(t - static_cast<double>(edges_ns_.back()), 0.0);
    for (std::size_t b = 0; b < nb; ++b) {
        const double lo = static_cast<double>(edges_ns_[b]);
        const double width = static_cast<double>(edges_ns_[b + 1]) - lo;
        double overlap = std::min(std::max(t - lo, 0.0), width);
        if (b + 1 == nb) overlap += extra;  // the last bucket's hazard continues
        log_s += std::log1p(-h[b]) * (overlap / width);
    }
    return -std::expm1(log_s);
}

}  // namespace iap
