// v1.17 F7 rule 5: the Almgren-Chriss eta path parity with
// tests/golden/expected_f7_almgren_chriss.json (make_golden_f7.py --only
// r05_almgren_chriss): ac_params_from_calibration, ac_kappa, ac_trajectory
// with drift, ac_cost, ac_drift_from_signal, on fixed cases and per sample
// of the real slice.

#include <gtest/gtest.h>

#include "f7_util.hpp"
#include "iap/execution/optimal.hpp"

namespace {

using namespace iap_f7;

iap::ACParams params_from(const Value& v) {
    iap::ACParams p;
    p.sigma = v.at("sigma").as_number();
    p.eta = v.at("eta").as_number();
    p.gamma = v.at("gamma").as_number();
    p.eps = v.at("eps").as_number();
    p.horizon = v.at("horizon").as_number();
    p.risk_aversion = v.at("risk_aversion").as_number();
    return p;
}

Value params_row(const iap::ACParams& p) {
    Object o;
    o["sigma"] = vd(p.sigma);
    o["eta"] = vd(p.eta);
    o["gamma"] = vd(p.gamma);
    o["eps"] = vd(p.eps);
    o["horizon"] = vd(p.horizon);
    o["risk_aversion"] = vd(p.risk_aversion);
    return Value(std::move(o));
}

Value doubles(const std::vector<double>& v) {
    Array a;
    for (const double x : v) a.push_back(vd(x));
    return Value(std::move(a));
}

Value error(const std::exception& e) {
    Object o;
    o["error"] = vs(e.what());
    return Value(std::move(o));
}

TEST(F7AlmgrenChriss, Calibration) {
    const Value g = load_golden("expected_f7_almgren_chriss.json");
    Array got;
    for (const Value& c : g.at("inputs").at("calibration").as_array()) {
        iap::ACCalibrationInputs in;
        in.coeff = c.at("coeff").as_number();
        in.sigma = c.at("sigma").as_number();
        in.adv = opt_num(c.at("adv"));
        in.price = c.at("price").as_number();
        in.horizon = opt_num(c.at("horizon"));
        in.risk_aversion = c.at("risk_aversion").as_number();
        in.permanent_fraction = c.at("permanent_fraction").as_number();
        in.eps = c.at("eps").as_number();
        in.adv_per_day = opt_num(c.at("adv_per_day"));
        in.horizon_s = opt_num(c.at("horizon_s"));
        in.session_s = c.at("session_s").as_number();
        try {
            got.push_back(params_row(iap::ac_params_from_calibration(in)));
        } catch (const std::invalid_argument& e) {
            got.push_back(error(e));
        }
    }
    expect_canonical_eq(g.at("expected").at("calibration"), Value(std::move(got)), "calibration");
}

TEST(F7AlmgrenChriss, Trajectories) {
    const Value g = load_golden("expected_f7_almgren_chriss.json");
    Array got;
    for (const Value& c : g.at("inputs").at("trajectories").as_array()) {
        const iap::ACParams p = params_from(c.at("params"));
        const int n = static_cast<int>(c.at("n").as_int64());
        const double qty = c.at("qty").as_number();
        const double drift = c.at("drift").as_number();
        try {
            Object o;
            o["kappa"] = vd(iap::ac_kappa(p, n));
            const auto tr = iap::ac_trajectory(qty, p, n, drift);
            const auto [e, v] = iap::ac_cost(qty, p, n, &tr);
            const auto [e0, v0] = iap::ac_cost(qty, p, n);
            o["trades"] = doubles(tr);
            o["cost"] = Value(Array{vd(e), vd(v)});
            o["cost_optimal"] = Value(Array{vd(e0), vd(v0)});
            got.push_back(Value(std::move(o)));
        } catch (const std::invalid_argument& e) {
            got.push_back(error(e));
        }
    }
    expect_canonical_eq(g.at("expected").at("trajectories"), Value(std::move(got)), "trajectories");
}

TEST(F7AlmgrenChriss, Drift) {
    const Value g = load_golden("expected_f7_almgren_chriss.json");
    Array got;
    for (const Value& c : g.at("inputs").at("drift").as_array()) {
        try {
            got.push_back(vd(iap::ac_drift_from_signal(
                static_cast<std::uint8_t>(c.at("side").as_int64()),
                c.at("expected_return").as_number(), c.at("price").as_number(),
                c.at("remaining_horizon").as_number())));
        } catch (const std::invalid_argument& e) {
            got.push_back(error(e));
        }
    }
    expect_canonical_eq(g.at("expected").at("drift"), Value(std::move(got)), "drift");
}

TEST(F7AlmgrenChriss, RealSliceSamples) {
    const Value g = load_golden("expected_f7_almgren_chriss.json");
    const Value& in = g.at("inputs").at("slice");
    const double sigma = in.at("sigma").as_number();
    const double coeff = in.at("coeff").as_number();
    const double horizon_s = in.at("horizon_s").as_number();
    const double session_s = in.at("session_s").as_number();
    const int n = static_cast<int>(in.at("n").as_int64());
    const double risk = in.at("risk_aversion").as_number();
    const double adv = in.at("adv").as_number();
    const auto events = read_slice();
    iap::ConsolidatedBook book(kSliceInstrument);
    Array rows;
    for (std::size_t k = 1; k <= events.size(); ++k) {
        book.apply(events[k - 1]);
        if (!is_sample(k)) continue;
        const auto bb = book.best_bid();
        const auto ba = book.best_ask();
        if (!bb.has_value() || !ba.has_value()) continue;
        const double price = 0.5 * static_cast<double>(bb->first + ba->first) * kSliceTick;
        iap::ACCalibrationInputs ci;
        ci.coeff = coeff;
        ci.sigma = sigma;
        ci.price = price;
        ci.adv_per_day = adv;
        ci.horizon_s = horizon_s;
        ci.session_s = session_s;
        ci.risk_aversion = risk;
        const iap::ACParams p = iap::ac_params_from_calibration(ci);
        const double er = static_cast<double>(bb->second - ba->second) /
                          static_cast<double>(bb->second + ba->second) * 1e-4;
        const double drift = iap::ac_drift_from_signal(0, er, price, p.horizon);
        const auto tr = iap::ac_trajectory(10'000.0, p, n, drift);
        const auto plain = iap::ac_trajectory(10'000.0, p, n);
        const auto [e, v] = iap::ac_cost(10'000.0, p, n, &tr);
        Object o;
        o["k"] = vi(static_cast<std::int64_t>(k));
        o["price"] = vd(price);
        o["params"] = params_row(p);
        o["kappa"] = vd(iap::ac_kappa(p, n));
        o["drift"] = vd(drift);
        o["trades"] = doubles(tr);
        o["plain"] = doubles(plain);
        o["cost"] = Value(Array{vd(e), vd(v)});
        rows.push_back(Value(std::move(o)));
    }
    expect_canonical_eq(g.at("expected").at("slice"), Value(std::move(rows)), "slice");
}

}  // namespace
