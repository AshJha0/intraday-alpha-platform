"""REL-17 enforced limits and kill switch: every limit refuses, the switch latches."""

from __future__ import annotations

import json

import numpy as np
import pytest
from iap.portfolio.enforced import constraints_from_limits, solve_enforced
from iap.risk.portfolio_limits import (
    LIMIT_KEYS,
    LimitRefused,
    PortfolioGuard,
    PortfolioLimits,
    check_signal_mix,
    check_weights,
)

IDS = [1, 2, 3, 4]
SECT = {1: "Tech", 2: "Tech", 3: "Energy", 4: "Energy"}
BETA = {1: 1.5, 2: 0.5, 3: 1.0, 4: 1.0}
ADV = {i: 1e8 for i in IDS}
CAP = 1e6


def only(**kw: float) -> PortfolioLimits:
    return PortfolioLimits("test_v1", **kw)


def names(breaches) -> set[str]:
    return {b.limit for b in breaches}


def test_committed_config_loads_and_round_trips() -> None:
    lim = PortfolioLimits.load()
    assert lim.limits_version == "portfolio_limits_v1"
    assert all(getattr(lim, k) is not None for k in LIMIT_KEYS)
    assert PortfolioLimits.from_dict(lim.to_dict()) == lim


@pytest.mark.parametrize(
    "doc",
    [
        {"x-version": 2, "limits_version": "v", "limits": {}},
        {"x-version": 1, "limits_version": "v", "limits": {"gross_mx": 1}},
        {"x-version": 1, "limits_version": "v", "limits": {"gross_max": -1}},
        {"x-version": 1, "limits_version": "v", "limits": {"gross_max": float("nan")}},
        {"x-version": 1, "limits_version": "v", "limits": {"gross_max": True}},
        {"x-version": 1, "limits_version": "", "limits": {}},
        {"x-version": 1, "limits_version": "v", "limits": {}, "extra": 1},
    ],
)
def test_bad_config_fails_closed(doc) -> None:
    with pytest.raises(ValueError):
        PortfolioLimits.from_dict(json.loads(json.dumps(doc)))


@pytest.mark.parametrize(
    ("limit", "w", "kw"),
    [
        ("gross_max", [0.5, -0.5, 0.5, -0.6], {"gross_max": 2.0}),
        ("net_max", [0.3, 0.0, 0.0, 0.0], {"net_max": 0.2}),
        ("name_max", [0.11, -0.05, 0.0, 0.0], {"name_max": 0.1}),
        ("sector_net_max", [0.1, 0.1, -0.1, -0.1], {"sector_net_max": 0.15}),
        ("sector_gross_max", [0.4, -0.4, 0.0, 0.0], {"sector_gross_max": 0.6}),
        ("beta_max", [0.2, 0.0, -0.1, 0.0], {"beta_max": 0.1}),
        ("participation_max", [0.02, 0.0, 0.0, 0.0], {"participation_max": 1e-4}),
    ],
)
def test_each_static_limit_breaches_and_passes(limit, w, kw) -> None:
    lim = only(**kw)
    args = dict(betas=BETA, sectors=SECT, adv_notional=ADV, capital=CAP)
    assert limit in names(check_weights(lim, np.array(w), IDS, **args))
    assert check_weights(lim, np.zeros(4), IDS, **args) == []
    g = PortfolioGuard(lim, CAP)
    with pytest.raises(LimitRefused) as e:
        g.check_weights(np.array(w), IDS, betas=BETA, sectors=SECT, adv_notional=ADV)
    assert e.value.limits_version == "test_v1" and not e.value.halted


def test_participation_without_adv_is_a_breach() -> None:
    b = check_weights(only(participation_max=0.5), np.array([0.01, 0, 0, 0]), IDS, capital=CAP)
    assert b[0].scope == "1:no_adv"


def test_signal_concentration_and_correlation() -> None:
    S = np.eye(4) * 1e-4
    sleeves = {"a": np.array([0.5, 0.0, 0.0, 0.0]), "b": np.array([0.1, 0.0, 0.0, 0.0])}
    b = check_signal_mix(only(signal_share_max=0.6, signal_corr_max=0.8), sleeves, S)
    assert ("signal_share_max", "a") in {(x.limit, x.scope) for x in b}
    assert ("signal_corr_max", "a~b") in {(x.limit, x.scope) for x in b}
    ok = {"a": np.array([0.3, 0, 0, 0]), "b": np.array([0, 0.3, 0, 0])}
    assert check_signal_mix(only(signal_share_max=0.6, signal_corr_max=0.8), ok, S) == []


class Sink:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool]] = []

    def report_halt(self, source: str, reason: str, active: bool) -> None:
        self.calls.append((source, reason, active))


def test_daily_loss_trips_latches_and_needs_explicit_reset() -> None:
    sink = Sink()
    g = PortfolioGuard(only(daily_loss_limit=0.02, gross_max=2.0), CAP, sink=sink)
    g.start_day(CAP)
    assert g.mark(CAP - 10_000) == []
    assert "daily_loss_limit" in names(g.mark(CAP - 25_000))
    assert g.halted and sink.calls[-1][2] is True
    for w in (np.zeros(4), np.array([0.1, -0.1, 0, 0])):
        with pytest.raises(LimitRefused) as e:
            g.check_weights(w, IDS)
        assert e.value.halted
    with pytest.raises(LimitRefused):
        g.check_order(1, 1_000.0, {})
    g.mark(CAP)  # recovery does not clear the latch
    g.start_day(CAP)  # nor does a new day
    assert g.halted
    with pytest.raises(ValueError):
        g.reset("", "x")
    g.reset("ops@desk", "loss reviewed")
    assert not g.halted and sink.calls[-1][2] is False
    g.check_weights(np.zeros(4), IDS)
    assert [h["event"] for h in g.history] == ["halt", "reset"]


def test_reset_refused_while_loss_still_breached() -> None:
    g = PortfolioGuard(only(daily_loss_limit=0.02), CAP)
    g.start_day(CAP)
    g.mark(CAP - 30_000)
    with pytest.raises(LimitRefused):
        g.reset("ops", "too early")
    assert g.halted


def test_drawdown_stop_from_peak() -> None:
    g = PortfolioGuard(only(drawdown_stop=0.05), CAP)
    g.start_day(CAP)
    g.mark(CAP + 40_000)
    g.start_day(CAP + 40_000)
    assert g.mark(CAP) == []  # 4 % from peak
    assert "drawdown_stop" in names(g.mark(CAP - 15_000))
    assert g.halted


def test_manual_halt_from_monitoring() -> None:
    sink = Sink()
    g = PortfolioGuard(only(), CAP, sink=sink)
    g.halt("REL-16 HALT: stale data")
    g.halt("again")  # idempotent
    assert sink.calls == [("portfolio_limits", "REL-16 HALT: stale data", True)]
    with pytest.raises(LimitRefused):
        g.check_weights(np.zeros(4), IDS)


def test_order_path_refuses_and_allows_risk_reducing() -> None:
    g = PortfolioGuard(only(name_max=0.1, gross_max=0.5), CAP)
    with pytest.raises(LimitRefused) as e:
        g.check_order(1, 150_000.0, {1: 0.0})
    assert names(e.value.breaches) == {"name_max"}
    g.check_order(1, 50_000.0, {})
    # book already over name_max on 2: reducing it is allowed, adding is not
    g.check_order(2, -50_000.0, {2: 200_000.0})
    with pytest.raises(LimitRefused):
        g.check_order(2, 10_000.0, {2: 200_000.0})


def _problem():
    rng = np.random.default_rng(3)
    A = rng.normal(size=(4, 4))
    Sigma = A @ A.T * 1e-4 + np.eye(4) * 1e-4
    alpha = np.array([0.05, -0.04, 0.03, 0.06])
    return alpha, Sigma


def test_optimizer_respects_linear_limits() -> None:
    lim = only(
        gross_max=1.0,
        net_max=0.05,
        name_max=0.3,
        beta_max=0.05,
        sector_net_max=0.1,
        participation_max=0.01,
    )
    g = PortfolioGuard(lim, CAP)
    alpha, Sigma = _problem()
    r = solve_enforced(
        alpha,
        Sigma,
        np.zeros(4),
        1.0,
        np.zeros(4),
        g,
        IDS,
        betas=BETA,
        sectors=SECT,
        adv_notional={i: 3e7 for i in IDS},
    )
    assert (
        check_weights(
            lim,
            r.weights,
            IDS,
            w_prev=np.zeros(4),
            betas=BETA,
            sectors=SECT,
            adv_notional={i: 3e7 for i in IDS},
            capital=CAP,
        )
        == []
    )
    assert np.abs(r.weights).max() <= 0.3 + 1e-8
    assert r.limits_version == "test_v1" and r.audit["feasible"]


def test_optimizer_output_breaching_nonlinear_limit_is_refused() -> None:
    lim = only(name_max=0.3, sector_gross_max=0.2)  # sector gross is checked, not projected
    alpha, Sigma = _problem()
    with pytest.raises(LimitRefused) as e:
        solve_enforced(
            alpha, Sigma, np.zeros(4), 0.1, np.zeros(4), PortfolioGuard(lim, CAP), IDS, sectors=SECT
        )
    assert "sector_gross_max" in names(e.value.breaches)


def test_optimizer_refuses_when_halted() -> None:
    g = PortfolioGuard(only(), CAP)
    g.halt("test")
    alpha, Sigma = _problem()
    with pytest.raises(LimitRefused):
        solve_enforced(alpha, Sigma, np.zeros(4), 1.0, np.zeros(4), g, IDS)


def test_no_adv_name_cannot_trade_in_optimizer() -> None:
    cons = constraints_from_limits(only(participation_max=0.01), IDS, CAP, adv_notional={1: 1e8})
    assert cons.participation is not None
    assert cons.participation[0] == pytest.approx(1.0) and cons.participation[1] == 0.0
