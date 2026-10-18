"""REL-11 walk-forward under enforced limits: limits hold, loss stop halts and latches."""

from __future__ import annotations

import numpy as np
from iap.portfolio.rel11_enforced import RebalanceInput, run_enforced_book
from iap.risk.portfolio_limits import PortfolioGuard, PortfolioLimits, check_weights

IDS = [1, 2, 3, 4]
SECT = {1: "A", 2: "A", 3: "B", 4: "B"}
CAP = 1e6


def _steps(realised_scale: float, n: int = 6) -> list[RebalanceInput]:
    rng = np.random.default_rng(5)
    S = np.eye(4) * 1e-4
    return [
        RebalanceInput(
            ts=t,
            alpha=np.array([0.02, -0.01, 0.015, -0.02]),
            sigma=S,
            realised=realised_scale * np.array([-1.0, 1.0, -1.0, 1.0]) + rng.normal(0, 1e-5, 4),
            tc_linear=np.full(4, 1e-4),
            day_start=t == 0,
        )
        for t in range(n)
    ]


def test_every_traded_book_satisfies_the_limits() -> None:
    lim = PortfolioLimits("t", gross_max=1.0, net_max=0.05, name_max=0.3, sector_net_max=0.1)
    g = PortfolioGuard(lim, CAP)
    r = run_enforced_book(_steps(1e-4), g, IDS, 1.0, sectors=SECT)
    assert r.status.count("traded") == 6
    for w in r.weights:
        assert check_weights(lim, w, IDS, sectors=SECT, capital=CAP) == []
    assert r.summary()["limits_version"] == "t"


def test_loss_stop_halts_and_book_stops_trading() -> None:
    lim = PortfolioLimits("t", gross_max=1.0, name_max=0.3, daily_loss_limit=0.01)
    g = PortfolioGuard(lim, CAP)
    # the book goes long 1,3 and short 2,4; realised moves the other way 2 % a period
    r = run_enforced_book(_steps(0.02), g, IDS, 1.0)
    assert r.status[0] == "traded"
    assert "halted" in r.status
    k = r.status.index("halted")
    assert all(s == "halted" for s in r.status[k:])
    assert all(np.array_equal(w, r.weights[k - 1]) for w in r.weights[k:])  # no new trades
    assert g.halted and g.history[0]["event"] == "halt"
