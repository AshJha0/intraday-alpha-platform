"""Walk-forward model comparison with the literal baseline gate (spec §14).

Flow (pinned):

1. Tier 0 (OLS / Ridge / ElasticNet) is trained and evaluated OOS on every
   walk-forward fold (expanding train, embargoed + purged test).
2. **The gate**: advanced models run only after the linear baseline shows
   positive out-of-sample IC — literally
   ``gate_passed = (best tier-0 mean OOS IC) > 0.0``.
3. If the gate passes, Tier 1 (trees) and Tier 2 (MLP) run on the same
   folds; otherwise they are skipped and the skip is recorded.

**v1.17 WS-D (review finding R-6; opt-in).**  The literal rule reads ONE
number pooled across fits with no significance: a pooled IC of +0.001
passes it.  ``gate="clustered"`` reads instead the fold-mean rank IC
against the mid-to-mid label (``mean_rank_ic_vs_mid``) and the
day-clustered t of the pooled OOS predictions
(:func:`iap.validation.metrics.gate_statistics` on the predictions
standardised per fold - each fold is its own fit - against the mid label),
and passes only when the mean rank IC is > 0 AND that t clears
``gate_t_threshold`` (default :data:`DEFAULT_GATE_T_THRESHOLD` = 2.0).
Both readings are always computed and recorded (``results["gate"]
["literal"]`` / ``["clustered"]`` and the ledger look); ``gate_rule``
names the one that decided.  The default stays ``"literal"`` so the
regenerated ML report is unchanged until the owner flips it.

Every trained model writes a tracker run: manifest + metrics + pickled model
(the model saved is the fit from the FINAL fold — the largest purged train
window).  Predictions are also returned pooled across folds for the
meta-labeling stage.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from iap.experiment.tracker import ExperimentTracker
from iap.models.dataset import (
    TARGET_COLUMN,
    TARGET_HORIZON_NS,
    Dataset,
    TrainScaler,
)
from iap.models.economics import signal_economics
from iap.models.splits import Fold, WalkForwardSplitter
from iap.models.zoo import (
    HYPERPARAMS,
    TREE_FALLBACK_ACTIVE,
    make_model,
    model_names,
)
from iap.validation.metrics import _ranks as _avg_ranks
from iap.validation.metrics import gate_statistics

#: Column of Dataset.meta_context holding half_spread_cost_bps_v1.
_HALF_SPREAD_COL = 4

#: The model-zoo gate rules (v1.17 WS-D, R-6): the literal v1 rule
#: (default) and the clustered rule.
GATE_RULES = ("literal", "clustered")
DEFAULT_GATE_RULE = "literal"
#: Day-clustered t the clustered gate requires of the best linear model.
DEFAULT_GATE_T_THRESHOLD = 2.0


def _asset_classes(ds, idx: np.ndarray) -> list:
    """Sorted asset classes present in a row subset (fold composition)."""
    inst = getattr(ds, "instrument_ids", None)
    if inst is None or len(inst) == 0:
        return []
    ids = np.asarray(inst)[idx]
    return sorted({"FX" if int(i) >= 100 else "EQUITY" for i in ids})


def information_coefficient(pred: np.ndarray, y: np.ndarray) -> float:
    """Pearson IC; **NaN** when degenerate (constant predictions).

    Round-3: a degenerate fold used to return 0.0, which then entered
    ``mean_ic`` as if it were evidence of "no skill" and diluted the mean.
    NaN is the same convention as :func:`iap.validation.metrics.ic`, and the
    caller reports how many folds were degenerate.
    """
    pred = np.asarray(pred, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    if len(pred) != len(y):
        raise ValueError("pred/y length mismatch")
    if len(pred) < 3 or np.std(pred) == 0.0 or np.std(y) == 0.0:
        return float("nan")
    return float(np.corrcoef(pred, y)[0, 1])


def rank_ic(pred: np.ndarray, y: np.ndarray) -> float:
    """Spearman rank IC. Ties receive the AVERAGE rank (the platform's pinned
    ranking, reused from :func:`iap.validation.metrics._ranks` — argsort alone
    would order ties arbitrarily and bias the correlation)."""
    return information_coefficient(
        _avg_ranks(np.asarray(pred, dtype=np.float64)),
        _avg_ranks(np.asarray(y, dtype=np.float64)),
    )


def ic_tstat(fold_ics: list[float]) -> float:
    """t-statistic of the per-fold IC series (mean / stderr)."""
    if len(fold_ics) < 2:
        return 0.0
    arr = np.asarray(fold_ics, dtype=np.float64)
    sd = float(arr.std(ddof=1))
    if sd == 0.0:
        return 0.0
    return float(arr.mean() / (sd / math.sqrt(len(arr))))


def _standardise(pred: np.ndarray) -> np.ndarray:
    """``pred`` at zero mean and unit scale (NaN when it has no spread)."""
    pred = np.asarray(pred, dtype=np.float64)
    sd = float(np.std(pred)) if pred.size else 0.0
    if not sd > 0.0:
        return np.full(pred.shape, np.nan)
    return (pred - float(np.mean(pred))) / sd


def _cost_estimate(ds: Dataset, idx: np.ndarray) -> np.ndarray:
    """Observable round-trip cost estimate (return units) at decision time.

    Floored at zero: the synthetic consolidated book is occasionally crossed
    (negative spread), and a negative *expected* cost would be a synthetic
    arbitrage no production rule should assume.
    """
    half_bps = ds.meta_context[idx, _HALF_SPREAD_COL]
    half_bps = np.where(np.isfinite(half_bps), half_bps, 0.0)
    return np.maximum(2.0 * half_bps / 1e4, 0.0)


def _evaluate_model(
    name: str,
    ds: Dataset,
    folds: list[Fold],
) -> dict[str, Any]:
    """Train/evaluate one model across all folds; returns metrics + preds."""
    per_fold: list[dict[str, float]] = []
    pooled_pred = np.full(len(ds), np.nan)
    pooled_pred_std = np.full(len(ds), np.nan)
    final_model: Any = None
    for fold in folds:
        scaler = TrainScaler().fit(ds.X[fold.train_idx])
        X_tr = scaler.transform(ds.X[fold.train_idx])
        X_te = scaler.transform(ds.X[fold.test_idx])
        y_tr = ds.y[fold.train_idx]
        model = make_model(name)
        model.fit(X_tr, y_tr)
        pred = np.asarray(model.predict(X_te), dtype=np.float64)
        pooled_pred[fold.test_idx] = pred
        # v1.17 WS-D (R-6): each fold is its own fit, so the pooled series
        # the clustered t reads is standardised per fold (R-9's lesson).
        pooled_pred_std[fold.test_idx] = _standardise(pred)
        y_te = ds.y[fold.test_idx]
        ic_f = information_coefficient(pred, y_te)
        per_fold.append(
            {
                "fold": fold.fold,
                "n_train": int(len(fold.train_idx)),
                "n_test": int(len(fold.test_idx)),
                "train_window": [int(fold.train_window[0]), int(fold.train_window[1])],
                "test_window": [int(fold.test_window[0]), int(fold.test_window[1])],
                "train_asset_classes": _asset_classes(ds, fold.train_idx),
                "test_asset_classes": _asset_classes(ds, fold.test_idx),
                "ic": ic_f,
                "rank_ic": rank_ic(pred, y_te),
                # v1.17 WS-D (R-6): the clustered gate's per-fold input
                "rank_ic_vs_mid": rank_ic(pred, ds.y_mid[fold.test_idx]),
                "degenerate": bool(not np.isfinite(ic_f)),
            }
        )
        final_model = model  # last fold = largest purged train window

    oos_mask = np.isfinite(pooled_pred)
    idx = np.flatnonzero(oos_mask)
    cost_est = _cost_estimate(ds, idx)
    econ = signal_economics(pooled_pred[idx], ds.y_mid[idx], ds.y[idx], cost_est, conservative=True)
    econ_exact = signal_economics(
        pooled_pred[idx], ds.y_mid[idx], ds.y[idx], cost_est, conservative=False
    )
    fold_ics = [f["ic"] for f in per_fold if np.isfinite(f["ic"])]
    rank_ics = [f["rank_ic"] for f in per_fold if np.isfinite(f["rank_ic"])]
    rank_ics_mid = [f["rank_ic_vs_mid"] for f in per_fold if np.isfinite(f["rank_ic_vs_mid"])]
    clustered = gate_statistics(
        ds.ts[idx], pooled_pred_std[idx], ds.y_mid[idx], horizon_ns=TARGET_HORIZON_NS
    )
    return {
        "model": name,
        "per_fold": per_fold,
        "n_folds": len(per_fold),
        "n_degenerate_folds": sum(1 for f in per_fold if f["degenerate"]),
        "asset_mix_warning": [
            f["fold"]
            for f in per_fold
            if set(f["test_asset_classes"]) - set(f["train_asset_classes"])
        ],
        "mean_ic": float(np.mean(fold_ics)) if fold_ics else float("nan"),
        "mean_rank_ic": float(np.mean(rank_ics)) if rank_ics else float("nan"),
        "ic_tstat": ic_tstat(fold_ics),
        "pooled_ic": information_coefficient(pooled_pred[idx], ds.y[idx]),
        # IC against the MID-TO-MID label: the honest directional-signal
        # measure — the cost-adjusted target contains an observable spread
        # component that inflates raw IC.
        "pooled_ic_vs_mid": information_coefficient(pooled_pred[idx], ds.y_mid[idx]),
        # v1.17 WS-D (R-6): the clustered gate's readings, for every model.
        "mean_rank_ic_vs_mid": float(np.mean(rank_ics_mid)) if rank_ics_mid else float("nan"),
        "tstat_day_cluster_vs_mid": float(clustered["tstat_day_cluster"]),
        "tstat_hac_vs_mid": float(clustered["tstat_hac"]),
        "n_days": int(clustered["n_days"]),
        "economics": econ,
        "economics_label_exact": econ_exact,
        "_pooled_pred": pooled_pred,
        "_final_model": final_model,
    }


def run_model_comparison(
    ds: Dataset,
    tracker: ExperimentTracker | None = None,
    n_folds: int = 4,
    embargo_ns: int = 60_000_000_000,
    ledger=None,
    gate: str = DEFAULT_GATE_RULE,
    gate_t_threshold: float = DEFAULT_GATE_T_THRESHOLD,
) -> dict[str, Any]:
    """Full gated comparison. Returns per-model metrics + gate record.

    ``gate`` (v1.17 WS-D, R-6) names the rule that decides: ``"literal"``
    (default, the pooled mid IC > 0) or ``"clustered"`` (fold-mean rank IC
    vs mid > 0 AND day-clustered t >= ``gate_t_threshold``); both readings
    are recorded whichever decides (module docs).

    When ``tracker`` is given, each trained model becomes a tracked run with
    manifest.json / metrics.json / model.pkl per spec §14/§26.

    ``ledger`` (an :class:`iap.validation.ledger.ExperimentLedger`, v1.17
    review R-4) records the gate decision as ONE look of kind
    ``"model_zoo_gate"`` (identity: the target, the predictor set, the fold
    protocol and the tier-0 model names; result: the gate record and every
    model's pooled ICs) so the model-zoo look enters the multiple-testing
    denominator the alpha reports quote.  The caller saves the ledger; the
    running total is returned as ``results["gate"]["ledger_n"]``.
    """
    if gate not in GATE_RULES:
        raise ValueError(f"unknown gate {gate!r}; known: {GATE_RULES}")
    if not np.isfinite(gate_t_threshold) or gate_t_threshold <= 0.0:
        raise ValueError("gate_t_threshold must be a positive number")
    splitter = WalkForwardSplitter(
        n_folds=n_folds, embargo_ns=embargo_ns, label_horizon_ns=TARGET_HORIZON_NS
    )
    folds = splitter.split(ds.ts)

    fold_records = [
        {
            "fold": f.fold,
            "n_train": int(len(f.train_idx)),
            "n_test": int(len(f.test_idx)),
            "train_window": [int(f.train_window[0]), int(f.train_window[1])],
            "test_window": [int(f.test_window[0]), int(f.test_window[1])],
        }
        for f in folds
    ]
    results: dict[str, Any] = {"models": {}, "folds": fold_records}

    def _track(res: dict[str, Any]) -> None:
        if tracker is None:
            return
        name = res["model"]
        run_id = tracker.new_run(name)
        last = folds[-1]
        # WHICH columns and WHICH rows: without features/target/folds a
        # manifest names a commit but cannot reproduce the fit (spec §14/§26).
        tracker.write_manifest(
            run_id,
            model_version=f"{name}_v1",
            hyperparams=dict(HYPERPARAMS.get(name, {})),
            train_window={
                "start_ts": int(last.train_window[0]),
                "end_ts": int(last.train_window[1]),
            },
            test_window={"start_ts": int(last.test_window[0]), "end_ts": int(last.test_window[1])},
            features=list(ds.feature_names),
            target=TARGET_COLUMN,
            folds=fold_records,
        )
        tracker.write_metrics(run_id, {k: v for k, v in res.items() if not k.startswith("_")})
        tracker.save_model(run_id, res["_final_model"])
        res["run_id"] = run_id

    # ---- Tier 0: linear baselines (always run)
    for name in model_names(0):
        res = _evaluate_model(name, ds, folds)
        _track(res)
        results["models"][name] = res

    # ---- The gate (literal): the best linear model must show a positive
    # OOS IC against the MID-TO-MID label.  The cost-adjusted target embeds
    # the observable half-spread (corr(spread, target) ~ -0.94), so gating on
    # it passed trivially — it measured spread, not direction (round-3).
    tier0_ics = {n: results["models"][n]["pooled_ic_vs_mid"] for n in model_names(0)}
    best_linear = max(
        tier0_ics, key=lambda n: tier0_ics[n] if np.isfinite(tier0_ics[n]) else -np.inf
    )
    literal_passed = bool(np.isfinite(tier0_ics[best_linear]) and tier0_ics[best_linear] > 0.0)
    # v1.17 WS-D (R-6): the clustered reading - the best tier-0 model by
    # fold-mean rank IC vs mid, with the day-clustered t of its pooled
    # (per-fold standardised) OOS predictions against the mid label.
    tier0_rank = {n: results["models"][n]["mean_rank_ic_vs_mid"] for n in model_names(0)}
    best_clustered = max(
        tier0_rank, key=lambda n: tier0_rank[n] if np.isfinite(tier0_rank[n]) else -np.inf
    )
    t_cluster = float(results["models"][best_clustered]["tstat_day_cluster_vs_mid"])
    clustered_passed = bool(
        np.isfinite(tier0_rank[best_clustered])
        and tier0_rank[best_clustered] > 0.0
        and np.isfinite(t_cluster)
        and t_cluster >= float(gate_t_threshold)
    )
    gate_passed = clustered_passed if gate == "clustered" else literal_passed
    literal_rule = (
        "advanced models run only if the best linear pooled OOS IC "
        "vs the MID-TO-MID label is > 0 (the cost-adjusted target "
        "embeds the observable half-spread)"
    )
    clustered_rule = (
        "advanced models run only if the best linear fold-mean rank IC vs the "
        "MID-TO-MID label is > 0 AND the day-clustered t of its pooled "
        f"(per-fold standardised) OOS predictions is >= {float(gate_t_threshold):g}"
    )
    results["gate"] = {
        "rule": clustered_rule if gate == "clustered" else literal_rule,
        "gate_rule": gate,
        "best_linear_model": best_clustered if gate == "clustered" else best_linear,
        "best_linear_pooled_ic_vs_mid": tier0_ics[best_linear],
        "best_linear_mean_oos_ic": results["models"][best_linear]["mean_ic"],
        "passed": bool(gate_passed),
        "tree_fallback_active": TREE_FALLBACK_ACTIVE,
        # both readings, whichever decided (v1.17 WS-D, R-6)
        "literal": {
            "rule": literal_rule,
            "best_linear_model": best_linear,
            "pooled_ic_vs_mid": float(tier0_ics[best_linear]),
            "passed": literal_passed,
        },
        "clustered": {
            "rule": clustered_rule,
            "best_linear_model": best_clustered,
            "mean_rank_ic_vs_mid": float(tier0_rank[best_clustered]),
            "tstat_day_cluster": t_cluster,
            "tstat_hac": float(results["models"][best_clustered]["tstat_hac_vs_mid"]),
            "n_days": int(results["models"][best_clustered]["n_days"]),
            "t_threshold": float(gate_t_threshold),
            "passed": clustered_passed,
        },
    }

    # ---- Tier 1 + 2: only behind the gate
    skipped: list[str] = []
    for tier in (1, 2):
        for name in model_names(tier):
            if not gate_passed:
                skipped.append(name)
                continue
            res = _evaluate_model(name, ds, folds)
            _track(res)
            results["models"][name] = res
    results["gate"]["skipped_models"] = skipped
    if ledger is not None:
        results["gate"]["ledger_n"] = ledger.record(
            "ML_ZOO",
            "model_zoo_gate",
            config={
                "target": TARGET_COLUMN,
                "features": list(ds.feature_names),
                "n_folds": int(n_folds),
                "embargo_ns": int(embargo_ns),
                "tier0_models": sorted(model_names(0)),
                "rule": results["gate"]["rule"],
            },
            result={
                "passed": bool(gate_passed),
                "gate_rule": gate,
                "best_linear_model": results["gate"]["best_linear_model"],
                "best_linear_pooled_ic_vs_mid": float(tier0_ics[best_linear]),
                # v1.17 WS-D (R-6): both readings on the look
                "literal": dict(results["gate"]["literal"]),
                "clustered": dict(results["gate"]["clustered"]),
                "models": {
                    n: {
                        "mean_ic": float(r["mean_ic"]),
                        "pooled_ic": float(r["pooled_ic"]),
                        "pooled_ic_vs_mid": float(r["pooled_ic_vs_mid"]),
                        "mean_rank_ic_vs_mid": float(r["mean_rank_ic_vs_mid"]),
                        "tstat_day_cluster_vs_mid": float(r["tstat_day_cluster_vs_mid"]),
                    }
                    for n, r in results["models"].items()
                },
                "skipped_models": skipped,
            },
        )
    return results
