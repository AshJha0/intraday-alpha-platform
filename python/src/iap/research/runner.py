"""``ExperimentRunner`` — one :class:`ExperimentSpec` in, one
:class:`ExperimentResult` out, both persisted (contracts §5,
``iap.contracts.protocols.ExperimentRunner``).

The runner adds no statistics of its own.  It drives the validated
machinery exactly as ``research/alpha_reports/run_all.py`` drives it and
maps the outcome onto the contract:

1. **Window.**  The feature frames are restricted to the experiment window
   ``[train_period.start_ts, test_period.end_ts)`` and to the alpha's
   universe.  The spec's ``horizon`` is the label the alpha is fitted and
   scored against (its pinned horizon unless the spec says otherwise).
2. **Walk-forward evidence** — :func:`iap.validation.validate.validate_alpha`
   over the window: expanding walk-forward with ``n_folds`` folds, purged
   at the horizon and embargoed by ``embargo_ns`` (row-mass boundaries,
   never a random split), leakage tests, Newey-West-lite t on 5-minute
   bucket ICs, fold sign consistency, hypothesis sign, the pinned
   cost / latency / regime stress grid and the §20 verdict.  The research
   backtester it carries uses the spec's ``latency_ns``,
   ``max_decision_age_ns`` and ``flatten_at_session_end`` at 1x costs (the
   gates read the 1x cost survival; the stress grid is absolute).
3. **Holdout economics** — a fresh model fitted on ``train_period`` (rows
   that ALSO satisfy the splitter's purge + embargo mask against
   ``test_period.start_ts``; ``validation_period`` rows are never fitted on),
   scored on ``test_period`` and run through the same backtester at
   ``cost_multiplier`` x costs.  Gross / cost / net P&L and max drawdown are
   expressed in basis points of the research capital line
   (``max_pos_qty x ref_price x unit`` per universe instrument, converted
   to USD — ``run_all.py``'s ``_capital_usd``); Sharpe is the backtester's
   annualised 1-minute-bar Sharpe.
4. **Ledger.**  The run is one entry of :data:`LOOKS_PER_EXPERIMENT` looks
   in the multiple-testing ledger under kind ``"experiment_runner"`` with
   the full spec as its configuration; the ledger de-duplicates by
   (alpha, kind, config), so re-running a spec does not inflate the
   denominator.  ``n_experiments_in_ledger`` is the total after recording.
5. **Result.**  The mapping from ``validate_alpha``'s report (contracts
   notes §2): ``oos_ic -> ic``, ``oos_rank_ic -> rank_ic``,
   ``nw_tstat -> t_stat``, ``nw_lags``, ``oos_hit_rate -> hit_rate``,
   ``turnover_flips_per_hour -> turnover``, ``fold_sign_consistency ->
   fold_consistency``, ``n_folds_run -> n_folds``, ``leakage.passed ->
   leakage_passed``, ``leakage -> leakage_detail``, ``hypothesis_confirmed
   -> hypothesis_sign_confirmed``, ``verdict``; the economics from step 3;
   ``git_commit`` from :mod:`iap.experiment.tracker`; ``created_ts`` is
   ``test_period.end_ts`` — event time, never the wall clock.  Every
   float must be finite: a metric the chain could not compute raises
   :class:`ResearchError` naming it — nothing is ever filled in.
6. **Persistence** (skipped with ``dry_run``): ``<out_dir>/<experiment_id>/
   spec.json`` and ``result.json`` — schema-validated, sorted keys,
   2-space indent, ASCII, trailing newline — and the ledger file.  A
   rerun that reproduces a different result under the same id (anything
   but ``git_commit`` / ``n_experiments_in_ledger``) is refused: the
   same spec on the same data must give the same numbers.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

import numpy as np
import pandas as pd

from iap.alpha import build
from iap.alpha.base import AlphaModel
from iap.alpha.data import load_features
from iap.backtest import Backtester, BacktestConfig, CostModel
from iap.contracts.types import ExperimentResult, ExperimentSpec, Verdict
from iap.contracts.validate import validate_typed
from iap.experiment import tracker
from iap.research.errors import ResearchError
from iap.research.specs import (
    normalise_configuration,
    pinned_horizon,
    verify_experiment_id,
)
from iap.validation.ledger import ExperimentLedger
from iap.validation.metrics import HORIZONS_NS
from iap.validation.splits import Fold
from iap.validation.validate import validate_alpha

__all__ = [
    "DOCUMENT_TOL",
    "LEDGER_KIND",
    "LOOKS_PER_EXPERIMENT",
    "ExperimentRunner",
    "build_result",
    "document_drift",
    "holdout_capital_usd",
    "load_instrument_meta",
    "render_document",
    "restrict_frames",
]

#: Looks at the data one experiment makes (= ``run_all.py``'s
#: ``LOOKS_PER_ALPHA``): 1 walk-forward evaluation + 11 decay horizons +
#: 3 cost multipliers + 3 latency shifts + 2 regimes + 1 holdout backtest.
LOOKS_PER_EXPERIMENT = 21

#: Ledger ``kind`` of every runner entry.
LEDGER_KIND = "experiment_runner"

#: ``ExperimentResult`` field <- ``validate_alpha`` report key.
_REPORT_METRICS = (
    ("ic", "oos_ic"),
    ("rank_ic", "oos_rank_ic"),
    ("t_stat", "nw_tstat"),
    ("hit_rate", "oos_hit_rate"),
    ("turnover", "turnover_flips_per_hour"),
    ("fold_consistency", "fold_sign_consistency"),
)

#: Result fields a rerun may legitimately change (provenance, not evidence).
_PROVENANCE_FIELDS = ("git_commit", "n_experiments_in_ledger")

#: Relative tolerance of the backtester's accounting identity.
_IDENTITY_TOL = 1e-9

#: Tolerance at which two research documents are "the same numbers"
#: (PLATFORM_CONVENTIONS §1: research doubles are compared at 1e-9).  IC
#: and t-statistics come out of numpy / BLAS reductions whose last ulp is
#: CPU-dependent, so a document is reproduced when every float agrees to
#: ``DOCUMENT_TOL`` (absolute + relative) and everything else is identical.
DOCUMENT_TOL = 1e-9

BPS = 1e4


def document_drift(previous: Any, current: Any, *, tol: float = DOCUMENT_TOL,
                   path: str = "$") -> List[str]:
    """Paths where ``current`` differs from ``previous`` beyond ``tol``.

    Floats agree when ``|a - b| <= tol + tol * |b|``; ints, bools, strings,
    ``None`` must be identical; mappings must have the same keys; sequences
    the same length.  Type changes (``1`` vs ``1.0``, ``True`` vs ``1``) are
    drift.  An empty list means the documents carry the same numbers.
    """
    if isinstance(previous, bool) or isinstance(current, bool):
        return [] if (type(previous) is type(current) and previous == current) else [path]
    if isinstance(previous, float) and isinstance(current, float):
        if math.isfinite(previous) and math.isfinite(current) and \
                abs(previous - current) <= tol + tol * abs(current):
            return []
        return [path]
    if isinstance(previous, Mapping) and isinstance(current, Mapping):
        if set(previous) != set(current):
            return [path]
        drift: List[str] = []
        for key in sorted(previous):
            drift.extend(document_drift(previous[key], current[key], tol=tol,
                                        path=f"{path}.{key}"))
        return drift
    if isinstance(previous, (list, tuple)) and isinstance(current, (list, tuple)):
        if len(previous) != len(current):
            return [path]
        drift = []
        for i, (a, b) in enumerate(zip(previous, current)):
            drift.extend(document_drift(a, b, tol=tol, path=f"{path}[{i}]"))
        return drift
    return [] if (type(previous) is type(current) and previous == current) else [path]


def _finite(value: Any, name: str) -> float:
    """``value`` as a finite float, or a :class:`ResearchError` naming it."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float, np.floating, np.integer)):
        raise ResearchError(
            f"metric {name!r} was not computed (got {value!r}); a result is never "
            "fabricated — the experiment window is too small or too sparse")
    out = float(value)
    if not math.isfinite(out):
        raise ResearchError(
            f"metric {name!r} is not finite ({out}); a result is never fabricated")
    return out


def _jsonable(value: Any, path: str) -> Any:
    """Python scalars / containers for a contract mapping field.

    numpy scalars are converted; a non-finite float anywhere is an error
    (the leakage detail carries the ICs the shift test compared — if the
    tester could not compute them the test was not evaluated)."""
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return _finite(value, path)
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v, f"{path}.{k}") for k, v in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [_jsonable(v, f"{path}[{i}]") for i, v in enumerate(value)]
    raise ResearchError(f"{path}: value {value!r} is not JSON-representable")


def load_instrument_meta(configs_dir: Path) -> Dict[int, dict]:
    """Instrument meta for the backtester / capacity proxy, exactly the
    rows ``run_all.py`` builds from ``configs/instruments/instruments.json``."""
    path = Path(configs_dir) / "instruments" / "instruments.json"
    cfg = json.loads(path.read_text())
    meta: Dict[int, dict] = {}
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


def holdout_capital_usd(backtester: Backtester, meta: Mapping[int, dict],
                        instrument_ids: List[int]) -> float:
    """Research capital line: ``max_pos_qty x ref_price x unit`` per
    instrument, converted to USD at the conversion pair's ``ref_price``
    (``run_all.py``'s ``_capital_usd``)."""
    total = 0.0
    for iid in instrument_ids:
        unit = meta[iid]["lot_size"] if meta[iid]["asset_class"] == "FX" else 1
        native = backtester.config.max_pos_qty * meta[iid]["ref_price"] * unit
        total += native * backtester.reference_rate(backtester.quote_currency(iid))
    return total


def restrict_frames(frames: Mapping[int, pd.DataFrame], start_ts: int,
                    end_ts: int) -> Dict[int, pd.DataFrame]:
    """Rows with ``start_ts <= exchange_ts < end_ts`` per instrument."""
    out: Dict[int, pd.DataFrame] = {}
    for iid in sorted(frames):
        ts = frames[iid]["exchange_ts"].to_numpy(dtype=np.int64)
        out[iid] = frames[iid][(ts >= start_ts) & (ts < end_ts)].reset_index(drop=True)
    return out


def render_document(doc: Mapping[str, Any]) -> str:
    """The persisted form: sorted keys, 2-space indent, ASCII, no NaN,
    trailing newline — byte-deterministic for a given document."""
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=True,
                      allow_nan=False) + "\n"


def build_result(
    spec: ExperimentSpec,
    report: Mapping[str, Any],
    holdout: Mapping[str, float],
    n_experiments_in_ledger: int,
    git_commit: str,
) -> ExperimentResult:
    """Map a ``validate_alpha`` report + holdout economics onto the contract.

    ``holdout`` carries ``gross_return_bps``, ``transaction_cost_bps``,
    ``max_drawdown_bps`` and ``sharpe``; ``net_return_bps`` is
    ``gross - cost`` so the contract identity holds exactly.  Any metric
    that is missing, ``None`` or non-finite raises :class:`ResearchError`
    naming it (``validate_alpha`` reports an incomputable metric as
    ``None``).
    """
    metrics = {field: _finite(report.get(key), key) for field, key in _REPORT_METRICS}
    leakage = report.get("leakage")
    if not isinstance(leakage, Mapping) or "passed" not in leakage:
        raise ResearchError("report carries no leakage block")
    gross = _finite(holdout.get("gross_return_bps"), "gross_return_bps")
    cost = _finite(holdout.get("transaction_cost_bps"), "transaction_cost_bps")
    try:
        return ExperimentResult(
            experiment_id=spec.experiment_id,
            alpha_id=spec.alpha_id,
            dataset_version=spec.dataset_version,
            feature_version=spec.feature_version,
            model_version=spec.model_version,
            ic=metrics["ic"],
            rank_ic=metrics["rank_ic"],
            t_stat=metrics["t_stat"],
            nw_lags=int(report["nw_lags"]),
            hit_rate=metrics["hit_rate"],
            turnover=metrics["turnover"],
            gross_return_bps=gross,
            transaction_cost_bps=cost,
            net_return_bps=gross - cost,
            max_drawdown_bps=_finite(holdout.get("max_drawdown_bps"), "max_drawdown_bps"),
            sharpe=_finite(holdout.get("sharpe"), "sharpe"),
            fold_consistency=metrics["fold_consistency"],
            n_folds=int(report["n_folds_run"]),
            leakage_passed=bool(leakage["passed"]),
            leakage_detail=_jsonable(leakage, "leakage"),
            hypothesis_sign_confirmed=bool(report["hypothesis_confirmed"]),
            verdict=Verdict(str(report["verdict"])),
            n_experiments_in_ledger=int(n_experiments_in_ledger),
            git_commit=str(git_commit),
            created_ts=int(spec.test_period.end_ts),
        )
    except (KeyError, ValueError) as exc:  # missing report key / ContractError
        raise ResearchError(f"cannot build ExperimentResult: {exc}") from exc


class ExperimentRunner:
    """Runs experiment specs against a feature store (see module docs).

    ``feature_store_dir`` holds the parquet frames written by
    ``python3 -m iap.features`` (``features_<instrument_id>.parquet``);
    pass ``frames`` instead to run on frames already in memory (golden /
    synthetic tests), in which case ``feature_store_dir`` may be ``None``.
    ``ledger_path`` is the multiple-testing ledger
    (``research/experiments.json``), ``out_dir`` the experiments folder
    (``research/experiments``), ``configs_dir`` the ``configs/`` tree
    (instruments + execution cost model).  With ``dry_run`` nothing is
    written: the ledger is updated in memory only, so the result still
    carries the count the run WOULD have had.
    """

    def __init__(
        self,
        feature_store_dir: Optional[Path],
        ledger_path: Path,
        out_dir: Path,
        configs_dir: Path,
        *,
        dry_run: bool = False,
        frames: Optional[Mapping[int, pd.DataFrame]] = None,
        repo_root: Optional[Path] = None,
    ) -> None:
        if feature_store_dir is None and frames is None:
            raise ResearchError("ExperimentRunner needs a feature_store_dir or frames")
        self.feature_store_dir = Path(feature_store_dir) if feature_store_dir else None
        self.ledger_path = Path(ledger_path)
        self.out_dir = Path(out_dir)
        self.configs_dir = Path(configs_dir)
        self.dry_run = bool(dry_run)
        self.repo_root = Path(repo_root) if repo_root is not None else None
        self._frames: Optional[Dict[int, pd.DataFrame]] = (
            {int(k): v for k, v in frames.items()} if frames is not None else None)
        self.meta = load_instrument_meta(self.configs_dir)
        exec_cfg = json.loads((self.configs_dir / "execution" / "execution.json").read_text())
        self.max_participation = float(exec_cfg["defaults"]["max_participation"])
        self.cost_model = CostModel.load(self.configs_dir / "execution" / "execution.json")
        self.ledger = ExperimentLedger(self.ledger_path)

    # -- inputs ---------------------------------------------------------

    def frames(self) -> Dict[int, pd.DataFrame]:
        """The full feature store (loaded once)."""
        if self._frames is None:
            self._frames = load_features(self.feature_store_dir)
        return self._frames

    def _backtester(self, spec: ExperimentSpec, cost_multiplier: float) -> Backtester:
        cfg = spec.configuration
        return Backtester(
            self.cost_model.with_multiplier(cost_multiplier),
            self.meta,
            BacktestConfig(
                latency_ns=int(cfg["latency_ns"]),
                max_decision_age_ns=int(cfg["max_decision_age_ns"]),
                flatten_at_session_end=bool(cfg["flatten_at_session_end"]),
            ),
        )

    @staticmethod
    def _model_factory(spec: ExperimentSpec) -> Callable[[], AlphaModel]:
        """A fresh unfitted model per call, scored at the spec's horizon."""
        def factory() -> AlphaModel:
            model = build(spec.alpha_id)
            model.horizon = spec.horizon
            return model
        return factory

    @staticmethod
    def _check_spec(spec: ExperimentSpec) -> None:
        validate_typed(spec)
        verify_experiment_id(spec)
        pinned_horizon(spec.alpha_id)
        if spec.horizon not in HORIZONS_NS:
            raise ResearchError(f"unknown horizon {spec.horizon!r}")
        if normalise_configuration(spec.configuration) != dict(spec.configuration):
            raise ResearchError(
                "spec.configuration is not normalised — build specs with "
                "iap.research.build_spec")

    # -- evidence -------------------------------------------------------

    def _holdout(self, spec: ExperimentSpec, window: Mapping[int, pd.DataFrame],
                 factory: Callable[[], AlphaModel]) -> Dict[str, float]:
        """Fit on the (purged, embargoed) train period, backtest the test period."""
        cfg = spec.configuration
        horizon_ns = HORIZONS_NS[spec.horizon]
        fold = Fold(index=0, train_end=spec.test_period.start_ts,
                    test_start=spec.test_period.start_ts,
                    test_end=spec.test_period.end_ts)
        train: Dict[int, pd.DataFrame] = {}
        test: Dict[int, pd.DataFrame] = {}
        for iid, df in window.items():
            ts = df["exchange_ts"].to_numpy(dtype=np.int64)
            in_train = (ts >= spec.train_period.start_ts) & (ts < spec.train_period.end_ts)
            purged = fold.train_mask(ts, horizon_ns, int(cfg["embargo_ns"]))
            train[iid] = df[in_train & purged].reset_index(drop=True)
            test[iid] = df[fold.test_mask(ts)].reset_index(drop=True)
        model = factory()
        model.fit(train)
        scores = model.score(test)
        backtester = self._backtester(spec, float(cfg["cost_multiplier"]))
        result = backtester.run(test, scores, model.asset_class)
        capital = holdout_capital_usd(backtester, self.meta, sorted(scores))
        if not capital > 0.0:
            raise ResearchError("holdout capital line is not positive")
        metrics = result.metrics(capital)
        gross = float(metrics["gross_pnl"])
        costs = float(metrics["total_costs"])
        net = float(metrics["total_pnl"])
        if abs(net - (gross - costs)) > _IDENTITY_TOL * max(1.0, abs(gross), abs(costs)):
            raise ResearchError(
                f"backtester accounting identity violated: net {net} != gross {gross} "
                f"- costs {costs}")
        return {
            "gross_return_bps": gross / capital * BPS,
            "transaction_cost_bps": costs / capital * BPS,
            "max_drawdown_bps": float(metrics["max_drawdown"]) / capital * BPS,
            "sharpe": float(metrics["sharpe_ann"]),
        }

    def _ledger_total_after(self, spec: ExperimentSpec, report: Mapping[str, Any]) -> int:
        return self.ledger.record(
            spec.alpha_id, LEDGER_KIND,
            config=spec.to_dict(),
            result={
                "experiment_id": spec.experiment_id,
                "oos_ic": report["oos_ic"],
                "nw_tstat": report["nw_tstat"],
                "verdict": report["verdict"],
            },
            count=LOOKS_PER_EXPERIMENT,
        )

    # -- persistence ----------------------------------------------------

    def experiment_dir(self, experiment_id: str) -> Path:
        return self.out_dir / experiment_id

    def _persist(self, spec: ExperimentSpec, result: ExperimentResult) -> None:
        spec_doc = validate_typed(spec)
        result_doc = validate_typed(result)
        target = self.experiment_dir(spec.experiment_id)
        existing = target / "result.json"
        reproduced = False
        if existing.is_file():
            previous = json.loads(existing.read_text())
            evidence_prev = {k: v for k, v in previous.items() if k not in _PROVENANCE_FIELDS}
            evidence_now = {k: v for k, v in result_doc.items() if k not in _PROVENANCE_FIELDS}
            drift = document_drift(evidence_prev, evidence_now)
            if drift:
                raise ResearchError(
                    f"{existing}: rerun of {spec.experiment_id} reproduced different "
                    f"values at {drift}; the same spec on the same data must give "
                    f"the same result (floats compared at {DOCUMENT_TOL:g}) — remove "
                    "the directory deliberately if the evidence chain changed")
            # Same numbers: keep the committed bytes (the last ulp of a BLAS
            # reduction is CPU-dependent; the artefact must not churn).
            reproduced = all(previous.get(k) == result_doc[k] for k in _PROVENANCE_FIELDS)
        target.mkdir(parents=True, exist_ok=True)
        (target / "spec.json").write_text(render_document(spec_doc), encoding="ascii")
        if not (existing.is_file() and reproduced):
            (target / "result.json").write_text(render_document(result_doc), encoding="ascii")
        self.ledger.save()

    # -- protocol -------------------------------------------------------

    def run(self, spec: ExperimentSpec) -> ExperimentResult:
        """Run ``spec`` (see the module docs for the six steps)."""
        self._check_spec(spec)
        factory = self._model_factory(spec)
        probe = factory()
        window = restrict_frames(self.frames(), spec.train_period.start_ts,
                                 spec.test_period.end_ts)
        universe = probe.universe(sorted(window))
        window = {iid: window[iid] for iid in universe}
        if not any(len(df) for df in window.values()):
            raise ResearchError(
                f"no rows for {spec.alpha_id}'s universe inside the experiment window")
        cfg = spec.configuration
        try:
            report = validate_alpha(
                factory, window, self._backtester(spec, 1.0), self.meta,
                self.max_participation, n_folds=int(cfg["n_folds"]),
                embargo_ns=int(cfg["embargo_ns"]),
            )
        except ValueError as exc:  # splitter: too few rows / degenerate boundaries
            raise ResearchError(f"walk-forward validation impossible: {exc}") from exc
        holdout = self._holdout(spec, window, factory)
        total = self._ledger_total_after(spec, report)
        result = build_result(spec, report, holdout, total,
                              tracker.git_commit(self.repo_root))
        if not self.dry_run:
            self._persist(spec, result)
        return result
