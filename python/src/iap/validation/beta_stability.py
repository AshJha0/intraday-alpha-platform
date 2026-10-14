"""Report-only beta-stability diagnostic for linear alphas (v1.16 item 12).

v1.15 (#114) added this to ``iap/alpha/base.py``, which every alpha's code
fingerprint covers, so it changed the registered ``code_hash`` of open
pre-registrations and was reverted (#117). It lives here instead: nothing in
``iap.alpha`` imports this module, so alpha fingerprints are unaffected
(tested), and nothing here changes a fit, a score or a verdict.

``beta_stability(items, label_col, valid_col)``: per-instrument and
per-instrument-day OLS slopes of the label on the raw signal, each slice
needing at least 32 valid pairs, summarised as count, share positive, sign
agreement with the median, median, IQR and IQR / |median|.
``alpha_beta_stability(alpha, frames)`` runs it for a ``LinearAlpha`` on
``frames`` at the alpha's horizon.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
import pandas as pd

__all__ = ["alpha_beta_stability", "beta_stability"]

_NS_DAY = 86_400 * 1_000_000_000
_MIN_OBS = 32
_EPS = 1e-12


def _slope(x: np.ndarray, y: np.ndarray) -> float | None:
    if x.size < _MIN_OBS:
        return None
    var = float(np.var(x))
    if var <= _EPS:
        return None
    return float(np.mean((x - x.mean()) * (y - y.mean())) / var)


def _summary(betas: list[float]) -> dict:
    keys = ("pos_frac", "sign_agreement", "median", "iqr", "dispersion")
    if not betas:
        return dict.fromkeys(keys) | {"n": 0}
    b = np.asarray(betas, dtype=float)
    med = float(np.median(b))
    iqr = float(np.quantile(b, 0.75) - np.quantile(b, 0.25))
    return {
        "n": int(b.size),
        "pos_frac": float(np.mean(b > 0.0)),
        "sign_agreement": float(np.mean(np.sign(b) == np.sign(med))) if med != 0 else None,
        "median": med,
        "iqr": iqr,
        "dispersion": iqr / abs(med) if med != 0 else None,
    }


def beta_stability(
    items: Sequence[tuple[int, pd.DataFrame, pd.Series]], label_col: str, valid_col: str
) -> dict:
    """``{"by_instrument": summary, "by_instrument_day": summary}`` (module docstring)."""
    per_inst: list[float] = []
    per_day: list[float] = []
    for _iid, df, sig in items:
        if label_col not in df.columns or valid_col not in df.columns:
            continue
        x = sig.to_numpy(dtype=float)
        y = df[label_col].to_numpy(dtype=float)
        ok = np.isfinite(x) & np.isfinite(y) & df[valid_col].to_numpy(dtype=bool)
        b = _slope(x[ok], y[ok])
        if b is not None:
            per_inst.append(b)
        if "exchange_ts" in df.columns:
            days = df["exchange_ts"].to_numpy(dtype=np.int64) // _NS_DAY
            for d in np.unique(days[ok]):
                m = ok & (days == d)
                bd = _slope(x[m], y[m])
                if bd is not None:
                    per_day.append(bd)
    return {"by_instrument": _summary(per_inst), "by_instrument_day": _summary(per_day)}


def alpha_beta_stability(alpha, frames: Mapping[int, pd.DataFrame]) -> dict:
    """:func:`beta_stability` of a ``LinearAlpha`` at its own horizon."""
    h = alpha.horizon
    use = {i: f for i, f in frames.items() if i in alpha.universe(sorted(frames))}
    sigs = alpha.signals(use)
    return beta_stability(
        [(i, use[i], s) for i, s in sigs.items()], f"label_mid_{h}", f"label_valid_{h}"
    )
