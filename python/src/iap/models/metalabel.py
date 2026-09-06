"""Meta-labeling: trade/no-trade classifier over a primary alpha (spec §14).

The secondary model answers a different question from the primary one: *given
that the primary model wants to trade, should we?*  It is conditioned on
alpha strength, spread, volatility, liquidity, queue imbalance and expected
cost — exactly the conditioning set the spec names.

Pinned methodology:

- The meta dataset is built ONLY from the primary model's pooled
  out-of-sample predictions (never in-sample fits) and only on samples where
  the primary trading rule takes a position.
- The meta label is *economic*: 1 iff the realized net P&L of the primary
  signal (through the platform cost model, ``iap.models.economics``) is > 0.
- Time-aware three-way split of the OOS region, chronological with a 60s
  embargo between segments: 50% meta-train / 25% calibration / 25% test.
  Both boundaries are PURGED: earlier-side rows survive only when
  ``ts + TARGET_HORIZON_NS <= boundary`` so no train/calibration label
  window overlaps the following segment; the label horizon is asserted to
  be shorter than the embargo.
- Probability calibration: isotonic regression via sklearn's
  ``CalibratedClassifierCV`` fitted on the calibration segment (prefit
  semantics; FrozenEstimator on sklearn >= 1.6).
- Evaluation is economic — P&L per signal with and without the meta-gate —
  in addition to AUC/Brier, per the spec's "not only AUC" requirement.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import brier_score_loss, roc_auc_score

from iap.experiment.tracker import ExperimentTracker
from iap.models.dataset import Dataset, META_CONTEXT_COLUMNS, TARGET_HORIZON_NS
from iap.models.economics import realized_net, signal_directions

_EMBARGO_NS = 60_000_000_000

# The purge below drops train-side rows whose forward label window crosses a
# segment boundary; the embargo then keeps the next segment clear of anything
# overlapping the boundary. That construction is only leak-free while the
# label horizon fits inside the embargo.
assert TARGET_HORIZON_NS < _EMBARGO_NS, (
    "meta-label split requires label horizon < embargo "
    f"({TARGET_HORIZON_NS} >= {_EMBARGO_NS})"
)

#: Pinned meta-classifier hyperparameters (recorded in the run manifest).
META_HYPERPARAMS: Dict[str, Any] = {
    "max_iter": 80,
    "max_depth": 3,
    "learning_rate": 0.1,
    "min_samples_leaf": 50,
    "random_state": 7,
}

META_FEATURE_NAMES = (
    "alpha_strength",          # |primary prediction|
    "alpha_signed",            # primary prediction
    "spread_bps",
    "rvol_1m",
    "log_depth_l1",
    "queue_imbalance_signed",  # book imbalance aligned with trade direction
    "half_spread_cost_bps",
    "expected_impact_bps",
)


def build_meta_features(pred: np.ndarray, direction: np.ndarray,
                        meta_context: np.ndarray) -> np.ndarray:
    """Assemble the pinned meta-feature matrix (NaN -> 0, documented)."""
    if meta_context.shape[1] != len(META_CONTEXT_COLUMNS):
        raise ValueError("meta_context has wrong column count")
    spread = meta_context[:, 0]
    rvol = meta_context[:, 1]
    depth = meta_context[:, 2]
    imb = meta_context[:, 3]
    half_cost = meta_context[:, 4]
    impact = meta_context[:, 5]
    X = np.column_stack([
        np.abs(pred),
        pred,
        spread,
        rvol,
        np.log1p(np.maximum(depth, 0.0)),
        imb * direction,
        half_cost,
        impact,
    ])
    return np.where(np.isfinite(X), X, 0.0)


#: Minimum POSITIVE calibration samples before isotonic regression is used
#: (pinned, round-3).  Isotonic is non-parametric: with a 7 % base rate and a
#: few dozen positives it interpolates noise into a step function whose steps
#: sit above every achievable probability, so the gate declines 100 % of test
#: signals for every tau and the "economically correct" reading is an
#: artefact of the calibrator, not of the signal.  Below the threshold the
#: platform falls back to Platt scaling (sigmoid, 2 parameters).
MIN_ISOTONIC_POSITIVES = 500


def _fit_isotonic_calibrated(base: Any, X_cal: np.ndarray,
                             y_cal: np.ndarray) -> Any:
    """Calibrate a prefit classifier on held-out calibration data.

    Isotonic when the calibration segment carries at least
    :data:`MIN_ISOTONIC_POSITIVES` positives, otherwise Platt (sigmoid).
    The method actually used is recorded on the returned object as
    ``iap_calibration_method``.
    """
    n_pos = int(np.sum(np.asarray(y_cal) > 0))
    method = "isotonic" if n_pos >= MIN_ISOTONIC_POSITIVES else "sigmoid"
    try:  # sklearn >= 1.6
        from sklearn.frozen import FrozenEstimator
        calib = CalibratedClassifierCV(FrozenEstimator(base), method=method)
    except ImportError:  # pragma: no cover - older sklearn
        calib = CalibratedClassifierCV(base, method=method, cv="prefit")
    calib.fit(X_cal, y_cal)
    calib.iap_calibration_method = method
    calib.iap_calibration_positives = n_pos
    return calib


def _economics_from_mask(direction: np.ndarray, y_mid: np.ndarray,
                         y_cost: np.ndarray,
                         take: np.ndarray) -> Dict[str, float]:
    d = np.where(take, direction, 0)
    net = realized_net(d, y_mid, y_cost)
    traded = d != 0
    n = int(traded.sum())
    return {
        "n_signals": int((direction != 0).sum()),
        "n_trades": n,
        "total_net_bps": float(net.sum() * 1e4),
        "mean_net_bps_per_trade": float(net[traded].mean() * 1e4) if n else 0.0,
        "hit_rate": float((net[traded] > 0).mean()) if n else 0.0,
    }


def run_meta_labeling(
    ds: Dataset,
    primary_pred: np.ndarray,
    tracker: Optional[ExperimentTracker] = None,
    primary_name: str = "primary",
    tau: float = 0.5,
) -> Dict[str, Any]:
    """Train + calibrate + economically evaluate the meta-label gate.

    ``primary_pred`` is the pooled OOS prediction vector (NaN where a sample
    was never out-of-sample).  ``tau`` is the pinned probability threshold of
    the meta-gate; a calibration-set sweep is also reported.
    """
    primary_pred = np.asarray(primary_pred, dtype=np.float64)
    if len(primary_pred) != len(ds):
        raise ValueError("primary_pred length mismatch with dataset")

    half_bps = ds.meta_context[:, 4]
    cost_est = np.maximum(
        2.0 * np.where(np.isfinite(half_bps), half_bps, 0.0) / 1e4, 0.0)
    direction = signal_directions(np.nan_to_num(primary_pred, nan=0.0),
                                  cost_est)
    usable = np.isfinite(primary_pred) & (direction != 0)
    idx = np.flatnonzero(usable)
    if len(idx) < 300:
        raise ValueError(f"too few meta samples: {len(idx)}")

    ts = ds.ts[idx]
    net = realized_net(direction[idx], ds.y_mid[idx], ds.y[idx])
    y_meta = (net > 0.0).astype(np.int8)
    X_meta = build_meta_features(primary_pred[idx], direction[idx],
                                 ds.meta_context[idx])

    # chronological 50/25/25 split with embargo (ts is already sorted
    # because the dataset is sorted by exchange_ts). PURGING (pinned): rows
    # on the earlier side of each boundary are kept only when their forward
    # label window closes at or before the boundary
    # (ts + TARGET_HORIZON_NS <= boundary) — a row whose label overlaps the
    # boundary would leak the later segment's outcomes into training.
    t_lo, t_hi = int(ts[0]), int(ts[-1])
    span = t_hi - t_lo
    b1 = t_lo + span // 2
    b2 = t_lo + (3 * span) // 4
    tr = ts + TARGET_HORIZON_NS <= b1
    ca = (ts > b1 + _EMBARGO_NS) & (ts + TARGET_HORIZON_NS <= b2)
    te = ts > b2 + _EMBARGO_NS
    if tr.sum() < 100 or ca.sum() < 50 or te.sum() < 50:
        raise ValueError("meta split produced degenerate segments")

    base = HistGradientBoostingClassifier(**META_HYPERPARAMS)
    base.fit(X_meta[tr], y_meta[tr])
    calib = _fit_isotonic_calibrated(base, X_meta[ca], y_meta[ca])

    p_test = calib.predict_proba(X_meta[te])[:, 1]
    p_cal = calib.predict_proba(X_meta[ca])[:, 1]

    # tau sweep on the CALIBRATION segment (never on test).  Grid = pinned
    # absolute levels + deciles of the calibrated probabilities (the base
    # rate of profitable trades can sit far below 0.5, so absolute levels
    # alone can land above the whole probability distribution).
    fixed = [round(0.30 + 0.05 * i, 2) for i in range(9)]  # 0.30 .. 0.70
    p_cal_deciles = [float(np.quantile(p_cal, q))
                     for q in (0.1, 0.25, 0.5, 0.75, 0.9)]
    taus = sorted(set(round(t, 6) for t in fixed + p_cal_deciles))
    cal_net = realized_net(direction[idx][ca], ds.y_mid[idx][ca],
                           ds.y[idx][ca])
    sweep = []
    for t in taus:
        take = p_cal >= t
        sweep.append({"tau": t, "n_trades": int(take.sum()),
                      "total_net_bps": float(cal_net[take].sum() * 1e4)})
    best_tau = max(sweep, key=lambda r: r["total_net_bps"])["tau"]

    # economics on the TEST segment: gate off / gate at tau / gate at best_tau
    d_te = direction[idx][te]
    ym_te, yc_te = ds.y_mid[idx][te], ds.y[idx][te]
    econ_off = _economics_from_mask(d_te, ym_te, yc_te,
                                    np.ones(len(d_te), dtype=bool))
    econ_tau = _economics_from_mask(d_te, ym_te, yc_te, p_test >= tau)
    econ_best = _economics_from_mask(d_te, ym_te, yc_te, p_test >= best_tau)

    # calibration curve (10 equal-width bins on [0,1])
    bins = np.linspace(0.0, 1.0, 11)
    curve = []
    which = np.digitize(p_test, bins[1:-1])
    for b in range(10):
        m = which == b
        if m.sum() == 0:
            continue
        curve.append({"bin": b, "p_mean": float(p_test[m].mean()),
                      "empirical": float(y_meta[te][m].mean()),
                      "count": int(m.sum())})

    auc = float(roc_auc_score(y_meta[te], p_test)) \
        if len(np.unique(y_meta[te])) > 1 else 0.5
    result: Dict[str, Any] = {
        "primary_model": primary_name,
        "n_meta_samples": int(len(idx)),
        "segments": {"train": int(tr.sum()), "calibration": int(ca.sum()),
                     "test": int(te.sum())},
        "base_rate_test": float(y_meta[te].mean()),
        "auc_test": auc,
        "brier_test": float(brier_score_loss(y_meta[te], p_test)),
        "tau": tau,
        "best_tau_from_calibration": best_tau,
        "tau_sweep_calibration": sweep,
        "calibration_method": getattr(calib, "iap_calibration_method", ""),
        "calibration_positives": int(
            getattr(calib, "iap_calibration_positives", 0)),
        # A gate that declines EVERY test signal is degenerate: say so
        # explicitly instead of reporting it as an economic decision.
        "gate_degenerate": bool(
            int(np.sum(p_test >= tau)) == 0
            and int(np.sum(p_test >= best_tau)) == 0),
        "n_taken_at_tau": int(np.sum(p_test >= tau)),
        "n_taken_at_best_tau": int(np.sum(p_test >= best_tau)),
        "economics_gate_off": econ_off,
        "economics_gate_tau": econ_tau,
        "economics_gate_best_tau": econ_best,
        "calibration_curve": curve,
    }

    if tracker is not None:
        run_id = tracker.new_run(f"metalabel_{primary_name}")
        # The meta run's "folds" are its three chronological segments: the
        # manifest has to say which rows trained, calibrated and scored, or
        # the fit cannot be replayed (spec §14/§26).
        segment_records = [
            {"segment": label, "n": int(mask.sum()),
             "window": [int(ts[mask].min()), int(ts[mask].max())]}
            for label, mask in (("train", tr), ("calibration", ca),
                                ("test", te))]
        tracker.write_manifest(
            run_id,
            model_version="metalabel_isotonic_v1",
            hyperparams=dict(META_HYPERPARAMS),
            train_window={"start_ts": int(ts[tr].min()),
                          "end_ts": int(ts[tr].max())},
            test_window={"start_ts": int(ts[te].min()),
                         "end_ts": int(ts[te].max())},
            features=list(META_FEATURE_NAMES),
            target=("meta_label_net_pnl_positive (1 iff the primary signal's "
                    "realized net P&L > 0 under the conservative cost model; "
                    f"primary = {primary_name})"),
            folds=segment_records,
        )
        tracker.write_metrics(run_id, result)
        tracker.save_model(run_id, calib)
        result["run_id"] = run_id
    return result
