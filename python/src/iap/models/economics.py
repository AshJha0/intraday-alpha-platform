"""Economic evaluation of signals: P&L per signal through a cost model.

Spec §14 demands economic value, not only accuracy/AUC.  The platform's
event-time labels make this exact rather than approximate:

- ``y_mid``  : realized mid-to-mid forward return over the horizon;
- ``y_cost`` : realized cost-adjusted (buy-side) forward return, which embeds
  the actual half-spread paid at both ends;
- realized round-trip cost ``c = y_mid - y_cost`` (>= 0 by construction).

Realized net return of a signal at horizon h:

- long  : ``y_cost``            (= +y_mid - c)
- short : ``y_cost - 2*y_mid``  (= -y_mid - c)

Trading rule (pinned): the primary model predicts the buy-side cost-adjusted
return.  Go LONG when the prediction exceeds ``threshold``; go SHORT when the
implied sell-side prediction ``-(pred + cost_est) - cost_est`` exceeds
``threshold`` (``cost_est`` is the *observable* round-trip cost estimate at
decision time — no lookahead); otherwise stand aside.

CROSSED-BOOK CAVEAT (synthetic data reality, reported honestly): the
consolidated synthetic book can occasionally be CROSSED across venues
(< 2% of equity event states with the shared-efficient-price generator;
an earlier per-venue-mid generator left it crossed ~95% of the time), and a
crossed state makes the realized round-trip cost ``c`` *negative* — the
label-exact economics would then contain a synthetic crossed-book
"arbitrage" that no production system should be credited with.  Every
evaluation therefore exists in two pinned variants:

- ``conservative=True`` (default): cost floored at zero, ``c+ = max(c, 0)``
  — you are never *paid* to cross the spread;
- ``conservative=False``: label-exact costs, reported for transparency.
"""

from __future__ import annotations

from typing import Dict

import numpy as np


def signal_directions(pred: np.ndarray, cost_est: np.ndarray,
                      threshold: float = 0.0) -> np.ndarray:
    """Return -1/0/+1 trade directions for buy-side cost-adjusted predictions."""
    pred = np.asarray(pred, dtype=np.float64)
    cost_est = np.asarray(cost_est, dtype=np.float64)
    if pred.shape != cost_est.shape:
        raise ValueError("pred and cost_est must have identical shapes")
    long_net = pred                     # predicted net of going long
    short_net = -pred - 2.0 * cost_est  # implied net of going short
    d = np.zeros(pred.shape, dtype=np.int8)
    d[long_net > threshold] = 1
    d[(short_net > threshold) & (short_net > long_net)] = -1
    return d


def realized_net(direction: np.ndarray, y_mid: np.ndarray,
                 y_cost: np.ndarray,
                 conservative: bool = True) -> np.ndarray:
    """Realized net return per signal (0 where direction == 0).

    ``conservative`` floors the realized round-trip cost at zero (crossed
    synthetic books otherwise pay you to trade — see module docstring).
    """
    direction = np.asarray(direction, dtype=np.float64)
    y_mid = np.asarray(y_mid, dtype=np.float64)
    y_cost = np.asarray(y_cost, dtype=np.float64)
    cost = y_mid - y_cost
    if conservative:
        cost = np.maximum(cost, 0.0)
    return direction * y_mid - np.abs(direction) * cost


def signal_economics(
    pred: np.ndarray,
    y_mid: np.ndarray,
    y_cost: np.ndarray,
    cost_est: np.ndarray,
    threshold: float = 0.0,
    conservative: bool = True,
) -> Dict[str, float]:
    """P&L-per-signal summary of a prediction vector through the cost model."""
    d = signal_directions(pred, cost_est, threshold)
    net = realized_net(d, y_mid, y_cost, conservative=conservative)
    traded = d != 0
    n_trades = int(traded.sum())
    traded_net = net[traded]
    gross = np.asarray(d, dtype=np.float64) * np.asarray(y_mid)
    return {
        "conservative_costs": bool(conservative),
        "n_samples": int(len(pred)),
        "n_trades": n_trades,
        "n_long": int((d > 0).sum()),
        "n_short": int((d < 0).sum()),
        "total_net_bps": float(net.sum() * 1e4),
        "mean_net_bps_per_signal": (
            float(traded_net.mean() * 1e4) if n_trades else 0.0),
        "mean_gross_bps_per_signal": (
            float(gross[traded].mean() * 1e4) if n_trades else 0.0),
        "hit_rate": float((traded_net > 0).mean()) if n_trades else 0.0,
    }
