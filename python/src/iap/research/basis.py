"""QQQ vs top-holdings basket: basis and lead-lag study (v1.15, A2).

Code only; no data is read here. The caller passes aligned prices (one row
per bar, one column per symbol, same clock). The symbol plan is
``docs/R7_DATA_PLAN.md`` §2: QQQ plus its top 15 holdings by weight.

Pipeline:

1. :func:`normalize_weights` renormalises the (partial, ~61%) weights to sum
   to one over the symbols actually present.
2. :func:`basket_index` builds a buy-and-hold basket price index from the
   first-bar prices (shares fixed at ``w_i / p_i(0)``), so it tracks the
   ETF's own holdings drift intrabar.
3. :func:`basis_bps` is ``1e4 * log(etf / (k * basket))`` with ``k`` fixing
   the basis to zero at the first bar (the level is not identified: the
   basket is a subset of the fund, so only changes in the basis matter).
4. :func:`residual_returns` regresses ETF log returns on basket log returns
   (beta fitted on the train slice only) and returns the residual.
5. :func:`lead_lag` gives cross-correlations ``corr(etf_t, basket_{t-k})``
   for ``k in [-K, K]``; positive ``k`` means the basket leads. The
   lead-lag ratio (Huth and Abergel, 2014) is ``sum_{k>0} rho_k^2 /
   sum_{k<0} rho_k^2``: above one, the basket leads.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd

ETF_SYMBOL = "QQQ"

#: Estimated QQQ weights (percent) of the top 15 holdings, docs/R7_DATA_PLAN.md
#: §2 (2026-10-09 close, rolled forward from the 2026-06-30 filing). Not
#: point-in-time: for 2019-2021 sessions pass point-in-time weights instead.
QQQ_TOP15_WEIGHTS_PCT: dict[str, float] = {
    "NVDA": 8.61,
    "AAPL": 7.63,
    "MSFT": 6.15,
    "GOOGL": 6.08,
    "MU": 4.94,
    "AMZN": 4.34,
    "AMD": 4.21,
    "META": 3.29,
    "TSLA": 2.95,
    "AVGO": 2.65,
    "WMT": 2.30,
    "INTC": 2.22,
    "PLTR": 2.02,
    "CSCO": 1.99,
    "COST": 1.78,
}


def study_symbols(weights: Mapping[str, float] | None = None) -> list[str]:
    """``[QQQ, *constituents by descending weight]``."""
    w = QQQ_TOP15_WEIGHTS_PCT if weights is None else weights
    return [ETF_SYMBOL, *sorted(w, key=lambda s: (-w[s], s))]


def normalize_weights(
    weights: Mapping[str, float], available: list[str] | None = None
) -> pd.Series:
    """Weights restricted to ``available`` symbols and rescaled to sum to 1."""
    w = pd.Series(dict(weights), dtype=float)
    if available is not None:
        w = w[w.index.isin(available)]
    if (w < 0).any():
        raise ValueError("negative weight")
    total = w.sum()
    if w.empty or total <= 0:
        raise ValueError("no positive weights for the available symbols")
    return (w / total).sort_index()


def basket_index(prices: pd.DataFrame, weights: Mapping[str, float]) -> pd.Series:
    """Buy-and-hold basket index, 1.0 at the first bar.

    ``index_t = sum_i w_i * p_i(t) / p_i(0)``. Rows with any missing
    constituent price raise: align and forward-fill upstream (causally).
    """
    w = normalize_weights(weights, list(prices.columns))
    px = prices[w.index]
    if px.isna().to_numpy().any():
        raise ValueError("missing constituent prices; align upstream")
    rel = px / px.iloc[0]
    return (rel * w).sum(axis=1).rename("basket")


def basis_bps(etf: pd.Series, basket: pd.Series) -> pd.Series:
    """ETF vs basket log basis in bps, zero at the first bar."""
    lr = np.log(etf / etf.iloc[0]) - np.log(basket / basket.iloc[0])
    return (1e4 * lr).rename("basis_bps")


def log_returns(x: pd.Series) -> pd.Series:
    return np.log(x).diff().dropna()


@dataclass(frozen=True)
class ResidualFit:
    alpha: float
    beta: float
    n_train: int
    residual: pd.Series


def residual_returns(
    etf_ret: pd.Series, basket_ret: pd.Series, train_frac: float = 0.5
) -> ResidualFit:
    """OLS ``etf = a + b * basket + e`` on the first ``train_frac`` rows; the
    residual is computed on all rows with that frozen fit (no look-ahead
    into the test slice)."""
    df = pd.concat([etf_ret.rename("e"), basket_ret.rename("b")], axis=1).dropna()
    n_train = int(len(df) * train_frac)
    if n_train < 3:
        raise ValueError("too few rows to fit")
    tr = df.iloc[:n_train]
    var = tr["b"].var(ddof=0)
    beta = float(((tr["b"] - tr["b"].mean()) * (tr["e"] - tr["e"].mean())).mean() / var)
    alpha = float(tr["e"].mean() - beta * tr["b"].mean())
    resid = (df["e"] - alpha - beta * df["b"]).rename("residual")
    return ResidualFit(alpha=alpha, beta=beta, n_train=n_train, residual=resid)


@dataclass(frozen=True)
class LeadLag:
    lags: np.ndarray  # k; positive = basket leads the ETF by k bars
    xcorr: np.ndarray
    ratio: float  # sum_{k>0} rho^2 / sum_{k<0} rho^2
    peak_lag: int


def _corr(a: np.ndarray, b: np.ndarray) -> float:
    if a.size < 3:
        return float("nan")
    sa, sb = a.std(), b.std()
    if sa == 0 or sb == 0:
        return 0.0
    return float(((a - a.mean()) * (b - b.mean())).mean() / (sa * sb))


def lead_lag(etf_ret: pd.Series, basket_ret: pd.Series, max_lag: int = 10) -> LeadLag:
    """``rho_k = corr(etf_t, basket_{t-k})`` for ``k = -max_lag .. max_lag``."""
    df = pd.concat([etf_ret.rename("e"), basket_ret.rename("b")], axis=1).dropna()
    e, b = df["e"].to_numpy(), df["b"].to_numpy()
    n = e.size
    lags = np.arange(-max_lag, max_lag + 1)
    rho = np.empty(lags.size)
    for i, k in enumerate(lags):
        if k >= 0:
            rho[i] = _corr(e[k:], b[: n - k])
        else:
            rho[i] = _corr(e[: n + k], b[-k:])
    pos = np.nansum(rho[lags > 0] ** 2)
    neg = np.nansum(rho[lags < 0] ** 2)
    ratio = float(pos / neg) if neg > 0 else float("inf")
    peak = int(lags[np.nanargmax(np.abs(rho))])
    return LeadLag(lags=lags, xcorr=rho, ratio=ratio, peak_lag=peak)


@dataclass(frozen=True)
class BasisStudy:
    weights: pd.Series
    basket: pd.Series
    basis_bps: pd.Series
    residual: ResidualFit
    lead_lag: LeadLag
    basis_half_life_bars: float

    def summary(self) -> dict[str, float | int]:
        return {
            "n_constituents": int(self.weights.size),
            "beta": self.residual.beta,
            "basis_std_bps": float(self.basis_bps.std(ddof=0)),
            "basis_half_life_bars": self.basis_half_life_bars,
            "lead_lag_ratio": self.lead_lag.ratio,
            "peak_lag": self.lead_lag.peak_lag,
        }


def half_life(x: pd.Series) -> float:
    """AR(1) half-life of ``x`` in bars (``inf`` if not mean-reverting)."""
    a = x.to_numpy()
    y, z = a[1:], a[:-1]
    zc = z - z.mean()
    denom = (zc**2).sum()
    if denom == 0:
        return float("inf")
    phi = float((zc * (y - y.mean())).sum() / denom)
    if not 0 < phi < 1:
        return float("inf")
    return float(-np.log(2) / np.log(phi))


def run_basis_study(
    prices: pd.DataFrame,
    weights: Mapping[str, float] | None = None,
    *,
    etf: str = ETF_SYMBOL,
    max_lag: int = 10,
    train_frac: float = 0.5,
) -> BasisStudy:
    """End-to-end: basket, basis, residual and lead-lag on aligned bars."""
    w_in = QQQ_TOP15_WEIGHTS_PCT if weights is None else weights
    cons = [c for c in prices.columns if c != etf]
    w = normalize_weights(w_in, cons)
    basket = basket_index(prices[cons], w)
    basis = basis_bps(prices[etf], basket)
    er, br = log_returns(prices[etf]), log_returns(basket)
    return BasisStudy(
        weights=w,
        basket=basket,
        basis_bps=basis,
        residual=residual_returns(er, br, train_frac),
        lead_lag=lead_lag(er, br, max_lag),
        basis_half_life_bars=half_life(basis),
    )
