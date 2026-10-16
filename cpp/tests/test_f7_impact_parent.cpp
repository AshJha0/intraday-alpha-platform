// v1.17 F7 rule 3: simulator rule 6b (parent impact) parity with
// tests/golden/expected_f7_impact_parent.json (make_golden_f7.py --only
// r03_impact_parent): the golden stream and the real slice replayed through
// ExecutionReplay with ExecConfig::impact_model = PARENT, canonical bytes of
// every fill, child and parent report equal (the slice arrays by digest).

#include <gtest/gtest.h>

#include "f7_replay_util.hpp"

namespace {

using namespace iap_f7;

TEST(F7ImpactParent, Scenarios) {
    const Value g = load_golden("expected_f7_impact_parent.json");
    const Array& scen = g.at("inputs").at("scenarios").as_array();
    const Array& exp = g.at("expected").at("scenarios").as_array();
    ASSERT_EQ(scen.size(), exp.size());
    std::vector<iap::MarketEvent> slice;
    for (std::size_t i = 0; i < scen.size(); ++i) {
        if (scen[i].at("events").as_string() == "slice" && slice.empty()) slice = read_slice();
        const Value got = run_scenario(scen[i], false, &slice);
        expect_canonical_eq(exp[i], got, "scenario " + std::to_string(i) + " (" +
                                             scen[i].at("label").as_string() + ")");
    }
}

TEST(F7ImpactParent, ConfigValidation) {
    iap::ExecConfig cfg;
    cfg.impact_exponent = 0.5;  // needs PARENT
    EXPECT_THROW(static_cast<void>(iap::ExecutionSimulator(cfg)), std::invalid_argument);
    cfg.impact_model = iap::ImpactModel::PARENT;
    EXPECT_NO_THROW(static_cast<void>(iap::ExecutionSimulator(cfg)));
    cfg.impact_exponent = 1.5;
    EXPECT_THROW(static_cast<void>(iap::ExecutionSimulator(cfg)), std::invalid_argument);
}

}  // namespace
