"""M1 models (ML protocol sections 4-5): ridge in closed form, elastic net by cyclic coordinate descent.

Both standardise the features with the development window's mean and standard
deviation, fit the label in bps, and expose the class-specific **complexity**
measure of the protocol (ridge: effective parameters ``trace(X (X'X + alpha
I)^-1 X')``; elastic net: non-zero coefficients).  Both implement the research
loop's :class:`~iap.research.loop.Forecaster` protocol (``predict`` on a
session's feature rows, ``model_class``, ``registry_id``, ``parameters``) so the
engine runs a candidate exactly as it runs M0.  Deterministic: no randomness in
either solver.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from iap.contracts.versions import content_hash

__all__ = ["ElasticNetModel", "RidgeModel", "design"]


def design(rows: pd.DataFrame, features: Sequence[str]) -> np.ndarray:
    x = rows[list(features)].to_numpy(dtype=float)
    return x


def _standardise(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    mu = x.mean(axis=0)
    sd = x.std(axis=0, ddof=1)
    sd = np.where(sd > 0, sd, 1.0)
    return mu, sd


@dataclass(slots=True)
class _Linear:
    features: tuple[str, ...]
    alpha: float
    coef: np.ndarray = field(default_factory=lambda: np.zeros(0))
    intercept: float = 0.0
    mu: np.ndarray = field(default_factory=lambda: np.zeros(0))
    sd: np.ndarray = field(default_factory=lambda: np.ones(0))
    resid_sd_bps: float = 0.0
    n: int = 0
    fitted: bool = False
    registry_id: str = "0" * 64
    model_class: str = "M1.linear"

    def _prepare(self, x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        ok = np.all(np.isfinite(x), axis=1) & np.isfinite(y)
        x, y = x[ok], y[ok]
        if x.shape[0] < max(30, 5 * x.shape[1]):
            raise ValueError(f"too few rows to fit ({x.shape[0]})")
        self.mu, self.sd = _standardise(x)
        self.n = int(x.shape[0])
        return (x - self.mu) / self.sd, y

    def predict(self, rows: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        x = design(rows, self.features)
        if not self.fitted:
            return np.full(x.shape[0], np.nan), np.full(x.shape[0], np.nan)
        z = (x - self.mu) / self.sd
        mu = self.intercept + z @ self.coef
        mu = np.where(np.all(np.isfinite(x), axis=1), mu, np.nan)
        unc = np.full(mu.shape, self.resid_sd_bps)
        return mu, unc

    def parameters(self) -> dict[str, Any]:
        return {
            "features": list(self.features),
            "alpha": self.alpha,
            "coef_bps_per_sd": [float(c) for c in self.coef],
            "intercept_bps": float(self.intercept),
            "feature_mean": [float(v) for v in self.mu],
            "feature_sd": [float(v) for v in self.sd],
            "resid_sd_bps": float(self.resid_sd_bps),
            "n": self.n,
            "complexity": float(self.complexity),
        }

    @property
    def complexity(self) -> float:  # pragma: no cover - overridden
        raise NotImplementedError

    def fit_hash(self) -> str:
        return content_hash({"class": self.model_class, **self.parameters()})


@dataclass(slots=True)
class RidgeModel(_Linear):
    """Ridge regression in closed form on standardised features; complexity = effective parameters."""

    df: float = 0.0
    model_class: str = "M1.ridge"

    def fit(self, x: np.ndarray, y: np.ndarray) -> RidgeModel:
        z, yy = self._prepare(x, y)
        p = z.shape[1]
        self.intercept = float(yy.mean())
        yc = yy - self.intercept
        a = z.T @ z + self.alpha * np.eye(p)
        self.coef = np.linalg.solve(a, z.T @ yc)
        hat_trace = float(np.trace(np.linalg.solve(a, z.T @ z)))
        self.df = hat_trace
        resid = yc - z @ self.coef
        self.resid_sd_bps = float(np.sqrt((resid @ resid) / max(self.n - 1, 1)))
        self.fitted = True
        return self

    @property
    def complexity(self) -> float:
        return float(self.df)


@dataclass(slots=True)
class ElasticNetModel(_Linear):
    """Elastic net by cyclic coordinate descent (the protocol's solver settings), complexity = non-zero coefficients.

    Objective (sklearn's convention): ``(1 / 2n) ||y - Xb||^2 + alpha * l1 * ||b||_1 + (alpha / 2) * (1 - l1) * ||b||^2``.
    """

    l1_ratio: float = 0.5
    max_iter: int = 10_000
    tol: float = 1e-6
    iterations: int = 0
    model_class: str = "M1.elastic_net"

    def fit(self, x: np.ndarray, y: np.ndarray) -> ElasticNetModel:
        z, yy = self._prepare(x, y)
        n, p = z.shape
        self.intercept = float(yy.mean())
        yc = yy - self.intercept
        b = np.zeros(p)
        l1 = self.alpha * self.l1_ratio
        l2 = self.alpha * (1.0 - self.l1_ratio)
        col_sq = (z * z).sum(axis=0) / n
        r = yc.copy()
        for it in range(1, self.max_iter + 1):
            self.iterations = it
            max_change = 0.0
            for j in range(p):  # cyclic selection: deterministic
                old = b[j]
                rho = (z[:, j] @ (r + z[:, j] * old)) / n
                new = np.sign(rho) * max(abs(rho) - l1, 0.0) / (col_sq[j] + l2)
                if new != old:
                    r -= z[:, j] * (new - old)
                    b[j] = new
                    max_change = max(max_change, abs(new - old))
            if max_change < self.tol:
                break
        self.coef = b
        resid = yc - z @ b
        self.resid_sd_bps = float(np.sqrt((resid @ resid) / max(n - 1, 1)))
        self.fitted = True
        return self

    @property
    def complexity(self) -> float:
        return float(np.count_nonzero(self.coef))

    def parameters(self) -> dict[str, Any]:
        return {
            **_Linear.parameters(self),
            "l1_ratio": self.l1_ratio,
            "iterations": self.iterations,
        }
