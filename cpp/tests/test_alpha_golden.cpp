// Alpha golden parity (API_ALPHA.md sections 2-7) + scoring invariants.
//
// Two layers per alpha, both against tests/golden/expected_alpha.json:
//  - embedded-input scoring: the golden cases carry every input feature
//    value, so the linear_z_v1 math is validated in isolation;
//  - engine-driven scoring (EQ01/EQ03/EQ06/FX01/FX09): the golden vectors
//    are replayed through the native feature engine at cadence 0 and the
//    pinned rows must reproduce expected_return/confidence at 1e-9.
// FX05 scores its embedded 8-pair grid cross-sections through the pinned
// currency-factor pseudo-inverse solve (golden deviation documented in
// API_ALPHA.md section 6: the golden vectors carry one FX pair only).

#include <gtest/gtest.h>

#include <cmath>
#include <limits>
#include <string>
#include <vector>

#include "golden_util.hpp"
#include "iap/alpha/alpha.hpp"
#include "iap/features/feature_engine.hpp"
#include "iap/marketdata/codec.hpp"

namespace {

using iap::FeatureVector;
using iap::LinearZParams;

constexpr double kNaN = std::numeric_limits<double>::quiet_NaN();

std::string configs_dir() { return iap_test::golden_dir() + "/../../configs"; }

const std::map<std::string, LinearZParams>& params() {
    static const auto p =
        iap::load_alpha_params(configs_dir() + "/strategies/alpha_params.json");
    return p;
}

void near(double got, double want, const std::string& what) {
    EXPECT_LE(std::fabs(got - want), 1e-9 + 1e-9 * std::fabs(want))
        << what << ": got " << got << " want " << want;
}

// Score one golden case from its embedded inputs.
void check_embedded(const std::string& aid) {
    const auto golden = iap_test::load_golden_json("expected_alpha.json");
    const auto& cases = golden["alphas"][aid]["cases"].a();
    ASSERT_EQ(cases.size(), 5u);
    const auto& p = params().at(aid);
    for (const auto& c : cases) {
        FeatureVector vec;
        vec.values.fill(kNaN);
        vec.valid.fill(false);
        for (const auto& [name, v] : c["inputs"].obj) {
            const int slot = iap::feature_index(name);
            ASSERT_GE(slot, 0) << name;
            if (v.type != iap_test::Json::Type::Null) {
                vec.values[static_cast<std::size_t>(slot)] = v.num();
                vec.valid[static_cast<std::size_t>(slot)] = true;
            }
        }
        const auto sig = iap::score_row(p, vec);
        near(sig.expected_return, c["expected_return"].num(),
             aid + " embedded expected_return");
        near(sig.confidence, c["confidence"].num(), aid + " embedded conf");
    }
}

// Engine-driven parity at the pinned golden rows.
void check_engine_driven(const std::string& vector_file,
                         const std::vector<std::string>& alpha_ids) {
    const auto golden = iap_test::load_golden_json("expected_alpha.json");
    const auto events =
        iap::read_jsonl(iap_test::golden_path(vector_file));
    iap::FeatureEngine engine({{1u, 0.01}, {101u, 1e-05}}, 0);
    std::vector<FeatureVector> rows;
    engine.run(events, &rows);
    for (const auto& aid : alpha_ids) {
        const auto& p = params().at(aid);
        for (const auto& c : golden["alphas"][aid]["cases"].a()) {
            const auto r =
                static_cast<std::size_t>(c["event_index_1based"].i64()) - 1;
            ASSERT_LT(r, rows.size());
            const auto sig = iap::score_row(p, rows[r]);
            EXPECT_EQ(sig.timestamp, c["exchange_ts"].i64()) << aid;
            near(sig.expected_return, c["expected_return"].num(),
                 aid + " engine expected_return row " + std::to_string(r));
            near(sig.confidence, c["confidence"].num(),
                 aid + " engine confidence row " + std::to_string(r));
        }
    }
}

TEST(AlphaGolden, ParamsLoadPinned) {
    const auto& p = params();
    ASSERT_EQ(p.size(), 6u);
    for (const char* aid : iap::GOLDEN_ALPHA_IDS) {
        const auto& lp = p.at(aid);
        EXPECT_EQ(lp.alpha_id, aid);
        EXPECT_DOUBLE_EQ(lp.z_clip, 4.0);
        EXPECT_DOUBLE_EQ(lp.conf_scale, 2.0);
        EXPECT_TRUE(std::isfinite(lp.mu));
        EXPECT_GT(lp.sigma, 0.0);
        EXPECT_TRUE(std::isfinite(lp.beta));
        EXPECT_FALSE(lp.features.empty());
    }
    // Pinned horizons (API_ALPHA.md section 4).
    EXPECT_EQ(p.at("EQ01").horizon, "1s");
    EXPECT_EQ(p.at("EQ03").horizon, "5s");
    EXPECT_EQ(p.at("EQ06").horizon, "10s");
    EXPECT_EQ(p.at("FX01").horizon, "500ms");
    EXPECT_EQ(p.at("FX05").horizon, "5m");
    EXPECT_EQ(p.at("FX09").horizon, "1m");
    // Honest-reporting rule: params ship as fitted — the golden file embeds
    // the same numbers (spot-check EQ01 against the golden copy).
    const auto golden = iap_test::load_golden_json("expected_alpha.json");
    EXPECT_DOUBLE_EQ(p.at("EQ01").beta,
                     golden["params"]["EQ01"]["beta"].num());
    EXPECT_DOUBLE_EQ(p.at("EQ01").mu, golden["params"]["EQ01"]["mu"].num());
}

TEST(AlphaGolden, Eq01EmbeddedCases) { check_embedded("EQ01"); }
TEST(AlphaGolden, Eq03EmbeddedCases) { check_embedded("EQ03"); }
TEST(AlphaGolden, Eq06EmbeddedCases) { check_embedded("EQ06"); }
TEST(AlphaGolden, Fx01EmbeddedCases) { check_embedded("FX01"); }
TEST(AlphaGolden, Fx09EmbeddedCases) { check_embedded("FX09"); }

TEST(AlphaGolden, Fx05EmbeddedGridCases) {
    const auto golden = iap_test::load_golden_json("expected_alpha.json");
    const auto& p = params().at("FX05");
    const auto& cases = golden["alphas"]["FX05"]["cases"].a();
    ASSERT_EQ(cases.size(), 5u);
    for (const auto& c : cases) {
        std::array<double, iap::FX_NUM_PAIRS> r;
        for (std::size_t i = 0; i < iap::FX_NUM_PAIRS; ++i) {
            const auto& v = c["inputs"][std::to_string(101 + i)];
            r[i] = v.type == iap_test::Json::Type::Null ? kNaN : v.num();
        }
        const auto raws = iap::fx05_raw_signals(r);
        ASSERT_EQ(c["target_pair"].i64(), 102);
        const double raw = raws[1];  // pair 102 = index 1
        near(raw, c["raw_residual_signal"].num(), "FX05 raw residual");
        double er = 0.0, conf = 0.0;
        iap::score_linear_z(raw, p, er, conf);
        near(er, c["expected_return"].num(), "FX05 expected_return");
        near(conf, c["confidence"].num(), "FX05 confidence");
    }
}

TEST(AlphaGolden, EngineDrivenEquities) {
    check_engine_driven("events_eq_mbo.jsonl", {"EQ01", "EQ03", "EQ06"});
}

TEST(AlphaGolden, EngineDrivenFx) {
    check_engine_driven("events_fx_quote.jsonl", {"FX01", "FX09"});
}

TEST(AlphaScoring, InvalidRawScoresExactZero) {
    // Invariant: confidence == 0 => expected_return == 0.0 exactly, and NaN
    // never leaves a scorer.
    const auto& p = params().at("EQ01");
    double er = 1.0, conf = 1.0;
    iap::score_linear_z(kNaN, p, er, conf);
    EXPECT_EQ(er, 0.0);
    EXPECT_EQ(conf, 0.0);
    FeatureVector vec;  // all-invalid vector
    vec.values.fill(kNaN);
    vec.valid.fill(false);
    for (const char* aid : {"EQ01", "EQ03", "EQ06", "FX01", "FX09"}) {
        const auto sig = iap::score_row(params().at(aid), vec);
        EXPECT_EQ(sig.expected_return, 0.0) << aid;
        EXPECT_EQ(sig.confidence, 0.0) << aid;
    }
}

TEST(AlphaScoring, ClipAndConfidenceBounds) {
    const auto& p = params().at("EQ01");
    double er = 0.0, conf = 0.0;
    // Enormous raw: z clips at +z_clip, confidence caps at 1.
    iap::score_linear_z(p.mu + 1e12 * p.sigma, p, er, conf);
    EXPECT_DOUBLE_EQ(er, p.beta * p.z_clip);
    EXPECT_DOUBLE_EQ(conf, 1.0);
    iap::score_linear_z(p.mu - 1e12 * p.sigma, p, er, conf);
    EXPECT_DOUBLE_EQ(er, -p.beta * p.z_clip);
    EXPECT_DOUBLE_EQ(conf, 1.0);
    // raw == mu: z = 0 => er 0, conf 0.
    iap::score_linear_z(p.mu, p, er, conf);
    EXPECT_EQ(er, 0.0);
    EXPECT_EQ(conf, 0.0);
}

TEST(AlphaScoring, Fx05DegenerateCrossSections) {
    // A lone valid pair carries no relative-value information: raw all NaN.
    std::array<double, iap::FX_NUM_PAIRS> r;
    r.fill(kNaN);
    r[0] = 1e-4;
    auto raws = iap::fx05_raw_signals(r);
    for (double v : raws) EXPECT_TRUE(std::isnan(v));
    // Two valid pairs: signals defined for exactly those two.
    r[1] = -5e-5;
    raws = iap::fx05_raw_signals(r);
    EXPECT_TRUE(std::isfinite(raws[0]));
    EXPECT_TRUE(std::isfinite(raws[1]));
    for (std::size_t i = 2; i < iap::FX_NUM_PAIRS; ++i) {
        EXPECT_TRUE(std::isnan(raws[i]));
    }
    // EUR/USD and GBP/USD load disjoint factors: minimum-norm attributes
    // each return fully to its own currency => residuals (and raws) are 0.
    EXPECT_NEAR(raws[0], 0.0, 1e-15);
    EXPECT_NEAR(raws[1], 0.0, 1e-15);
}

TEST(AlphaScoring, Fx05ResidualOrthogonalToExposures) {
    // Full cross-section: the LS residual must be orthogonal to every free
    // currency column of the exposure matrix.
    std::array<double, iap::FX_NUM_PAIRS> r = {
        3e-5, -2e-5, 1e-5, 4e-5, -1e-5, 2e-5, -3e-5, 5e-6};
    std::array<double, iap::FX_NUM_FREE_CCY> f;
    std::array<double, iap::FX_NUM_PAIRS> fitted;
    ASSERT_TRUE(iap::fx_solve_factors(r, f, fitted));
    const auto a = iap::fx_free_exposure_matrix();
    for (std::size_t j = 0; j < iap::FX_NUM_FREE_CCY; ++j) {
        double dot = 0.0;
        for (std::size_t i = 0; i < iap::FX_NUM_PAIRS; ++i) {
            dot += a[i][j] * (r[i] - fitted[i]);
        }
        EXPECT_NEAR(dot, 0.0, 1e-15) << "free currency column " << j;
    }
}

TEST(AlphaScoring, RawSignalFormulaPinned) {
    // EQ03's pinned 0.5/0.3/0.2 weighting and FX09's product form.
    FeatureVector vec;
    vec.values.fill(kNaN);
    vec.valid.fill(false);
    auto set = [&](int slot, double v) {
        vec.values[static_cast<std::size_t>(slot)] = v;
        vec.valid[static_cast<std::size_t>(slot)] = true;
    };
    set(iap::F_OFI_NORM_L1_W1S, 1.0);
    set(iap::F_OFI_NORM_L5_W1S, 2.0);
    set(iap::F_OFI_NORM_L5_W5S, 3.0);
    EXPECT_DOUBLE_EQ(iap::raw_signal("EQ03", vec),
                     0.5 * 1.0 + 0.3 * 2.0 + 0.2 * 3.0);
    set(iap::F_RET_VOL_ADJ_10S, 1.5);
    set(iap::F_VOL_REGIME_RATIO, 0.5);
    EXPECT_DOUBLE_EQ(iap::raw_signal("FX09", vec), -1.5 * 0.5);
    EXPECT_DOUBLE_EQ(iap::raw_signal("EQ06", vec), 1.5);
    set(iap::F_MICRO_MID_DEV_BPS, -0.25);
    EXPECT_DOUBLE_EQ(iap::raw_signal("EQ01", vec), -0.25);
    EXPECT_DOUBLE_EQ(iap::raw_signal("FX01", vec), -0.25);
    EXPECT_THROW(iap::raw_signal("FX05", vec), std::invalid_argument);
    EXPECT_THROW(iap::raw_signal("EQ99", vec), std::invalid_argument);
}

}  // namespace
