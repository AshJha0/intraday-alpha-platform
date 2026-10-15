// v1.17 F7 rule 7: B3 signal-aware pacing parity with
// tests/golden/expected_f7_pacing.json (make_golden_f7.py --only
// r07_pacing): pacing_scale, apportion, tilted_slice_qty, the POV pacing
// accumulation, on fixed cases and over the real slice's TRADEs.

#include <gtest/gtest.h>

#include "f7_util.hpp"
#include "iap/execution/algos.hpp"
#include "iap/execution/pacing.hpp"
#include "iap/execution/signal.hpp"

namespace {

using namespace iap_f7;

iap::PacingParams params_from(const Value& v) {
    iap::PacingParams p;
    p.gain = v.at("gain").as_number();
    p.min_scale = v.at("min_scale").as_number();
    p.max_scale = v.at("max_scale").as_number();
    p.max_participation = v.at("max_participation").as_number();
    p.max_age_ns = v.at("max_age_ns").as_int64();
    p.latency_ns = v.at("latency_ns").as_int64();
    p.validate();
    return p;
}

std::vector<double> doubles_from(const Value& a) {
    std::vector<double> out;
    for (const Value& x : a.as_array()) out.push_back(x.as_number());
    return out;
}

Value ints(const std::vector<std::int64_t>& v) {
    Array a;
    for (const auto x : v) a.push_back(vi(x));
    return Value(std::move(a));
}

Value step_row(const iap::PovPacingStep& s) {
    Object o;
    o["scale"] = vd(s.scale);
    o["p_eff"] = vd(s.p_eff);
    o["rate"] = vd(s.rate);
    o["target_f"] = vd(s.target_f);
    o["target"] = vi(s.target);
    return Value(std::move(o));
}

TEST(F7Pacing, FixedVectors) {
    const Value g = load_golden("expected_f7_pacing.json");
    const Value& in = g.at("inputs");
    std::vector<iap::PacingParams> params;
    for (const Value& p : in.at("params").as_array()) params.push_back(params_from(p));
    Array scale;
    for (const Value& c : in.at("scale").as_array()) {
        scale.push_back(vd(iap::pacing_scale(static_cast<std::uint8_t>(c.at("side").as_int64()),
                                             opt_num(c.at("z")),
                                             params[static_cast<std::size_t>(c.at("params").as_int64())])));
    }
    expect_canonical_eq(g.at("expected").at("scale"), Value(std::move(scale)), "scale");
    Array app;
    for (const Value& c : in.at("apportion").as_array()) {
        app.push_back(ints(iap::apportion(c.at("qty").as_int64(), doubles_from(c.at("weights")))));
    }
    expect_canonical_eq(g.at("expected").at("apportion"), Value(std::move(app)), "apportion");
    Array tilt;
    for (const Value& c : in.at("tilt").as_array()) {
        tilt.push_back(vi(iap::tilted_slice_qty(c.at("remaining_qty").as_int64(),
                                                doubles_from(c.at("remaining_weights")),
                                                c.at("scale").as_number())));
    }
    expect_canonical_eq(g.at("expected").at("tilt"), Value(std::move(tilt)), "tilt");
    Array pov;
    for (const Value& seq : in.at("pov").as_array()) {
        const iap::PacingParams& p = params[static_cast<std::size_t>(seq.at("params").as_int64())];
        const double part = seq.at("participation").as_number();
        const bool exact = seq.at("pov_exact").as_bool();
        const auto side = static_cast<std::uint8_t>(seq.at("side").as_int64());
        double target_f = 0.0;
        Array steps;
        for (const Value& st : seq.at("steps").as_array()) {
            const iap::PovPacingStep s = iap::pov_pacing_step(side, opt_num(st.at(1)), p, part, exact,
                                                              st.at(0).as_int64(), target_f);
            target_f = s.target_f;
            steps.push_back(step_row(s));
        }
        pov.push_back(Value(std::move(steps)));
    }
    expect_canonical_eq(g.at("expected").at("pov"), Value(std::move(pov)), "pov");
}

TEST(F7Pacing, RealSlice) {
    const Value g = load_golden("expected_f7_pacing.json");
    const Value& in = g.at("inputs").at("slice");
    const iap::PacingParams pac = params_from(in.at("params"));
    // the planted readings (rule 6 of the goldens; expected_f7_urgency.json
    // pins them): rebuilt here from the book
    const auto events = read_slice();
    iap::ConsolidatedBook book(kSliceInstrument);
    struct Raw {
        std::int64_t ts;
        double er;
    };
    std::vector<Raw> raw;
    std::vector<std::pair<std::size_t, std::int64_t>> samples;
    std::vector<std::pair<std::size_t, std::int64_t>> trades;  // (k, qty) past the prefix
    std::vector<std::int64_t> trade_ts;
    for (std::size_t k = 1; k <= events.size(); ++k) {
        const auto& ev = events[k - 1];
        book.apply(ev);
        const auto bb = book.books().at(kSliceVenue).best_bid();
        const auto ba = book.books().at(kSliceVenue).best_ask();
        const bool two_sided = bb.has_value() && ba.has_value();
        if (k > static_cast<std::size_t>(kSlicePrefix) && k % 50 == 0 && two_sided) {
            raw.push_back({ev.exchange_ts, static_cast<double>(bb->second - ba->second) /
                                               static_cast<double>(bb->second + ba->second) * 1e-4});
        }
        if (is_sample(k) && two_sided) samples.emplace_back(k, ev.exchange_ts);
        if (k > static_cast<std::size_t>(kSlicePrefix) &&
            static_cast<iap::EventType>(ev.event_type) == iap::EventType::TRADE && ev.qty > 0) {
            trades.emplace_back(k, ev.qty);
            trade_ts.push_back(ev.exchange_ts);
        }
    }
    std::vector<double> ers;
    for (const Raw& r : raw) ers.push_back(r.er);
    const iap::ZScore z = iap::zscore(ers);
    std::vector<iap::ArraySignalSource::Row> rows;
    for (std::size_t i = 0; i < raw.size(); ++i) {
        if (!rows.empty() && rows.back().ts == raw[i].ts) {
            rows.back() = {raw[i].ts, z.z[i], raw[i].er};
        } else {
            rows.push_back({raw[i].ts, z.z[i], raw[i].er});
        }
    }
    iap::ArraySignalSource src;
    src.add(kSliceInstrument, rows);
    // POV runs
    struct Run {
        double participation;
        bool exact;
        std::uint8_t side;
        double target_f = 0.0;
    };
    std::vector<Run> runs;
    for (const Value& r : in.at("pov_runs").as_array()) {
        runs.push_back({r.at("participation").as_number(), r.at("pov_exact").as_bool(),
                        static_cast<std::uint8_t>(r.at("side").as_int64())});
    }
    Array pov_rows;
    std::int64_t n_trades = 0;
    for (std::size_t i = 0; i < trades.size(); ++i) {
        ++n_trades;
        const auto rd = src.at(kSliceInstrument, trade_ts[i]);
        const std::optional<double> zz = rd.has_value() ? std::optional<double>(rd->z) : std::nullopt;
        Array runs_out;
        for (Run& run : runs) {
            const iap::PovPacingStep s = iap::pov_pacing_step(run.side, zz, pac, run.participation,
                                                              run.exact, trades[i].second, run.target_f);
            run.target_f = s.target_f;
            runs_out.push_back(step_row(s));
        }
        if (n_trades % 20 != 0) continue;
        Object row;
        row["k"] = vi(static_cast<std::int64_t>(trades[i].first));
        row["dv"] = vi(trades[i].second);
        row["z"] = vod(zz);
        row["runs"] = Value(std::move(runs_out));
        row["n_trades"] = vi(n_trades);
        pov_rows.push_back(Value(std::move(row)));
    }
    expect_canonical_eq(g.at("expected").at("slice").at("pov"), Value(std::move(pov_rows)), "slice pov");
    // tilt over the pinned 8-slice curves
    std::vector<double> scales;
    for (std::size_t i = 0; i < 8 && i < samples.size(); ++i) {
        const auto rd = src.at(kSliceInstrument, samples[i].second);
        scales.push_back(iap::pacing_scale(0, rd.has_value() ? std::optional<double>(rd->z) : std::nullopt, pac));
    }
    Array sc;
    for (const double s : scales) sc.push_back(vd(s));
    expect_canonical_eq(g.at("expected").at("slice").at("scales"), Value(std::move(sc)), "slice scales");
    Object tilt;
    const std::pair<const char*, iap::AlgoType> algos[3] = {
        {"TWAP", iap::AlgoType::TWAP}, {"VWAP", iap::AlgoType::VWAP}, {"IS", iap::AlgoType::IS}};
    for (const auto& [name, algo] : algos) {
        iap::ParentOrder p;
        p.qty = 10'000;
        p.algo = algo;
        p.slices = 8;
        p.risk_aversion = 1.0;
        p.start_ts = 0;
        p.end_ts = 1;
        const std::vector<double> w = iap::slice_weights(p);
        std::int64_t remaining = 10'000;
        std::vector<std::int64_t> q;
        for (std::size_t k = 0; k < w.size(); ++k) {
            const std::vector<double> rest(w.begin() + static_cast<std::ptrdiff_t>(k), w.end());
            const std::int64_t qk = remaining > 0 ? iap::tilted_slice_qty(remaining, rest, scales[k]) : 0;
            q.push_back(qk);
            remaining -= qk;
        }
        Object o;
        Array wa;
        for (const double x : w) wa.push_back(vd(x));
        o["weights"] = Value(std::move(wa));
        o["quantities"] = ints(q);
        tilt[name] = Value(std::move(o));
    }
    expect_canonical_eq(g.at("expected").at("slice").at("tilt"), Value(std::move(tilt)), "slice tilt");
}

}  // namespace
