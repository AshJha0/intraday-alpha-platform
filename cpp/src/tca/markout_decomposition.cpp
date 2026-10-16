// B6 markout decomposition: port of iap.tca.markout_decomposition (header).

// GCC (12/13, -O2 and up) reports maybe-uninitialized false positives on
// std::variant temporaries moved into contracts::Value trees; no sanitizer
// build sees an uninitialised read. Report-plane code, silenced here.
#if defined(__GNUC__) && !defined(__clang__)
#pragma GCC diagnostic ignored "-Wmaybe-uninitialized"
#pragma GCC diagnostic ignored "-Warray-bounds"  // GCC 12: initializer-list Array{...} false positive
#endif

#include "iap/tca/markout_decomposition.hpp"

#include <cmath>
#include <stdexcept>

#include "iap/util/student_t.hpp"

namespace iap {

using contracts::Array;
using contracts::Object;
using contracts::Value;

namespace {

const char* const kMeasures[2] = {"markout", "price_impact"};

Value i64(std::int64_t v) { return Value(static_cast<long long>(v)); }

std::string day_label(std::int64_t ts, std::int64_t day_ns) {
    // Python: str(ts // day_ns) — floor division.
    std::int64_t q = ts / day_ns;
    if ((ts % day_ns != 0) && ((ts < 0) != (day_ns < 0))) --q;
    return std::to_string(q);
}

}  // namespace

const std::vector<std::string>& fill_types() {
    // v1.17 B10 "dark": a modelled dark fill (Fill::dark_midpoint, F7 part 2).
    static const std::vector<std::string> t = {"passive", "aggressive", "faded",
                                               "cancelled_replaced", "dark"};
    return t;
}

std::string fill_type(const Fill& fill, const ChildOrder& order) {
    // v1.17 B10 (F7 part 2): a MODELLED dark fill, tested before the
    // liquidity split (Python: Fill.provenance == "dark_midpoint").
    if (fill.dark_midpoint) return "dark";
    if (fill.liquidity == Liquidity::TAKER) return "aggressive";
    if (order.cancel_rule != 0 && fill.ts >= order.cancel_rule_ts) return "faded";
    if (order.replaces_order_id != 0) return "cancelled_replaced";
    return "passive";
}

std::string regime_of(const ChildOrder& order) {
    const char* name = urgency_regime_name(static_cast<UrgencyRegime>(order.urgency_regime));
    return name[0] == '\0' ? std::string("plain") : std::string(name);
}

Value fill_rows(const std::vector<Fill>& fills, const std::map<std::uint64_t, ChildOrder>& orders,
                const MarketTimeline& tl, double tick_size, double qty_unit,
                const Horizons* horizons_ns, const std::string* cluster, std::int64_t day_ns,
                const std::string& algo) {
    (void)algo;       // carried by the Python MarkoutFill; unused by the measures
    (void)qty_unit;   // idem (fill_rows reports per unit of price)
    const Horizons& hz = horizons_ns == nullptr ? default_horizons_ns() : *horizons_ns;
    Array rows;
    for (const Fill& f : fills) {
        const ChildOrder& o = orders.at(f.order_id);
        const double price = static_cast<double>(f.price_ticks) * tick_size;
        Object mk;
        Object pi;
        for (const auto& [name, h] : hz) {
            const auto x = fill_measures(tl, f.ts, price, f.side, f.liquidity == Liquidity::TAKER, h);
            if (!x.has_value()) {
                mk[name] = Value(nullptr);
                pi[name] = Value(nullptr);
            } else {
                mk[name] = Value(1e4 * x->markout / price);
                pi[name] = Value(1e4 * x->price_impact / price);
            }
        }
        Object row;
        row["fill_type"] = Value(fill_type(f, o));
        row["regime"] = Value(regime_of(o));
        row["cluster"] = Value(cluster == nullptr ? day_label(f.ts, day_ns) : *cluster);
        row["side"] = Value(static_cast<int>(f.side));
        row["ts"] = i64(f.ts);
        row["price"] = Value(price);
        row["qty"] = i64(f.qty);
        row["markout_bps"] = Value(std::move(mk));
        row["price_impact_bps"] = Value(std::move(pi));
        rows.push_back(Value(std::move(row)));
    }
    return Value(std::move(rows));
}

Value cluster_mean(const std::vector<double>& values, const std::vector<std::string>& clusters) {
    const std::size_t n = values.size();
    if (n != clusters.size()) {
        throw std::invalid_argument("values and clusters must have the same length");
    }
    Object out;
    out["n"] = i64(static_cast<std::int64_t>(n));
    out["n_clusters"] = i64(0);
    out["mean"] = Value(nullptr);
    out["se"] = Value(nullptr);
    out["t"] = Value(nullptr);
    out["df"] = Value(nullptr);
    out["p_student_t"] = Value(nullptr);
    out["ci95"] = Value(nullptr);
    if (n == 0) return Value(std::move(out));
    double total = 0.0;
    for (const double v : values) total += v;
    const double mean = total / static_cast<double>(n);
    out["mean"] = Value(mean);
    // Python: sums[c] = sums.get(c, 0.0) + (v - mean) in row order; the
    // iteration over sums.values() for ss is in first-seen order.
    std::map<std::string, double> sums;
    std::vector<std::string> order;
    for (std::size_t i = 0; i < n; ++i) {
        auto it = sums.find(clusters[i]);
        if (it == sums.end()) {
            sums[clusters[i]] = 0.0 + (values[i] - mean);
            order.push_back(clusters[i]);
        } else {
            it->second = it->second + (values[i] - mean);
        }
    }
    const std::size_t g = sums.size();
    out["n_clusters"] = i64(static_cast<std::int64_t>(g));
    if (g < 2) return Value(std::move(out));
    double ss = 0.0;
    for (const std::string& c : order) ss += sums[c] * sums[c];
    const double gd = static_cast<double>(g);
    const double se = std::sqrt(gd / (gd - 1.0) * ss / static_cast<double>(n * n));
    out["se"] = Value(se);
    out["df"] = i64(static_cast<std::int64_t>(g) - 1);
    if (se > 0.0) {
        const double t = mean / se;
        const double df = gd - 1.0;
        out["t"] = Value(t);
        out["p_student_t"] = Value(2.0 * student_t_sf(std::fabs(t), df));
        const double half = student_t_isf(0.025, df) * se;
        out["ci95"] = Value(Array{Value(mean - half), Value(mean + half)});
    } else {
        out["ci95"] = Value(Array{Value(mean), Value(mean)});
    }
    return Value(std::move(out));
}

namespace {

Value cell(const std::vector<const Value*>& rows, const std::vector<std::string>& horizons) {
    std::int64_t qty = 0;
    for (const Value* r : rows) qty += r->at("qty").as_int64();
    Object out;
    out["n_fills"] = i64(static_cast<std::int64_t>(rows.size()));
    out["qty"] = i64(qty);
    Object hz;
    for (const std::string& name : horizons) {
        Object hrow;
        for (const char* m : kMeasures) {
            std::vector<double> vals;
            std::vector<std::string> cl;
            const std::string key = std::string(m) + "_bps";
            for (const Value* r : rows) {
                const Value& v = r->at(key).at(name);
                if (v.is_null()) continue;
                vals.push_back(v.as_number());
                cl.push_back(r->at("cluster").as_string());
            }
            hrow[m] = cluster_mean(vals, cl);
        }
        hz[name] = Value(std::move(hrow));
    }
    out["horizons"] = Value(std::move(hz));
    return Value(std::move(out));
}

}  // namespace

Value decompose(const Value& rows_v, const std::vector<std::string>* horizons) {
    const Array& rows = rows_v.as_array();
    std::vector<std::string> hz;
    if (horizons != nullptr) {
        hz = *horizons;
    } else if (!rows.empty()) {
        for (const auto& [k, v] : rows.front().at("markout_bps").as_object()) {
            (void)v;
            hz.push_back(k);
        }
    }
    std::map<std::string, std::vector<const Value*>> by_type, by_regime, by_both;
    std::vector<const Value*> all;
    std::int64_t total_qty = 0;
    for (const Value& r : rows) {
        const std::string ft = r.at("fill_type").as_string();
        const std::string rg = r.at("regime").as_string();
        by_type[ft].push_back(&r);
        by_regime[rg].push_back(&r);
        by_both[ft + "|" + rg].push_back(&r);
        all.push_back(&r);
        total_qty += r.at("qty").as_int64();
    }
    Object share;
    for (const std::string& ft : fill_types()) {
        auto it = by_type.find(ft);
        if (it != by_type.end() && total_qty > 0) {
            std::int64_t q = 0;
            for (const Value* r : it->second) q += r->at("qty").as_int64();
            share[ft] = Value(static_cast<double>(q) / static_cast<double>(total_qty));
        }
    }
    Object out;
    Array hz_a;
    for (const auto& h : hz) hz_a.push_back(Value(h));
    out["horizons"] = Value(std::move(hz_a));
    out["inference"] = Value("equal-weight mean per fill, CR1 SE clustered by day, Student-t G-1 df");
    Array ft_a;
    for (const auto& t : fill_types()) ft_a.push_back(Value(t));
    out["fill_types"] = Value(std::move(ft_a));
    out["all"] = cell(all, hz);
    out["fill_type_share_of_qty"] = Value(std::move(share));
    Object bt;
    for (const std::string& ft : fill_types()) {
        auto it = by_type.find(ft);
        if (it != by_type.end()) bt[ft] = cell(it->second, hz);
    }
    out["by_fill_type"] = Value(std::move(bt));
    Object br;
    for (const auto& [k, v] : by_regime) br[k] = cell(v, hz);
    out["by_regime"] = Value(std::move(br));
    Object bb;
    for (const auto& [k, v] : by_both) bb[k] = cell(v, hz);
    out["by_fill_type_and_regime"] = Value(std::move(bb));
    return Value(std::move(out));
}

Value markout_decomposition(const std::vector<Fill>& fills,
                            const std::map<std::uint64_t, ChildOrder>& orders,
                            const MarketTimeline& tl, double tick_size, double qty_unit,
                            const Horizons* horizons_ns, std::int64_t day_ns) {
    const Horizons& hz = horizons_ns == nullptr ? default_horizons_ns() : *horizons_ns;
    const Value rows = fill_rows(fills, orders, tl, tick_size, qty_unit, &hz, nullptr, day_ns);
    std::vector<std::string> names;
    for (const auto& [name, h] : hz) {
        (void)h;
        names.push_back(name);
    }
    return decompose(rows, &names);
}

}  // namespace iap
