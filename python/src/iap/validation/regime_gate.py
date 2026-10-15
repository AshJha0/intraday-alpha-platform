"""Regime as a gate input (v1.17 C5), in ``gate_statistics``-compatible form.

The research reports split the IC by the volatility regime for
information only (:func:`iap.validation.stress.regime_split`); no gate
reads it, so an alpha that owes its whole IC to one regime passes the
same gates as one that works in every state.  This module computes, for
each regime, the SAME statistic every gate reads
(:func:`iap.validation.metrics.gate_statistics`: pooled IC, pooled-slope
HAC t with the automatic bandwidth and no lag across a day, the
day-cluster t, pair and day counts) and derives one gate decision from
them.  It is opt-in: ``validate_alpha`` and the lifecycle are unchanged;
a pipeline that wants the regime gate calls :func:`regime_gate` on the
rows its own gate reads and records the block beside its gates.

**Regime label.**  :func:`iap.alpha.regime_conditioned.regime_labels`
(declared flag features read as bits; ``-1`` unknown).  Unknown rows are
excluded from every per-regime block and counted in ``n_unknown``.

**Statistics** (:func:`regime_gate_statistics`): ``{"pooled":
gate_statistics(all known rows), "by_regime": {str(r): gate_statistics(rows
of r)}, "n_unknown", "regime_features", "min_pairs"}``.  A regime with
fewer than ``min_pairs`` finite pairs is reported but NOT judged
(``"judged": False``): too few rows is no evidence either way.

**Gate** (:func:`regime_gate`): PASS when every judged regime has ``ic >
min_ic`` and ``tstat_hac >= min_t`` (defaults ``min_ic = 0.0``, ``min_t
= 0.0``: the sign must hold in every regime; a caller that wants
significance per regime raises ``min_t``), at least ``min_regimes``
regimes were judged, and the worst judged regime's IC is at least
``max_ic_ratio`` of the pooled IC (default 0: off) — the check that no
single regime carries the alpha.  Returns ``{"passed", "worst_regime",
"worst_ic", "judged", "failed", "thresholds"}``; ``passed`` is ``False``
with ``failed = ["no_regime_judged"]`` when nothing could be judged.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np
import pandas as pd

from iap.alpha.regime_conditioned import DEFAULT_REGIME_FEATURES, regime_labels
from iap.validation.metrics import NS_S, gate_statistics

__all__ = [
    "DEFAULT_MIN_PAIRS",
    "regime_gate",
    "regime_gate_statistics",
    "regime_rows",
]

DEFAULT_MIN_PAIRS = 256


def regime_rows(
    frames: dict[int, pd.DataFrame],
    scores: dict[int, pd.DataFrame],
    horizon: str,
    features: Sequence[str] = DEFAULT_REGIME_FEATURES,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """``(ts, z, label, regime)`` pooled over the instruments of ``scores``:
    the standardised score where confidence > 0 (``expected_return``
    divided by nothing — the caller's scores are scaled as its gate
    wants), the valid mid label at ``horizon`` and the regime label."""
    ts, xs, ys, rs = [], [], [], []
    for iid, sc in scores.items():
        df = frames[iid]
        er = sc["expected_return"].to_numpy(dtype=float).copy()
        er[sc["confidence"].to_numpy(dtype=float) <= 0.0] = np.nan
        y = df[f"label_mid_{horizon}"].to_numpy(dtype=float).copy()
        y[~df[f"label_valid_{horizon}"].to_numpy(dtype=bool)] = np.nan
        ts.append(df["exchange_ts"].to_numpy(dtype=np.int64))
        xs.append(er)
        ys.append(y)
        rs.append(regime_labels(df, features))
    if not ts:
        e = np.empty(0)
        return np.empty(0, np.int64), e, e, np.empty(0, np.int64)
    return np.concatenate(ts), np.concatenate(xs), np.concatenate(ys), np.concatenate(rs)


def regime_gate_statistics(
    ts: np.ndarray,
    scores: np.ndarray,
    labels: np.ndarray,
    regime: np.ndarray,
    *,
    horizon_ns: int,
    regime_features: Sequence[str] = DEFAULT_REGIME_FEATURES,
    min_pairs: int = DEFAULT_MIN_PAIRS,
    hac_bandwidth: str = "auto",
    day_separated: bool = True,
    bucket_ns: int = 300 * NS_S,
    day_ns: int = 86_400 * NS_S,
) -> dict[str, Any]:
    """Per-regime :func:`gate_statistics` blocks (module docs)."""
    ts = np.asarray(ts, dtype=np.int64)
    x = np.asarray(scores, dtype=float)
    y = np.asarray(labels, dtype=float)
    r = np.asarray(regime, dtype=np.int64)
    if not (ts.shape == x.shape == y.shape == r.shape):
        raise ValueError("ts/score/label/regime length mismatch")
    if min_pairs < 2:
        raise ValueError("min_pairs must be >= 2")
    kw = dict(
        horizon_ns=int(horizon_ns),
        hac_bandwidth=hac_bandwidth,
        day_separated=day_separated,
        bucket_ns=bucket_ns,
        day_ns=day_ns,
    )
    known = r >= 0
    by_regime: dict[str, Any] = {}
    for rv in sorted(int(v) for v in np.unique(r[known])) if known.any() else []:
        rows = r == rv
        block = gate_statistics(ts[rows], x[rows], y[rows], **kw)
        block["regime"] = rv
        block["flags"] = {name: bool((rv >> bit) & 1) for bit, name in enumerate(regime_features)}
        block["judged"] = bool(block["n_pairs"] >= min_pairs)
        by_regime[str(rv)] = block
    pooled = gate_statistics(ts[known], x[known], y[known], **kw)
    return {
        "pooled": pooled,
        "by_regime": by_regime,
        "n_unknown": int(np.sum(~known)),
        "regime_features": list(regime_features),
        "min_pairs": int(min_pairs),
    }


def regime_gate(
    stats: dict[str, Any],
    *,
    min_ic: float = 0.0,
    min_t: float = 0.0,
    min_regimes: int = 1,
    max_ic_ratio: float = 0.0,
) -> dict[str, Any]:
    """One gate decision from :func:`regime_gate_statistics` (module docs)."""
    judged = {k: v for k, v in stats["by_regime"].items() if v.get("judged")}
    failed: list[str] = []
    worst: str | None = None
    worst_ic = float("nan")
    for k, v in judged.items():
        ic_v = float(v["ic"]) if v["ic"] == v["ic"] else float("nan")
        if not np.isfinite(ic_v):
            failed.append(f"regime_{k}_ic_undefined")
            continue
        if worst is None or ic_v < worst_ic:
            worst, worst_ic = k, ic_v
        if not ic_v > min_ic:
            failed.append(f"regime_{k}_ic")
        if not float(v["tstat_hac"]) >= min_t:
            failed.append(f"regime_{k}_tstat")
    if len(judged) < max(int(min_regimes), 1):
        failed.append("no_regime_judged" if not judged else "too_few_regimes")
    pooled_ic = float(stats["pooled"]["ic"])
    if max_ic_ratio > 0.0 and worst is not None and np.isfinite(pooled_ic) and pooled_ic > 0.0:
        if worst_ic < max_ic_ratio * pooled_ic:
            failed.append("one_regime_carries_the_alpha")
    return {
        "passed": not failed,
        "worst_regime": worst,
        "worst_ic": worst_ic if worst is not None else None,
        "judged": sorted(judged, key=int),
        "failed": failed,
        "thresholds": {
            "min_ic": float(min_ic),
            "min_t": float(min_t),
            "min_regimes": int(min_regimes),
            "max_ic_ratio": float(max_ic_ratio),
        },
    }
