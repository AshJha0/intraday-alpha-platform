"""``python -m iap.research.conformal_report`` -- conformal calibration of alpha forecasts.

Reads a forecast table (CSV or parquet) with columns ``y`` (realised
return), ``pred`` (alpha forecast) and ``session`` (session label, e.g. the
trading date), plus optional ``sigma`` (per-row scale estimate for the
normalised score).  Rows must be in time order.  The first ``--cal-frac``
of rows calibrate; the rest are evaluated out of sample with either split
conformal or online ACI, and :func:`iap.models.conformal.calibration_report`
prints coverage vs nominal, overall and per session, as one JSON document
on stdout.

``--synthetic N`` generates a heteroscedastic demo table instead of reading
one.  Read-only research tooling: it writes nothing and debits no looks.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pandas as pd

from iap.models.conformal import AdaptiveConformal, SplitConformal, calibration_report


def synthetic_frame(n: int, n_sessions: int = 4, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    x = rng.normal(size=n)
    sigma = 0.5 + np.abs(rng.normal(size=n))
    y = 0.3 * x + sigma * rng.normal(size=n)
    sess = np.repeat([f"S{i}" for i in range(n_sessions)], -(-n // n_sessions))[:n]
    return pd.DataFrame({"y": y, "pred": 0.3 * x, "sigma": sigma, "session": sess})


def run_report(
    df: pd.DataFrame,
    *,
    alpha: float = 0.1,
    method: str = "split",
    score: str = "absolute",
    cal_frac: float = 0.3,
    gamma: float = 0.005,
    window: int = 500,
) -> dict:
    for col in ("y", "pred", "session"):
        if col not in df.columns:
            raise ValueError(f"missing column {col!r}")
    if score == "normalized" and "sigma" not in df.columns:
        raise ValueError("score='normalized' needs a 'sigma' column")
    if not 0.0 < cal_frac < 1.0:
        raise ValueError("cal_frac must be in (0, 1)")
    n_cal = int(len(df) * cal_frac)
    if n_cal < 1 or n_cal >= len(df):
        raise ValueError("not enough rows to split calibration / evaluation")
    cal, ev = df.iloc[:n_cal], df.iloc[n_cal:]
    y, pred = ev["y"].to_numpy(float), ev["pred"].to_numpy(float)
    extra: dict = {}
    if method == "split":
        norm = score == "normalized"
        sig_c = cal["sigma"].to_numpy(float) if norm else None
        sig_e = ev["sigma"].to_numpy(float) if norm else None
        cp = SplitConformal(alpha, score=score).fit(cal["y"], cal["pred"], sig_c)
        lo, hi = cp.interval(pred, sig_e)
    elif method == "adaptive":
        if score != "absolute":
            raise ValueError("adaptive supports score='absolute' only")
        aci = AdaptiveConformal(alpha=alpha, gamma=gamma, window=window)
        aci.warm_start(cal["y"], cal["pred"])
        lo, hi = np.empty(len(ev)), np.empty(len(ev))
        for i in range(len(ev)):
            lo[i], hi[i] = aci.interval(pred[i])
            aci.update(y[i], pred[i])
        extra = {"saturations": aci.saturations, "final_alpha_t": aci.alpha_t}
    else:
        raise ValueError("method must be 'split' or 'adaptive'")
    rep = calibration_report(y, lo, hi, ev["session"].astype(str).tolist(), alpha)
    rep["config"] = {
        "alpha": alpha,
        "method": method,
        "score": score,
        "cal_rows": n_cal,
        "eval_rows": len(ev),
        **extra,
    }
    return rep


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m iap.research.conformal_report")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--input", type=Path, help="CSV or parquet with y, pred, session[, sigma]")
    src.add_argument("--synthetic", type=int, metavar="N", help="generate N synthetic rows")
    ap.add_argument("--alpha", type=float, default=0.1)
    ap.add_argument("--method", choices=("split", "adaptive"), default="split")
    ap.add_argument("--score", choices=("absolute", "normalized"), default="absolute")
    ap.add_argument("--cal-frac", type=float, default=0.3)
    ap.add_argument("--gamma", type=float, default=0.005)
    ap.add_argument("--window", type=int, default=500)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args(argv)
    if a.synthetic is not None:
        df = synthetic_frame(a.synthetic, seed=a.seed)
    elif a.input.suffix == ".parquet":
        df = pd.read_parquet(a.input)
    else:
        df = pd.read_csv(a.input)
    try:
        rep = run_report(
            df,
            alpha=a.alpha,
            method=a.method,
            score=a.score,
            cal_frac=a.cal_frac,
            gamma=a.gamma,
            window=a.window,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    json.dump(rep, sys.stdout, indent=2, sort_keys=True)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
