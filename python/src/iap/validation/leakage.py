"""Automatic leakage testing (spec §13: "test feature leakage automatically").

Two independent detectors, both mandatory in the promotion pipeline:

1. **Label-column guard** — the model is scored twice on the same frames,
   once as-is and once with EVERY label column replaced by garbage
   (0.12345 constants).  Any difference in output proves the scoring path
   reads labels (the canonical catastrophic leak).  Alphas may read labels
   in fit() only.

2. **Shift-by-one test** (conventions §7: "shift-by-one destroys IC") —
   compare the IC of score_t vs label_t with the IC of score_{t-1} vs
   label_t (scores lagged one emission).  A genuine microstructure signal
   loses part of its IC under a one-event lag but not implausibly much,
   and its unlagged IC is bounded by realistic signal-to-noise.  A signal
   whose unlagged IC exceeds ``suspicious_ic`` while collapsing by more
   than ``collapse_ratio`` under the lag has the signature of lookahead
   (e.g. a feature built from the same events the label measures) and is
   flagged for manual review; promotion is blocked.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, Mapping

import numpy as np
import pandas as pd

from iap.validation.metrics import ic

_GARBAGE = 0.12345


@dataclass
class LeakageResult:
    label_guard_ok: bool          # False = score() output depends on labels
    ic_unshifted: float
    ic_shifted: float
    shift_ok: bool                # False = lookahead signature
    passed: bool

    def to_dict(self) -> dict:
        return asdict(self)


def _obfuscate_labels(frames: Mapping[int, pd.DataFrame]) -> Dict[int, pd.DataFrame]:
    out: Dict[int, pd.DataFrame] = {}
    for iid, df in frames.items():
        df2 = df.copy()
        for c in df2.columns:
            if c.startswith(("label_mid_", "label_cost_")):
                df2[c] = _GARBAGE
            elif c.startswith("label_valid_"):
                df2[c] = True
        out[iid] = df2
    return out


class LeakageTester:
    """Runs both detectors against a fitted model on test frames."""

    def __init__(
        self, suspicious_ic: float = 0.15, collapse_ratio: float = 0.5
    ) -> None:
        self.suspicious_ic = suspicious_ic
        self.collapse_ratio = collapse_ratio

    def label_guard(self, model, frames: Mapping[int, pd.DataFrame]) -> bool:
        """True when score() is invariant to label-column contents."""
        try:
            a = model.score(frames)
            b = model.score(_obfuscate_labels(frames))
        except Exception:
            return False  # scoring crashed on obfuscated labels -> reads them
        for iid in a:
            xa = a[iid]["expected_return"].to_numpy()
            xb = b[iid]["expected_return"].to_numpy()
            ca = a[iid]["confidence"].to_numpy()
            cb = b[iid]["confidence"].to_numpy()
            if not (
                np.array_equal(xa, xb, equal_nan=True)
                and np.array_equal(ca, cb, equal_nan=True)
            ):
                return False
        return True

    def shift_test(
        self, model, frames: Mapping[int, pd.DataFrame]
    ) -> Dict[str, float]:
        """(ic_unshifted, ic_shifted) pooled across the model's universe."""
        scores = model.score(frames)
        h = model.horizon
        xs, ys, xs_lag, ys_lag = [], [], [], []
        for iid, sc in scores.items():
            df = frames[iid]
            er = sc["expected_return"].to_numpy(dtype=float).copy()
            er[sc["confidence"].to_numpy(dtype=float) <= 0.0] = np.nan
            lab = df[f"label_mid_{h}"].to_numpy(dtype=float).copy()
            lab[~df[f"label_valid_{h}"].to_numpy(dtype=bool)] = np.nan
            xs.append(er)
            ys.append(lab)
            xs_lag.append(er[:-1])  # score_{t-1}
            ys_lag.append(lab[1:])  # label_t
        ic0 = ic(np.concatenate(xs), np.concatenate(ys))
        ic1 = ic(np.concatenate(xs_lag), np.concatenate(ys_lag))
        return {"ic_unshifted": ic0, "ic_shifted": ic1}

    def run(self, model, frames: Mapping[int, pd.DataFrame]) -> LeakageResult:
        guard = self.label_guard(model, frames)
        shifts = self.shift_test(model, frames)
        ic0, ic1 = shifts["ic_unshifted"], shifts["ic_shifted"]
        shift_ok = True
        if np.isfinite(ic0) and abs(ic0) > self.suspicious_ic:
            collapsed = (not np.isfinite(ic1)) or (
                abs(ic1) < self.collapse_ratio * abs(ic0)
            )
            if collapsed:
                shift_ok = False  # implausibly strong AND destroyed by 1 shift
        return LeakageResult(
            label_guard_ok=guard,
            ic_unshifted=float(ic0) if np.isfinite(ic0) else float("nan"),
            ic_shifted=float(ic1) if np.isfinite(ic1) else float("nan"),
            shift_ok=shift_ok,
            passed=guard and shift_ok,
        )
