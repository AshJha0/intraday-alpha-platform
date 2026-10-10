"""v1.10 execution opt-ins: Almgren-Chriss (X1), alpha urgency (X2),
forecast VWAP volume curve (X3). Synthetic data only."""

from __future__ import annotations

import json
import math
import random

import pytest
from iap.core.events import EventType, MarketEvent
from iap.execution.algos import AlgoType, ISModel, ParentOrder, slice_quantities, slice_weights
from iap.execution.calibration import ExecCalibration
from iap.execution.config import ExecConfig
from iap.execution.optimal import (
    ACParams,
    ac_cost,
    ac_holdings,
    ac_kappa,
    ac_params_from_calibration,
    ac_trajectory,
    efficient_frontier,
)
from iap.execution.passive import ExecPolicy
from iap.execution.urgency import AlphaUrgencyParams, apply_alpha_urgency, urgency_regime
from iap.execution.volume_curve import (
    DAY_NS,
    curve_slice_weights,
    estimate_volume_curves,
    load_volume_curve,
    main,
    u_shape,
)

P = ACParams(sigma=0.95, eta=2.5e-6, gamma=2.5e-7, eps=0.0625, horizon=5.0, risk_aversion=1e-6)


# ------------------------------------------------------------------ X1


def test_ac_matches_closed_form() -> None:
    n, x0 = 5, 1_000_000.0
    tau = P.horizon / n
    eta_t = P.eta - P.gamma * tau / 2
    kt = math.sqrt(P.risk_aversion * P.sigma**2 / eta_t)
    kappa = math.acosh(0.5 * kt**2 * tau**2 + 1) / tau
    assert ac_kappa(P, n) == pytest.approx(kappa, rel=1e-12)
    x = ac_holdings(x0, P, n)
    for j in range(n + 1):
        want = x0 * math.sinh(kappa * (P.horizon - j * tau)) / math.sinh(kappa * P.horizon)
        assert x[j] == pytest.approx(want, abs=1e-6)
    tr = ac_trajectory(x0, P, n)
    assert sum(tr) == pytest.approx(x0)
    # closed form of the trade list: 2 sinh(k tau/2)/sinh(kT) cosh(k(T - t_{j-1/2})) X
    for j in range(1, n + 1):
        want = (
            2
            * math.sinh(kappa * tau / 2)
            / math.sinh(kappa * P.horizon)
            * math.cosh(kappa * (P.horizon - (j - 0.5) * tau))
            * x0
        )
        assert tr[j - 1] == pytest.approx(want, rel=1e-9)
    assert all(a > b for a, b in zip(tr, tr[1:], strict=False))


def test_ac_risk_neutral_is_linear_and_optimal() -> None:
    p0 = ACParams(sigma=0.95, eta=2.5e-6, horizon=5.0, risk_aversion=0.0)
    assert ac_trajectory(100.0, p0, 4) == pytest.approx([25.0] * 4)
    # the optimal trajectory beats perturbations on E + lam V
    n, x0 = 6, 1e6
    tr = ac_trajectory(x0, P, n)
    e, v = ac_cost(x0, P, n)
    best = e + P.risk_aversion * v
    for k in range(n - 1):
        alt = list(tr)
        alt[k] += 1000.0
        alt[k + 1] -= 1000.0
        e2, v2 = ac_cost(x0, P, n, alt)
        assert e2 + P.risk_aversion * v2 > best


def test_frontier_monotone() -> None:
    fr = efficient_frontier(1e6, P, 10, [0.0, 1e-7, 1e-6, 1e-5, 1e-4])
    es = [e for _, e, _ in fr]
    vs = [v for _, _, v in fr]
    assert all(a < b for a, b in zip(es, es[1:], strict=False))
    assert all(a > b for a, b in zip(vs, vs[1:], strict=False))


def test_params_from_calibration_and_fallback() -> None:
    cal = ExecCalibration(doc={}, impact_coeff_bps_per_pct_adv=4.0)
    p = ac_params_from_calibration(sigma=1.0, adv=1e6, price=50.0, calibration=cal)
    assert p.eta == pytest.approx(4.0 * 1e-2 * 50.0 / 1e6)
    q = ac_params_from_calibration(sigma=1.0, adv=1e6, price=50.0, config=ExecConfig())
    assert q.eta == pytest.approx(ExecConfig().impact_coeff_bps_per_pct_adv * 1e-2 * 50.0 / 1e6)
    with pytest.raises(ValueError):
        ac_params_from_calibration(sigma=1.0, adv=0.0, price=50.0)


def test_is_default_unchanged_and_ac_opt_in() -> None:
    base = ParentOrder(algo=AlgoType.IS, qty=600, slices=3, risk_aversion=1.0)
    assert slice_quantities(base) == [304, 184, 112]  # pinned golden
    ac = ParentOrder(
        algo=AlgoType.IS, qty=600, slices=3, is_model=ISModel.ALMGREN_CHRISS, ac_params=P
    )
    q = slice_quantities(ac)
    assert sum(q) == 600 and q[0] > q[1] > q[2]
    with pytest.raises(ValueError):
        slice_weights(ParentOrder(algo=AlgoType.IS, slices=3, is_model=ISModel.ALMGREN_CHRISS))


# ------------------------------------------------------------------ X2


def test_urgency_switches() -> None:
    prm = AlphaUrgencyParams(threshold=0.5)
    buy = ParentOrder(side=0, qty=100, risk_aversion=1.0)
    sell = ParentOrder(side=1, qty=100, risk_aversion=1.0)
    assert urgency_regime(0, -1.0, prm) == "agree"
    assert urgency_regime(0, 1.0, prm) == "adverse"
    assert urgency_regime(1, 0.2, prm) == "neutral"
    a = apply_alpha_urgency(buy, -1.0, prm)
    assert a.policy == ExecPolicy.PASSIVE and a.urgency == prm.passive_urgency
    b = apply_alpha_urgency(buy, 1.0, prm)
    assert b.policy == ExecPolicy.AGGRESSIVE and b.risk_aversion == 2.0
    c = apply_alpha_urgency(sell, 1.0, prm)
    assert c.policy == ExecPolicy.PASSIVE
    assert apply_alpha_urgency(sell, 0.1, prm) is sell
    with pytest.raises(ValueError):
        AlphaUrgencyParams(accelerate=0.5)


# ------------------------------------------------------------------ X3


def _events(profile: list[float], days: int, seed: int = 7) -> list[MarketEvent]:
    rng = random.Random(seed)
    session = (0, 10_000)
    width = (session[1] - session[0]) / len(profile)
    out = []
    eid = 0
    for d in range(days):
        for b, w in enumerate(profile):
            for _ in range(int(w * 2000)):
                eid += 1
                ts = d * DAY_NS + int(b * width + rng.random() * width)
                out.append(
                    MarketEvent(
                        eid,
                        1,
                        1,
                        ts,
                        ts,
                        eid,
                        int(EventType.TRADE),
                        0,
                        100,
                        rng.randint(1, 9),
                        0,
                        eid,
                    )
                )
    return out


def test_curve_sums_to_one_and_recovers_planted() -> None:
    planted = [0.3, 0.1, 0.05, 0.05, 0.1, 0.4]
    doc = estimate_volume_curves(_events(planted, 20), n_bins=6, session=(0, 10_000), shrinkage=0.0)
    c = doc["curves"]["1"]["curve"]
    assert sum(c) == pytest.approx(1.0)
    assert c == pytest.approx(planted, abs=0.02)
    shr = estimate_volume_curves(_events(planted, 2), n_bins=6, session=(0, 10_000), shrinkage=2.0)
    s = shr["curves"]["1"]
    assert s["weight_empirical"] == pytest.approx(0.5)
    assert sum(s["curve"]) == pytest.approx(1.0)


def test_vwap_curve_opt_in_and_cli(tmp_path) -> None:
    base = ParentOrder(algo=AlgoType.VWAP, qty=400, slices=4)
    assert slice_quantities(base) == [129, 71, 71, 129]  # pinned golden
    w = curve_slice_weights([0.5, 0.25, 0.25], 2)
    assert w == pytest.approx([0.625, 0.375])
    assert curve_slice_weights(u_shape(4), 4) == pytest.approx(u_shape(4))
    custom = ParentOrder(algo=AlgoType.VWAP, qty=400, slices=2, volume_curve=(0.5, 0.25, 0.25))
    assert slice_quantities(custom) == [250, 150]
    src = tmp_path / "ev.jsonl"
    src.write_text(
        "\n".join(json.dumps(e.to_dict()) for e in _events([0.5, 0.5], 1)) + "\n", encoding="utf-8"
    )
    out = tmp_path / "curve.json"
    rc = main(
        [
            "--events",
            str(src),
            "--out",
            str(out),
            "--bins",
            "2",
            "--session-start-ns",
            "0",
            "--session-end-ns",
            "10000",
        ]
    )
    assert rc == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["schema"] == "iap.volume_curve" and doc["version"] == 1
    assert sum(load_volume_curve(out, 1)) == pytest.approx(1.0)
    assert load_volume_curve(out, 99) == pytest.approx(tuple(u_shape(2)))
