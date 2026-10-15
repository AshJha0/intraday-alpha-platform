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
  The percentages are of ROW MASS (quantiles of the row index), never of the
  wall-clock span — this data occupies a fraction of each calendar day, and
  wall-span boundaries put the calibration segment in the dormant overnight
  stretch instead of in a quarter of the samples.
  Both boundaries are PURGED: earlier-side rows survive only when
  ``ts + TARGET_HORIZON_NS <= boundary`` so no train/calibration label
  window overlaps the following segment; the label horizon is asserted to
  be shorter than the embargo.
- Probability calibration: isotonic regression via sklearn's
  ``CalibratedClassifierCV`` fitted on the calibration segment (prefit
  semantics; FrozenEstimator on sklearn >= 1.6).
- Evaluation is economic — P&L per signal with and without the meta-gate —
  in addition to AUC/Brier, per the spec's "not only AUC" requirement.

**v1.17 (WS-D item D2, review finding R-5).**  The three-way split above
chose ``best_tau`` on the calibration segment, i.e. on the rows the
calibrator itself was fitted on, so the sweep read an optimistic
probability.  ``split_mode="calibrate_select"`` (opt-in) splits the OOS
region four ways, 40 / 20 / 20 / 20 meta-train / calibrate / select / test
(row mass, purged and embargoed at every boundary): the calibrator is
fitted on *calibrate*, ``best_tau`` is chosen on *select* and both taus are
read on *test*.  ``primary_selection_window`` names the fold the primary
signal was selected on (its ``[start_ts, end_ts]``): calibration rows inside
it are purged, so the calibration fold never overlaps that fold, and the
result document records the split (``split_mode``, ``segments``,
``selection_segment``, ``primary_selection_window``,
``n_calibration_rows_purged_for_primary_selection``).  The default
``split_mode="legacy"`` is the three-way split, unchanged, so every stored
``run_*`` result reproduces.  :func:`meta_probability_hook` turns a
calibrated probability column into a :data:`iap.backtest.engine.SizingHook`.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import brier_score_loss, roc_auc_score

from iap.experiment.tracker import ExperimentTracker
from iap.models.dataset import META_CONTEXT_COLUMNS, TARGET_HORIZON_NS, Dataset
from iap.models.economics import realized_net, signal_directions

_EMBARGO_NS = 60_000_000_000

# The purge below drops train-side rows whose forward label window crosses a
# segment boundary; the embargo then keeps the next segment clear of anything
# overlapping the boundary. That construction is only leak-free while the
# label horizon fits inside the embargo.
assert TARGET_HORIZON_NS < _EMBARGO_NS, (
    f"meta-label split requires label horizon < embargo ({TARGET_HORIZON_NS} >= {_EMBARGO_NS})"
)

#: Split modes of :func:`run_meta_labeling` (module docs): the v1.16 three-way
#: split and the v1.17 four-way calibrate/select split (R-5).
SPLIT_MODES = ("legacy", "calibrate_select")
#: Row-mass boundaries of each split mode (fractions of the meta rows).
_SPLIT_BOUNDS = {"legacy": (0.50, 0.75), "calibrate_select": (0.40, 0.60, 0.80)}
#: Multiplier rules of :func:`meta_probability_hook`.
HOOK_MODES = ("gate", "linear")

#: Pinned meta-classifier hyperparameters (recorded in the run manifest).
META_HYPERPARAMS: dict[str, Any] = {
    "max_iter": 80,
    "max_depth": 3,
    "learning_rate": 0.1,
    "min_samples_leaf": 50,
    "random_state": 7,
}

META_FEATURE_NAMES = (
    "alpha_strength",  # |primary prediction|
    "alpha_signed",  # primary prediction
    "spread_bps",
    "rvol_1m",
    "log_depth_l1",
    "queue_imbalance_signed",  # book imbalance aligned with trade direction
    "half_spread_cost_bps",
    "expected_impact_bps",
)


def build_meta_features(
    pred: np.ndarray, direction: np.ndarray, meta_context: np.ndarray, impute_nan: bool = False
) -> np.ndarray:
    """Assemble the pinned meta-feature matrix.

    ``impute_nan=False`` (the default since v1.5.0) keeps NaN for a missing
    value (infinities become NaN too): ``HistGradientBoostingClassifier``
    handles NaN natively, with a learned missing-value direction per split.
    ``impute_nan=True`` is the LEGACY rule, the default up to v1.4.0: every
    non-finite entry becomes 0.  That is a weakness for the tree model — 0
    is a legitimate value of several meta-features
    (``queue_imbalance_signed``, ``alpha_signed``), so "missing" and
    "balanced" become indistinguishable — and it stays selectable by name
    so the model runs up to ``run_0040`` can be reproduced.
    """
    if meta_context.shape[1] != len(META_CONTEXT_COLUMNS):
        raise ValueError("meta_context has wrong column count")
    spread = meta_context[:, 0]
    rvol = meta_context[:, 1]
    depth = meta_context[:, 2]
    imb = meta_context[:, 3]
    half_cost = meta_context[:, 4]
    impact = meta_context[:, 5]
    X = np.column_stack(
        [
            np.abs(pred),
            pred,
            spread,
            rvol,
            np.log1p(np.maximum(depth, 0.0)),
            imb * direction,
            half_cost,
            impact,
        ]
    )
    return np.where(np.isfinite(X), X, 0.0 if impute_nan else np.nan)


#: Minimum POSITIVE calibration samples before isotonic regression is used
#: (pinned, round-3).  Isotonic is non-parametric: with a 7 % base rate and a
#: few dozen positives it interpolates noise into a step function whose steps
#: sit above every achievable probability, so the gate declines 100 % of test
#: signals for every tau and the "economically correct" reading is an
#: artefact of the calibrator, not of the signal.  Below the threshold the
#: platform falls back to Platt scaling (sigmoid, 2 parameters).
MIN_ISOTONIC_POSITIVES = 500


def _fit_isotonic_calibrated(base: Any, X_cal: np.ndarray, y_cal: np.ndarray) -> Any:
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


def _economics_from_mask(
    direction: np.ndarray, y_mid: np.ndarray, y_cost: np.ndarray, take: np.ndarray
) -> dict[str, float]:
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
    tracker: ExperimentTracker | None = None,
    primary_name: str = "primary",
    tau: float = 0.5,
    impute_nan: bool = False,
    ledger=None,
    split_mode: str = "legacy",
    primary_selection_window: tuple[int, int] | None = None,
    return_model: bool = False,
) -> dict[str, Any]:
    """Train + calibrate + economically evaluate the meta-label gate.

    ``split_mode`` (v1.17 D2, module docs): ``"legacy"`` (default; the
    v1.16 three-way split, ``best_tau`` chosen on the calibration segment)
    or ``"calibrate_select"`` (four-way; ``best_tau`` chosen on a select
    segment disjoint from the calibration segment).
    ``primary_selection_window`` = ``(start_ts, end_ts)`` of the fold the
    primary signal was selected on; calibration rows inside it are purged
    and the count is recorded.  ``return_model=True`` adds the fitted
    calibrator under ``"calibrator"`` (not JSON; added after the tracker and
    ledger writes).

    ``ledger`` (an :class:`iap.validation.ledger.ExperimentLedger`, v1.17
    review R-4) records the tau decision - the pinned ``tau`` read on the
    test segment and ``best_tau`` chosen on the calibration segment and
    read on the test segment - as ONE look of kind ``"metalabel_tau"``
    (identity: the primary model, ``tau``, the sweep grid rule and the
    imputation); the caller saves the ledger.

    ``primary_pred`` is the pooled OOS prediction vector (NaN where a sample
    was never out-of-sample).  ``tau`` is the pinned probability threshold of
    the meta-gate; a calibration-set sweep is also reported.
    ``impute_nan`` is passed to :func:`build_meta_features` (default
    ``False``: NaN is kept for the tree model; ``True`` is the legacy NaN ->
    0 imputation) and recorded in the result and the run manifest.
    ``auc_test`` is ``None`` when the test segment holds a single class
    (AUC undefined).
    """
    primary_pred = np.asarray(primary_pred, dtype=np.float64)
    if len(primary_pred) != len(ds):
        raise ValueError("primary_pred length mismatch with dataset")

    half_bps = ds.meta_context[:, 4]
    cost_est = np.maximum(2.0 * np.where(np.isfinite(half_bps), half_bps, 0.0) / 1e4, 0.0)
    direction = signal_directions(np.nan_to_num(primary_pred, nan=0.0), cost_est)
    usable = np.isfinite(primary_pred) & (direction != 0)
    idx = np.flatnonzero(usable)
    if len(idx) < 300:
        raise ValueError(f"too few meta samples: {len(idx)}")

    ts = ds.ts[idx]
    net = realized_net(direction[idx], ds.y_mid[idx], ds.y[idx])
    y_meta = (net > 0.0).astype(np.int8)
    X_meta = build_meta_features(
        primary_pred[idx], direction[idx], ds.meta_context[idx], impute_nan=impute_nan
    )

    if split_mode not in SPLIT_MODES:
        raise ValueError(f"unknown split_mode {split_mode!r}; known: {SPLIT_MODES}")
    sel_lo = sel_hi = 0
    if primary_selection_window is not None:
        sel_lo, sel_hi = (int(v) for v in primary_selection_window)
        if sel_lo > sel_hi:
            raise ValueError("primary_selection_window must be (start_ts, end_ts)")
    # chronological split with embargo (ts is already sorted because the
    # dataset is sorted by exchange_ts): 50/25/25 train / calibration / test
    # ("legacy") or 40/20/20/20 train / calibration / select / test
    # ("calibrate_select", v1.17 R-5).  PURGING (pinned): rows on the
    # earlier side of each boundary are kept only when their forward label
    # window closes at or before the boundary (ts + TARGET_HORIZON_NS <=
    # boundary) — a row whose label overlaps the boundary would leak the
    # later segment's outcomes into training.  Boundaries at quantiles of
    # the ROW INDEX, not of the wall span (pinned, round-4 — the same
    # correction both walk-forward splitters already carry,
    # iap.validation.splits.folds_by_row_mass and iap.models.splits.split).
    # The frames are not uniform in time (an equity session is 6.5 h of each
    # 24 h day), so wall-span boundaries do not give the documented shares:
    # on the dataset this was found on (v1.3.0 and earlier, 206 190 rows)
    # they gave 50.0 / 6.0 / 43.9 and the calibration window landed almost
    # entirely in the dormant overnight stretch.
    n_rows = ts.size
    bounds = [int(ts[min(int(round(f * n_rows)), n_rows - 1)]) for f in _SPLIT_BOUNDS[split_mode]]
    if not all(a < b for a, b in zip([int(ts[0])] + bounds[:-1], bounds, strict=True)):
        raise ValueError(
            "meta split boundaries are not strictly increasing (too many identical timestamps)"
        )

    def _segment(lo: int | None, hi: int | None) -> np.ndarray:
        m = np.ones(n_rows, dtype=bool)
        if lo is not None:
            m &= ts > lo + _EMBARGO_NS
        if hi is not None:
            m &= ts + TARGET_HORIZON_NS <= hi
        return m

    edges: list[int | None] = [None, *bounds, None]
    segs = [_segment(edges[k], edges[k + 1]) for k in range(len(edges) - 1)]
    if split_mode == "legacy":
        tr, ca, te = segs
        se = ca  # the v1.16 rule: tau selected on the calibration rows
        selection_segment = "calibration"
    else:
        tr, ca, se, te = segs
        selection_segment = "select"
    n_purged_sel = 0
    if primary_selection_window is not None:
        inside = (ts >= sel_lo) & (ts <= sel_hi)
        n_purged_sel = int(np.sum(ca & inside))
        ca = ca & ~inside
        if split_mode == "legacy":
            se = ca
    if tr.sum() < 100 or ca.sum() < 50 or se.sum() < 50 or te.sum() < 50:
        raise ValueError("meta split produced degenerate segments")

    base = HistGradientBoostingClassifier(**META_HYPERPARAMS)
    base.fit(X_meta[tr], y_meta[tr])
    calib = _fit_isotonic_calibrated(base, X_meta[ca], y_meta[ca])

    p_test = calib.predict_proba(X_meta[te])[:, 1]
    p_sel = calib.predict_proba(X_meta[se])[:, 1]

    # tau sweep on the SELECTION segment (never on test): the calibration
    # rows under "legacy", the disjoint select rows under "calibrate_select"
    # (R-5).  Grid = pinned absolute levels + deciles of the calibrated
    # probabilities (the base rate of profitable trades can sit far below
    # 0.5, so absolute levels alone can land above the whole probability
    # distribution).
    fixed = [round(0.30 + 0.05 * i, 2) for i in range(9)]  # 0.30 .. 0.70
    p_sel_deciles = [float(np.quantile(p_sel, q)) for q in (0.1, 0.25, 0.5, 0.75, 0.9)]
    taus = sorted(set(round(t, 6) for t in fixed + p_sel_deciles))
    sel_net = realized_net(direction[idx][se], ds.y_mid[idx][se], ds.y[idx][se])
    sweep = []
    for t in taus:
        take = p_sel >= t
        sweep.append(
            {
                "tau": t,
                "n_trades": int(take.sum()),
                "total_net_bps": float(sel_net[take].sum() * 1e4),
            }
        )
    best_tau = max(sweep, key=lambda r: r["total_net_bps"])["tau"]

    # economics on the TEST segment: gate off / gate at tau / gate at best_tau
    d_te = direction[idx][te]
    ym_te, yc_te = ds.y_mid[idx][te], ds.y[idx][te]
    econ_off = _economics_from_mask(d_te, ym_te, yc_te, np.ones(len(d_te), dtype=bool))
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
        curve.append(
            {
                "bin": b,
                "p_mean": float(p_test[m].mean()),
                "empirical": float(y_meta[te][m].mean()),
                "count": int(m.sum()),
            }
        )

    # AUC is undefined on a single-class test segment.  It is reported as
    # ``None`` (JSON null), never as a fabricated 0.5: 0.5 reads as "the
    # model was evaluated and has no skill", which is a different statement
    # from "the segment could not rank anything".
    auc: float | None = (
        float(roc_auc_score(y_meta[te], p_test)) if len(np.unique(y_meta[te])) > 1 else None
    )
    result: dict[str, Any] = {
        "primary_model": primary_name,
        "impute_nan": bool(impute_nan),
        "n_missing_meta_values": int(np.sum(~np.isfinite(X_meta))) if not impute_nan else None,
        "n_meta_samples": int(len(idx)),
        "split_mode": split_mode,
        "segments": {
            "train": int(tr.sum()),
            "calibration": int(ca.sum()),
            "select": int(se.sum()),
            "test": int(te.sum()),
        },
        "segment_windows": {
            label: [int(ts[mask].min()), int(ts[mask].max())]
            for label, mask in (("train", tr), ("calibration", ca), ("select", se), ("test", te))
        },
        "selection_segment": selection_segment,
        "primary_selection_window": (
            None if primary_selection_window is None else [sel_lo, sel_hi]
        ),
        "n_calibration_rows_purged_for_primary_selection": n_purged_sel,
        "calibration_overlaps_primary_selection": (
            None if primary_selection_window is None else False
        ),
        "base_rate_test": float(y_meta[te].mean()),
        "auc_test": auc,
        "brier_test": float(brier_score_loss(y_meta[te], p_test)),
        "tau": tau,
        "best_tau_from_calibration": best_tau,  # chosen on ``selection_segment``
        "tau_sweep_calibration": sweep,  # the sweep on ``selection_segment``
        "calibration_method": getattr(calib, "iap_calibration_method", ""),
        "calibration_positives": int(getattr(calib, "iap_calibration_positives", 0)),
        # A gate that declines EVERY test signal is degenerate: say so
        # explicitly instead of reporting it as an economic decision.
        "gate_degenerate": bool(
            int(np.sum(p_test >= tau)) == 0 and int(np.sum(p_test >= best_tau)) == 0
        ),
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
            {
                "segment": label,
                "n": int(mask.sum()),
                "window": [int(ts[mask].min()), int(ts[mask].max())],
            }
            for label, mask in (
                ("train", tr),
                ("calibration", ca),
                *((("select", se),) if selection_segment == "select" else ()),
                ("test", te),
            )
        ]
        tracker.write_manifest(
            run_id,
            model_version="metalabel_isotonic_v1",
            hyperparams={
                **META_HYPERPARAMS,
                "impute_nan": bool(impute_nan),
                "split_mode": split_mode,
            },
            train_window={"start_ts": int(ts[tr].min()), "end_ts": int(ts[tr].max())},
            test_window={"start_ts": int(ts[te].min()), "end_ts": int(ts[te].max())},
            features=list(META_FEATURE_NAMES),
            target=(
                "meta_label_net_pnl_positive (1 iff the primary signal's "
                "realized net P&L > 0 under the conservative cost model; "
                f"primary = {primary_name})"
            ),
            folds=segment_records,
        )
        tracker.write_metrics(run_id, result)
        tracker.save_model(run_id, calib)
        result["run_id"] = run_id
    if ledger is not None:
        result["ledger_n"] = ledger.record(
            f"META_{primary_name}",
            "metalabel_tau",
            config={
                "primary_model": primary_name,
                "tau": float(tau),
                "tau_grid": "0.30..0.70 step 0.05 + calibration deciles 10/25/50/75/90",
                "impute_nan": bool(impute_nan),
                "split_mode": split_mode,
                "selection": f"best_tau = argmax total_net_bps on the {selection_segment} segment",
                "primary_selection_window": (
                    None if primary_selection_window is None else [sel_lo, sel_hi]
                ),
            },
            result={
                "best_tau_from_calibration": float(best_tau),
                "segments": result["segments"],
                "auc_test": auc,
                "gate_degenerate": result["gate_degenerate"],
                "economics_gate_off": econ_off,
                "economics_gate_tau": econ_tau,
                "economics_gate_best_tau": econ_best,
            },
        )
    if return_model:
        result["calibrator"] = calib
    return result


def meta_probability_hook(
    tau: float, column: str = "meta_prob", mode: str = "gate", missing: float = 0.0
):
    """A :data:`iap.backtest.engine.SizingHook` over a calibrated meta
    probability column of the scores frame (v1.17 D2, opt-in).

    ``mode="gate"``: multiplier 1 when ``p >= tau`` else 0 (the meta-gate as
    a size).  ``mode="linear"``: 0 below ``tau``, ``(p - tau) / (1 - tau)``
    above it (size grows with the calibrated probability).  A missing or
    non-finite probability gives ``missing`` (default 0: no trade).  The
    probabilities are the caller's: score the frame's rows with the
    calibrator :func:`run_meta_labeling` returns (``return_model=True``) and
    write them to ``column`` BEFORE the backtest; the hook only reads.
    """
    if not 0.0 <= tau < 1.0:
        raise ValueError("tau must be in [0, 1)")
    if mode not in HOOK_MODES:
        raise ValueError(f"unknown mode {mode!r}; known: {HOOK_MODES}")
    if not 0.0 <= missing <= 1.0:
        raise ValueError("missing must be in [0, 1]")

    def hook(row) -> float:
        p = row.scores.get(column)
        if p is None or not np.isfinite(p):
            return missing
        if mode == "gate":
            return 1.0 if p >= tau else 0.0
        return float(min(max((p - tau) / (1.0 - tau), 0.0), 1.0))

    hook.__name__ = f"meta_probability_hook[{column},{mode},tau={tau}]"
    return hook
