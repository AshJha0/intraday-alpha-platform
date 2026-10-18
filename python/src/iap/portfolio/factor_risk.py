"""Factor risk model: exposures, shrunk covariance, decomposition, VaR/ES, stress (v1.18 REL-17).

A linear factor model ``r = B f + e`` with ``Cov(f) = F`` and ``Cov(e) = D``
diagonal gives the asset covariance ``Sigma = B F B' + D``.  This module is
the research-side model the optimizer and the enforced limits
(:mod:`iap.risk.portfolio_limits`) read; it does not touch the pinned
order-level risk engine (:mod:`iap.risk.engine`, Rust parity).

**Exposures** (:func:`build_factor_exposures`, columns in this order):

- ``market``: the name's beta to its reference ETF (REL-07 ``xasset_v2``
  map, the features' ``beta_w5m_v1``); a missing beta reads as 1.0 and is
  listed in ``missing_beta``;
- ``sector:<name>``: one-hot sector membership from the REL-07 map's
  ``sectors`` (missing -> ``sector_unknown``);
- ``size:q<k>`` / ``liquidity:q<k>``: quantile-bucket dummies of a size
  proxy (market value or price x shares) and of ADV notional, with the
  FIRST bucket dropped as the baseline (the sector dummies already sum to
  one, so a full set of bucket dummies would be collinear with them).

**Estimation** (:func:`estimate_factor_model`): per period, the factor
returns are the cross-sectional least-squares fit ``f_t = argmin |r_t -
B f|`` (``lstsq``, minimum-norm on a rank-deficient ``B``); the residuals
give the specific variances ``D`` (mean square, a floor keeps them
positive).  ``F`` is the RiskMetrics EWMA of the factor returns
(:func:`iap.portfolio.covariance.ewma_covariance`, ``cov="ewma"``) or the
demeaned sample covariance (``cov="sample"``), optionally shrunk with the
estimated Ledoit-Wolf intensity (``shrink=True``; the same estimator the
combiner uses).  Units are those of the input returns (per bar or per day);
:meth:`FactorRiskModel.scaled` changes the horizon by ``sqrt(h)``.

**Decomposition** (:meth:`FactorRiskModel.decompose`): with ``x = B' w``,
``factor_var = x' F x``, ``specific_var = w' D w``; per-factor Euler
contributions ``x_k (F x)_k`` sum to ``factor_var`` and per-asset Euler
contributions ``w_i (Sigma w)_i`` sum to the total (Tasche 1999; Grinold &
Kahn 2000, ch. 3).

**VaR / ES** (losses positive, level ``a`` e.g. 0.99):
parametric normal ``VaR = z_a s - m``, ``ES = s phi(z_a) / (1 - a) - m``;
historical ``VaR = -Q_{1-a}(pnl)`` with the lower empirical quantile
(order statistic ``ceil(n (1 - a))``, no interpolation) and ``ES`` the
mean loss of the observations at or beyond it.

**Stress** (:class:`StressScenario`): a named set of factor moves (a market
shock enters through each name's beta); the scenario P&L is
``w' B s`` (plus optional per-name idiosyncratic moves).

Validation against published answers: ``docs/FACTOR_RISK.md``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from scipy.stats import norm

from iap.core.shrinkage import ledoit_wolf
from iap.portfolio.covariance import ewma_covariance
from iap.portfolio.exposures import SECTOR_UNKNOWN

__all__ = [
    "DEFAULT_STRESS_PATH",
    "FactorExposures",
    "FactorRiskModel",
    "StressScenario",
    "build_factor_exposures",
    "estimate_factor_model",
    "historical_var_es",
    "load_stress_scenarios",
    "parametric_var_es",
    "run_stress",
]

DEFAULT_STRESS_PATH = (
    Path(__file__).resolve().parents[4] / "configs" / "risk" / "stress_scenarios.json"
)
_SPECIFIC_FLOOR = 1e-12


@dataclass(frozen=True)
class FactorExposures:
    """``B`` (N x K) with column labels and provenance notes."""

    instrument_ids: tuple[int, ...]
    matrix: np.ndarray
    labels: tuple[str, ...]
    missing_beta: tuple[int, ...] = ()
    sector_of: Mapping[int, str] = field(default_factory=dict)

    def column(self, label: str) -> np.ndarray:
        return self.matrix[:, self.labels.index(label)]


def _bucket_dummies(
    values: np.ndarray, n_buckets: int, prefix: str
) -> tuple[list[np.ndarray], list[str]]:
    """Quantile buckets (ties broken by order), first bucket dropped."""
    n = len(values)
    if n_buckets < 2 or n < n_buckets:
        return [], []
    if not np.all(np.isfinite(values)) or np.any(values <= 0):
        raise ValueError(f"{prefix}: values must be finite and > 0")
    order = np.argsort(np.log(values), kind="stable")
    bucket = np.empty(n, dtype=int)
    bucket[order] = (np.arange(n) * n_buckets) // n
    cols = [(bucket == k).astype(np.float64) for k in range(1, n_buckets)]
    return cols, [f"{prefix}:q{k + 1}" for k in range(1, n_buckets)]


def build_factor_exposures(
    instrument_ids: Sequence[int],
    betas: Mapping[int, float] | None = None,
    sectors: Mapping[int, str] | None = None,
    size: Mapping[int, float] | None = None,
    adv_notional: Mapping[int, float] | None = None,
    n_size_buckets: int = 3,
    n_liquidity_buckets: int = 3,
) -> FactorExposures:
    """Build ``B`` for ``instrument_ids`` (row order = the given order; module docs)."""
    ids = tuple(int(i) for i in instrument_ids)
    n = len(ids)
    if n == 0 or len(set(ids)) != n:
        raise ValueError("instrument_ids must be non-empty and distinct")
    cols: list[np.ndarray] = []
    labels: list[str] = []
    missing: list[int] = []
    beta = np.ones(n)
    for j, iid in enumerate(ids):
        v = None if betas is None else betas.get(iid)
        if v is None or not math.isfinite(float(v)):
            missing.append(iid)
        else:
            beta[j] = float(v)
    cols.append(beta)
    labels.append("market")
    sector_of = {iid: (sectors or {}).get(iid) or SECTOR_UNKNOWN for iid in ids}
    for s in sorted(set(sector_of.values())):
        cols.append(np.array([1.0 if sector_of[i] == s else 0.0 for i in ids]))
        labels.append(f"sector:{s}")
    for vals, k, prefix in (
        (size, n_size_buckets, "size"),
        (adv_notional, n_liquidity_buckets, "liquidity"),
    ):
        if vals is None:
            continue
        arr = np.array([float(vals[i]) for i in ids])
        c, lab = _bucket_dummies(arr, k, prefix)
        cols += c
        labels += lab
    return FactorExposures(ids, np.column_stack(cols), tuple(labels), tuple(missing), sector_of)


@dataclass(frozen=True)
class FactorRiskModel:
    """``Sigma = B F B' + diag(specific_var)`` (module docs)."""

    exposures: np.ndarray  # B, (N, K)
    factor_cov: np.ndarray  # F, (K, K)
    specific_var: np.ndarray  # diag(D), (N,)
    factor_labels: tuple[str, ...]
    instrument_ids: tuple[int, ...] = ()
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        n, k = self.exposures.shape
        if self.factor_cov.shape != (k, k) or self.specific_var.shape != (n,):
            raise ValueError("factor model shapes disagree")
        if len(self.factor_labels) != k:
            raise ValueError("one label per factor")
        if np.any(self.specific_var < 0):
            raise ValueError("specific variances must be >= 0")

    @property
    def covariance(self) -> np.ndarray:
        B = self.exposures
        S = B @ self.factor_cov @ B.T + np.diag(self.specific_var)
        return 0.5 * (S + S.T)

    def scaled(self, horizon: float) -> FactorRiskModel:
        """The same model at ``horizon`` periods (variances x ``horizon``)."""
        if not horizon > 0:
            raise ValueError("horizon must be > 0")
        return FactorRiskModel(
            self.exposures,
            self.factor_cov * horizon,
            self.specific_var * horizon,
            self.factor_labels,
            self.instrument_ids,
            {**self.meta, "horizon_scale": float(horizon)},
        )

    def volatility(self, w: np.ndarray) -> float:
        """Ex-ante volatility ``sqrt(w' Sigma w)`` (same units as ``w``)."""
        w = np.asarray(w, dtype=np.float64)
        return float(math.sqrt(max(float(w @ self.covariance @ w), 0.0)))

    def decompose(self, w: np.ndarray) -> dict[str, Any]:
        """Factor / specific variance split with Euler contributions (module docs)."""
        w = np.asarray(w, dtype=np.float64)
        x = self.exposures.T @ w
        Fx = self.factor_cov @ x
        factor_var = float(x @ Fx)
        specific_var = float(np.sum(self.specific_var * w * w))
        total = factor_var + specific_var
        asset_ctr = w * (self.covariance @ w)
        return {
            "total_var": total,
            "total_vol": math.sqrt(max(total, 0.0)),
            "factor_var": factor_var,
            "specific_var": specific_var,
            "factor_share": factor_var / total if total > 0 else float("nan"),
            "factor_exposure": dict(zip(self.factor_labels, x.tolist(), strict=True)),
            "factor_contribution": dict(zip(self.factor_labels, (x * Fx).tolist(), strict=True)),
            "asset_contribution": asset_ctr.tolist(),
        }


def _factor_returns(returns: np.ndarray, B: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    pinv = np.linalg.pinv(B)  # (K, N): minimum-norm least squares
    f = returns @ pinv.T
    resid = returns - f @ B.T
    return f, resid


def estimate_factor_model(
    returns: np.ndarray,
    exposures: FactorExposures | np.ndarray,
    cov: str = "ewma",
    lam: float = 0.94,
    shrink: bool = True,
    labels: Sequence[str] | None = None,
) -> FactorRiskModel:
    """Fit ``F`` and ``D`` from a (T, N) return matrix (module docs)."""
    r = np.asarray(returns, dtype=np.float64)
    if isinstance(exposures, FactorExposures):
        B = exposures.matrix
        labs = exposures.labels
        ids = exposures.instrument_ids
    else:
        B = np.asarray(exposures, dtype=np.float64)
        labs = tuple(labels) if labels is not None else tuple(f"f{k}" for k in range(B.shape[1]))
        ids = ()
    if r.ndim != 2 or r.shape[1] != B.shape[0]:
        raise ValueError("returns must be (T, N) with N = rows of B")
    if r.shape[0] < 3 or not np.all(np.isfinite(r)):
        raise ValueError("need >= 3 finite return rows")
    f, resid = _factor_returns(r, B)
    delta = None
    if cov == "ewma":
        F = ewma_covariance(f, lam=lam, ridge=0.0)
        if shrink:
            delta = ledoit_wolf(f)[1]
            mu = float(np.trace(F)) / F.shape[0]
            F = (1.0 - delta) * F + delta * mu * np.eye(F.shape[0])
    elif cov == "sample":
        if shrink:
            F, delta, _ = ledoit_wolf(f)
        else:
            fc = f - f.mean(axis=0)
            F = fc.T @ fc / len(f)
    else:
        raise ValueError("cov must be 'ewma' or 'sample'")
    spec = np.maximum(np.mean(resid * resid, axis=0), _SPECIFIC_FLOOR)
    meta = {
        "cov": cov,
        "lam": lam if cov == "ewma" else None,
        "shrink_intensity": delta,
        "n_periods": int(r.shape[0]),
        "rank_B": int(np.linalg.matrix_rank(B)),
    }
    return FactorRiskModel(B, 0.5 * (F + F.T), spec, labs, ids, meta)


def parametric_var_es(
    sigma: float, level: float = 0.99, mean: float = 0.0, z: float | None = None
) -> dict[str, float]:
    """Normal VaR and ES of a P&L with s.d. ``sigma`` and mean ``mean`` (losses > 0).

    ``z`` overrides the exact quantile (textbooks often round ``z_0.99`` to
    2.33); the ES then uses the same ``z``."""
    if not 0.5 < level < 1.0:
        raise ValueError("level must be in (0.5, 1)")
    if not sigma >= 0:
        raise ValueError("sigma must be >= 0")
    if z is None:
        z = float(norm.ppf(level))
    return {
        "level": level,
        "z": z,
        "var": z * sigma - mean,
        "es": sigma * float(norm.pdf(z)) / (1.0 - level) - mean,
    }


def historical_var_es(pnl: Sequence[float], level: float = 0.99) -> dict[str, float]:
    """Historical VaR and ES (lower order statistic, no interpolation; module docs)."""
    x = np.sort(np.asarray(pnl, dtype=np.float64))
    n = len(x)
    if n == 0 or not np.all(np.isfinite(x)):
        raise ValueError("pnl must be non-empty and finite")
    if not 0.5 < level < 1.0:
        raise ValueError("level must be in (0.5, 1)")
    k = max(int(math.ceil(n * (1.0 - level) - 1e-12)), 1)
    var = -float(x[k - 1])
    return {"level": level, "n": n, "order_statistic": k, "var": var, "es": -float(np.mean(x[:k]))}


@dataclass(frozen=True)
class StressScenario:
    """Named factor moves (returns), plus optional per-name idiosyncratic moves."""

    name: str
    factor_moves: Mapping[str, float]
    description: str = ""
    specific_moves: Mapping[int, float] = field(default_factory=dict)


def load_stress_scenarios(path: str | Path | None = None) -> list[StressScenario]:
    """Read ``configs/risk/stress_scenarios.json`` (versioned; strict keys)."""
    p = Path(path) if path is not None else DEFAULT_STRESS_PATH
    doc = json.loads(p.read_text(encoding="utf-8"))
    if doc.get("x-version") != 1:
        raise ValueError(f"{p}: x-version must be 1")
    out = []
    for s in doc["scenarios"]:
        extra = set(s) - {"name", "description", "factor_moves", "specific_moves"}
        if extra:
            raise ValueError(f"{p}: scenario {s.get('name')}: unknown keys {sorted(extra)}")
        out.append(
            StressScenario(
                str(s["name"]),
                {str(k): float(v) for k, v in s["factor_moves"].items()},
                str(s.get("description", "")),
                {int(k): float(v) for k, v in s.get("specific_moves", {}).items()},
            )
        )
    return out


def run_stress(
    model: FactorRiskModel,
    w: np.ndarray,
    scenarios: Sequence[StressScenario],
    strict: bool = False,
) -> list[dict[str, Any]]:
    """Scenario P&L ``w' B s`` (+ specific moves).  A factor the model does not
    carry is skipped and listed (``strict=True`` raises instead)."""
    w = np.asarray(w, dtype=np.float64)
    x = model.exposures.T @ w
    pos = {lab: k for k, lab in enumerate(model.factor_labels)}
    col = {iid: j for j, iid in enumerate(model.instrument_ids)}
    out = []
    for sc in scenarios:
        pnl = 0.0
        by_factor: dict[str, float] = {}
        skipped = []
        for lab, move in sc.factor_moves.items():
            if lab not in pos:
                if strict:
                    raise ValueError(f"scenario {sc.name}: model has no factor {lab!r}")
                skipped.append(lab)
                continue
            c = float(x[pos[lab]] * move)
            by_factor[lab] = c
            pnl += c
        for iid, move in sc.specific_moves.items():
            if iid in col:
                pnl += float(w[col[iid]] * move)
        out.append(
            {"name": sc.name, "pnl": pnl, "by_factor": by_factor, "skipped_factors": skipped}
        )
    return out
