"""The combination as a first-class research object: evaluation and report.

``run_combination`` evaluates, for each asset class, the combination of ALL
of its flagship alphas under each method of :mod:`iap.combine.weights`:

* **the same chain as a single alpha** — every (asset class, method) pair is
  a :class:`iap.combine.model.CombinedAlpha` handed to
  ``iap.validation.validate.validate_alpha`` with the research backtester,
  cost model and method bundle the alpha reports use
  (``research/alpha_reports/run_all.py``): purged and embargoed
  walk-forward, the four leakage detectors, cost / latency / regime stress,
  the bootstrap interval, the PROMOTE gates and the ledger-derived t
  threshold;
* **members** — all alphas of the asset class, whatever their verdict.
  Picking the members that looked good would be survivorship in member
  selection; a caller who passes another list gets another experiment
  identity and pays for it;
* **experiment identity** — ``experiment_id`` is the content hash of the
  asset class, the sorted member list, the method, the horizon, the
  protocol (folds, inner folds, embargo, ridge penalties, method bundle),
  the dataset and feature versions and the bootstrap seed;
* **looks (pinned).**  One combination experiment — one member list under
  one method — costs

      looks_per_validation(n_folds) + K

  looks: the validation chain it runs (83 at four folds) plus one for each
  of its ``K`` members, whose signal is scored against the combination's
  label to build and to report it (the member table).  A report that tries
  ``M`` methods on one member list is ``M`` experiments, ``M * (83 + K)``
  looks, ALL declared to the ledger before the first is evaluated — so the
  method that is reported as the default is judged at a threshold that has
  paid for the alternatives tried beside it.  Combining is a search; the
  ridge penalty grid is not debited separately because it is searched
  inside each training window and never scored on a test row (it is a fitted
  parameter, like a member's ``beta``).  At 12 members, 4 methods and 2
  asset classes: 760 looks;
* **the member pass** — the same outer walk-forward with every member fitted
  on each fold's training rows and scored on its test rows gives, on one
  set of rows: each member's IC and gate t at the combination horizon, the
  signal correlation matrix, the effective number of independent bets, the
  member P&L correlation (1-minute bar net P&L of each member's own
  backtest at its own horizon; undefined for a member that never trades),
  the per-fold weights of every method and the breadth arithmetic.

Outputs (``research/combination/``): ``REPORT.md``, ``COMBINATION.json``
(``x-version`` 1), ``reports/<combination>.<method>.json`` (the full
validation report of each experiment) and — when both asset classes ran
with their full member lists — ``signal_correlation.json``, the pairwise
correlation document the lifecycle's ``cross_alpha_correlation`` gate is
fed from (:func:`correlation_document`).

Everything is deterministic and wall-clock free; a rerun on the same data
reproduces every number and adds nothing to the ledger.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from iap.alpha import ALPHA_IDS, build
from iap.alpha.base import AlphaModel
from iap.alpha.data import load_features, slim_columns
from iap.backtest import Backtester, CostModel
from iap.combine.model import INNER_FOLDS, CombinedAlpha, member_purged_train, member_signal
from iap.combine.weights import (
    DEFAULT_METHOD,
    METHODS,
    MIN_OBS,
    RIDGE_PENALTIES,
    correlation_matrix,
    effective_bets,
)
from iap.contracts.versions import content_hash
from iap.experiment import tracker
from iap.labels.frames import scored_labels
from iap.validation.leakage import RecomputeSources
from iap.validation.ledger import ExperimentLedger
from iap.validation.methods import DEFAULT_METHODS, ResearchMethods, methods
from iap.validation.metrics import HORIZONS_NS, ic, nw_lags, pooled_slope_hac_tstat
from iap.validation.splits import WalkForwardSplitter
from iap.validation.validate import GATES, looks_per_validation, validate_alpha

__all__ = [
    "ASSET_CLASSES",
    "COMBINATION_X_VERSION",
    "CORRELATION_X_VERSION",
    "LEDGER_KIND",
    "combination_looks",
    "correlation_document",
    "experiment_identity",
    "member_pass",
    "render_markdown",
    "run_combination",
    "write_reports",
]

COMBINATION_X_VERSION = 1
CORRELATION_X_VERSION = 1
LEDGER_KIND = "combination"
ASSET_CLASSES = ("EQUITY", "FX")
#: Pinned protocol: the alpha reports' (``research/alpha_reports/run_all.py``).
N_FOLDS = 4
EMBARGO_S = 60
BOOTSTRAP_SEED = 20260829
RESEARCH_LATENCY_NS = 1_000_000_000
RESEARCH_MAX_DECISION_AGE_NS = 60_000_000_000
_SHORT = {"EQUITY": "EQ", "FX": "FX"}


def _fnum(v: Any) -> float | None:
    if v is None:
        return None
    v = float(v)
    return v if np.isfinite(v) else None


def combination_looks(n_members: int, n_folds: int = N_FOLDS) -> int:
    """Looks ONE combination experiment debits (module docs, "looks")."""
    if n_members < 1:
        raise ValueError("a combination has at least one member")
    return looks_per_validation(n_folds) + int(n_members)


def default_members(asset_class: str) -> list[str]:
    """Every flagship alpha of ``asset_class`` (sorted) — no selection."""
    out = sorted(a for a in ALPHA_IDS if build(a).asset_class == asset_class)
    if not out:
        raise ValueError(f"no flagship alpha of asset class {asset_class!r}")
    return out


def experiment_identity(
    asset_class: str,
    members: Sequence[str],
    method: str,
    horizon: str,
    bundle: str = DEFAULT_METHODS,
    n_folds: int = N_FOLDS,
    inner_folds: int = INNER_FOLDS,
    embargo_s: int = EMBARGO_S,
) -> dict[str, Any]:
    """The ledger configuration of one combination experiment — what was
    looked at: the member list and the method are part of it."""
    return {
        "asset_class": asset_class,
        "members": sorted(members),
        "method": method,
        "horizon": horizon,
        "n_folds": int(n_folds),
        "inner_folds": int(inner_folds),
        "embargo_s": int(embargo_s),
        "ridge_penalties": list(RIDGE_PENALTIES),
        "methods": bundle,
    }


def _load_meta(configs_dir: Path) -> dict[int, dict]:
    cfg = json.loads((configs_dir / "instruments" / "instruments.json").read_text())
    meta: dict[int, dict] = {}
    for row in cfg["instruments"]:
        meta[int(row["instrument_id"])] = {
            "symbol": row["symbol"],
            "asset_class": row["asset_class"],
            "tick_size": float(row["tick_size"]),
            "lot_size": int(row["lot_size"]),
            "adv": float(row["adv"]),
            "ref_price": float(row.get("ref_price", 1.0)),
            "base_currency": row.get("base_currency"),
            "quote_currency": row.get("quote_currency", row.get("currency", "USD")),
        }
    return meta


def _backtester(bundle: ResearchMethods, meta: dict[int, dict], configs_dir: Path) -> Backtester:
    cost_model = bundle.cost_model(CostModel.load(configs_dir / "execution" / "execution.json"))
    return Backtester(
        cost_model,
        meta,
        bundle.backtest_config(
            latency_ns=RESEARCH_LATENCY_NS,
            max_decision_age_ns=RESEARCH_MAX_DECISION_AGE_NS,
            flatten_at_session_end=True,
        ),
    )


def _gate_stats(ts, x, y, crossed, horizon: str) -> dict[str, float | None]:
    """IC and pooled-slope HAC t on the rows the gate reads (uncrossed when
    that IC is finite) — the statistics ``validate_alpha`` gates on."""
    unc = ~crossed
    ic_all = ic(x, y)
    ic_unc = ic(x[unc], y[unc])
    lags = nw_lags(HORIZONS_NS[horizon])
    t_all = pooled_slope_hac_tstat(ts, x, y, lags=lags)
    t_unc = pooled_slope_hac_tstat(ts[unc], x[unc], y[unc], lags=lags)
    return {
        "ic": _fnum(ic_all),
        "gate_ic": _fnum(ic_unc if np.isfinite(ic_unc) else ic_all),
        "gate_tstat": _fnum(t_unc if np.isfinite(t_unc) else t_all),
        "n_pairs": int(np.sum(np.isfinite(x) & np.isfinite(y))),
    }


def _bar_series(result) -> dict[int, float]:
    bars: dict[int, float] = {}
    for r in result.per_instrument.values():
        for t, pnl in zip(r.bar_ts, r.bar_pnl, strict=False):
            bars[int(t)] = bars.get(int(t), 0.0) + float(pnl)
    return bars


def _pnl_correlation(
    bars: Mapping[str, Mapping[int, float]], trades: Mapping[str, int], ids: Sequence[str]
) -> list[list[float | None]]:
    """Correlation of the members' 1-minute bar net P&L on their common
    bars; ``None`` when either member never trades or has no variance."""
    k = len(ids)
    out: list[list[float | None]] = [[None] * k for _ in range(k)]
    for i, a in enumerate(ids):
        for j, b in enumerate(ids):
            if j < i:
                out[i][j] = out[j][i]
                continue
            if trades[a] == 0 or trades[b] == 0:
                continue
            common = sorted(set(bars[a]) & set(bars[b]))
            if len(common) < MIN_OBS:
                continue
            x = np.array([bars[a][t] for t in common])
            y = np.array([bars[b][t] for t in common])
            if not (np.std(x) > 0.0 and np.std(y) > 0.0):
                continue
            out[i][j] = 1.0 if i == j else float(np.clip(np.corrcoef(x, y)[0, 1], -1.0, 1.0))
    return out


def member_pass(
    frames: Mapping[int, pd.DataFrame],
    member_ids: Sequence[str],
    horizon: str,
    asset_class: str,
    backtester: Backtester,
    method_names: Sequence[str] = METHODS,
    *,
    ic_rows: str,
    n_folds: int = N_FOLDS,
    embargo_ns: int = EMBARGO_S * 1_000_000_000,
    member_factory: Callable[[str], AlphaModel] = build,
) -> dict[str, Any]:
    """The member pass of the module docs: one outer walk-forward, every
    member and every method fitted per fold on the training rows only.

    Returns the pooled out-of-sample arrays (``z`` rows x K member signals,
    ``combined`` per method, ``y``, ``ts``, ``crossed``), the per-fold
    weights of every method and the members' bar P&L and trade counts.
    """
    ids = sorted(member_ids)
    probe = CombinedAlpha(
        ids, asset_class=asset_class, horizon=horizon, member_factory=member_factory
    )
    universe = probe.universe(list(frames))
    uframes = {i: frames[i] for i in universe}
    splitter = WalkForwardSplitter(n_folds=n_folds, embargo_ns=embargo_ns)
    z_all, y_all, ts_all, c_all = [], [], [], []
    combined: dict[str, list[np.ndarray]] = {m: [] for m in method_names}
    weights: dict[str, list[dict[str, Any]]] = {m: [] for m in method_names}
    bars: dict[str, dict[int, float]] = {aid: {} for aid in ids}
    trades = dict.fromkeys(ids, 0)
    net = dict.fromkeys(ids, 0.0)
    for fold, train, test in splitter.split_frames(uframes, HORIZONS_NS[horizon]):
        columns = []
        for aid in ids:
            member = member_factory(aid)
            member.fit(member_purged_train(train, member.horizon, horizon))
            signal = member_signal(member, test, universe)
            columns.append(np.concatenate([signal[i] for i in universe]))
            scores = member.score(test)
            result = backtester.for_horizon(member.horizon).run(
                frames=test, scores=scores, asset_class=asset_class
            )
            for t, pnl in _bar_series(result).items():
                bars[aid][t] = bars[aid].get(t, 0.0) + pnl
            trades[aid] += int(result.trade_count)
            net[aid] += float(result.total_pnl)
        z_all.append(np.column_stack(columns))
        labels, crossed, stamps = [], [], []
        for i in universe:
            df = test[i]
            labels.append(scored_labels(df, horizon, ic_rows)[0])
            if "spread_ticks_v1" in df.columns:
                sp = df["spread_ticks_v1"].to_numpy(dtype=float)
                crossed.append(np.isfinite(sp) & (sp < 0.0))
            else:
                crossed.append(np.zeros(len(df), dtype=bool))
            stamps.append(df["exchange_ts"].to_numpy(dtype=np.int64))
        y_all.append(np.concatenate(labels))
        c_all.append(np.concatenate(crossed))
        ts_all.append(np.concatenate(stamps))
        for name in method_names:
            model = CombinedAlpha(
                ids,
                name,
                asset_class=asset_class,
                horizon=horizon,
                embargo_ns=embargo_ns,
                member_factory=member_factory,
            )
            model.fit(train)
            params = model.params()
            weights[name].append(
                {
                    "fold": int(fold.index),
                    "weights": params["weights"],
                    "beta": float(params["beta"]),
                    "hypothesis_confirmed": bool(params["hypothesis_confirmed"]),
                    "dead": bool(params["dead"]),
                    "n_stack_rows": int(params["n_stack_rows"]),
                    "detail": params["detail"],
                }
            )
            scores = model.score(test)
            beta = float(params["beta"])
            parts = []
            for i in universe:
                er = scores[i]["expected_return"].to_numpy(dtype=float).copy()
                er[scores[i]["confidence"].to_numpy(dtype=float) <= 0.0] = np.nan
                parts.append(er / beta if beta != 0.0 else er)
            combined[name].append(np.concatenate(parts))
    return {
        "member_ids": ids,
        "universe": universe,
        "z": np.vstack(z_all),
        "y": np.concatenate(y_all),
        "ts": np.concatenate(ts_all),
        "crossed": np.concatenate(c_all),
        "combined": {m: np.concatenate(v) for m, v in combined.items()},
        "weights_by_fold": weights,
        "bars": bars,
        "trades": trades,
        "net_pnl": net,
    }


def _weight_stability(folds: Sequence[Mapping[str, Any]], ids: Sequence[str]) -> dict[str, Any]:
    """How much the weights move between folds: per-member mean and standard
    deviation, and the mean cosine similarity of every pair of fold vectors
    (1.0 = the same direction in every fold; ``None`` when a fold is dead)."""
    w = np.array([[float(f["weights"][aid]) for aid in ids] for f in folds])
    cosines = []
    for i in range(len(w)):
        for j in range(i + 1, len(w)):
            den = float(np.linalg.norm(w[i]) * np.linalg.norm(w[j]))
            cosines.append(float(w[i] @ w[j]) / den if den > 0.0 else np.nan)
    return {
        "mean": {aid: float(v) for aid, v in zip(ids, w.mean(axis=0), strict=True)},
        "std": {aid: float(v) for aid, v in zip(ids, w.std(axis=0), strict=True)},
        "mean_pairwise_cosine": _fnum(np.mean(cosines)) if cosines else None,
        "min_pairwise_cosine": _fnum(np.min(cosines)) if cosines else None,
    }


def _breadth(
    member_stats: Mapping[str, Mapping[str, Any]],
    bets: Mapping[str, Any],
    measured: Mapping[str, Any],
) -> dict[str, Any]:
    """The breadth arithmetic next to what was measured.

    For an equal-weight blend of ``K`` standardised signals with mean IC
    ``ic_mean`` and mean pairwise correlation ``rho``::

        IC_blend = ic_mean * sqrt(K / (1 + (K - 1) * rho))

    — the fundamental law (IR ~ IC * sqrt(breadth)) with the effective
    breadth ``K / (1 + (K - 1) * rho)``; with uncorrelated members it is
    ``ic_mean * sqrt(K)``.  The same multiplier applied to the mean member
    gate t is what the blend's t would be if breadth were all that mattered.
    """
    ics = [s["gate_ic"] for s in member_stats.values() if s["gate_ic"] is not None]
    ts = [s["gate_tstat"] for s in member_stats.values() if s["gate_tstat"] is not None]
    k = len(ics)
    rho = bets.get("mean_offdiag")
    ic_mean = float(np.mean(ics)) if ics else None
    t_mean = float(np.mean(ts)) if ts else None
    mult_rho = None
    if k and rho is not None and 1.0 + (k - 1) * rho > 0.0:
        mult_rho = float(np.sqrt(k / (1.0 + (k - 1) * rho)))

    def times(a: float | None, b: float | None) -> float | None:
        return None if a is None or b is None else float(a * b)

    return {
        "n_members_with_ic": k,
        "member_ic_mean": ic_mean,
        "member_gate_tstat_mean": t_mean,
        "mean_pairwise_signal_correlation": rho,
        "breadth_if_independent": float(k),
        "breadth_effective": None if mult_rho is None else float(mult_rho**2),
        "n_effective_eigen": bets.get("n_effective"),
        "expected_ic_if_independent": times(ic_mean, float(np.sqrt(k)) if k else None),
        "expected_ic_at_measured_correlation": times(ic_mean, mult_rho),
        "expected_tstat_at_measured_correlation": times(t_mean, mult_rho),
        "measured_ic_equal_weight": measured.get("gate_ic"),
        "measured_tstat_equal_weight": measured.get("gate_tstat"),
    }


def _weights_ignore_test_rows(
    frames: Mapping[int, pd.DataFrame],
    ids: Sequence[str],
    method: str,
    asset_class: str,
    horizon: str,
    n_folds: int,
    embargo_ns: int,
) -> bool:
    """The leakage self-check printed in the report: for the last outer
    fold, fit the combination (a) on the training rows as the validation
    hands them over and (b) on the training rows of a dataset whose labels
    from the test start onwards were replaced by garbage; the fitted
    parameters must be identical."""
    probe = CombinedAlpha(ids, method, asset_class=asset_class, horizon=horizon)
    universe = probe.universe(list(frames))
    uframes = {i: frames[i] for i in universe}
    splitter = WalkForwardSplitter(n_folds=n_folds, embargo_ns=embargo_ns)
    fold, train, _ = list(splitter.split_frames(uframes, HORIZONS_NS[horizon]))[-1]
    corrupted: dict[int, pd.DataFrame] = {}
    for i, df in uframes.items():
        df = df.copy()
        late = df["exchange_ts"].to_numpy(dtype=np.int64) >= fold.test_start
        for col in df.columns:
            if col.startswith(("label_mid_", "label_cost_", "label_reopen_")):
                df.loc[late, col] = 0.12345
        corrupted[i] = df
    _, train_c, _ = list(splitter.split_frames(corrupted, HORIZONS_NS[horizon]))[-1]

    def fitted(tr) -> str:
        model = CombinedAlpha(
            ids, method, asset_class=asset_class, horizon=horizon, embargo_ns=embargo_ns
        )
        model.fit(tr)
        return json.dumps(model.params(), sort_keys=True)

    return fitted(train) == fitted(train_c)


def _summary(report: Mapping[str, Any]) -> dict[str, Any]:
    boot = report.get("net_pnl_bootstrap") or {}
    return {
        "verdict": report["verdict"],
        "promote_gates": report["promote_gates"],
        "oos_ic": report["oos_ic"],
        "gate_ic": report["gate_ic"],
        "gate_tstat": report["gate_tstat"],
        "tstat_threshold": report["gates"]["min_nw_tstat"],
        "oos_rank_ic": report["oos_rank_ic"],
        "fold_sign_consistency": report["fold_sign_consistency"],
        "n_nondegenerate_folds": report["n_nondegenerate_folds"],
        "hypothesis_confirmed": report["hypothesis_confirmed"],
        "leakage": {
            k: report["leakage"][k]
            for k in ("passed", "label_guard_ok", "shift_ok", "truncation_ok", "recompute_ok")
        },
        "net_pnl_1x_cost": report["net_pnl_1x_cost"],
        "trade_count_1x_cost": report["trade_count_1x_cost"],
        "net_pnl_1x_pooled": report.get("net_pnl_1x_pooled"),
        "n_folds_survive_1x_cost": report.get("n_folds_survive_1x_cost"),
        "net_pnl_bootstrap_ci": [boot.get("ci_low"), boot.get("ci_high")],
        "trade_count_all_folds": sum(
            int(f["trade_count_1x"]) for f in report.get("fold_diagnostics", [])
        ),
        "fold_ics": [f["ic"] for f in report["folds"]],
    }


def _own_report(repo: Path, alpha_id: str) -> dict[str, Any]:
    path = repo / "research" / "alpha_reports" / f"{alpha_id}.json"
    if not path.is_file():
        return {}
    rep = json.loads(path.read_text(encoding="utf-8"))
    return {
        "own_gate_ic": rep.get("gate_ic"),
        "own_gate_tstat": rep.get("gate_tstat"),
        "own_verdict": rep.get("verdict"),
    }


def run_combination(
    repo: Path,
    *,
    asset_classes: Sequence[str] = ASSET_CLASSES,
    method_names: Sequence[str] = METHODS,
    members: Mapping[str, Sequence[str]] | None = None,
    horizon: str | None = None,
    features_dir: Path | None = None,
    normalized_dir: Path | None = None,
    configs_dir: Path | None = None,
    ledger_path: Path | None = None,
    frames: Mapping[int, pd.DataFrame] | None = None,
    progress: Callable[[str], None] | None = None,
    dataset_version: str | None = None,
    feature_version: str | None = None,
) -> dict[str, Any]:
    """Evaluate the combinations (module docs) and return the report
    document together with the full validation reports
    (``{"document", "reports", "correlation"}``).  The looks are debited in
    the ledger at ``ledger_path``; nothing else is written
    (:func:`write_reports` does that)."""
    repo = Path(repo)
    features_dir = Path(features_dir) if features_dir else repo / "data" / "features"
    configs_dir = Path(configs_dir) if configs_dir else repo / "configs"
    if normalized_dir is None:
        normalized_dir = features_dir.parent / "normalized"
    ledger_path = Path(ledger_path) if ledger_path else repo / "research" / "experiments.json"
    for name in method_names:
        if name not in METHODS:
            raise ValueError(f"unknown combination method {name!r}; known: {METHODS}")
    for ac in asset_classes:
        if ac not in ASSET_CLASSES:
            raise ValueError(f"unknown asset class {ac!r}; known: {ASSET_CLASSES}")
    say = progress if progress is not None else (lambda _line: None)
    bundle = methods(DEFAULT_METHODS)
    meta = _load_meta(configs_dir)
    if frames is None:
        needed = {
            f
            for ac in asset_classes
            for aid in (sorted(members[ac]) if members and ac in members else default_members(ac))
            for f in build(aid).features
        }
        frames = load_features(features_dir, columns=slim_columns(needed))
    backtester = _backtester(bundle, meta, configs_dir)
    exec_cfg = json.loads((configs_dir / "execution" / "execution.json").read_text())
    max_participation = float(exec_cfg["defaults"]["max_participation"])
    recompute = RecomputeSources(normalized_dir, configs_dir)
    # an ingested dataset passes its own versions (research --dataset-dir)
    dataset_version = dataset_version or tracker.data_version(repo)
    feature_version = feature_version or tracker.feature_version(repo)
    embargo_ns = EMBARGO_S * 1_000_000_000

    plan: list[dict[str, Any]] = []
    for ac in asset_classes:
        ids = sorted(members[ac]) if members and ac in members else default_members(ac)
        probe = CombinedAlpha(ids, asset_class=ac, horizon=horizon)
        for name in method_names:
            config = experiment_identity(ac, ids, name, probe.horizon, bundle.name)
            plan.append(
                {
                    "asset_class": ac,
                    "combination_id": f"COMB_{_SHORT[ac]}",
                    "members": ids,
                    "method": name,
                    "horizon": probe.horizon,
                    "config": config,
                    "looks": combination_looks(len(ids)),
                    "experiment_id": content_hash(
                        {
                            **config,
                            "dataset_version": dataset_version,
                            "feature_version": feature_version,
                            "seed": BOOTSTRAP_SEED,
                        }
                    )[:16],
                }
            )

    # Declared before the first experiment is evaluated: one gate look count
    # for the whole report (iap.validation.ledger, "Gate look count").
    ledger = ExperimentLedger(ledger_path, dataset_version=dataset_version)
    identities = [(p["combination_id"], LEDGER_KIND, p["config"], p["looks"]) for p in plan]
    batch_total = ledger.batch_total(identities)
    seen: set[int] = set()
    for p in plan:
        p["gate_looks"] = ledger.gate_looks_for(
            p["combination_id"], LEDGER_KIND, p["config"], batch_total
        )
        seen.add(p["gate_looks"])
    if len(seen) > 1:
        raise RuntimeError(
            f"the report would be judged at several look counts {sorted(seen)}: the ledger "
            "holds some of its experiments from an earlier run and not others"
        )
    gate_looks = next(iter(seen))
    ledger_t = ledger.bonferroni_t_threshold_at(gate_looks)

    reports: dict[str, dict[str, Any]] = {}
    combos: dict[str, Any] = {}
    for ac in asset_classes:
        rows = [p for p in plan if p["asset_class"] == ac]
        ids, comb_horizon, comb_id = (
            rows[0]["members"],
            rows[0]["horizon"],
            rows[0]["combination_id"],
        )
        say(f"{comb_id}: member pass ({len(ids)} members, horizon {comb_horizon})")
        mp = member_pass(
            frames,
            ids,
            comb_horizon,
            ac,
            backtester,
            method_names,
            ic_rows=bundle.ic_rows,
            n_folds=N_FOLDS,
            embargo_ns=embargo_ns,
        )
        corr, n_common = correlation_matrix(mp["z"])
        bets = effective_bets(corr)
        member_table: dict[str, Any] = {}
        for k, aid in enumerate(ids):
            member_table[aid] = {
                "own_horizon": build(aid).horizon,
                **_own_report(repo, aid),
                **_gate_stats(mp["ts"], mp["z"][:, k], mp["y"], mp["crossed"], comb_horizon),
                "net_pnl_1x_all_folds": float(mp["net_pnl"][aid]),
                "trade_count_all_folds": int(mp["trades"][aid]),
            }
        method_docs: dict[str, Any] = {}
        for p in rows:
            name = p["method"]
            say(f"{comb_id}: validating {name}")
            report = validate_alpha(
                lambda ids=ids, name=name, ac=ac, h=comb_horizon: CombinedAlpha(
                    ids, name, asset_class=ac, horizon=h, embargo_ns=embargo_ns
                ),
                frames,
                backtester,
                meta,
                max_participation,
                n_folds=N_FOLDS,
                embargo_ns=embargo_ns,
                ledger_t_threshold=ledger_t,
                ledger_looks=gate_looks,
                seed=BOOTSTRAP_SEED,
                recompute=recompute.get(ac) if bundle.recompute_probe else None,
                **bundle.validate_kwargs(),
            )
            report["experiment_id"] = p["experiment_id"]
            report["combination"] = {**p["config"], "looks": p["looks"]}
            reports[f"{comb_id}.{name}"] = report
            ledger.record(
                comb_id,
                LEDGER_KIND,
                config=p["config"],
                result={
                    "experiment_id": p["experiment_id"],
                    "oos_ic": report["oos_ic"],
                    "gate_ic": report["gate_ic"],
                    "gate_tstat": report["gate_tstat"],
                    "verdict": report["verdict"],
                },
                count=p["looks"],
                gate_looks=gate_looks,
            )
            z_comb = mp["combined"][name]
            with_members = {}
            for k, aid in enumerate(ids):
                pair, _ = correlation_matrix(np.column_stack([z_comb, mp["z"][:, k]]))
                with_members[aid] = _fnum(pair[0, 1])
            finite = [abs(v) for v in with_members.values() if v is not None]
            method_docs[name] = {
                "experiment_id": p["experiment_id"],
                "looks": p["looks"],
                **_summary(report),
                "member_pass_check": _gate_stats(
                    mp["ts"], z_comb, mp["y"], mp["crossed"], comb_horizon
                ),
                "weights_by_fold": mp["weights_by_fold"][name],
                "weight_stability": _weight_stability(mp["weights_by_fold"][name], ids),
                "signal_correlation_with_members": with_members,
                "max_abs_correlation_with_a_member": max(finite) if finite else None,
            }
        baseline = method_docs.get("equal_weight", {})
        combos[ac] = {
            "combination_id": comb_id,
            "asset_class": ac,
            "horizon": comb_horizon,
            "members": ids,
            "universe": [int(i) for i in mp["universe"]],
            "member_table": member_table,
            "signal_correlation": {
                "alpha_ids": ids,
                "matrix": [[_fnum(v) for v in row] for row in corr],
                "n_common": [[int(v) for v in row] for row in n_common],
            },
            "pnl_correlation": {
                "alpha_ids": ids,
                "matrix": _pnl_correlation(mp["bars"], mp["trades"], ids),
                "n_members_trading": sum(1 for a in ids if mp["trades"][a] > 0),
            },
            "effective_bets": bets,
            "breadth": _breadth(member_table, bets, baseline),
            "methods": method_docs,
            "weights_ignore_test_rows": {
                name: _weights_ignore_test_rows(
                    frames, ids, name, ac, comb_horizon, N_FOLDS, embargo_ns
                )
                for name in method_names
            },
        }
    ledger.save()

    document = {
        "x-version": COMBINATION_X_VERSION,
        "description": (
            "Signal combination report (iap.combine.report). Each asset class's flagship "
            "alphas are combined under each method and every (asset class, method) pair is "
            "validated as an alpha by iap.validation.validate_alpha. Deterministic; no wall "
            "clock."
        ),
        "dataset_version": dataset_version,
        "feature_version": feature_version,
        "methods_bundle": bundle.name,
        "default_method": DEFAULT_METHOD,
        "protocol": {
            "n_folds": N_FOLDS,
            "inner_folds": INNER_FOLDS,
            "embargo_s": EMBARGO_S,
            "ridge_penalties": list(RIDGE_PENALTIES),
            "bootstrap_seed": BOOTSTRAP_SEED,
            "member_selection": "every flagship alpha of the asset class, whatever its verdict",
        },
        "looks": {
            "per_experiment": {p["combination_id"]: p["looks"] for p in plan},
            "experiments": len(plan),
            "declared_by_this_report": int(sum(p["looks"] for p in plan)),
            # what the ledger held when the report was first judged (stable
            # under a rerun, which is judged at the recorded count)
            "ledger_before": int(gate_looks - sum(p["looks"] for p in plan)),
            "gate_looks": int(gate_looks),
            "tstat_threshold": float(max(GATES["min_nw_tstat"], ledger_t)),
        },
        "combinations": combos,
    }
    full = all(
        ac in combos and combos[ac]["members"] == default_members(ac) for ac in ASSET_CLASSES
    )
    correlation = correlation_document(document) if full else None
    return {"document": document, "reports": reports, "correlation": correlation}


def correlation_document(document: Mapping[str, Any]) -> dict[str, Any]:
    """The pairwise signal-correlation document of every flagship alpha
    (``research/combination/signal_correlation.json``) — the input of the
    lifecycle's ``cross_alpha_correlation`` gate.

    ``correlation[a][b]`` is the Pearson correlation of the two alphas'
    out-of-sample standardised signals on the rows both score, pooled over
    instruments and walk-forward test folds (the member pass).  A pair with
    fewer than ``min_common_rows`` such rows — two alphas of different asset
    classes share none — has correlation 0.0 by definition, and
    ``n_common[a][b]`` says how many rows it had.
    """
    ids: list[str] = []
    rho: dict[tuple[str, str], float] = {}
    n: dict[tuple[str, str], int] = {}
    for combo in document["combinations"].values():
        block = combo["signal_correlation"]
        members = block["alpha_ids"]
        ids.extend(members)
        for i, a in enumerate(members):
            for j, b in enumerate(members):
                value = block["matrix"][i][j]
                rho[(a, b)] = 0.0 if value is None else float(value)
                n[(a, b)] = int(block["n_common"][i][j])
    ids = sorted(ids)
    return {
        "x-version": CORRELATION_X_VERSION,
        "description": (
            "Pairwise correlation of the flagship alphas' out-of-sample standardised signals "
            "on the rows both score (iap.combine.report.correlation_document). Read by "
            "iap.lifecycle.bootstrap for the cross_alpha_correlation gate. Pairs with fewer "
            "than min_common_rows common rows are 0.0 by definition."
        ),
        "dataset_version": document["dataset_version"],
        "feature_version": document["feature_version"],
        "min_common_rows": MIN_OBS,
        "alpha_ids": ids,
        "correlation": {a: {b: rho.get((a, b), 0.0) for b in ids if b != a} for a in ids},
        "n_common": {a: {b: n.get((a, b), 0) for b in ids if b != a} for a in ids},
    }


def _f(v: Any, spec: str = "+.4f") -> str:
    return "-" if v is None else format(v, spec)


def render_markdown(document: Mapping[str, Any], max_correlation: float | None = None) -> str:
    """``REPORT.md`` for a combination document.  ``max_correlation`` is the
    lifecycle's ``max_cross_alpha_correlation``; when given, the pairs above
    it are listed."""
    lines: list[str] = []
    a = lines.append
    looks = document["looks"]
    proto = document["protocol"]
    a("# Signal combination report")
    a("")
    a("Generated by `python -m iap.research combine` (`iap.combine.report`) on the")
    a("bundled seeded synthetic dataset. Every number is deterministic: a rerun on")
    a("the same data reproduces this file and adds nothing to the ledger.")
    a("")
    a("**Honesty note.** The dataset is synthetic and none of the 24 member alphas")
    a("is promotable on it: the few with a statistically visible IC do not earn")
    a("their costs. Combining them can raise the IC and its t — breadth does that")
    a("— but it cannot turn forecasts smaller than the spread into net P&L. A")
    a("PROMOTE here would be a reason to look for leakage before anything else.")
    a("")
    a("## Protocol")
    a("")
    a(f"- **Members:** {proto['member_selection']}.")
    a(
        f"- **Outer validation:** `validate_alpha`, {proto['n_folds']} expanding walk-forward "
        f"folds purged at the combination horizon, {proto['embargo_s']} s embargo, method "
        f"bundle `{document['methods_bundle']}` — the chain of the alpha reports."
    )
    a(
        f"- **Weights:** fitted inside each fold's training window on the members' "
        f"out-of-sample predictions from {proto['inner_folds']} inner walk-forward folds "
        "(same splitter); standardisation, weights, ridge penalty and scale never see a "
        "test row."
    )
    a(
        f"- **Methods:** {', '.join('`' + m + '`' for m in METHODS)}; default "
        f"`{document['default_method']}` (the baseline). `ridge` and `shrinkage_mv` account "
        "for member correlation explicitly; `equal_weight` and `ic_weighted` do not, and "
        "nothing is orthogonalised."
    )
    a(
        f"- **Looks:** each experiment debits `looks_per_validation({proto['n_folds']}) + K` "
        f"(83 + members); this report declared {looks['declared_by_this_report']:,} looks over "
        f"{looks['experiments']} experiments on a ledger of {looks['ledger_before']:,} and "
        f"was judged at {looks['gate_looks']:,} looks: PROMOTE needs gate t >= "
        f"{looks['tstat_threshold']:.3f}."
    )
    a("")
    for combo in document["combinations"].values():
        ids = combo["members"]
        a(f"## {combo['combination_id']} — {combo['asset_class']}, horizon {combo['horizon']}")
        a("")
        a("### Combination vs members")
        a("")
        a("`gate IC` / `gate t` are the statistics the PROMOTE gate reads (pooled IC of the")
        a("standardised signal on uncrossed rows; HAC t of the pooled slope), for the")
        a("members at the COMBINATION horizon on the rows of the member pass. `own` is the")
        a("member's committed report at its own horizon.")
        a("")
        a(
            "| signal | gate IC | gate t | folds+ | hyp | leak | trades (all folds) "
            "| net P&L 1x last fold | net P&L 1x pooled | CI low | CI high | verdict | fails |"
        )
        a(
            "|--------|---------|--------|--------|-----|------|--------------------"
            "|----------------------|-------------------|--------|---------|---------|-------|"
        )
        for name, m in combo["methods"].items():
            failed = [g for g, ok in m["promote_gates"].items() if not ok]
            a(
                f"| **{name}** | {_f(m['gate_ic'])} | {_f(m['gate_tstat'], '+.2f')} | "
                f"{_f(m['fold_sign_consistency'], '.2f')} | "
                f"{'yes' if m['hypothesis_confirmed'] else 'no'} | "
                f"{'pass' if m['leakage']['passed'] else 'FAIL'} | "
                f"{m['trade_count_all_folds']} | {_f(m['net_pnl_1x_cost'], '+.0f')} | "
                f"{_f(m['net_pnl_1x_pooled'], '+.0f')} | "
                f"{_f(m['net_pnl_bootstrap_ci'][0], '+.0f')} | "
                f"{_f(m['net_pnl_bootstrap_ci'][1], '+.0f')} | **{m['verdict']}** | "
                f"{', '.join(failed) if failed else '-'} |"
            )
        a("")
        a(
            "| member | own horizon | own gate IC | own gate t | own verdict | gate IC here "
            "| gate t here | trades (all folds) | net P&L 1x (all folds) |"
        )
        a(
            "|--------|-------------|-------------|------------|-------------|--------------"
            "|-------------|--------------------|------------------------|"
        )
        for aid in ids:
            m = combo["member_table"][aid]
            a(
                f"| {aid} | {m['own_horizon']} | {_f(m.get('own_gate_ic'))} | "
                f"{_f(m.get('own_gate_tstat'), '+.2f')} | {m.get('own_verdict', '-')} | "
                f"{_f(m['gate_ic'])} | {_f(m['gate_tstat'], '+.2f')} | "
                f"{m['trade_count_all_folds']} | {_f(m['net_pnl_1x_all_folds'], '+.0f')} |"
            )
        a("")
        a("### Breadth: arithmetic vs measurement")
        a("")
        b = combo["breadth"]
        bets = combo["effective_bets"]
        a(
            f"- Members with a defined IC: {b['n_members_with_ic']}; mean member gate IC "
            f"{_f(b['member_ic_mean'])}, mean member gate t "
            f"{_f(b['member_gate_tstat_mean'], '+.2f')}."
        )
        a(
            f"- Mean pairwise signal correlation {_f(b['mean_pairwise_signal_correlation'], '+.3f')} "
            f"(mean |rho| {_f(bets['mean_abs_offdiag'], '.3f')}); effective independent bets "
            f"{_f(bets['n_effective'], '.2f')} of {bets['n_members']} (eigenvalue participation "
            f"ratio), {_f(b['breadth_effective'], '.2f')} by `K / (1 + (K - 1) rho)`."
        )
        a(
            f"- Expected equal-weight IC = mean IC x sqrt(breadth): "
            f"{_f(b['expected_ic_if_independent'])} if the members were independent, "
            f"{_f(b['expected_ic_at_measured_correlation'])} at the measured correlation. "
            f"**Measured: {_f(b['measured_ic_equal_weight'])}.**"
        )
        a(
            f"- Expected gate t = mean member t x sqrt(effective breadth): "
            f"{_f(b['expected_tstat_at_measured_correlation'], '+.2f')}. "
            f"**Measured: {_f(b['measured_tstat_equal_weight'], '+.2f')}.**"
        )
        a("")
        a("### Signal correlation of the members (out-of-sample z, common rows)")
        a("")
        a("| | " + " | ".join(ids) + " |")
        a("|---|" + "|".join(["---"] * len(ids)) + "|")
        for i, aid in enumerate(ids):
            row = combo["signal_correlation"]["matrix"][i]
            a(f"| {aid} | " + " | ".join(_f(v, "+.2f") for v in row) + " |")
        a("")
        if max_correlation is not None:
            block = combo["signal_correlation"]["matrix"]
            pairs = [
                (ids[i], ids[j], block[i][j])
                for i in range(len(ids))
                for j in range(i + 1, len(ids))
                if block[i][j] is not None and abs(block[i][j]) > max_correlation
            ]
            a(
                f"Pairs above the lifecycle's `max_cross_alpha_correlation` "
                f"({max_correlation}): "
                + (", ".join(f"{x}/{y} ({v:+.2f})" for x, y, v in pairs) if pairs else "none")
                + ". Of such a pair the alpha with the larger id would be held at CANDIDATE "
                "once the other had reached the gate's peer state; today no alpha has, so the "
                "gate passes vacuously for all 24."
            )
            a("")
        a("### P&L correlation of the members (1-minute bar net P&L at 1x costs)")
        a("")
        pc = combo["pnl_correlation"]
        trading = [aid for aid in ids if combo["member_table"][aid]["trade_count_all_folds"] > 0]
        a(
            f"{pc['n_members_trading']} of {len(ids)} members make any trade under the "
            "cost-aware policy; the correlation is undefined for the others."
        )
        if len(trading) >= 2:
            a("")
            a("| | " + " | ".join(trading) + " |")
            a("|---|" + "|".join(["---"] * len(trading)) + "|")
            index = {aid: k for k, aid in enumerate(ids)}
            for x in trading:
                a(
                    f"| {x} | "
                    + " | ".join(_f(pc["matrix"][index[x]][index[y]], "+.2f") for y in trading)
                    + " |"
                )
        a("")
        a("### Weights per fold and their stability")
        a("")
        for name, m in combo["methods"].items():
            st = m["weight_stability"]
            a(
                f"**{name}** — mean pairwise cosine of the fold weight vectors "
                f"{_f(st['mean_pairwise_cosine'], '+.3f')} (min "
                f"{_f(st['min_pairwise_cosine'], '+.3f')}); largest |signal correlation| with "
                f"a member {_f(m['max_abs_correlation_with_a_member'], '.2f')}."
            )
            a("")
            a("| fold | " + " | ".join(ids) + " | beta > 0 | detail |")
            a("|------|" + "|".join(["---"] * len(ids)) + "|---|---|")
            for f in m["weights_by_fold"]:
                detail = f["detail"]
                note = []
                if "ridge_penalty" in detail:
                    note.append(f"lambda {detail['ridge_penalty']:g}")
                if "shrinkage" in detail:
                    note.append(f"shrinkage {detail['shrinkage']:.3f}")
                if f["dead"]:
                    note.append("dead")
                a(
                    f"| {f['fold']} | "
                    + " | ".join(_f(f["weights"][aid], "+.3f") for aid in ids)
                    + f" | {'yes' if f['beta'] > 0 else 'no'} | {', '.join(note) or '-'} |"
                )
            a("")
        a("### Leakage checks")
        a("")
        checks = combo["weights_ignore_test_rows"]
        a(
            "- Fitted parameters of the last fold are identical when every label from the "
            "test start onwards is replaced by garbage: "
            + ", ".join(f"`{k}` {'yes' if v else 'NO'}" for k, v in checks.items())
            + "."
        )
        for name, m in combo["methods"].items():
            leak = m["leakage"]
            a(
                f"- `{name}`: label guard {'ok' if leak['label_guard_ok'] else 'FAIL'}, "
                f"shift {'ok' if leak['shift_ok'] else 'FAIL'}, truncation "
                f"{'ok' if leak['truncation_ok'] else 'FAIL'}, recompute "
                f"{'not run' if leak['recompute_ok'] is None else ('ok' if leak['recompute_ok'] else 'FAIL')}; "
                f"member-pass gate IC {_f(m['member_pass_check']['gate_ic'])} against the "
                f"validation's {_f(m['gate_ic'])}."
            )
        a("")
    verdicts = [
        (combo["combination_id"], name, m["verdict"])
        for combo in document["combinations"].values()
        for name, m in combo["methods"].items()
    ]
    promoted = [f"{c} {n}" for c, n, v in verdicts if v == "PROMOTE"]
    a("## Result")
    a("")
    counts = {v: sum(1 for *_, x in verdicts if x == v) for v in ("PROMOTE", "ITERATE", "REJECT")}
    a(
        f"**{counts['PROMOTE']} PROMOTE / {counts['ITERATE']} ITERATE / {counts['REJECT']} "
        f"REJECT** over {len(verdicts)} combination experiments."
        + (
            " No combination is promotable."
            if not promoted
            else f" PROMOTE: {', '.join(promoted)} — verify the leakage checks above first."
        )
    )
    a("")
    return "\n".join(lines) + "\n"


def write_reports(
    result: Mapping[str, Any], out_dir: Path, max_correlation: float | None = None
) -> dict[str, Path]:
    """Write ``REPORT.md``, ``COMBINATION.json``, the per-experiment
    validation reports and (when present) ``signal_correlation.json``."""
    out_dir = Path(out_dir)
    (out_dir / "reports").mkdir(parents=True, exist_ok=True)
    paths = {"md": out_dir / "REPORT.md", "json": out_dir / "COMBINATION.json"}

    def clean(value: Any) -> Any:
        """Non-finite floats -> null (a validation report keeps a NaN IC)."""
        if isinstance(value, float):
            return value if np.isfinite(value) else None
        if isinstance(value, Mapping):
            return {str(k): clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [clean(v) for v in value]
        return value

    def dump(path: Path, doc: Any) -> None:
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(clean(doc), indent=2, sort_keys=True, allow_nan=False) + "\n")

    with open(paths["md"], "w", encoding="utf-8", newline="\n") as fh:
        fh.write(render_markdown(result["document"], max_correlation))
    dump(paths["json"], result["document"])
    for name, report in sorted(result["reports"].items()):
        dump(out_dir / "reports" / f"{name}.json", report)
    if result.get("correlation") is not None:
        paths["correlation"] = out_dir / "signal_correlation.json"
        dump(paths["correlation"], result["correlation"])
    return paths
