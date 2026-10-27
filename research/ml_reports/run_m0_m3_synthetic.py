#!/usr/bin/env python3
"""M0-M3 on synthetic panels (v2.1 Track 4): purged walk-forward plus the leakage canaries.

    python research/ml_reports/run_m0_m3_synthetic.py      # about a minute; writes M0_M3_SYNTHETIC.json

SYNTHETIC DATA ONLY, no evidence class.  It is a teaching run of the model ladder of
``research/v2/ML_PROTOCOL.md`` (read, not changed), not a registered study: no ledger look,
no pre-registration, no admission test.  Two seeded data-generating processes:

``linear``     ``iap.data.synthetic.make_canary_panel``: fwd_ret = 0.3 * signal + noise.
``nonlinear``  the same panel, label replaced by 0.3 * (signal**2 - 1) + noise, so a linear
               model on ``signal`` has nothing to find and a nonlinear one does.

Features: ``canary.signal`` and ``canary.trailing_mean_ret`` (causal, no information).
Splits: ``iap.models.splits.WalkForwardSplitter`` on the period index, 4 expanding folds,
label horizon 1 period, embargo 3 periods (the longest feature lookback), so train rows whose
label crosses the boundary are purged and the first test rows cannot see train-period data.
M3 early-stops on the last fifth of each fold's train window (itself purged from the rest).
Metric: out-of-sample cross-sectional IC (Spearman per period, then mean), from
``iap.data.leakage.ic``.  The leakage canaries of ``iap.data.leakage.run_canaries`` run first
and the script stops if any fails.
"""

from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))

from iap.data import synthetic  # noqa: E402
from iap.data.leakage import chance_band, ic, run_canaries  # noqa: E402
from iap.ml import GAMModel, GBTModel  # noqa: E402
from iap.ml.models import RidgeModel  # noqa: E402
from iap.models.splits import WalkForwardSplitter  # noqa: E402

SEED = 20261027
N_PERIODS, N_NAMES, BETA = 200, 60, 0.3
FEATURES = ("canary.signal", "canary.trailing_mean_ret")
HORIZON, EMBARGO = 1, 3
OUT = Path(__file__).with_name("M0_M3_SYNTHETIC.json")


def panel() -> pd.DataFrame:
    f = synthetic.make_canary_panel(seed=SEED, n_periods=N_PERIODS, n_names=N_NAMES, beta=BETA)
    f["canary.signal"] = synthetic.feature_canary_signal(f)
    f["canary.trailing_mean_ret"] = synthetic.feature_canary_trailing_mean_ret(f)
    return f


def nonlinear_label(f: pd.DataFrame) -> pd.Series:
    # fwd_ret - beta*signal is the pure noise term; swap in a quadratic signal effect
    noise = f[synthetic.LABEL] - BETA * f["signal"]
    return BETA * (f["signal"] ** 2 - 1.0) + noise


def candidates() -> dict[str, object]:
    return {
        "M0 linear (signal only)": RidgeModel(FEATURES[:1], 0.0),
        "M1 ridge alpha=1": RidgeModel(FEATURES, 1.0),
        "M2 GAM lambda=1": GAMModel(FEATURES, 1.0),
        "M3 GBT 8 leaves lr=0.1": GBTModel(FEATURES, 8, 0.1, 1.0, min_samples_leaf=200),
    }


def mean_ic(f: pd.DataFrame, pred: np.ndarray, y: np.ndarray) -> float:
    vals = []
    for _, idx in f.groupby("period").indices.items():
        v, n = ic(pred[idx], y[idx])
        if n >= 10 and np.isfinite(v):
            vals.append(v)
    return float(np.mean(vals)) if vals else float("nan")


def walk_forward(f: pd.DataFrame, y: np.ndarray) -> dict[str, list[float]]:
    ts = f["period"].to_numpy(np.int64)
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # an embargo shorter than horizon or lookback is a bug here
        folds = WalkForwardSplitter(4, EMBARGO, HORIZON, max_feature_lookback_ns=3).split(ts)
    x = f[list(FEATURES)].to_numpy(float)
    ok = np.all(np.isfinite(x), axis=1) & np.isfinite(y)
    out: dict[str, list[float]] = {k: [] for k in candidates()}
    for fold in folds:
        tr = fold.train_idx[ok[fold.train_idx]]
        te = fold.test_idx[ok[fold.test_idx]]
        cut = np.quantile(ts[tr], 0.8)
        fit_rows = tr[ts[tr] + HORIZON + EMBARGO <= cut]
        es_rows = tr[ts[tr] >= cut]
        test = f.iloc[te].reset_index(drop=True)
        for name, model in candidates().items():
            cols = [FEATURES.index(c) for c in model.features]
            if isinstance(model, GBTModel):
                model.fit(x[fit_rows][:, cols], y[fit_rows], x[es_rows][:, cols], y[es_rows])
            else:
                model.fit(x[tr][:, cols], y[tr])
            pred, _ = model.predict(test)
            out[name].append(round(mean_ic(test, pred, y[te]), 4))
    return out


def main() -> int:
    f = panel()
    canaries = run_canaries(
        f, synthetic.feature_canary_signal, synthetic.LABEL, synthetic.feature_canary_planted_leak
    )
    result: dict[str, object] = {
        "evidence_class": None,
        "data": "SYNTHETIC",
        "seed": SEED,
        "panel": {"n_periods": N_PERIODS, "n_names": N_NAMES, "beta": BETA},
        "splits": {"folds": 4, "label_horizon_periods": HORIZON, "embargo_periods": EMBARGO},
        "chance_band_ic_per_period": round(chance_band(N_NAMES), 4),
        "canaries": {
            k: {"passed": bool(v["passed"])} for k, v in canaries.items() if isinstance(v, dict)
        },
        "leaked_feature_ic_if_undetected": round(
            float(canaries["planted_leak"]["ic_leaked_if_undetected"]), 4
        ),
        "results": {},
    }
    for dgp, y in (
        ("linear", f[synthetic.LABEL].to_numpy(float)),
        ("nonlinear", nonlinear_label(f).to_numpy(float)),
    ):
        per_fold = walk_forward(f, y)
        result["results"][dgp] = {
            k: {"fold_ic": v, "mean_ic": round(float(np.mean(v)), 4)} for k, v in per_fold.items()
        }
    OUT.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    for dgp, rows in result["results"].items():
        print(dgp)
        for k, v in rows.items():
            print(f"  {k:26s} mean IC {v['mean_ic']:+.4f}  folds {v['fold_ic']}")
    print("canaries", result["canaries"], "leak IC", result["leaked_feature_ic_if_undetected"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
