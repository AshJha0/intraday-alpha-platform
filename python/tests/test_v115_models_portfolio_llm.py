"""v1.15: normalised conformal scores, ACI clamp, conformal report CLI,
optimizer step cap, covariance shrinkage, growing-context LLM budget."""

import json

import numpy as np
import pytest
from iap.llm.budget import CACHE_WRITE_MULT, PRICES, Budget, BudgetExceeded
from iap.models.conformal import (
    AdaptiveConformal,
    SplitConformal,
    calibration_report,
    confidence_from_width,
    conformal_size,
)
from iap.portfolio.covariance import (
    condition_number,
    ewma_covariance,
    shrink_constant_correlation,
)
from iap.portfolio.optimizer import Constraints, solve
from iap.research import conformal_report as cr

# --- conformal -------------------------------------------------------------


def test_normalized_score_widths_vary_and_cover():
    rng = np.random.default_rng(3)
    n = 4000
    sigma = 0.2 + 2.0 * rng.random(n)
    y = sigma * rng.normal(size=n)
    pred = np.zeros(n)
    cp = SplitConformal(0.1, score="normalized").fit(y[:2000], pred[:2000], sigma[:2000])
    lo, hi = cp.interval(pred[2000:], sigma[2000:])
    w = hi - lo
    assert w.std() > 0.1 * w.mean()  # input-dependent widths
    assert np.allclose(w, 2 * cp.q * sigma[2000:])
    rep = calibration_report(y[2000:], lo, hi, ["s"] * 2000, 0.1)
    assert abs(rep["overall"]["coverage"] - 0.9) < 0.03
    # absolute default is unchanged: constant width
    lo2, hi2 = SplitConformal(0.1).fit(y[:2000], pred[:2000]).interval(pred[2000:])
    assert np.ptp(hi2 - lo2) == 0


def test_normalized_requires_sigma():
    with pytest.raises(ValueError):
        SplitConformal(0.1, score="normalized").fit([1.0, 2.0], [0.0, 0.0])
    with pytest.raises(ValueError):
        SplitConformal(0.1, score="bogus")


def test_aci_long_miss_streak_clamped_not_zero_size(caplog):
    rng = np.random.default_rng(0)
    aci = AdaptiveConformal(alpha=0.1, gamma=0.05, window=500)
    aci.warm_start(rng.normal(size=500), np.zeros(500))
    ref = 2 * aci.half_width()
    with caplog.at_level("WARNING", logger="iap.models.conformal"):
        for _ in range(200):
            aci.update(1.01 * aci.half_width() + 1e-9, 0.0)  # always just missed
    assert aci.alpha_t >= 0.01 - 1e-12
    assert aci.saturations > 0
    assert "saturated" in caplog.text
    w = 2 * aci.half_width()
    assert np.isfinite(w)
    assert conformal_size(100, confidence_from_width(w, ref), 1000) > 0


def test_aci_long_cover_streak_upper_clamp():
    aci = AdaptiveConformal(alpha=0.1, gamma=0.5, window=50)
    aci.warm_start(np.ones(50), np.zeros(50))
    for _ in range(50):
        aci.update(0.0, 0.0)
    assert aci.alpha_t < 1.0
    assert aci.saturations > 0


def test_conformal_report_per_session():
    df = cr.synthetic_frame(2000, n_sessions=4, seed=1)
    for kw in ({}, {"score": "normalized"}, {"method": "adaptive"}):
        rep = cr.run_report(df, **kw)
        assert set(rep["by_session"]) <= {"S0", "S1", "S2", "S3"}
        assert sum(r["n"] for r in rep["by_session"].values()) == rep["overall"]["n"]
        assert abs(rep["overall"]["coverage"] - 0.9) < 0.05
    assert "saturations" in cr.run_report(df, method="adaptive")["config"]


def test_conformal_report_cli(capsys, tmp_path):
    assert cr.main(["--synthetic", "800", "--score", "normalized"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["config"]["score"] == "normalized"
    p = tmp_path / "f.csv"
    cr.synthetic_frame(300).drop(columns="sigma").to_csv(p, index=False)
    assert cr.main(["--input", str(p), "--score", "normalized"]) == 2


# --- optimizer ---------------------------------------------------------------


def test_risk_aversion_zero_with_eta_cap():
    n = 4
    alpha = np.array([0.3, -0.2, 0.1, -0.05])
    cons = Constraints(w_min=np.full(n, -1.0), w_max=np.full(n, 1.0), gross_cap=2.0)
    res = solve(alpha, np.zeros((n, n)), np.zeros(n), 0.0, np.zeros(n), cons, eta_max=0.5)
    assert res.feasible
    assert np.all(np.isfinite(res.weights))
    assert np.abs(res.weights).sum() <= 2.0 + 1e-6
    # sensible: weights follow alpha's sign; the two strongest names get the gross
    nz = np.abs(res.weights) > 1e-9
    assert np.all(np.sign(res.weights[nz]) == np.sign(alpha[nz]))
    assert res.objective >= 0.49  # optimum 0.5: |w|=1 on the two largest |alpha|
    # default (no cap) is unchanged and also finite
    base = solve(alpha, np.zeros((n, n)), np.zeros(n), 0.0, np.zeros(n), cons)
    assert np.all(np.isfinite(base.weights))
    with pytest.raises(ValueError):
        solve(alpha, np.zeros((n, n)), np.zeros(n), 0.0, np.zeros(n), cons, eta_max=0.0)


# --- covariance --------------------------------------------------------------


def test_shrinkage_opt_in_improves_conditioning():
    rng = np.random.default_rng(5)
    r = rng.normal(size=(30, 10)) * 1e-3
    base = ewma_covariance(r, ridge=0.0)
    np.testing.assert_array_equal(base, ewma_covariance(r, ridge=0.0, shrinkage=None))
    shr = ewma_covariance(r, ridge=0.0, shrinkage="ledoit_wolf", shrink_intensity=0.5)
    assert condition_number(shr) < condition_number(base)
    np.testing.assert_allclose(np.diag(shr), np.diag(base))
    np.testing.assert_allclose(shrink_constant_correlation(base, 0.0), base)
    with pytest.raises(ValueError):
        ewma_covariance(r, shrinkage="nope")
    assert condition_number(np.zeros((2, 2))) == float("inf")


# --- llm budget --------------------------------------------------------------

M = "claude-opus-5-5"


def _usage(i, o):
    return {"input_tokens": i, "output_tokens": o}


def test_default_projection_unchanged():
    b = Budget(max_usd=10.0)
    b.charge(M, _usage(1000, 500))
    assert b.projected_input_tokens() == 1000


def test_growing_context_projection_and_pricing():
    b = Budget(max_usd=10.0, context_growth=True, prompt_estimate_tokens=5000)
    assert b.projected_input_tokens() == 5000
    b.charge(M, _usage(5000, 800))
    b.add_pending_tool_tokens(1200)
    assert b.projected_input_tokens() == 5000 + 800 + 1200
    b.charge(M, _usage(7000, 100))
    assert b.pending_tool_tokens == 0


def test_growing_context_catches_overshoot_default_misses():
    pin, pout, _ = PRICES[M]
    max_out = 4096

    def run(growth):
        b = Budget(
            max_usd=0.5,
            max_tokens=10**9,
            context_growth=growth,
            prompt_estimate_tokens=20_000,
        )
        ctx = 20_000
        for _ in range(100):
            try:
                b.check_model_call(M)
            except BudgetExceeded:
                return b
            b.charge(M, _usage(ctx, max_out))
            b.add_pending_tool_tokens(30_000)
            ctx += max_out + 30_000  # context grows by reply + tool result
        return b

    # default projection can overshoot; growing-context one never does
    assert run(True).usd <= 0.5
    assert run(False).usd > run(True).usd
    assert CACHE_WRITE_MULT > 1 and pin > 0 and pout > 0


def test_first_call_seeded():
    b = Budget(max_usd=0.01, context_growth=True, prompt_estimate_tokens=10_000)
    with pytest.raises(BudgetExceeded):
        b.check_model_call(M)
