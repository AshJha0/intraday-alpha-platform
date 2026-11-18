"""Three further cross-sectional families on one-minute bars (round 1, pre-specified).

The plain idea.  ``breadth_bars`` asks whether a 15-minute reversal earns more
than the quoted spread.  This module asks three further questions of the same
bars, universe and book, each fixed in ``research/round1/FAMILIES_PRESPEC_2026-11-18.md``
before any of them was run: does the last half hour continue (F1), does the
opening move reverse (F2), and does the closing auction's published imbalance
move the price into the cross (F3)?

Formal statement.  Bar ``m`` ends at 09:31 + m minutes; ``mid`` and ``hs``
(half spread over mid) are those of ``breadth_bars.Panel``.  A decision at the
end of bar ``k`` enters at bar ``e = k + delay`` (delay 1 is the primary) and
exits at bar ``x``; ``f = mid[x] / mid[e] - 1`` and ``c = hs[e] + hs[x]``.

* F1 ``momentum_30``: ``k = 30, 60, ..., 330``, ``x = e + 30``,
  score ``r - mean r`` with ``r = mid[k] / mid[k - 30] - 1``.
* F2 ``open_reversal``: ``k = 14`` (09:45), ``x = 44`` (10:15), score
  ``-(r - mean r)`` with ``r = mid[14] / mid[0] - 1``.  The overnight-gap
  version (``overnight_gap``) is defined only when the previous session's
  bars are given and that session is the previous weekday; it costs no look.
* F3 ``closing_imbalance``: ``k = 324`` (15:55), score ``d * imbalance / V``
  from the last closing NOII message at or before 15:55:00 (``d`` = +1 buy,
  -1 sell, 0 none; ``O`` undefined) and ``V`` the displayed volume of bars
  0..324; exit at the closing cross price, so ``c = hs[e]`` only.

The book and its cost are ``breadth_bars._book`` itself: long the top and short
the bottom decile, ``net = g - (mean_long c + mean_short c) / 2``.  The primary
statistic of a session is its mean net bps per rebalance (``session_net_bps``);
sessions are pooled with ``breadth_bars.pool_sessions``.

Not modelled: fees, impact, borrow, halts; for F3 the fill of the exit in the
cross is assumed (an upper bound, see the pre-specification).
"""

from __future__ import annotations

import datetime as _dt
import math
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pandas as pd

from iap.research import breadth_bars as bb

# The decile book and its spread arithmetic are taken from breadth_bars, not
# rewritten, so the families pay exactly the cost the reversal study pays.
from iap.research.breadth_bars import _book  # noqa: PLC2701

__all__ = [
    "DECILE_Q",
    "F1_LOOKBACK",
    "F2_DECISION",
    "F2_EXIT",
    "F3_DECISION",
    "F3_DECISION_NS",
    "FAMILIES",
    "closing_imbalance",
    "f1_score",
    "f2_gap_score",
    "f2_score",
    "f3_score",
    "is_previous_weekday",
    "load_auction",
    "momentum_30",
    "open_reversal",
    "overnight_gap",
]

FAMILIES = ("momentum_30", "open_reversal", "closing_imbalance")
DECILE_Q = 0.1
F1_LOOKBACK = F1_STEP = F1_HORIZON = 30
F2_START, F2_DECISION, F2_EXIT = 0, 14, 44  # 09:31, 09:45, 10:15
F2_GAP_FROM = 4  # bar ending 09:35
F3_DECISION = 324  # bar ending 15:55
OPEN_NS = (9 * 3600 + 30 * 60) * 1_000_000_000
F3_DECISION_NS = OPEN_NS + (F3_DECISION + 1) * bb.MINUTE_NS  # 15:55:00 since midnight
LAST_BAR = bb.BARS - 1
BPS = 1e4


# --------------------------------------------------------------------------- scores


def _demeaned(r: np.ndarray) -> np.ndarray:
    if not np.isfinite(r).any():
        return np.full(len(r), np.nan)
    return r - np.nanmean(r)


def f1_score(panel: bb.Panel, rows: np.ndarray, k: int) -> np.ndarray:
    """F1 score at bar end ``k``: cross-sectionally demeaned 30-bar return."""
    mid = panel.mid[rows]
    with np.errstate(invalid="ignore", divide="ignore"):
        r = mid[:, k] / mid[:, k - F1_LOOKBACK] - 1.0
    return _demeaned(r)


def f2_score(panel: bb.Panel, rows: np.ndarray) -> np.ndarray:
    """F2 score at 09:45: minus the demeaned 09:31 to 09:45 return."""
    mid = panel.mid[rows]
    with np.errstate(invalid="ignore", divide="ignore"):
        r = mid[:, F2_DECISION] / mid[:, F2_START] - 1.0
    return -_demeaned(r)


def _first_valid(mid: np.ndarray, lo: int, hi: int) -> np.ndarray:
    """Per row, the first finite value in columns ``lo..hi`` (NaN if none)."""
    win = mid[:, lo : hi + 1]
    ok = np.isfinite(win)
    first = np.argmax(ok, axis=1)
    out = win[np.arange(len(win)), first]
    return np.where(ok.any(axis=1), out, np.nan)


def _last_valid(mid: np.ndarray) -> np.ndarray:
    return _first_valid(mid[:, ::-1], 0, mid.shape[1] - 1)


def f2_gap_score(
    panel: bb.Panel, rows: np.ndarray, prev: bb.Panel, prev_rows: np.ndarray
) -> np.ndarray:
    """Overnight-gap score at 09:45: minus the demeaned gap from the previous
    session's last valid mid to today's first valid mid from 09:35 to 09:45.
    ``prev_rows`` must list the same names as ``rows``, in the same order
    (-1 for a name the previous session does not have)."""
    today = _first_valid(panel.mid[rows], F2_GAP_FROM, F2_DECISION)
    have = prev_rows >= 0
    last = np.full(len(rows), np.nan)
    if have.any():
        last[have] = _last_valid(prev.mid[prev_rows[have]])
    with np.errstate(invalid="ignore", divide="ignore"):
        g = today / last - 1.0
    return -_demeaned(g)


def load_auction(path: str | Path) -> pd.DataFrame:
    """Read an ``itchbars --auction`` CSV (prices converted to dollars)."""
    df = pd.read_csv(
        path,
        skiprows=1,
        dtype={"symbol": str, "kind": str, "direction": str, "cross_type": str},
        keep_default_na=False,
        na_values={
            c: [""]
            for c in (
                "paired_shares",
                "imbalance_shares",
                "far",
                "near",
                "reference",
                "price",
                "shares",
            )
        },
    )
    for c in ("far", "near", "reference", "price"):
        df[c] = df[c].astype(float) * bb.PRICE_UNIT
    return df


def f3_score(panel: bb.Panel, rows: np.ndarray, auction: pd.DataFrame) -> np.ndarray:
    """F3 score at 15:55: signed imbalance shares of the last closing NOII
    message at or before 15:55:00 over the displayed volume of bars 0..324."""
    imb = auction[
        (auction["kind"] == "I")
        & (auction["cross_type"] == "C")
        & (auction["ts_ns"] <= F3_DECISION_NS)
    ].sort_values("ts_ns", kind="stable")
    last = imb.groupby("symbol", sort=False).tail(1).set_index("symbol")
    sign = {"B": 1.0, "S": -1.0, "N": 0.0}
    vol = panel.vol[rows, : F3_DECISION + 1].sum(axis=1)
    out = np.full(len(rows), np.nan)
    for j, r in enumerate(rows):
        sym = panel.symbols[r]
        if sym not in last.index or vol[j] <= 0:
            continue
        row = last.loc[sym]
        d = sign.get(str(row["direction"]))
        if d is None:
            continue
        out[j] = d * float(row["imbalance_shares"]) / vol[j]
    return out


def _cross_prices(panel: bb.Panel, rows: np.ndarray, auction: pd.DataFrame) -> np.ndarray:
    q = auction[(auction["kind"] == "Q") & (auction["cross_type"] == "C")]
    price = q.groupby("symbol", sort=False)["price"].last().to_dict()
    return np.array([price.get(panel.symbols[r], np.nan) for r in rows], dtype=float)


# --------------------------------------------------------------------------- engine


def _run(
    family: str,
    params: dict,
    decisions: list[tuple[int, int, int]],
    score_at: Callable[[int], np.ndarray],
    fwd_cost: Callable[[int, int], tuple[np.ndarray, np.ndarray]],
) -> dict:
    """One session of a family: ``decisions`` are ``(k, entry, exit)``."""
    ic, gross, net, used, costs = [], [], [], [], []
    for k, e, x in decisions:
        fwd, cost = fwd_cost(e, x)
        if np.isfinite(cost).any():
            costs.append(float(np.nanmedian(cost)))
        s = score_at(k)
        g, n, u = _book(s, fwd, cost, DECILE_Q)
        ic.append(bb.rank_ic(s, fwd))
        gross.append(g * BPS)
        net.append(n * BPS)
        used.append(float(u))
    net_summary = bb.series_summary(net)
    return {
        "family": family,
        "params": params,
        "rebalances": len(decisions),
        "rank_ic": bb.series_summary(ic),
        "gross_bps_per_rebalance": bb.series_summary(gross),
        "net_bps_per_rebalance": net_summary,
        "session_net_bps": net_summary["mean"],
        "median_round_trip_cost_bps": float(np.median(costs) * BPS) if costs else math.nan,
        "median_names_used": float(np.median(used)) if used else math.nan,
        "per_rebalance": {"ic": ic, "gross_bps": gross, "net_bps": net},
    }


def _mid_fwd_cost(panel: bb.Panel, rows: np.ndarray):
    mid, hs = panel.mid[rows], panel.half_spread[rows]

    def fc(e: int, x: int) -> tuple[np.ndarray, np.ndarray]:
        with np.errstate(invalid="ignore", divide="ignore"):
            fwd = mid[:, x] / mid[:, e] - 1.0
        return fwd, hs[:, e] + hs[:, x]

    return fc


def momentum_30(panel: bb.Panel, rows: np.ndarray, delay: int = 1) -> dict:
    """F1 on one session (delay 1 primary, 0 diagnostic)."""
    ks = bb.decision_bars(F1_LOOKBACK, F1_STEP, delay, F1_HORIZON)
    return _run(
        "momentum_30",
        {
            "lookback": F1_LOOKBACK,
            "step": F1_STEP,
            "horizon": F1_HORIZON,
            "delay": delay,
            "decile_q": DECILE_Q,
        },
        [(k, k + delay, k + delay + F1_HORIZON) for k in ks],
        lambda k: f1_score(panel, rows, k),
        _mid_fwd_cost(panel, rows),
    )


def open_reversal(panel: bb.Panel, rows: np.ndarray, delay: int = 1) -> dict:
    """F2 (within-day primary) on one session."""
    return _run(
        "open_reversal",
        {
            "start": F2_START,
            "decision": F2_DECISION,
            "exit": F2_EXIT,
            "delay": delay,
            "decile_q": DECILE_Q,
        },
        [(F2_DECISION, F2_DECISION + delay, F2_EXIT)],
        lambda k: f2_score(panel, rows),
        _mid_fwd_cost(panel, rows),
    )


def is_previous_weekday(prev: str | _dt.date, day: str | _dt.date) -> bool:
    """True when ``prev`` is the weekday before ``day`` (Friday before a
    Monday).  No holiday calendar: a holiday in between makes it False, so a
    pair is missed rather than a wrong pair used."""
    p = _dt.date.fromisoformat(str(prev)) if not isinstance(prev, _dt.date) else prev
    d = _dt.date.fromisoformat(str(day)) if not isinstance(day, _dt.date) else day
    back = 3 if d.weekday() == 0 else 1
    return d.weekday() < 5 and p == d - _dt.timedelta(days=back)


def overnight_gap(
    panel: bb.Panel,
    rows: np.ndarray,
    prev: bb.Panel | None,
    prev_rows: np.ndarray | None,
    consecutive: bool,
    delay: int = 1,
) -> dict:
    """F2's overnight-gap version (no look).  ``{"defined": False}`` unless the
    previous session's bars are given and it is the previous session."""
    if prev is None or prev_rows is None or not consecutive:
        return {"family": "overnight_gap", "defined": False}
    out = _run(
        "overnight_gap",
        {
            "from": F2_GAP_FROM,
            "decision": F2_DECISION,
            "exit": F2_EXIT,
            "delay": delay,
            "decile_q": DECILE_Q,
        },
        [(F2_DECISION, F2_DECISION + delay, F2_EXIT)],
        lambda k: f2_gap_score(panel, rows, prev, prev_rows),
        _mid_fwd_cost(panel, rows),
    )
    out["defined"] = True
    return out


def closing_imbalance(
    panel: bb.Panel,
    rows: np.ndarray,
    auction: pd.DataFrame,
    delay: int = 1,
    exit_at: str = "cross",
) -> dict:
    """F3 on one session.  ``exit_at="cross"`` is the primary (exit at the
    closing cross price, cost ``hs[e]``); ``"mid"`` is the diagnostic exit at
    the 16:00 bar-end mid paying ``hs[e] + hs[389]``."""
    if exit_at not in ("cross", "mid"):
        raise ValueError("exit_at must be 'cross' or 'mid'")
    mid, hs = panel.mid[rows], panel.half_spread[rows]
    cross = _cross_prices(panel, rows, auction)
    score = f3_score(panel, rows, auction)

    def fc(e: int, x: int) -> tuple[np.ndarray, np.ndarray]:
        with np.errstate(invalid="ignore", divide="ignore"):
            if exit_at == "cross":
                return cross / mid[:, e] - 1.0, hs[:, e].copy()
            return mid[:, x] / mid[:, e] - 1.0, hs[:, e] + hs[:, x]

    out = _run(
        "closing_imbalance",
        {
            "decision": F3_DECISION,
            "decision_ns": F3_DECISION_NS,
            "delay": delay,
            "exit": exit_at,
            "normaliser": "displayed volume bars 0..324",
            "decile_q": DECILE_Q,
        },
        [(F3_DECISION, F3_DECISION + delay, LAST_BAR)],
        lambda k: score,
        fc,
    )
    out["coverage"] = float(np.isfinite(score).mean()) if len(rows) else math.nan
    out["names_with_score"] = int(np.isfinite(score).sum())
    return out
