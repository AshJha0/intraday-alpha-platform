// B10 dark / midpoint venue model: line-for-line port of iap.execution.dark.

#if defined(__GNUC__) && !defined(__clang__)
#pragma GCC diagnostic ignored "-Wmaybe-uninitialized"
#pragma GCC diagnostic ignored "-Warray-bounds"
#endif

#include "iap/execution/dark.hpp"

#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace iap {

const char* dark_control_code_name(DarkControlCode c) {
    switch (c) {
        case DarkControlCode::NONE: return "";
        case DarkControlCode::VENUE_TYPE: return "venue_type";
        case DarkControlCode::PARTICIPATION: return "participation";
        case DarkControlCode::NOTIONAL: return "notional";
    }
    return "";
}

const char* dark_outcome_name(DarkOutcome o) {
    switch (o) {
        case DarkOutcome::NONE: return "";
        case DarkOutcome::FILLED: return "filled";
        case DarkOutcome::EXPIRED: return "expired";
    }
    return "";
}

double sigmoid(double x) {
    if (x >= 0.0) return 1.0 / (1.0 + std::exp(-x));
    const double e = std::exp(x);
    return e / (1.0 + e);
}

std::array<double, 4> dark_features(std::int64_t lit_volume, std::int64_t spread_ticks,
                                    std::int64_t qty, std::int64_t queue_ahead) {
    if (lit_volume < 0 || spread_ticks < 0 || qty <= 0 || queue_ahead < 0) {
        throw std::invalid_argument("dark_features: lit_volume / spread / queue >= 0 and qty > 0");
    }
    // Python: lit_volume / 100.0 (int / float), (qty + queue_ahead) / 100.0
    return {1.0, std::log1p(static_cast<double>(lit_volume) / 100.0),
            static_cast<double>(spread_ticks),
            std::log1p(static_cast<double>(qty + queue_ahead) / 100.0)};
}

void DarkFillModel::validate() const {
    if (horizon_ns <= 0) throw std::invalid_argument("DarkFillModel.horizon_ns must be > 0");
}

DarkFillModel DarkFillModel::from_value(const contracts::Value& doc) {
    if (!doc.has("label") || !doc.at("label").is_string() ||
        doc.at("label").as_string() != kModelledLabel) {
        throw std::invalid_argument(
            "dark fill model document must carry label 'modelled, not measured'");
    }
    DarkFillModel m;
    const auto& c = doc.at("coef").as_array();
    if (c.size() != 4) throw std::invalid_argument("DarkFillModel.coef needs 4 coefficients");
    for (std::size_t i = 0; i < 4; ++i) m.coef[i] = c[i].as_number();
    m.horizon_ns = doc.at("horizon_ns").as_int64();
    m.fitted = doc.has("fitted") && doc.at("fitted").is_bool() && doc.at("fitted").as_bool();
    if (doc.has("source") && doc.at("source").is_string()) m.source = doc.at("source").as_string();
    m.validate();
    return m;
}

DarkFillModel DarkFillModel::prior(std::int64_t horizon_ns) {
    DarkFillModel m;
    m.coef = {-1.42, 0.8, -0.15, -0.5};
    m.horizon_ns = horizon_ns;
    m.fitted = false;
    m.source = "prior (not fitted)";
    return m;
}

double DarkFillModel::p_fill(std::int64_t lit_volume, std::int64_t spread_ticks,
                             std::int64_t qty, std::int64_t queue_ahead) const {
    const std::array<double, 4> x = dark_features(lit_volume, spread_ticks, qty, queue_ahead);
    double z = 0.0;
    for (std::size_t i = 0; i < 4; ++i) z += coef[i] * x[i];
    return sigmoid(z);
}

void DarkControls::validate() const {
    if (max_participation < 0.0 || max_dark_notional < 0.0) {
        throw std::invalid_argument("DarkControls: max_participation / max_dark_notional must be >= 0");
    }
    if (participation_window_ns <= 0) {
        throw std::invalid_argument("DarkControls.participation_window_ns must be > 0");
    }
}

DarkControlCode dark_pretrade_check(const DarkControls& controls, VenueType venue_type,
                                    std::int64_t qty, std::int64_t trailing_lit_volume,
                                    double dark_notional_so_far, double child_notional) {
    if (qty <= 0) throw std::invalid_argument("dark_pretrade_check: qty must be > 0");
    if (std::find(controls.allowed_venue_types.begin(), controls.allowed_venue_types.end(),
                  venue_type) == controls.allowed_venue_types.end()) {
        return DarkControlCode::VENUE_TYPE;
    }
    if (controls.max_participation > 0.0) {
        if (static_cast<double>(qty) >
            controls.max_participation * static_cast<double>(trailing_lit_volume)) {
            return DarkControlCode::PARTICIPATION;
        }
    }
    if (controls.max_dark_notional > 0.0) {
        if (dark_notional_so_far + child_notional > controls.max_dark_notional) {
            return DarkControlCode::NOTIONAL;
        }
    }
    return DarkControlCode::NONE;
}

void DarkFirstParams::validate() const {
    if (!(max_urgency >= 0.0 && max_urgency <= 1.0)) {
        throw std::invalid_argument("DarkFirstParams.max_urgency must be in [0, 1]");
    }
    if (horizon_ns < 0) throw std::invalid_argument("DarkFirstParams.horizon_ns / venue_id must be >= 0");
    model.validate();
    controls.validate();
}

std::int64_t midpoint_ticks(std::int64_t bid_ticks, std::int64_t ask_ticks, std::uint8_t side) {
    const std::int64_t s = bid_ticks + ask_ticks;
    // Python // on a non-negative sum; prices are positive ticks
    if (s % 2 == 0) return s / 2;
    return side == 0 ? s / 2 + 1 : s / 2;
}

std::optional<std::int64_t> consolidated_touch_price(const ConsolidatedBook& book, Side side) {
    std::optional<std::int64_t> best;
    for (const auto& [vid, vb] : book.books()) {
        (void)vid;
        if (vb.stale()) continue;
        const std::optional<LevelEntry> q = side == Side::BID ? vb.best_bid() : vb.best_ask();
        if (!q.has_value()) continue;
        if (!best.has_value() || (side == Side::BID ? q->first > *best : q->first < *best)) {
            best = q->first;
        }
    }
    return best;
}

}  // namespace iap
