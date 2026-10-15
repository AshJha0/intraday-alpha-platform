// v1.17 F7 rule 4: ParentOrder::pov_exact parity with
// tests/golden/expected_f7_pov_exact.json (make_golden_f7.py --only
// r04_pov_exact): NATIVE / PASSIVE / AGGRESSIVE POV parents with and without
// the exact target + catch-up on the golden stream and the real slice.

#include <gtest/gtest.h>

#include "f7_replay_util.hpp"

namespace {

using namespace iap_f7;

TEST(F7PovExact, Scenarios) {
    const Value g = load_golden("expected_f7_pov_exact.json");
    const Array& scen = g.at("inputs").at("scenarios").as_array();
    const Array& exp = g.at("expected").at("scenarios").as_array();
    ASSERT_EQ(scen.size(), exp.size());
    std::vector<iap::MarketEvent> slice;
    for (std::size_t i = 0; i < scen.size(); ++i) {
        if (scen[i].at("events").as_string() == "slice" && slice.empty()) slice = read_slice();
        const Value got = run_scenario(scen[i], true, &slice);
        expect_canonical_eq(exp[i], got, "scenario " + std::to_string(i) + " (" +
                                             scen[i].at("label").as_string() + ")");
    }
}

TEST(F7PovExact, TargetRule) {
    iap::ParentOrder p;
    p.algo = iap::AlgoType::POV;
    p.participation = 0.2;
    EXPECT_EQ(iap::pov_target(p, 1000), 200);
    p.pov_exact = true;
    EXPECT_EQ(iap::pov_target(p, 1000), 250);  // floor(0.2 / 0.8 * 1000)
    p.participation = 1.0;
    EXPECT_THROW(iap::pov_target(p, 1000), std::invalid_argument);
}

}  // namespace
