// B4 / B5 rules and the B4 counterfactual: port of iap.execution.fading.

// GCC (12/13, -O2 and up) reports maybe-uninitialized false positives on
// std::variant temporaries moved into contracts::Value trees; no sanitizer
// build sees an uninitialised read. Report-plane code, silenced here.
#if defined(__GNUC__) && !defined(__clang__)
#pragma GCC diagnostic ignored "-Wmaybe-uninitialized"
#pragma GCC diagnostic ignored "-Warray-bounds"  // GCC 12: initializer-list Array{...} false positive
#endif

#include "iap/execution/fading.hpp"

#include <algorithm>
#include <stdexcept>

#include "iap/execution/signal.hpp"

namespace iap {

using contracts::Array;
using contracts::Object;
using contracts::Value;

void FadeParams::validate() const {
    if (threshold_z < 0.0) throw std::invalid_argument("FadeParams.threshold_z must be >= 0 (0 = off)");
    if (pressure_threshold != 0.0 && !(pressure_threshold > 0.5 && pressure_threshold <= 1.0)) {
        throw std::invalid_argument("FadeParams.pressure_threshold must be in (0.5, 1] or 0 (off)");
    }
    if (threshold_z == 0.0 && pressure_threshold == 0.0) {
        throw std::invalid_argument("FadeParams needs a signal threshold or a pressure threshold");
    }
    if (max_fades < 1) throw std::invalid_argument("FadeParams.max_fades must be >= 1");
    if (max_age_ns < 0 || latency_ns < 0) {
        throw std::invalid_argument("FadeParams.max_age_ns / latency_ns must be >= 0");
    }
}

void QueueCancelParams::validate() const {
    if (max_ahead_qty < 0) throw std::invalid_argument("QueueCancelParams.max_ahead_qty must be >= 0 (0 = off)");
    if (!(min_p_fill >= 0.0 && min_p_fill <= 1.0)) {
        throw std::invalid_argument("QueueCancelParams.min_p_fill must be in [0, 1] (0 = off)");
    }
    if (max_ahead_qty == 0 && min_p_fill == 0.0) {
        throw std::invalid_argument("QueueCancelParams needs max_ahead_qty or min_p_fill");
    }
    if (min_p_fill > 0.0 && !has_hazard) {
        throw std::invalid_argument("QueueCancelParams.min_p_fill needs a fitted hazard model");
    }
    if (horizon_ns <= 0) throw std::invalid_argument("QueueCancelParams.horizon_ns must be > 0");
    if (min_rest_ns < 0) throw std::invalid_argument("QueueCancelParams.min_rest_ns must be >= 0");
}

double book_pressure(std::int64_t own_qty, std::int64_t opp_qty) {
    const std::int64_t total = own_qty + opp_qty;
    if (total <= 0) return 0.5;
    return static_cast<double>(opp_qty) / static_cast<double>(total);
}

CancelRule fade_rule(std::uint8_t side, std::optional<double> z, double threshold_z,
                     std::optional<double> pressure, double pressure_threshold) {
    if (z.has_value() && threshold_z > 0.0 &&
        regime_from_z(side, *z, threshold_z) == UrgencyRegime::ADVERSE) {
        return CancelRule::FADE_SIGNAL;
    }
    if (pressure.has_value() && pressure_threshold > 0.0 && *pressure >= pressure_threshold) {
        return CancelRule::FADE_PRESSURE;
    }
    return CancelRule::NONE;
}

CancelRule queue_cancel_rule(std::int64_t ahead_qty, std::int64_t max_ahead_qty,
                             std::optional<double> p_fill, double min_p_fill) {
    if (max_ahead_qty > 0 && ahead_qty > max_ahead_qty) return CancelRule::QUEUE_AHEAD;
    if (p_fill.has_value() && min_p_fill > 0.0 && *p_fill < min_p_fill) {
        return CancelRule::QUEUE_P_FILL;
    }
    return CancelRule::NONE;
}

Value PullRecord::to_value() const {
    Object o;
    o["parent_id"] = Value(static_cast<unsigned long long>(parent_id));
    o["order_id"] = Value(static_cast<unsigned long long>(order_id));
    o["side"] = Value(static_cast<int>(side));
    o["price_ticks"] = Value(static_cast<long long>(price_ticks));
    o["qty_at_decision"] = Value(static_cast<long long>(qty_at_decision));
    o["decision_ts"] = Value(static_cast<long long>(decision_ts));
    o["rule"] = Value(rule);
    o["ahead_qty"] = Value(static_cast<long long>(ahead_qty));
    o["signal_z"] = Value(signal_z);
    o["pressure"] = Value(pressure);
    o["p_fill"] = Value(p_fill);
    o["effect_ts"] = Value(static_cast<long long>(effect_ts));
    o["pulled_qty"] = Value(static_cast<long long>(pulled_qty));
    o["filled_in_flight"] = Value(static_cast<long long>(filled_in_flight));
    o["outcome"] = Value(outcome);
    return Value(std::move(o));
}

PullRecord PullRecord::from_value(const Value& v) {
    PullRecord r;
    r.parent_id = v.at("parent_id").as_uint64();
    r.order_id = v.at("order_id").as_uint64();
    r.side = static_cast<std::uint8_t>(v.at("side").as_int64());
    r.price_ticks = v.at("price_ticks").as_int64();
    r.qty_at_decision = v.at("qty_at_decision").as_int64();
    r.decision_ts = v.at("decision_ts").as_int64();
    r.rule = v.at("rule").as_string();
    r.ahead_qty = v.at("ahead_qty").as_int64();
    r.signal_z = v.at("signal_z").as_number();
    r.pressure = v.at("pressure").as_number();
    r.p_fill = v.at("p_fill").as_number();
    r.effect_ts = v.at("effect_ts").as_int64();
    r.pulled_qty = v.at("pulled_qty").as_int64();
    r.filled_in_flight = v.at("filled_in_flight").as_int64();
    r.outcome = v.at("outcome").as_string();
    return r;
}

bool would_have_filled(const MarketTimeline& tl, std::uint8_t side, double price,
                       std::int64_t start_ts, std::int64_t end_ts) {
    const auto i = std::lower_bound(tl.ts.begin(), tl.ts.end(), start_ts) - tl.ts.begin();
    const auto j = std::upper_bound(tl.ts.begin(), tl.ts.end(), end_ts) - tl.ts.begin();
    for (auto k = i; k < j; ++k) {
        const auto idx = static_cast<std::size_t>(k);
        if (side == 0) {
            if (tl.ask[idx] <= price) return true;
        } else if (tl.bid[idx] >= price) {
            return true;
        }
    }
    return false;
}

const std::vector<std::string>& fade_rules() {
    static const std::vector<std::string> r = {"fade:signal", "fade:pressure"};
    return r;
}

namespace {

bool wanted(const std::vector<std::string>& rules, const std::string& rule) {
    return std::find(rules.begin(), rules.end(), rule) != rules.end();
}

}  // namespace

Value pull_rows(const std::vector<PullRecord>& pulls, const MarketTimeline& tl,
                double tick_size, const Horizons* horizons_ns, double qty_unit,
                const std::vector<std::string>* rules, const std::string& cluster) {
    const Horizons& hz = horizons_ns == nullptr ? default_horizons_ns() : *horizons_ns;
    const std::vector<std::string>& want = rules == nullptr ? fade_rules() : *rules;
    Array rows;
    for (const PullRecord& r : pulls) {
        if (!wanted(want, r.rule) || r.outcome.empty() || r.pulled_qty <= 0) continue;
        const double price = static_cast<double>(r.price_ticks) * tick_size;
        Object cf;
        for (const auto& [name, h] : hz) {
            const auto m_h = markout_mid(tl, r.effect_ts, h);
            if (!m_h.has_value() || price <= 0.0) {
                cf[name] = Value(nullptr);
                continue;
            }
            const double s = r.side == 0 ? 1.0 : -1.0;
            Object c;
            c["would_fill"] = Value(would_have_filled(tl, r.side, price, r.effect_ts, r.effect_ts + h));
            c["markout_bps"] = Value(1e4 * s * (*m_h - price) / price);
            cf[name] = Value(std::move(c));
        }
        Object row;
        row["rule"] = Value(r.rule);
        row["side"] = Value(static_cast<int>(r.side));
        row["price"] = Value(price);
        row["qty"] = Value(static_cast<long long>(r.pulled_qty));
        row["qty_unit"] = Value(qty_unit);
        row["cluster"] = Value(cluster);
        row["counterfactual"] = Value(std::move(cf));
        rows.push_back(Value(std::move(row)));
    }
    return Value(std::move(rows));
}

Value pull_summary(const Value& rows_v, const std::vector<std::string>* horizons,
                   std::int64_t lost_race, const std::vector<std::string>* rules) {
    const Array& rows = rows_v.as_array();
    std::vector<std::string> hz;
    if (horizons == nullptr) {
        if (!rows.empty()) {
            // Python: list(first) over the dict in insertion order. The
            // canonical tree holds the keys sorted; the insertion order was
            // the horizon list the rows were priced with, which the caller
            // passes here when it matters. Sorted keys reproduce the Python
            // output only when the horizon names sort like their insertion
            // order, so callers of this port pass `horizons` explicitly.
            for (const auto& [k, v] : rows.front().at("counterfactual").as_object()) {
                (void)v;
                hz.push_back(k);
            }
        }
    } else {
        hz = *horizons;
    }
    const std::vector<std::string>& want = rules == nullptr ? fade_rules() : *rules;
    std::int64_t total_qty = 0;
    for (const Value& r : rows) total_qty += r.at("qty").as_int64();
    Object out;
    out["label"] = Value("counterfactual: touch-through proxy on the replayed book, not a measurement");
    Array rl;
    for (const auto& r : want) rl.push_back(Value(r));
    out["rules"] = Value(std::move(rl));
    out["n_pulls"] = Value(static_cast<long long>(rows.size()));
    out["pulled_qty"] = Value(static_cast<long long>(total_qty));
    out["lost_race"] = Value(static_cast<long long>(lost_race));
    Object horizons_out;
    for (const std::string& name : hz) {
        std::int64_t n = 0, qty = 0, wf_n = 0, wf_qty = 0;
        double wf_notional = 0.0;
        std::vector<double> saved_bps, missed_bps;
        double saved_ccy = 0.0, missed_ccy = 0.0;
        std::int64_t saved_qty = 0, missed_qty = 0;
        for (const Value& r : rows) {
            const Value& cf = r.at("counterfactual").at(name);
            if (cf.is_null()) continue;
            const std::int64_t q = r.at("qty").as_int64();
            const double price = r.at("price").as_number();
            const double unit = r.at("qty_unit").as_number();
            n += 1;
            qty += q;
            if (!cf.at("would_fill").as_bool()) continue;
            const double notional = price * static_cast<double>(q) * unit;
            wf_n += 1;
            wf_qty += q;
            wf_notional += notional;
            const double bps = cf.at("markout_bps").as_number();
            const double ccy = bps * 1e-4 * notional;
            if (bps < 0.0) {
                saved_bps.push_back(-bps);
                saved_ccy += -ccy;
                saved_qty += q;
            } else {
                missed_bps.push_back(bps);
                missed_ccy += ccy;
                missed_qty += q;
            }
        }
        const double net = saved_ccy - missed_ccy;
        auto mean_of = [](const std::vector<double>& v) -> Value {
            if (v.empty()) return Value(nullptr);
            double s = 0.0;
            for (const double x : v) s += x;
            return Value(s / static_cast<double>(v.size()));
        };
        Object saved;
        saved["n"] = Value(static_cast<long long>(saved_bps.size()));
        saved["qty"] = Value(static_cast<long long>(saved_qty));
        saved["bps_mean"] = mean_of(saved_bps);
        saved["ccy"] = Value(saved_ccy);
        Object missed;
        missed["n"] = Value(static_cast<long long>(missed_bps.size()));
        missed["qty"] = Value(static_cast<long long>(missed_qty));
        missed["bps_mean"] = mean_of(missed_bps);
        missed["ccy"] = Value(missed_ccy);
        Object h;
        h["n"] = Value(static_cast<long long>(n));
        h["qty"] = Value(static_cast<long long>(qty));
        h["would_fill"] = Value(static_cast<long long>(wf_n));
        h["would_fill_qty"] = Value(static_cast<long long>(wf_qty));
        h["saved"] = Value(std::move(saved));
        h["missed"] = Value(std::move(missed));
        h["net_ccy"] = Value(net);
        h["net_bps"] = wf_notional > 0.0 ? Value(1e4 * net / wf_notional) : Value(nullptr);
        horizons_out[name] = Value(std::move(h));
    }
    out["horizons"] = Value(std::move(horizons_out));
    return Value(std::move(out));
}

Value pull_outcomes(const std::vector<PullRecord>& pulls, const MarketTimeline& tl,
                    double tick_size, const Horizons* horizons_ns, double qty_unit,
                    const std::vector<std::string>* rules) {
    const Horizons& hz = horizons_ns == nullptr ? default_horizons_ns() : *horizons_ns;
    const std::vector<std::string>& want = rules == nullptr ? fade_rules() : *rules;
    const Value rows = pull_rows(pulls, tl, tick_size, &hz, qty_unit, &want);
    std::int64_t lost = 0;
    for (const PullRecord& r : pulls) {
        if (wanted(want, r.rule) && r.outcome == "lost_race") ++lost;
    }
    std::vector<std::string> names;
    for (const auto& [name, h] : hz) {
        (void)h;
        names.push_back(name);
    }
    return pull_summary(rows, &names, lost, &want);
}

}  // namespace iap
