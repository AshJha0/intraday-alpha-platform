"""Probabilistic and deflated Sharpe ratios (v1.12, plan item R7; opt-in).

All Sharpe ratios here are PER PERIOD (per session day for the helpers
below), not annualised; the annualised figure is reported beside them.

- **PSR** (Bailey & López de Prado, "The Sharpe ratio efficient frontier",
  *J. Risk* 15(2), 2012): the probability that the true Sharpe exceeds a
  benchmark ``SR*`` given ``T`` observations with skewness ``g3`` and
  (non-excess) kurtosis ``g4``::

      PSR(SR*) = Phi( (SR - SR*) sqrt(T - 1)
                      / sqrt(1 - g3 SR + (g4 - 1) / 4 SR^2) )

- **Minimum track record length**: the ``T`` at which ``PSR(SR*)`` reaches
  ``1 - alpha``::

      MinTRL = 1 + (1 - g3 SR + (g4 - 1) / 4 SR^2) (z_{1-alpha} / (SR - SR*))^2

- **DSR** (Bailey & López de Prado, "The deflated Sharpe ratio",
  *J. Portfolio Management* 40(5), 2014): PSR with the benchmark set to the
  expected MAXIMUM Sharpe of ``N`` independent skill-less trials::

      SR0 = sqrt(V) ((1 - gamma) Z^-1(1 - 1/N) + gamma Z^-1(1 - 1/(N e)))

  with ``gamma`` the Euler-Mascheroni constant and ``V`` the variance of
  the Sharpe estimates across the trials.

**Which N (pinned, documented choice).**  The multiple-testing ledger
(:mod:`iap.validation.ledger`) counts LOOKS: one ``validate_alpha`` call
debits 83+ of them, most of which are diagnostics of the same fitted
configuration (decay per horizon, cost multipliers, regimes) and are
nearly perfectly correlated with each other.  DSR's ``N`` is the number of
INDEPENDENT trials a selection was made from, so:

- the headline ``dsr`` uses ``N = distinct configurations`` in the ledger
  (``ExperimentLedger.distinct_experiments`` — de-duplicated
  ``(alpha, kind, config, dataset)`` identities, counting the run being
  judged; :func:`effective_trials`).  Distinct configurations of the same
  alpha are still correlated, so this is an UPPER bound on the effective
  number of independent trials — conservative for DSR;
- the report also gives ``dsr_at_looks``, the same statistic at
  ``N = total looks``: the most conservative reading, a lower bound on DSR;
- a caller with a better estimate (e.g. the number of clusters of a
  correlation-clustered sweep) passes ``n_trials`` directly.

**Which V (pinned).**  Without the cross-trial Sharpe estimates, ``V`` is
the sampling variance of a Sharpe estimate under the null of no skill,
``1 / (T - 1)`` (the PSR denominator at ``SR = 0``): the trials are assumed
to be ``N`` skill-less configurations scored on the same ``T`` days.  A
caller that has the trials' Sharpes passes ``trials_sr_variance``.

Few observations (2-7 sessions is the platform's power bottleneck) make
``g3`` and ``g4`` themselves noisy: the block reports them, refuses ``T <
3`` (``None``) and never feeds any gate.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import numpy as np

from iap.validation.ledger import ExperimentLedger, _norm_ppf

EULER_GAMMA = 0.5772156649015329
#: Session days per year for the annualised Sharpe beside the per-day one.
DAYS_PER_YEAR = 252
#: Significance of the minimum track record length.
DEFAULT_MIN_TRL_ALPHA = 0.05
#: Fewer observations than this: no PSR / DSR (``None``).
MIN_OBSERVATIONS = 3


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def sharpe_moments(returns: Sequence[float]) -> dict[str, float]:
    """Per-period Sharpe, skewness and NON-excess kurtosis (population
    moments, as in the papers) and ``T``."""
    x = np.asarray(returns, dtype=float)
    x = x[np.isfinite(x)]
    t = int(x.size)
    if t < 2:
        return {"n": t, "sharpe": float("nan"), "skew": float("nan"), "kurtosis": float("nan")}
    mu = float(x.mean())
    sd_s = float(x.std(ddof=1))
    d = x - mu
    m2 = float(np.mean(d**2))
    skew = float(np.mean(d**3) / m2**1.5) if m2 > 0 else 0.0
    kurt = float(np.mean(d**4) / m2**2) if m2 > 0 else 3.0
    sharpe = mu / sd_s if sd_s > 0 else float("nan")
    return {"n": t, "sharpe": sharpe, "skew": skew, "kurtosis": kurt}


def _sr_denominator(sr: float, skew: float, kurtosis: float) -> float:
    return 1.0 - skew * sr + (kurtosis - 1.0) / 4.0 * sr * sr


def probabilistic_sharpe_ratio(
    sr: float, sr_benchmark: float, n: int, skew: float = 0.0, kurtosis: float = 3.0
) -> float:
    """PSR(SR*) from per-period moments (module docs)."""
    if n < 2:
        raise ValueError("n must be >= 2")
    den = _sr_denominator(sr, skew, kurtosis)
    if den <= 0:
        raise ValueError("non-positive SR variance term (inconsistent moments)")
    return norm_cdf((sr - sr_benchmark) * math.sqrt(n - 1) / math.sqrt(den))


def min_track_record_length(
    sr: float,
    sr_benchmark: float,
    skew: float = 0.0,
    kurtosis: float = 3.0,
    alpha: float = DEFAULT_MIN_TRL_ALPHA,
) -> float:
    """Observations needed for ``PSR(SR*) >= 1 - alpha`` (``inf`` when
    ``SR <= SR*``)."""
    if sr <= sr_benchmark:
        return math.inf
    z = _norm_ppf(1.0 - alpha)
    return 1.0 + _sr_denominator(sr, skew, kurtosis) * (z / (sr - sr_benchmark)) ** 2


def expected_max_sharpe(n_trials: int, trials_sr_variance: float) -> float:
    """``SR0``: E[max Sharpe] of ``n_trials`` independent skill-less trials
    whose Sharpe estimates have variance ``trials_sr_variance``.  0 for a
    single trial (no selection)."""
    n = int(n_trials)
    if n < 1:
        raise ValueError("n_trials must be >= 1")
    if trials_sr_variance < 0:
        raise ValueError("trials_sr_variance must be >= 0")
    if n == 1:
        return 0.0
    return math.sqrt(trials_sr_variance) * (
        (1.0 - EULER_GAMMA) * _norm_ppf(1.0 - 1.0 / n)
        + EULER_GAMMA * _norm_ppf(1.0 - 1.0 / (n * math.e))
    )


def deflated_sharpe_ratio(
    sr: float,
    n: int,
    n_trials: int,
    skew: float = 0.0,
    kurtosis: float = 3.0,
    trials_sr_variance: float | None = None,
) -> dict[str, float]:
    """DSR = PSR(SR0); ``trials_sr_variance`` defaults to ``1 / (n - 1)``
    (module docs, "Which V").  Returns ``dsr`` and ``sr0``."""
    v = 1.0 / (n - 1) if trials_sr_variance is None else float(trials_sr_variance)
    sr0 = expected_max_sharpe(n_trials, v)
    return {
        "dsr": probabilistic_sharpe_ratio(sr, sr0, n, skew, kurtosis),
        "sr0": sr0,
        "trials_sr_variance": v,
    }


def effective_trials(ledger: ExperimentLedger, include_new: bool = True) -> int:
    """``N`` for DSR from the ledger (module docs, "Which N"): the distinct
    configurations recorded, plus one for the run being judged when it is
    not recorded yet (``include_new``)."""
    return max(int(ledger.distinct_experiments) + (1 if include_new else 0), 1)


def _fnum(v: float) -> float | None:
    return float(v) if v is not None and math.isfinite(v) else None


def deflated_sharpe_block(
    daily_pnl: Sequence[float],
    n_trials: int,
    *,
    n_looks: int | None = None,
    trials_basis: str = "distinct_configurations",
    trials_sr_variance: float | None = None,
    sr_benchmark: float = 0.0,
    alpha: float = DEFAULT_MIN_TRL_ALPHA,
) -> dict[str, Any]:
    """The report block for one per-day P&L series — the helper the
    validation chain and the maker / quoting / auction studies call.

    ``n_trials`` is the DSR ``N`` and ``trials_basis`` says what it counts;
    ``n_looks`` (optional) adds ``dsr_at_looks``.  Report-only: no
    registered verdict rule reads it.  ``None`` statistics when there are
    fewer than :data:`MIN_OBSERVATIONS` days or the series has no variance.
    """
    mom = sharpe_moments(daily_pnl)
    n = int(mom["n"])
    sr = mom["sharpe"]
    out: dict[str, Any] = {
        "n_days": n,
        "n_trials": int(n_trials),
        "trials_basis": trials_basis,
        "sharpe_per_day": _fnum(sr),
        "sharpe_annualised": _fnum(sr * math.sqrt(DAYS_PER_YEAR)) if math.isfinite(sr) else None,
        "skew": _fnum(mom["skew"]),
        "kurtosis": _fnum(mom["kurtosis"]),
        "sr_benchmark": float(sr_benchmark),
        "psr": None,
        "dsr": None,
        "sr0": None,
        "min_track_record_days": None,
        "n_looks": None if n_looks is None else int(n_looks),
        "dsr_at_looks": None,
        "min_trl_alpha": float(alpha),
    }
    if n < MIN_OBSERVATIONS or not math.isfinite(sr):
        return out
    try:
        out["psr"] = probabilistic_sharpe_ratio(sr, sr_benchmark, n, mom["skew"], mom["kurtosis"])
        d = deflated_sharpe_ratio(sr, n, n_trials, mom["skew"], mom["kurtosis"], trials_sr_variance)
        out["dsr"], out["sr0"] = d["dsr"], d["sr0"]
        out["trials_sr_variance"] = d["trials_sr_variance"]
        out["min_track_record_days"] = _fnum(
            min_track_record_length(sr, sr_benchmark, mom["skew"], mom["kurtosis"], alpha)
        )
        if n_looks is not None:
            out["dsr_at_looks"] = deflated_sharpe_ratio(
                sr, n, n_looks, mom["skew"], mom["kurtosis"], trials_sr_variance
            )["dsr"]
    except ValueError:
        pass
    return out


def daily_pnl_from_bars(bar_ts: Sequence[int], bar_pnl: Sequence[float]) -> list[float]:
    """Sum bar P&L by UTC session day, in day order."""
    ts = np.asarray(bar_ts, dtype=np.int64)
    pnl = np.asarray(bar_pnl, dtype=float)
    if ts.size == 0:
        return []
    days = ts // (86_400 * 1_000_000_000)
    out: dict[int, float] = {}
    for d, p in zip(days.tolist(), pnl.tolist(), strict=True):
        out[d] = out.get(d, 0.0) + p
    return [out[d] for d in sorted(out)]


def study_deflated_sharpe(
    per_day: Any,
    n_trials: int,
    *,
    column: str = "net",
    **kwargs: Any,
) -> dict[str, Any]:
    """DSR block of a study's per-day table (e.g. the ``run_days`` frame of
    the quoting backtest, a maker or auction study's daily rows) or of a
    plain sequence of per-day net P&L.  The study's registered verdict rule
    is untouched: this is a block to report beside it."""
    if hasattr(per_day, "columns"):
        series = per_day[column].to_numpy(dtype=float)
    elif isinstance(per_day, list | tuple) and per_day and isinstance(per_day[0], dict):
        series = [float(r[column]) for r in per_day]
    else:
        series = per_day
    return deflated_sharpe_block(series, n_trials, **kwargs)
