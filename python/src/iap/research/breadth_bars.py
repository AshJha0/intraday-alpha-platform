"""Cross-sectional development study on one-minute bars (round 1, exploratory).

The plain idea.  The REL-08 screen asked whether a 15-minute cross-sectional
score survives the spread, on 100 names that needed the full feature store.
The ``itchbars`` tool (``rust/itchbars``) gives one-minute top-of-book bars for
every symbol of a Nasdaq ITCH day in minutes, so the same question can be put
to several hundred names.  This module is that question on bars: a universe
fixed by liquidity facts only, two scores, a decile book, and its return
before and after the quoted spread.

Formal statement.  Bars are indexed ``m = 0..389`` (bar ``m`` ends at
09:31 + m minutes).  ``mid[m]`` is the mid of the end-of-bar quote, missing
when a side is empty or the book is locked or crossed.  Decisions are taken
at bar ends ``k = lookback, lookback + step, ...`` while the exit bar exists.
With entry delay ``d`` bars and horizon ``h`` bars:

* forward return   ``f_i = mid_i[k + d + h] / mid_i[k + d] - 1``;
* round-trip cost  ``c_i = hs_i[k + d] + hs_i[k + d + h]``, ``hs`` the quoted
  half spread over the mid (the book crosses the spread to enter and to exit);
* scores at ``k`` (higher = buy):
  ``vwap_deviation``     ``-(mid[k] / vwap[k - lookback + 1 .. k] - 1)``;
  ``residual_reversion`` ``-(r_i - mean_j r_j)``, ``r = mid[k] / mid[k - lookback] - 1``;
* rank IC: Spearman correlation of score and ``f`` across names;
* decile book: equal weight long the top ``q`` of names and short the bottom
  ``q``; return on gross ``g = (mean_long f - mean_short f) / 2``.  Each unit
  of gross pays its own name's round trip, so
  ``net = g - (mean_long c + mean_short c) / 2``.

Everything is a per-rebalance series; the summary is its mean, standard error
and Student t.  With one session these are descriptive: sessions are the
independent unit (``iap.research.pnl_power``).

Not modelled: fees, market impact, halts, short-sale constraints.  The quote
is one venue's, so the spread is wider than the consolidated one.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

__all__ = [
    "BARS",
    "SCORES",
    "UNIVERSE_RULE",
    "Panel",
    "decision_bars",
    "load_panel",
    "rank_ic",
    "run_study",
    "scores_at",
    "select_universe",
    "series_summary",
]

BARS = 390
MINUTE_NS = 60_000_000_000
PRICE_UNIT = 1e-4
SCORES = ("vwap_deviation", "residual_reversion")
UNIVERSE_RULE = (
    "breadth_bars_universe/v1: symbols that are not exchange-traded products (etp flag not Y), "
    "with a median end-of-bar mid of at least {min_price} dollars and a valid two-sided quote "
    "(both sides present, ask above bid) for at least {min_quote_frac:.0%} of the session; "
    "ranked by displayed continuous-session notional; the top {top} are the universe. "
    "No return statistic enters the rule."
)


@dataclass(frozen=True)
class Panel:
    """Per-symbol arrays over the session's bars (rows: symbols, columns: bars)."""

    symbols: tuple[str, ...]
    etp: np.ndarray  # bool
    mid: np.ndarray  # dollars, NaN when no valid two-sided quote at the bar end
    half_spread: np.ndarray  # fraction of mid, NaN with mid
    vol: np.ndarray  # displayed shares
    notional: np.ndarray  # dollars
    valid_ns: np.ndarray  # ns of valid two-sided quote in the bar


def load_panel(path: str | Path) -> Panel:
    """Read an ``itchbars`` CSV into dense arrays (absent bars are empty)."""
    # The first line is the tool's "# itchbars/1 ..." note.  It is skipped by
    # position, not with comment="#": Nasdaq symbols may contain "#" (a
    # when-issued suffix), and a comment character would cut those rows short.
    df = pd.read_csv(path, skiprows=1, dtype={"symbol": str, "etp": str}, keep_default_na=False)
    syms, row = np.unique(df["symbol"].to_numpy(), return_inverse=True)
    col = df["minute"].to_numpy()
    shape = (len(syms), BARS)

    def dense(values: np.ndarray, fill: float = 0.0) -> np.ndarray:
        out = np.full(shape, fill)
        out[row, col] = values
        return out

    bid = dense(df["bid"].to_numpy(dtype=float))
    ask = dense(df["ask"].to_numpy(dtype=float))
    ok = (bid > 0) & (ask > bid)
    mid = np.where(ok, 0.5 * (bid + ask) * PRICE_UNIT, np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        hs = np.where(ok, (ask - bid) / (bid + ask), np.nan)
    etp = np.zeros(len(syms), dtype=bool)
    etp[row] = df["etp"].to_numpy() == "Y"
    return Panel(
        symbols=tuple(syms.tolist()),
        etp=etp,
        mid=mid,
        half_spread=hs,
        vol=dense(df["vol"].to_numpy(dtype=float)),
        notional=dense(df["notional"].to_numpy(dtype=float) * PRICE_UNIT),
        valid_ns=dense(df["tw_valid_ns"].to_numpy(dtype=float)),
    )


def select_universe(
    panel: Panel, top: int = 500, min_price: float = 5.0, min_quote_frac: float = 0.95
) -> np.ndarray:
    """Row indices of the universe, by the written rule (liquidity facts only),
    in rank order (largest displayed notional first; ties by symbol)."""
    with np.errstate(all="ignore"):
        med_mid = np.nanmedian(panel.mid, axis=1)
    quote_frac = panel.valid_ns.sum(axis=1) / (BARS * MINUTE_NS)
    eligible = (~panel.etp) & (med_mid >= min_price) & (quote_frac >= min_quote_frac)
    total = panel.notional.sum(axis=1)
    idx = np.flatnonzero(eligible)
    order = sorted(idx.tolist(), key=lambda i: (-total[i], panel.symbols[i]))
    return np.asarray(order[:top], dtype=np.intp)


def decision_bars(lookback: int, step: int, delay: int, horizon: int) -> list[int]:
    """Bar-end indices at which a decision is taken and the exit bar exists."""
    if min(lookback, step, horizon) < 1 or delay < 0:
        raise ValueError("lookback, step and horizon must be positive, delay non-negative")
    return [k for k in range(lookback, BARS, step) if k + delay + horizon < BARS]


def scores_at(panel: Panel, rows: np.ndarray, k: int, lookback: int) -> dict[str, np.ndarray]:
    """Both scores at bar end ``k`` for the given rows (NaN where undefined)."""
    mid = panel.mid[rows]
    win = slice(k - lookback + 1, k + 1)
    v = panel.vol[rows, win].sum(axis=1)
    n = panel.notional[rows, win].sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        vwap = np.where(v > 0, n / v, np.nan)
        dev = -(mid[:, k] / vwap - 1.0)
        r = mid[:, k] / mid[:, k - lookback] - 1.0
    resid = -(r - np.nanmean(r)) if np.isfinite(r).any() else np.full(len(rows), np.nan)
    return {"vwap_deviation": dev, "residual_reversion": resid}


def _ranks(x: np.ndarray) -> np.ndarray:
    return pd.Series(x).rank(method="average").to_numpy()


def rank_ic(score: np.ndarray, fwd: np.ndarray) -> float:
    """Spearman correlation over the names where both are finite (NaN if fewer than 3)."""
    ok = np.isfinite(score) & np.isfinite(fwd)
    if ok.sum() < 3:
        return math.nan
    a, b = _ranks(score[ok]), _ranks(fwd[ok])
    if a.std() == 0 or b.std() == 0:
        return math.nan
    return float(np.corrcoef(a, b)[0, 1])


def series_summary(x: Sequence[float]) -> dict[str, float]:
    """Mean, standard error and Student t of a per-rebalance series (NaN dropped)."""
    a = np.asarray([v for v in x if math.isfinite(v)], dtype=float)
    n = len(a)
    if n < 2:
        return {"n": n, "mean": float(a.mean()) if n else math.nan, "se": math.nan, "t": math.nan}
    se = float(a.std(ddof=1) / math.sqrt(n))
    mean = float(a.mean())
    return {"n": n, "mean": mean, "se": se, "t": mean / se if se > 0 else math.nan}


def _book(
    score: np.ndarray, fwd: np.ndarray, cost: np.ndarray, q: float
) -> tuple[float, float, int]:
    ok = np.isfinite(score) & np.isfinite(fwd) & np.isfinite(cost)
    n = int(ok.sum())
    side = max(1, int(math.floor(q * n)))
    if n < 2 * side or n < 10:
        return math.nan, math.nan, n
    idx = np.flatnonzero(ok)
    order = idx[np.argsort(score[idx], kind="stable")]
    short, long_ = order[:side], order[-side:]
    gross = 0.5 * (fwd[long_].mean() - fwd[short].mean())
    paid = 0.5 * (cost[long_].mean() + cost[short].mean())
    return float(gross), float(gross - paid), n


def run_study(
    panel: Panel,
    rows: np.ndarray,
    lookback: int = 15,
    step: int = 15,
    horizon: int = 15,
    delay: int = 1,
    decile_q: float = 0.1,
) -> dict:
    """Per-rebalance rank IC and decile-book returns for both scores."""
    ks = decision_bars(lookback, step, delay, horizon)
    mid, hs = panel.mid[rows], panel.half_spread[rows]
    out: dict = {
        "params": {
            "lookback": lookback,
            "step": step,
            "horizon": horizon,
            "delay": delay,
            "decile_q": decile_q,
        },
        "names": len(rows),
        "rebalances": len(ks),
        "scores": {},
    }
    series: dict[str, dict[str, list[float]]] = {
        s: {"ic": [], "gross": [], "net": [], "names": []} for s in SCORES
    }
    costs: list[float] = []
    for k in ks:
        e, x = k + delay, k + delay + horizon
        with np.errstate(invalid="ignore", divide="ignore"):
            fwd = mid[:, x] / mid[:, e] - 1.0
        cost = hs[:, e] + hs[:, x]
        if np.isfinite(cost).any():
            costs.append(float(np.nanmedian(cost)))
        sc = scores_at(panel, rows, k, lookback)
        for s in SCORES:
            g, n, used = _book(sc[s], fwd, cost, decile_q)
            series[s]["ic"].append(rank_ic(sc[s], fwd))
            series[s]["gross"].append(g)
            series[s]["net"].append(n)
            series[s]["names"].append(float(used))
    bps = 1e4
    for s in SCORES:
        ic = series_summary(series[s]["ic"])
        gross = series_summary([v * bps for v in series[s]["gross"]])
        net = series_summary([v * bps for v in series[s]["net"]])
        out["scores"][s] = {
            "rank_ic": ic,
            "gross_bps_per_rebalance": gross,
            "net_bps_per_rebalance": net,
            "median_names_used": float(np.median(series[s]["names"])) if ks else math.nan,
            "per_rebalance": {
                "ic": series[s]["ic"],
                "gross_bps": [v * bps for v in series[s]["gross"]],
                "net_bps": [v * bps for v in series[s]["net"]],
            },
        }
    out["median_round_trip_cost_bps"] = float(np.median(costs) * bps) if costs else math.nan
    return out
