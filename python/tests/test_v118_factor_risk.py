"""REL-17 factor risk model: published known answers and synthetic recovery.

Known answers (docs/FACTOR_RISK.md section 2):

* Hull, "Options, Futures, and Other Derivatives" / "Risk Management and
  Financial Institutions", model-building VaR example: $10M in Microsoft
  (daily sd 2 %), $5M in AT&T (daily sd 1 %), correlation 0.3, 10-day 99 %
  VaR with z = 2.33 -> $1,473,621 (Microsoft), $368,405 (AT&T), $1,622,657
  (portfolio), diversification benefit $219,369.
* Standard-normal constants: VaR_0.99 = 2.3263 sd, ES_0.99 = 2.6652 sd,
  ES_0.975 = 2.3378 sd (the Basel FRTB 97.5 % ES ~ 99 % VaR equivalence).
* Single-index model (Grinold & Kahn ch. 3): total variance =
  beta_P^2 sigma_M^2 + sum w_i^2 omega_i^2, by hand.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from iap.portfolio.factor_risk import (
    FactorRiskModel,
    StressScenario,
    build_factor_exposures,
    estimate_factor_model,
    historical_var_es,
    load_stress_scenarios,
    parametric_var_es,
    run_stress,
)


def _hull_model() -> tuple[FactorRiskModel, np.ndarray]:
    sd = np.array([0.02, 0.01])
    corr = np.array([[1.0, 0.3], [0.3, 1.0]])
    F = corr * np.outer(sd, sd)
    m = FactorRiskModel(np.eye(2), F, np.zeros(2), ("MSFT", "T"))
    return m, np.array([10_000_000.0, 5_000_000.0])


def test_hull_two_asset_var_reproduced() -> None:
    m, w = _hull_model()
    m10 = m.scaled(10)
    one_day = m.volatility(w)
    assert one_day == pytest.approx(220_227, abs=1)
    v_port = parametric_var_es(m10.volatility(w), 0.99, z=2.33)["var"]
    v_msft = parametric_var_es(m10.volatility(np.array([w[0], 0.0])), 0.99, z=2.33)["var"]
    v_t = parametric_var_es(m10.volatility(np.array([0.0, w[1]])), 0.99, z=2.33)["var"]
    assert v_msft == pytest.approx(1_473_621, abs=1)
    assert v_t == pytest.approx(368_405, abs=1)
    assert v_port == pytest.approx(1_622_657, abs=1)
    assert v_msft + v_t - v_port == pytest.approx(219_369, abs=2)


def test_normal_var_es_constants() -> None:
    r99 = parametric_var_es(1.0, 0.99)
    assert r99["var"] == pytest.approx(2.3263, abs=1e-4)
    assert r99["es"] == pytest.approx(2.6652, abs=1e-4)
    assert parametric_var_es(1.0, 0.975)["es"] == pytest.approx(2.3378, abs=1e-4)
    assert parametric_var_es(2.0, 0.99, mean=0.5)["var"] == pytest.approx(
        2 * 2.3263 - 0.5, abs=1e-3
    )


def test_single_index_decomposition_by_hand() -> None:
    beta = np.array([0.8, 1.0, 1.5])
    omega2 = np.array([0.04, 0.01, 0.09])  # specific variances
    sigma_m2 = 0.0225  # 15 % market vol
    w = np.array([0.5, 0.3, 0.2])
    m = FactorRiskModel(beta[:, None], np.array([[sigma_m2]]), omega2, ("market",))
    d = m.decompose(w)
    beta_p = 0.5 * 0.8 + 0.3 * 1.0 + 0.2 * 1.5  # 1.0
    assert d["factor_exposure"]["market"] == pytest.approx(beta_p)
    assert d["factor_var"] == pytest.approx(beta_p**2 * sigma_m2)  # 0.0225
    assert d["specific_var"] == pytest.approx(0.25 * 0.04 + 0.09 * 0.01 + 0.04 * 0.09)  # 0.0145
    assert d["total_var"] == pytest.approx(0.037)
    assert sum(d["asset_contribution"]) == pytest.approx(d["total_var"])
    assert sum(d["factor_contribution"].values()) == pytest.approx(d["factor_var"])
    assert m.volatility(w) == pytest.approx(math.sqrt(0.037))


def test_historical_var_es_order_statistic() -> None:
    pnl = np.arange(1, 101, dtype=float) - 50.0  # -49 .. 50
    r = historical_var_es(pnl, 0.95)  # k = 5 worst: -49..-45
    assert r["order_statistic"] == 5
    assert r["var"] == 45.0
    assert r["es"] == pytest.approx(47.0)
    assert historical_var_es(pnl, 0.99)["var"] == 49.0


def test_historical_matches_parametric_on_large_normal_sample() -> None:
    x = np.random.default_rng(7).normal(0.0, 1.0, 400_000)
    h = historical_var_es(x, 0.99)
    assert h["var"] == pytest.approx(2.3263, abs=0.03)
    assert h["es"] == pytest.approx(2.6652, abs=0.03)


def test_exposure_builder_columns_and_buckets() -> None:
    ids = [1, 2, 3, 4, 5, 6]
    e = build_factor_exposures(
        ids,
        betas={1: 1.2, 2: 0.9, 3: float("nan")},
        sectors={1: "Tech", 2: "Tech", 3: "Energy", 4: "Energy", 5: "Financials"},
        size={i: 10.0**i for i in ids},
        adv_notional={i: 10.0 ** (7 - i) for i in ids},
    )
    assert e.labels[0] == "market"
    assert e.missing_beta == (3, 4, 5, 6)
    assert list(e.column("market")[:3]) == [1.2, 0.9, 1.0]
    assert "sector:sector_unknown" in e.labels  # instrument 6
    assert e.labels[-4:] == ("size:q2", "size:q3", "liquidity:q2", "liquidity:q3")
    assert list(e.column("size:q3")) == [0, 0, 0, 0, 1, 1]
    assert list(e.column("liquidity:q3")) == [1, 1, 0, 0, 0, 0]
    sector_cols = [k for k, lab in enumerate(e.labels) if lab.startswith("sector:")]
    assert np.allclose(e.matrix[:, sector_cols].sum(axis=1), 1.0)


def test_estimation_recovers_known_model() -> None:
    rng = np.random.default_rng(11)
    n, t = 60, 4000
    beta = rng.uniform(0.5, 1.5, n)
    sector = np.repeat([0, 1, 2], n // 3)
    B = np.column_stack([beta] + [(sector == s).astype(float) for s in range(3)])
    F = np.diag([1e-4, 4e-5, 2.5e-5, 1e-5])
    spec = rng.uniform(1e-5, 4e-5, n)
    f = rng.multivariate_normal(np.zeros(4), F, t)
    r = f @ B.T + rng.normal(0, 1, (t, n)) * np.sqrt(spec)
    m = estimate_factor_model(r, B, cov="sample", shrink=False)
    true_sigma = B @ F @ B.T + np.diag(spec)
    w = rng.normal(0, 1, n) / n
    assert m.volatility(w) == pytest.approx(math.sqrt(w @ true_sigma @ w), rel=0.06)
    assert np.allclose(m.specific_var, spec, rtol=0.15)
    ew = estimate_factor_model(r, B, cov="ewma", shrink=True)
    assert ew.meta["shrink_intensity"] is not None
    assert np.all(np.linalg.eigvalsh(ew.covariance) > 0)


def test_stress_scenarios_config_and_pnl() -> None:
    scen = {s.name: s for s in load_stress_scenarios()}
    assert {"market_down_5pct", "black_monday_1987", "tech_selloff"} <= set(scen)
    e = build_factor_exposures(
        [1, 2], betas={1: 1.5, 2: 0.5}, sectors={1: "Information Technology", 2: "Energy"}
    )
    m = FactorRiskModel(
        e.matrix, np.eye(e.matrix.shape[1]) * 1e-4, np.full(2, 1e-4), e.labels, e.instrument_ids
    )
    w = np.array([1_000_000.0, -1_000_000.0])
    res = {r["name"]: r for r in run_stress(m, w, list(scen.values()))}
    # beta-weighted market: (1.5 - 0.5) * 1e6 * -0.05
    assert res["market_down_5pct"]["pnl"] == pytest.approx(-50_000.0)
    # tech: market -3 % on net beta 1e6, IT -6 % on the long 1e6
    assert res["tech_selloff"]["pnl"] == pytest.approx(-30_000.0 - 60_000.0)
    assert "liquidity:q2" in res["liquidity_crunch"]["skipped_factors"]
    with pytest.raises(ValueError):
        run_stress(m, w, [StressScenario("x", {"nope": 0.1})], strict=True)
