"""Model monitoring (v1.11, AI3): feature-distribution, prediction and
calibration drift, each check ``OK`` or ``ALERT`` against a threshold.

* **Feature / prediction drift** reuse :mod:`iap.adaptive.drift`: a PSI
  baseline captured on the reference sample (pinned 10-bucket quantile
  recipe) and the two-sample KS test.  A column ALERTs when
  ``PSI >= psi_alert`` or when ``KS p < ks_alpha / n_columns`` (Bonferroni
  over the columns checked) *and* ``D >= ks_min_d`` — the D floor keeps a
  very large sample from alerting on a negligible shift.
* **Calibration (classifiers)**: reliability bins (``n_bins`` equal-width
  bins of the predicted probability), the Brier score and the expected
  calibration error ``ECE = sum_b n_b/n * |mean(y_b) - mean(p_b)|``.  ALERT
  when the current ECE exceeds ``ece_max`` or the reference ECE by more than
  ``ece_increase``.
* **IC decay (regressors)**: Spearman IC of prediction against target on
  reference and current; ALERT when ``ic_ref - ic_cur > ic_decay``.

The report is a JSON-ready dict; ``status`` is ``ALERT`` if any check is.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

from iap.adaptive.drift import MIN_BASELINE_N, capture_baseline, ks_test, psi

OK = "OK"
ALERT = "ALERT"


@dataclass(frozen=True)
class MonitorThresholds:
    psi_alert: float = 0.25
    ks_alpha: float = 0.01
    ks_min_d: float = 0.1
    ece_max: float = 0.10
    ece_increase: float = 0.05
    ic_decay: float = 0.05
    n_bins: int = 10


def _finite(x) -> np.ndarray:
    a = np.asarray(x, dtype=float).ravel()
    return a[np.isfinite(a)]


def distribution_check(name: str, ref, cur, th: MonitorThresholds, n_tests: int = 1) -> dict:
    r, c = _finite(ref), _finite(cur)
    out: dict = {"name": name, "n_ref": int(r.size), "n_cur": int(c.size)}
    if r.size < MIN_BASELINE_N or c.size < 1:
        out.update(psi=None, ks_d=None, ks_p=None, status=ALERT, reason="insufficient data")
        return out
    p = psi(capture_baseline(r, kind="feature", name=name, feature_version="-"), c)
    d, pv = ks_test(r, c)
    ks_fire = pv < th.ks_alpha / max(n_tests, 1) and d >= th.ks_min_d
    fire = (p is not None and p >= th.psi_alert) or ks_fire
    out.update(psi=p, ks_d=d, ks_p=pv, status=ALERT if fire else OK)
    return out


def _paired(p, y) -> tuple[np.ndarray, np.ndarray]:
    p = np.asarray(p, dtype=float).ravel()
    y = np.asarray(y, dtype=float).ravel()
    if p.shape != y.shape:
        raise ValueError("predictions and targets must have the same length")
    ok = np.isfinite(p) & np.isfinite(y)
    return p[ok], y[ok]


def calibration(prob, y, n_bins: int = 10) -> dict:
    """Reliability bins, Brier score and ECE of probabilities ``prob`` for
    binary outcomes ``y`` (``y > 0`` is the positive class)."""
    p, yy = _paired(prob, y)
    yb = (yy > 0).astype(float)
    if p.size == 0:
        return {"n": 0, "brier": None, "ece": None, "bins": []}
    p = np.clip(p, 0.0, 1.0)
    idx = np.minimum((p * n_bins).astype(int), n_bins - 1)
    bins, ece = [], 0.0
    for b in range(n_bins):
        m = idx == b
        n = int(m.sum())
        if n == 0:
            bins.append({"lo": b / n_bins, "hi": (b + 1) / n_bins, "n": 0})
            continue
        mp, my = float(p[m].mean()), float(yb[m].mean())
        ece += n / p.size * abs(my - mp)
        bins.append({"lo": b / n_bins, "hi": (b + 1) / n_bins, "n": n, "mean_p": mp, "freq": my})
    return {
        "n": int(p.size),
        "brier": float(np.mean((p - yb) ** 2)),
        "ece": float(ece),
        "bins": bins,
    }


def spearman_ic(pred, y) -> float | None:
    p, yy = _paired(pred, y)
    if p.size < 3 or np.ptp(p) == 0 or np.ptp(yy) == 0:
        return None
    rp = np.argsort(np.argsort(p, kind="stable"), kind="stable").astype(float)
    ry = np.argsort(np.argsort(yy, kind="stable"), kind="stable").astype(float)
    return float(np.corrcoef(rp, ry)[0, 1])


def monitor(
    *,
    kind: str,
    ref_features: dict[str, np.ndarray] | None = None,
    cur_features: dict[str, np.ndarray] | None = None,
    ref_pred=None,
    cur_pred=None,
    ref_y=None,
    cur_y=None,
    thresholds: MonitorThresholds | None = None,
    model_id: str | None = None,
) -> dict:
    """The monitoring report (module docs) for one reference/current pair."""
    if kind not in ("classifier", "regressor"):
        raise ValueError("kind must be 'classifier' or 'regressor'")
    th = thresholds or MonitorThresholds()
    checks: list[dict] = []
    ref_features, cur_features = ref_features or {}, cur_features or {}
    names = sorted(set(ref_features) & set(cur_features))
    for n in names:
        c = distribution_check(n, ref_features[n], cur_features[n], th, n_tests=len(names))
        checks.append({"check": "feature_drift", **c})
    if ref_pred is not None and cur_pred is not None:
        checks.append(
            {
                "check": "prediction_drift",
                **distribution_check("prediction", ref_pred, cur_pred, th),
            }
        )
    if cur_pred is not None and cur_y is not None:
        if kind == "classifier":
            cur = calibration(cur_pred, cur_y, th.n_bins)
            ref = (
                calibration(ref_pred, ref_y, th.n_bins)
                if ref_pred is not None and ref_y is not None
                else None
            )
            ref_ece = None if ref is None else ref["ece"]
            fire = cur["ece"] is None or cur["ece"] > th.ece_max
            if ref_ece is not None and cur["ece"] is not None:
                fire = fire or cur["ece"] - ref_ece > th.ece_increase
            checks.append(
                {
                    "check": "calibration",
                    "name": "prediction",
                    "ece": cur["ece"],
                    "ece_ref": ref_ece,
                    "brier": cur["brier"],
                    "brier_ref": None if ref is None else ref["brier"],
                    "bins": cur["bins"],
                    "status": ALERT if fire else OK,
                }
            )
        else:
            ic_cur = spearman_ic(cur_pred, cur_y)
            ic_ref = (
                spearman_ic(ref_pred, ref_y) if ref_pred is not None and ref_y is not None else None
            )
            fire = ic_cur is None or (ic_ref is not None and ic_ref - ic_cur > th.ic_decay)
            checks.append(
                {
                    "check": "ic_decay",
                    "name": "prediction",
                    "ic": ic_cur,
                    "ic_ref": ic_ref,
                    "status": ALERT if fire else OK,
                }
            )
    status = ALERT if any(c["status"] == ALERT for c in checks) else OK
    return {
        "model_id": model_id,
        "kind": kind,
        "thresholds": asdict(th),
        "status": status,
        "alerts": [f"{c['check']}:{c['name']}" for c in checks if c["status"] == ALERT],
        "checks": checks,
    }
