"""Drift and alpha-decay monitors of the forward paper trade.

* **Feature drift** -- the day's feature store against the in-sample
  reference stores, with :func:`iap.research.drift.compute_drift` (PSI over
  reference-quantile bins, KS, mean shift, std ratio) and its thresholds.
* **Alpha decay** -- the day's realised IC of each monitored alpha (EQ10,
  EQ05 at 1s; the research definitions of ``tools/r7_confirm.py``: the
  alpha's ``signals``, :func:`iap.labels.frames.scored_labels`, uncrossed
  rows, :func:`iap.validation.metrics.ic`) against its confirmed holdout IC,
  for the day and as a rolling mean over the previous dated reports.

Alerts are descriptive: they never change a pre-registration, a gate or a
result.  :func:`ic_check` and :func:`drift_alerts` are pure functions.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from iap.research.drift import DriftThresholds, compute_drift, iter_store


def feature_drift(
    reference_dirs: Sequence[str | Path],
    target_dir: str | Path,
    thresholds: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The drift report of ``target_dir`` against ``reference_dirs`` (pooled
    and per symbol), with its alerts."""
    th = DriftThresholds(**dict(thresholds or {}))
    refs = [Path(d) for d in reference_dirs]
    rep = compute_drift(
        lambda: iter_store(refs), lambda: iter_store([Path(target_dir)]), thresholds=th
    )
    rep["alerts"] = drift_alerts(rep)
    return rep


def drift_alerts(rep: Mapping[str, Any], top: int = 5) -> list[str]:
    """One compact alert per group with drifted features: the count and the
    ``top`` features by PSI (the full list is in ``monitors.json``)."""
    out = []
    groups = rep.get("groups") or {}
    for group, s in sorted((rep.get("summary") or {}).items()):
        drifted = s.get("drifted") or []
        if not drifted:
            continue
        rows = groups.get(group) or {}

        def psi(f: str, rows: Mapping[str, Any] = rows) -> float:
            v = (rows.get(f) or {}).get("psi")
            return float(v) if isinstance(v, (int, float)) and math.isfinite(v) else -1.0

        worst = sorted(drifted, key=lambda f: (-psi(f), f))[:top]
        more = f" (+{len(drifted) - len(worst)} more)" if len(drifted) > len(worst) else ""
        out.append(
            f"feature drift [{group}]: {len(drifted)}/{s.get('features')} features; "
            f"top by PSI: {', '.join(worst)}{more}"
        )
    return out


def realised_ic(
    features_dir: str | Path,
    instruments_json: str | Path,
    alpha: str,
    horizon: str,
    methods_name: str,
    single_venue: bool,
) -> dict[str, Any]:
    """The pooled uncrossed IC of ``alpha`` at ``horizon`` over one feature store."""
    from iap.alpha import build, configure_universe
    from iap.alpha.data import load_features
    from iap.labels.frames import scored_labels
    from iap.validation.methods import methods
    from iap.validation.metrics import ic

    configure_universe(Path(instruments_json))
    frames = load_features(Path(features_dir))
    model = build(alpha)
    model.horizon = horizon
    ic_rows = methods(methods_name).ic_rows
    universe = model.universe(sorted(frames))
    sigs = model.signals({i: frames[i] for i in universe})
    xs, ys = [], []
    for iid, sig in sigs.items():
        df = frames[iid]
        lab, _ = scored_labels(df, model.horizon, ic_rows)
        if single_venue or "spread_ticks_v1" not in df.columns:
            unc = np.ones(len(df), dtype=bool)
        else:
            sp = df["spread_ticks_v1"].to_numpy(dtype=float)
            unc = ~(np.isfinite(sp) & (sp < 0.0))
        xs.append(np.asarray(sig, dtype=float)[unc])
        ys.append(np.asarray(lab, dtype=float)[unc])
    if not xs:
        return {"ic": None, "n": 0}
    x, y = np.concatenate(xs), np.concatenate(ys)
    ok = np.isfinite(x) & np.isfinite(y)
    v = ic(x, y)
    return {"ic": float(v) if math.isfinite(v) else None, "n": int(ok.sum())}


def ic_check(
    alpha: str,
    realised: float | None,
    n: int,
    holdout_ic: float,
    history: Sequence[float],
    *,
    min_ratio: float,
    rolling_window: int,
    min_rolling_days: int,
) -> dict[str, Any]:
    """Day and rolling realised IC against the holdout IC, with alerts.

    ``history`` holds the previous days' realised ICs, oldest first; the
    rolling mean takes the last ``rolling_window`` values including today."""
    alerts = []
    ratio = None
    if realised is None:
        alerts.append(f"{alpha}: no realised IC today")
    else:
        ratio = realised / holdout_ic if holdout_ic else None
        if (realised > 0) != (holdout_ic > 0):
            alerts.append(f"{alpha}: realised IC {realised:+.4f} has the opposite sign")
        elif ratio is not None and ratio < min_ratio:
            alerts.append(
                f"{alpha}: realised IC {realised:.4f} < {min_ratio:g} x holdout {holdout_ic:.4f}"
            )
    series = [float(v) for v in history] + ([] if realised is None else [float(realised)])
    window = series[-rolling_window:]
    rolling = float(np.mean(window)) if window else None
    rolling_ratio = None
    if rolling is not None and len(window) >= min_rolling_days and holdout_ic:
        rolling_ratio = rolling / holdout_ic
        if rolling_ratio < min_ratio:
            alerts.append(
                f"{alpha}: rolling {len(window)}-day IC {rolling:.4f} < {min_ratio:g} x "
                f"holdout {holdout_ic:.4f} (alpha decay)"
            )
    return {
        "alpha": alpha,
        "realised_ic": realised,
        "n_rows": n,
        "holdout_ic": holdout_ic,
        "ratio_to_holdout": ratio,
        "rolling": {
            "days": len(window),
            "mean_ic": rolling,
            "ratio_to_holdout": rolling_ratio,
            "min_days_for_alert": min_rolling_days,
        },
        "alerts": alerts,
    }
