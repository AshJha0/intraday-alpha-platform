// v1.17 F7 part 2, rule 12: the hazard evaluation over many models — parity
// with tests/golden/expected_f7_hazard_grid.json (make_golden_f7.py --only
// r12_hazard_grid): FillHazardModel::features / bucket_hazards /
// p_fill_within on a feature grid under the part-1 fixture, the wave-5 fit
// that diverged by one ulp, dense cloglog / logit models, an eta-clipping
// model and six seeded coefficient sets. The Python reference (#200) is
// scalar and left-to-right, this port's order: byte parity on every row.

#include <gtest/gtest.h>

#include "f7_util.hpp"
#include "iap/execution/fill_hazard.hpp"

namespace {

using namespace iap_f7;

TEST(F7HazardGrid, EveryModelEveryRow) {
    const Value g = load_golden("expected_f7_hazard_grid.json");
    const Value& in = g.at("inputs");
    std::vector<std::int64_t> t_ns;
    for (const Value& t : in.at("t_ns").as_array()) t_ns.push_back(t.as_int64());
    const Array& feats = in.at("features").as_array();
    int n_models = 0;
    for (const auto& [name, doc] : in.at("models").as_object()) {
        const iap::FillHazardModel m = iap::FillHazardModel::from_value(doc);
        Array rows;
        for (const Value& c : feats) {
            const iap::HazardFeatures x =
                m.features(c.at("queue_ahead").as_number(), c.at("spread_ticks").as_number(),
                           c.at("imbalance").as_number(), opt_num(c.at("depletion_per_s")),
                           c.at("ts_ns").as_int64());
            Object o;
            o["tod"] = vd(x.tod);
            Array h;
            for (const double v : m.bucket_hazards(x)) h.push_back(vd(v));
            o["bucket_hazards"] = Value(std::move(h));
            Array p;
            for (const std::int64_t t : t_ns) p.push_back(vd(m.p_fill_within(x, t)));
            o["p_fill"] = Value(std::move(p));
            rows.push_back(Value(std::move(o)));
        }
        expect_canonical_eq(g.at("expected").at(name), Value(std::move(rows)), "model " + name);
        ++n_models;
    }
    EXPECT_GE(n_models, 10);
}

}  // namespace
