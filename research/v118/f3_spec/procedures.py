"""F3 spec pilot: vectorised p-value procedures for the mean of G session values (one-sided, greater).

EXPLORATORY.  Columns of ``x`` (shape G x M) are independent hypotheses.  P0 reproduces
``iap.research.known_null.cluster_inference`` (tested equal on a sample in ``check_equal.py``).
"""

from __future__ import annotations

import itertools
import math
import sys
from functools import lru_cache
from pathlib import Path

import numpy as np
from scipy import stats

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "python" / "src"))
from iap.research import known_null as kn  # noqa: E402

SMALL_G = kn.SMALL_G


def cr1(x: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """mean, SE, t per column.  CR1 = CR2 = CR3 here (one observation per cluster, intercept only)."""
    g = x.shape[0]
    m = x.mean(axis=0)
    ss = ((x - m) ** 2).sum(axis=0)
    se = np.sqrt(g / (g - 1) * ss / (g * g))
    with np.errstate(divide="ignore", invalid="ignore"):
        t = np.where(se > 0, m / se, np.nan)
    return m, se, t


def cr3_se(x: np.ndarray) -> np.ndarray:
    """Jackknife (CR3) SE of the mean: sqrt((G-1)/G sum (mean_{-g} - mean)^2)."""
    g = x.shape[0]
    loo = (x.sum(axis=0) - x) / (g - 1)
    return np.sqrt((g - 1) / g * ((loo - x.mean(axis=0)) ** 2).sum(axis=0))


def cr2_se(x: np.ndarray) -> np.ndarray:
    """CR2: squared deviations divided by (1 - leverage), leverage 1/G."""
    g = x.shape[0]
    m = x.mean(axis=0)
    return np.sqrt((((x - m) ** 2) / (1 - 1 / g)).sum(axis=0)) / g


@lru_cache(maxsize=4)
def _webb(g: int) -> tuple[np.ndarray, np.ndarray]:
    return kn.webb_weights(g)


@lru_cache(maxsize=4)
def _signs(g: int) -> np.ndarray:
    return np.array(list(itertools.product((-1.0, 1.0), repeat=g)), dtype=float)


def p_student(t: np.ndarray, g: int) -> np.ndarray:
    return np.where(np.isfinite(t), stats.t.sf(np.nan_to_num(t), g - 1), np.nan)


def _enum_p(
    x: np.ndarray, t: np.ndarray, w: np.ndarray, w2: np.ndarray | None, chunk: int | None
) -> np.ndarray:
    """Share of weight vectors (rows of ``w``) whose studentised bootstrap t >= observed t."""
    g, mcols = x.shape
    chunk = chunk or max(8, int(8.0e6 // w.shape[0]))
    out = np.full(mcols, np.nan)
    for a in range(0, mcols, chunk):
        xs = x[:, a : a + chunk]
        mb = (w @ xs) / g
        s2 = (w2 @ (xs * xs)) if w2 is not None else (xs * xs).sum(axis=0)
        var_b = (s2 - g * mb * mb) * g / (g - 1) / (g * g)
        with np.errstate(divide="ignore", invalid="ignore"):
            tb = np.where(var_b > 0, mb / np.sqrt(np.maximum(var_b, 0.0)), -np.inf)
        tt = t[a : a + chunk]
        thr = tt - 1e-12 * np.maximum(1.0, np.abs(tt))
        out[a : a + chunk] = (tb >= thr[None, :]).sum(axis=0) / tb.shape[0]
    out[~np.isfinite(t)] = np.nan
    return out


def p_p0(x: np.ndarray, chunk: int | None = None) -> np.ndarray:
    """Current platform procedure: Webb full enumeration for G < SMALL_G, else Student t(G-1)."""
    g = x.shape[0]
    _, _, t = cr1(x)
    if g >= SMALL_G:
        return p_student(t, g)
    w, w2 = _webb(g)
    return _enum_p(x, t, w, w2, chunk)


def p_p1(x: np.ndarray) -> np.ndarray:
    """Student t(G-1) reference at every G (= CR2/CR3 with t(G-1))."""
    _, _, t = cr1(x)
    return p_student(t, x.shape[0])


def df_ik(x: np.ndarray) -> np.ndarray:
    """Satterthwaite df of the CR2 variance estimator, plug-in unequal variances (unclipped)."""
    g = x.shape[0]
    s = (x - x.mean(axis=0)) ** 2 / (1 - 1 / g)
    ssum = s.sum(axis=0)
    s2 = (s * s).sum(axis=0)
    tr = (1 - 1 / g) ** 2 * s2 + (ssum**2 - s2) / g**2
    c = 1.0 / (g * (g - 1))
    ev = c * (1 - 1 / g) * ssum
    var = c * c * 2 * tr
    with np.errstate(divide="ignore", invalid="ignore"):
        return 2 * ev * ev / var


def p_p2(x: np.ndarray) -> tuple[np.ndarray, dict]:
    g = x.shape[0]
    _, _, t = cr1(x)
    df = df_ik(x)
    ok = np.isfinite(df) & np.isfinite(t)
    dfc = np.clip(np.where(ok, df, g - 1), 1.0, g - 1.0)
    p = np.where(ok, stats.t.sf(np.nan_to_num(t), dfc), np.nan)
    info = {
        "df_nonfinite": int((~np.isfinite(df)).sum()),
        "df_below1": int((df < 1).sum()),
        "mean_df": float(np.mean(dfc)),
    }
    return p, info


def p_p3(x: np.ndarray, chunk: int | None = None) -> np.ndarray:
    """Rademacher wild, full 2^G enumeration of the studentised statistic."""
    _, _, t = cr1(x)
    return _enum_p(x, t, _signs(x.shape[0]), None, chunk)


def wilson(k: int, n: int, z: float = 1.959963984540054) -> list[float]:
    if n == 0:
        return [float("nan"), float("nan")]
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(c - h, 6), round(c + h, 6)]
