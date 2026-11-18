"""Session-level P&L power: how many sessions a net-of-cost Sharpe needs.

The plain idea.  A strategy's net P&L is one number per session, and sessions
are the independent unit.  A test of "mean session P&L > 0" on ``S`` sessions
can only see an annualised Sharpe above a floor that falls like ``1/sqrt(S)``.
This module writes that floor down so that no study is run which cannot
succeed.

Formal statement.  Session P&L ``x_1..x_S`` i.i.d. normal with mean ``mu`` and
sd ``sigma``; the per-session Sharpe is ``mu/sigma`` and the annualised Sharpe
``SR = (mu/sigma) * sqrt(sessions_per_year)``.  The statistic is the one-sample
Student t, ``T = mean(x) / (sd(x)/sqrt(S))``, rejected one-sided at level
``alpha/K`` (Bonferroni over ``K`` registered tests).  Under the alternative
``T`` is noncentral t with ``S - 1`` degrees of freedom and noncentrality
``delta = SR * sqrt(S / sessions_per_year)``, so

    power(SR, S) = P(T_{S-1}(delta) > t_{S-1, 1 - alpha/K}).

Assumptions, stated because they flatter the answer: independent sessions (no
P&L autocorrelation across days), normal session P&L (fat tails need more
sessions), a single fixed strategy (selection among variants is what ``K``
is for).  Everything here is a pure function: no I/O, no market data.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from scipy import stats

from iap.validation.confirmatory import mde_at_t, power_at_t

__all__ = [
    "PNL_POWER_VERSION",
    "SESSIONS_PER_YEAR",
    "critical_t",
    "mde_sharpe",
    "power",
    "render_markdown",
    "sessions_needed",
    "table",
]

PNL_POWER_VERSION = 1
SESSIONS_PER_YEAR = 252
#: Largest session count searched by :func:`sessions_needed` (about 40 years).
MAX_SESSIONS = 10_000


def _check(sessions: int, alpha: float, n_tests: int) -> None:
    if sessions < 2:
        raise ValueError("sessions must be at least 2 (the t test needs a variance)")
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be in (0, 1)")
    if n_tests < 1:
        raise ValueError("n_tests must be at least 1")


def critical_t(sessions: int, alpha: float = 0.025, n_tests: int = 1) -> float:
    """One-sided Student-t critical value at ``alpha / n_tests`` on ``sessions - 1`` df."""
    _check(sessions, alpha, n_tests)
    return float(stats.t.ppf(1.0 - alpha / n_tests, sessions - 1))


def power(
    sharpe_annual: float,
    sessions: int,
    alpha: float = 0.025,
    n_tests: int = 1,
    sessions_per_year: int = SESSIONS_PER_YEAR,
) -> float:
    """Probability that the session-level t test rejects when the true
    annualised Sharpe is ``sharpe_annual``.  The noncentral t is evaluated by
    :func:`iap.validation.confirmatory.power_at_t`, the one place the
    repository does so."""
    crit = critical_t(sessions, alpha, n_tests)
    delta = sharpe_annual / math.sqrt(sessions_per_year)  # per-session Sharpe, in sd units
    return power_at_t(sessions, crit, delta)


def sessions_needed(
    sharpe_annual: float,
    target_power: float = 0.8,
    alpha: float = 0.025,
    n_tests: int = 1,
    sessions_per_year: int = SESSIONS_PER_YEAR,
) -> int | None:
    """Smallest ``S`` with ``power >= target_power``; ``None`` if none up to
    :data:`MAX_SESSIONS` (or the Sharpe is not positive)."""
    if sharpe_annual <= 0.0:
        return None

    def ok(s: int) -> bool:
        return power(sharpe_annual, s, alpha, n_tests, sessions_per_year) >= target_power

    if not ok(MAX_SESSIONS):
        return None
    lo, hi = 2, MAX_SESSIONS  # power is increasing in S for a positive Sharpe
    while lo < hi:
        mid = (lo + hi) // 2
        if ok(mid):
            hi = mid
        else:
            lo = mid + 1
    return lo


def mde_sharpe(
    sessions: int,
    target_power: float = 0.8,
    alpha: float = 0.025,
    n_tests: int = 1,
    sessions_per_year: int = SESSIONS_PER_YEAR,
) -> float:
    """Minimum detectable annualised Sharpe on ``sessions`` sessions: the
    repository's one noncentral-t inversion
    (:func:`iap.validation.confirmatory.mde_at_t`, in per-session sd units)
    scaled to a year."""
    crit = critical_t(sessions, alpha, n_tests)
    return mde_at_t(sessions, crit, target_power) * math.sqrt(sessions_per_year)


def table(
    sharpes: Sequence[float] = (0.5, 1.0, 1.5, 2.0, 3.0, 5.0),
    session_counts: Sequence[int] = (7, 19, 60, 125, 250, 500),
    n_tests_grid: Sequence[int] = (1, 10, 100),
    target_power: float = 0.8,
    alpha: float = 0.025,
) -> dict:
    """The two tables of the report as plain data."""
    return {
        "schema": f"iap.research.pnl_power/{PNL_POWER_VERSION}",
        "alpha": alpha,
        "target_power": target_power,
        "sessions_per_year": SESSIONS_PER_YEAR,
        "sessions_needed": [
            {
                "sharpe": sr,
                **{f"K{k}": sessions_needed(sr, target_power, alpha, k) for k in n_tests_grid},
            }
            for sr in sharpes
        ],
        "mde_sharpe": [
            {
                "sessions": s,
                **{f"K{k}": round(mde_sharpe(s, target_power, alpha, k), 2) for k in n_tests_grid},
            }
            for s in session_counts
        ],
        "n_tests_grid": list(n_tests_grid),
    }


def render_markdown(res: dict) -> str:
    ks = res["n_tests_grid"]
    pct = f"{100 * res['target_power']:.0f} %"
    lines = [
        "# Sessions needed against net Sharpe (session-level P&L test)",
        "",
        "Generated by `tools/pnl_power_table.py` (`iap.research.pnl_power`, version "
        f"{PNL_POWER_VERSION}); rerun it, do not edit the tables by hand. No market data is read.",
        "",
        "**The plain idea.** Net P&L is one number per session. A test on S sessions can only",
        "see a Sharpe above a floor that falls like 1/sqrt(S). Below the floor a study cannot",
        "succeed, whatever the signal, so it is not run.",
        "",
        "**Formal statement.** One-sided one-sample Student t on S i.i.d. normal session P&Ls,",
        f"level {res['alpha']} divided by K registered tests (Bonferroni), power {pct}, "
        f"{res['sessions_per_year']} sessions a year. Noncentrality: Sharpe x sqrt(S / "
        f"{res['sessions_per_year']}).",
        "",
        f"## Sessions needed for {pct} power",
        "",
        "| annualised net Sharpe | " + " | ".join(f"K = {k}" for k in ks) + " |",
        "|---:|" + "---:|" * len(ks),
    ]
    for row in res["sessions_needed"]:
        cells = [("more than 10,000" if row[f"K{k}"] is None else f"{row[f'K{k}']:,}") for k in ks]
        lines.append(f"| {row['sharpe']:g} | " + " | ".join(cells) + " |")
    lines += [
        "",
        f"## Smallest Sharpe detectable with {pct} power",
        "",
        "| sessions | " + " | ".join(f"K = {k}" for k in ks) + " |",
        "|---:|" + "---:|" * len(ks),
    ]
    for row in res["mde_sharpe"]:
        lines.append(
            f"| {row['sessions']:,} | " + " | ".join(f"{row[f'K{k}']:.2f}" for k in ks) + " |"
        )
    lines += [
        "",
        "## Worked example",
        "",
        "A strategy with a true net Sharpe of 2 has a per-session Sharpe of 2 / sqrt(252) = 0.126.",
        "On 19 sessions the noncentrality is 2 x sqrt(19 / 252) = 0.55 against a critical value",
        "of 2.10, so the test almost never rejects. The first table gives the session count at",
        "which it rejects four times in five.",
        "",
        "## What this assumes, and which way each assumption errs",
        "",
        "- Independent sessions. Positive day-to-day P&L correlation needs more sessions.",
        "- Normal session P&L. Fat tails need more sessions.",
        "- K counts every variant tried on the same data, not only the ones reported.",
        "- The table is about detection only. It says nothing about capacity or costs: the",
        "  Sharpe here is net of the cost model the study registers.",
        "",
        "## Stop rule (Round 0, 2026-11-18)",
        "",
        "A confirmatory study is registered only if its sealed session count is at or above the",
        "first table's entry for the Sharpe it claims to look for and the K it registers. On the",
        "seven and nineteen session samples used so far, only a Sharpe in the second table's",
        "first two rows was ever detectable.",
        "",
    ]
    return "\n".join(lines)
