"""Study summariser for per-session strategy studies (v1.13, review R-P1-5).

The maker / quoting / auction study drivers each summarised their own
output, and the review found three faults there: sessions with no
round trip were dropped from the estimand (so a strategy that rarely
traded was averaged over its good days only), confidence intervals were
reported on one or two degrees of freedom, and nothing said whether the
registered rule was reachable at the number of sessions available.

:func:`summarise_study` is the one helper future drivers call:

- **Estimand: per-session net P&L in USD.**  Every session in
  ``sessions`` counts; a session with no trip is ``0.0`` USD, never
  missing.  Its mean, standard deviation and t-statistic are over sessions.
- **Per-trip bps is descriptive only** (``per_trip``): mean / median net
  bps and the trip count; no interval and no test.
- **Intervals need n >= 3 sessions.**  Below :data:`MIN_SESSIONS_FOR_CI` the
  t interval and t-statistic are ``None`` ("not estimable"), not a 1-df
  interval.
- **Power statement.**  The minimum detectable mean per-session P&L at the
  given ``n`` (two-sided ``alpha``, ``power``; normal approximation, sd
  from the data unless ``sd_usd`` is given), and, when the registered rule
  is a t threshold, whether that threshold is reachable: the expected t at
  an effect ``e`` is ``e / (sd / sqrt(n))``.

Standard library + numpy only; no gate reads this.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from statistics import NormalDist
from typing import Any

import numpy as np

#: Fewer sessions than this: no interval, no t (``None``).
MIN_SESSIONS_FOR_CI = 3
DEFAULT_ALPHA = 0.05
DEFAULT_POWER = 0.80

# Two-sided 95 % Student-t quantiles by degrees of freedom (df 2..30);
# beyond 30 the normal quantile is used.
_T975 = {
    2: 4.302653,
    3: 3.182446,
    4: 2.776445,
    5: 2.570582,
    6: 2.446912,
    7: 2.364624,
    8: 2.306004,
    9: 2.262157,
    10: 2.228139,
    11: 2.200985,
    12: 2.178813,
    13: 2.160369,
    14: 2.144787,
    15: 2.131450,
    16: 2.119905,
    17: 2.109816,
    18: 2.100922,
    19: 2.093024,
    20: 2.085963,
    21: 2.079614,
    22: 2.073873,
    23: 2.068658,
    24: 2.063899,
    25: 2.059539,
    26: 2.055529,
    27: 2.051831,
    28: 2.048407,
    29: 2.045230,
    30: 2.042272,
}


def _t975(df: int) -> float:
    return _T975.get(int(df), NormalDist().inv_cdf(0.975))


def per_session_pnl(
    trips: Iterable[Mapping[str, Any]], sessions: Sequence[str], *, usd_key: str = "net_usd"
) -> dict[str, float]:
    """Net USD per session; every session in ``sessions`` present, sessions
    without a trip at ``0.0``.  A trip whose ``session`` is not listed is an
    error (it would silently leave the estimand)."""
    out = {str(s): 0.0 for s in sessions}
    if len(out) != len(sessions):
        raise ValueError("duplicate session in sessions")
    for t in trips:
        s = str(t["session"])
        if s not in out:
            raise ValueError(f"trip on session {s!r} which is not in sessions")
        out[s] += float(t[usd_key])
    return out


def mde(
    sd: float, n: int, *, alpha: float = DEFAULT_ALPHA, power: float = DEFAULT_POWER
) -> float | None:
    """Minimum detectable mean in the units of ``sd`` (the per-session standard
    deviation), two-sided ``alpha``, ``power``.

    v1.19 R2 (defect D-03): exact noncentral-t inversion with ``n - 1`` df via
    :func:`iap.validation.confirmatory.mde`, the one MDE implementation; the
    two-sided ``alpha`` is passed as the one-sided ``alpha / 2`` (the convention
    table in docs/RESEARCH_VALIDITY.md).  Up to v1.18 this was the normal
    approximation ``(z_{1-alpha/2} + z_{power}) * sd / sqrt(n)``, which
    understates the MDE at small ``n`` (at n = 4 by about a third).  ``None``
    when not estimable (``n < 2``, non-finite or non-positive ``sd``)."""
    if n < 2 or not math.isfinite(sd) or sd <= 0.0:
        return None
    from iap.validation.confirmatory import MDERefused
    from iap.validation.confirmatory import mde as _mde_sd

    try:
        return float(sd) * _mde_sd(int(n), alpha / 2.0, power)
    except MDERefused:
        return None


def summarise_study(
    trips: Iterable[Mapping[str, Any]],
    sessions: Sequence[str],
    *,
    usd_key: str = "net_usd",
    bps_key: str = "net_bps",
    alpha: float = DEFAULT_ALPHA,
    power: float = DEFAULT_POWER,
    sd_usd: float | None = None,
    registered_t: float | None = None,
) -> dict[str, Any]:
    """The study summary (module docs).  ``trips`` are round trips with
    ``session``, ``usd_key`` and ``bps_key``; ``sessions`` lists EVERY
    session the study ran on."""
    trips = list(trips)
    by_session = per_session_pnl(trips, sessions, usd_key=usd_key)
    x = np.array(list(by_session.values()), dtype=float)
    n = int(x.size)
    mean = float(x.mean()) if n else None
    sd = float(x.std(ddof=1)) if n >= 2 else None
    estimable = n >= MIN_SESSIONS_FOR_CI and sd is not None and sd > 0.0
    se = sd / math.sqrt(n) if estimable else None
    ci = None
    t_stat = None
    if estimable:
        half = _t975(n - 1) * se
        ci = [mean - half, mean + half]
        t_stat = mean / se
    power_sd = float(sd_usd) if sd_usd is not None else sd
    m = mde(power_sd, n, alpha=alpha, power=power) if power_sd is not None else None
    statement = (
        f"at n={n} sessions the minimum detectable mean per-session P&L "
        f"(alpha={alpha:g} two-sided, power={power:g}) is {m:.2f} USD"
        if m is not None
        else f"at n={n} sessions no power statement is possible (sd not estimable)"
    )
    reach: dict[str, Any] | None = None
    if registered_t is not None:
        # The effect needed for the EXPECTED t to reach the registered rule.
        needed = (
            float(registered_t) * power_sd / math.sqrt(n)
            if power_sd is not None and power_sd > 0 and n
            else None
        )
        reach = {
            "registered_t": float(registered_t),
            "effect_for_expected_t_usd": needed,
            "observed_t": t_stat,
            "reachable_at_observed_mean": (
                None if t_stat is None else bool(abs(t_stat) >= float(registered_t))
            ),
        }
        if n < MIN_SESSIONS_FOR_CI:
            reach["note"] = "fewer than 3 sessions: no t-statistic is estimable"
    bps = np.array([float(t[bps_key]) for t in trips], dtype=float)
    return {
        "estimand": "per_session_net_usd",
        "n_sessions": n,
        "n_sessions_without_trips": int(
            sum(1 for s in by_session if not any(str(t["session"]) == s for t in trips))
        ),
        "per_session_usd": by_session,
        "mean_usd": mean,
        "sd_usd": sd,
        "t_stat": t_stat,
        "ci95_usd": ci,
        "ci_estimable": bool(estimable),
        "power": {
            "alpha": float(alpha),
            "power": float(power),
            "sd_usd_used": power_sd,
            "mde_usd": m,
            "statement": statement,
            **({"registered_rule": reach} if reach is not None else {}),
        },
        "per_trip": {
            "descriptive_only": True,
            "n_trips": int(bps.size),
            "mean_net_bps": float(bps.mean()) if bps.size else None,
            "median_net_bps": float(np.median(bps)) if bps.size else None,
        },
    }
