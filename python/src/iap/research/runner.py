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
   never a random split), leakage tests (with the recompute probe when the
   normalized events are beside the feature store), the pooled IC and its
   HAC t, fold sign consistency, hypothesis sign, the pinned cost / latency
   / regime stress grid, per-fold diagnostics and the §20 verdict — all
   under the method bundle the spec names (``configuration.methods``,
   :mod:`iap.validation.methods`).  The research backtester it carries uses
   the spec's ``latency_ns``, ``max_decision_age_ns`` and
   ``flatten_at_session_end`` at 1x costs (the gates read the 1x cost
   survival; the stress grid is absolute).
3. **Holdout economics** — a fresh model fitted on ``train_period`` (rows
   that ALSO satisfy the splitter's purge + embargo mask against
   ``test_period.start_ts``; ``validation_period`` rows are never fitted on),
   scored on ``test_period`` and run through the same backtester at
   ``cost_multiplier`` x costs.  Gross / cost / net P&L and max drawdown are
   expressed in basis points of the research capital line
   (``max_pos_qty x ref_price x unit`` per universe instrument, converted
   to USD — ``run_all.py``'s ``_capital_usd``); Sharpe is the backtester's
   annualised 1-minute-bar Sharpe.
4. **Ledger.**  The run is one entry in the multiple-testing ledger under
   kind ``"experiment_runner"`` with the full spec as its configuration,
   debiting ``ResearchMethods.looks(n_folds)`` looks — what the chain
   evaluates under the spec's bundle (84 under ``v2`` at four folds,
   :data:`LOOKS_PER_EXPERIMENT`; 28 under ``legacy_v1``).  The ledger
   de-duplicates by (alpha, kind, config), so re-running a spec does not
   inflate the denominator.  ``n_experiments_in_ledger`` is the total
   after recording.

   *The t threshold (pinned, ``v2``).*  PROMOTE needs the gate t to reach
   ``max(3.0, Bonferroni |t| at N looks)`` with ``N`` the run's gate look
   count (``iap.validation.ledger``, "Gate look count"): the looks in the
   ledger before the run plus the looks this run adds, or — for a spec
   that has been run before — the count recorded on its entry.  A rerun is
   therefore judged at the threshold of its first run and reproduces its
   verdict; ``N`` is stored on the ledger entry (``gate_looks``) and, with
   the threshold, in ``eligibility.json``.
5. **Result.**  The mapping from ``validate_alpha``'s report (contracts
   notes §2).  Under ``v2``: ``gate_ic -> ic`` and ``gate_tstat -> t_stat``
   — the two numbers the verdict read (the pooled IC on uncrossed rows and
   its pooled-slope HAC t).  Under ``legacy_v1``: ``oos_ic -> ic`` and
   ``nw_tstat -> t_stat``, the mapping up to v1.4.0.  In both:
   ``oos_rank_ic -> rank_ic``, ``nw_lags``, ``oos_hit_rate -> hit_rate``,
   ``turnover_flips_per_hour -> turnover``, ``fold_sign_consistency ->
   fold_consistency``, ``n_folds_run -> n_folds``, ``leakage.passed ->
   leakage_passed``, ``leakage -> leakage_detail``, ``hypothesis_confirmed
   -> hypothesis_sign_confirmed``, ``verdict``; the economics from step 3;
   ``git_commit`` from :mod:`iap.experiment.tracker`; ``created_ts`` is
   ``test_period.end_ts`` — event time, never the wall clock.  Every
   float must be finite: a metric the chain could not compute raises
   :class:`ResearchError` naming it — nothing is ever filled in.

   *A holdout without a trade (pinned).*  Under the cost-aware position
   policy an alpha whose expected return never clears its round-trip cost
   does not trade at all.  Its holdout P&L series is identically zero:
   gross, cost, net and drawdown are 0 and the Sharpe ratio is 0/0.  That
   outcome is a result, not a failure of the chain, so ``sharpe`` is
   reported as ``0.0`` for a holdout with ZERO trades (and only then — a
   non-finite Sharpe of a holdout that traded still raises).  A net return
   of 0 does not pass the strict ``net_return_bps > 0`` gate.
6. **Persistence**: ``<out_dir>/<experiment_id>/spec.json``,
   ``result.json`` and ``eligibility.json`` — schema-validated, sorted
   keys, 2-space indent, ASCII, LF, trailing newline — and the ledger file.
   A rerun that reproduces a different result under the same id (anything
   but ``git_commit`` / ``n_experiments_in_ledger``) is refused: the
   same spec on the same data must give the same numbers.

   *Atomicity (pinned).*  A new experiment directory is staged under
   ``<out_dir>/.staging-<id>-<pid>`` and moved into place with one
   ``os.replace``, so a reader — or a second writer — sees either no
   directory or a complete one, never a ``spec.json`` without its
   ``result.json``.  A writer that loses the race to claim the id falls
   through to the rerun path (drift check, per-file atomic replace).

   *Dry runs still cost looks (pinned).*  With ``dry_run`` no experiment
   directory is written, but the ledger IS saved: a dry run evaluates all
   :data:`LOOKS_PER_EXPERIMENT` statistics and shows them to the caller, so
   it is a look at the data whether or not the documents are kept.  A dry
   run that left the ledger untouched let a caller scan configurations for
   free and persist only the winner.  The later real run of the same spec
   de-duplicates against the dry run's entry, so nothing is counted twice.
7. **Gate eligibility** — :func:`iap.research.specs.gate_eligibility` on the
   spec, the runner's dataset and the facts of the run, kept on the runner
   as ``last_eligibility`` and persisted as ``eligibility.json``.  A result
   from a configuration outside the pinned bounds, under the legacy method
   bundle, on caller-chosen periods, or from a run that could not apply the
   whole default chain (no normalized events for the recompute probe, no
   ``label_reopen`` columns in the frames) is recorded and ledgered like
   any other but flagged not gate-eligible.
"""

from __future__ import annotations

import json
import math
import os
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from iap.alpha import build
from iap.alpha.base import AlphaModel
from iap.alpha.data import load_features
from iap.backtest import BacktestConfig, Backtester, CostModel
from iap.contracts.types import ExperimentResult, ExperimentSpec, Verdict
from iap.contracts.validate import validate_typed
from iap.experiment import tracker
from iap.experiment.locking import atomic_write_text
from iap.research.errors import ResearchError
from iap.research.specs import (
    GateEligibility,
    gate_eligibility,
    normalise_configuration,
    pinned_horizon,
    verify_experiment_id,
)
from iap.validation.leakage import RecomputeSources
from iap.validation.ledger import ExperimentLedger
from iap.validation.methods import METHODS_LEGACY, METHODS_V2, ResearchMethods, methods
from iap.validation.metrics import HORIZONS_NS
from iap.validation.splits import Fold
from iap.validation.validate import validate_alpha

__all__ = [
    "DOCUMENT_TOL",
    "LEDGER_KIND",
    "LEGACY_LOOKS_PER_EXPERIMENT",
    "LOOKS_PER_EXPERIMENT",
    "ExperimentRunner",
    "build_result",
    "document_drift",
    "holdout_capital_usd",
    "load_instrument_meta",
    "render_document",
    "restrict_frames",
]

#: Looks at the data one experiment makes under the default method bundle
#: at the pinned four folds.  The ledger is the denominator of every
#: multiple-testing correction in the research, so it counts what the chain
#: ACTUALLY evaluates, not a round number.  The itemisation is
#: :func:`iap.validation.validate.looks_per_validation` (83 at four folds:
#: 19 pooled statistics and stresses, plus 16 per fold — 11 decay ICs, 3 cost
#: backtests, 2 regime ICs) plus the 1 holdout backtest of the declared test
#: period.  A spec with another fold count debits
#: ``methods(...).looks(n_folds)``; this constant is the default case.
#:
#: Up to v1.4.0 the chain evaluated decay, cost stress and regime on the
#: last fold only and counted 28 (:data:`LEGACY_LOOKS_PER_EXPERIMENT`, what
#: a ``legacy_v1`` run still debits).  The statistics v1.3.0 added beside
#: the gate — the pooled-slope t, the scale-free ICs — were computed and
#: reported without being counted; they are counted now.
LOOKS_PER_EXPERIMENT = methods(METHODS_V2).looks(4)
LEGACY_LOOKS_PER_EXPERIMENT = methods(METHODS_LEGACY).looks(4)

#: Ledger ``kind`` of every runner entry.
LEDGER_KIND = "experiment_runner"

#: Documents of one experiment directory.
SPEC_FILE = "spec.json"
RESULT_FILE = "result.json"
ELIGIBILITY_FILE = "eligibility.json"
#: Prefix of the staging directory a new experiment is assembled in.
STAGING_PREFIX = ".staging-"

#: ``ExperimentResult`` field <- ``validate_alpha`` report key, per method
#: bundle (module docs, step 5): ``v2`` records the two numbers the verdict
#: read; ``legacy_v1`` keeps the mapping up to v1.4.0.
_REPORT_METRICS = {
    METHODS_V2: (
        ("ic", "gate_ic"),
        ("rank_ic", "oos_rank_ic"),
        ("t_stat", "gate_tstat"),
        ("hit_rate", "oos_hit_rate"),
        ("turnover", "turnover_flips_per_hour"),
        ("fold_consistency", "fold_sign_consistency"),
    ),
    METHODS_LEGACY: (
        ("ic", "oos_ic"),
        ("rank_ic", "oos_rank_ic"),
        ("t_stat", "nw_tstat"),
        ("hit_rate", "oos_hit_rate"),
        ("turnover", "turnover_flips_per_hour"),
        ("fold_consistency", "fold_sign_consistency"),
    ),
}

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


def document_drift(
    previous: Any, current: Any, *, tol: float = DOCUMENT_TOL, path: str = "$"
) -> list[str]:
    """Paths where ``current`` differs from ``previous`` beyond ``tol``.

    Floats agree when ``|a - b| <= tol + tol * |b|``; ints, bools, strings,
    ``None`` must be identical; mappings must have the same keys; sequences
    the same length.  Type changes (``1`` vs ``1.0``, ``True`` vs ``1``) are
    drift.  An empty list means the documents carry the same numbers.
    """
    if isinstance(previous, bool) or isinstance(current, bool):
        return [] if (type(previous) is type(current) and previous == current) else [path]
    if isinstance(previous, float) and isinstance(current, float):
        if (
            math.isfinite(previous)
            and math.isfinite(current)
            and abs(previous - current) <= tol + tol * abs(current)
        ):
            return []
        return [path]
    if isinstance(previous, Mapping) and isinstance(current, Mapping):
        if set(previous) != set(current):
            return [path]
        drift: list[str] = []
        for key in sorted(previous):
            drift.extend(document_drift(previous[key], current[key], tol=tol, path=f"{path}.{key}"))
        return drift
    if isinstance(previous, (list, tuple)) and isinstance(current, (list, tuple)):
        if len(previous) != len(current):
            return [path]
        drift = []
        for i, (a, b) in enumerate(zip(previous, current, strict=False)):
            drift.extend(document_drift(a, b, tol=tol, path=f"{path}[{i}]"))
        return drift
    return [] if (type(previous) is type(current) and previous == current) else [path]


def _finite(value: Any, name: str) -> float:
    """``value`` as a finite float, or a :class:`ResearchError` naming it."""
    if (
        value is None
        or isinstance(value, bool)
        or not isinstance(value, (int, float, np.floating, np.integer))
    ):
        raise ResearchError(
            f"metric {name!r} was not computed (got {value!r}); a result is never "
            "fabricated — the experiment window is too small or too sparse",
            code="metric_not_computed",
        )
    out = float(value)
    if not math.isfinite(out):
        raise ResearchError(
            f"metric {name!r} is not finite ({out}); a result is never fabricated",
            code="metric_not_computed",
        )
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


def load_instrument_meta(configs_dir: Path) -> dict[int, dict]:
    """Instrument meta for the backtester / capacity proxy, exactly the
    rows ``run_all.py`` builds from ``configs/instruments/instruments.json``."""
    path = Path(configs_dir) / "instruments" / "instruments.json"
    cfg = json.loads(path.read_text())
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


def holdout_capital_usd(
    backtester: Backtester, meta: Mapping[int, dict], instrument_ids: list[int]
) -> float:
    """Research capital line: ``max_pos_qty x ref_price x unit`` per
    instrument, converted to USD at the conversion pair's ``ref_price``
    (``run_all.py``'s ``_capital_usd``)."""
    total = 0.0
    for iid in instrument_ids:
        unit = meta[iid]["lot_size"] if meta[iid]["asset_class"] == "FX" else 1
        native = backtester.config.max_pos_qty * meta[iid]["ref_price"] * unit
        total += native * backtester.reference_rate(backtester.quote_currency(iid))
    return total


def restrict_frames(
    frames: Mapping[int, pd.DataFrame], start_ts: int, end_ts: int
) -> dict[int, pd.DataFrame]:
    """Rows with ``start_ts <= exchange_ts < end_ts`` per instrument."""
    out: dict[int, pd.DataFrame] = {}
    for iid in sorted(frames):
        ts = frames[iid]["exchange_ts"].to_numpy(dtype=np.int64)
        out[iid] = frames[iid][(ts >= start_ts) & (ts < end_ts)].reset_index(drop=True)
    return out


def _assert_holdout_is_held_out(window: Mapping[int, pd.DataFrame], test_start_ts: int) -> None:
    """Fail loudly if any walk-forward row reaches into the declared holdout.

    The walk-forward window and the holdout backtest are separate pieces of
    evidence in the same result document, and a reader is entitled to assume
    the second was not used to produce the first. That assumption was false
    for a year of this repository's history (see ``run``), silently, because
    nothing checked it. This is cheap and runs on every experiment.
    """
    for iid in sorted(window):
        ts = window[iid]["exchange_ts"].to_numpy(dtype=np.int64)
        if ts.size and int(ts.max()) >= test_start_ts:
            raise ResearchError(
                f"instrument {iid}: walk-forward window reaches ts {int(ts.max())}, "
                f"at or past the declared holdout start {test_start_ts} — the "
                "reported OOS statistics would not be out of sample"
            )


def render_document(doc: Mapping[str, Any]) -> str:
    """The persisted form: sorted keys, 2-space indent, ASCII, no NaN,
    trailing newline — byte-deterministic for a given document."""
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n"


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
    ``None``).  Which report keys feed ``ic`` and ``t_stat`` follows the
    spec's method bundle (:data:`_REPORT_METRICS`).
    """
    bundle = str(spec.configuration.get("methods", METHODS_LEGACY))
    metrics = {field: _finite(report.get(key), key) for field, key in _REPORT_METRICS[bundle]}
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
    (instruments + execution cost model).  With ``dry_run`` no experiment
    directory is written, but the ledger is saved — a dry run is a look
    (module docs, step 6).  ``normalized_dir`` holds the normalized event
    files the recompute leakage probe rebuilds features from; it defaults
    to ``<feature_store_dir>/../normalized`` and to nothing when the runner
    is given frames in memory (the probe then does not run and the result
    is not gate-eligible).
    """

    def __init__(
        self,
        feature_store_dir: Path | None,
        ledger_path: Path,
        out_dir: Path,
        configs_dir: Path,
        *,
        dry_run: bool = False,
        frames: Mapping[int, pd.DataFrame] | None = None,
        repo_root: Path | None = None,
        normalized_dir: Path | None = None,
        gate: Callable[[ExperimentSpec], None] | None = None,
    ) -> None:
        if feature_store_dir is None and frames is None:
            raise ResearchError("ExperimentRunner needs a feature_store_dir or frames")
        #: eligibility of the most recent ``run`` (``None`` before any run)
        self.last_eligibility: GateEligibility | None = None
        #: ``validate_alpha`` report of the most recent ``run``
        self.last_report: dict[str, Any] | None = None
        self.feature_store_dir = Path(feature_store_dir) if feature_store_dir else None
        if normalized_dir is None and self.feature_store_dir is not None and frames is None:
            normalized_dir = self.feature_store_dir.parent / "normalized"
        self.normalized_dir = Path(normalized_dir) if normalized_dir is not None else None
        self.ledger_path = Path(ledger_path)
        self.out_dir = Path(out_dir)
        self.configs_dir = Path(configs_dir)
        self.dry_run = bool(dry_run)
        #: called with the spec before ``run`` reads any data; raises to refuse
        #: (the pre-registration check; ``None`` = ungated, e.g. test fixtures)
        self._gate = gate
        self.repo_root = Path(repo_root) if repo_root is not None else None
        self._frames: dict[int, pd.DataFrame] | None = (
            {int(k): v for k, v in frames.items()} if frames is not None else None
        )
        self.meta = load_instrument_meta(self.configs_dir)
        exec_cfg = json.loads((self.configs_dir / "execution" / "execution.json").read_text())
        self.max_participation = float(exec_cfg["defaults"]["max_participation"])
        self.cost_model = CostModel.load(self.configs_dir / "execution" / "execution.json")
        self.ledger = ExperimentLedger(self.ledger_path)
        self._recompute = RecomputeSources(self.normalized_dir, self.configs_dir)

    # -- inputs ---------------------------------------------------------

    def frames(self) -> dict[int, pd.DataFrame]:
        """The full feature store (loaded once)."""
        if self._frames is None:
            self._frames = load_features(self.feature_store_dir)
        return self._frames

    @staticmethod
    def _methods(spec: ExperimentSpec) -> ResearchMethods:
        return methods(str(spec.configuration["methods"]))

    def _backtester(self, spec: ExperimentSpec, cost_multiplier: float) -> Backtester:
        """The research backtester under the spec's protocol and method
        bundle, with the spec's label horizon set."""
        cfg = spec.configuration
        bundle = self._methods(spec)
        config: BacktestConfig = bundle.backtest_config(
            latency_ns=int(cfg["latency_ns"]),
            max_decision_age_ns=int(cfg["max_decision_age_ns"]),
            flatten_at_session_end=bool(cfg["flatten_at_session_end"]),
        )
        return Backtester(
            bundle.cost_model(self.cost_model).with_multiplier(cost_multiplier),
            self.meta,
            config,
        ).for_horizon(spec.horizon)

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
        if "methods" not in spec.configuration:
            raise ResearchError(
                "spec.configuration names no method bundle: the spec was written before "
                "v1.5.0, when the legacy methods were the only ones. It cannot be re-run "
                "under its old id — build it again with configuration methods='legacy_v1' "
                "to reproduce it, or 'v2' for the default protocol",
                code="invalid_spec",
            )
        if normalise_configuration(spec.configuration) != dict(spec.configuration):
            raise ResearchError(
                "spec.configuration is not normalised — build specs with iap.research.build_spec"
            )

    # -- evidence -------------------------------------------------------

    def _holdout(
        self,
        spec: ExperimentSpec,
        window: Mapping[int, pd.DataFrame],
        factory: Callable[[], AlphaModel],
    ) -> dict[str, float]:
        """Fit on the (purged, embargoed) train period, backtest the test period."""
        cfg = spec.configuration
        horizon_ns = HORIZONS_NS[spec.horizon]
        fold = Fold(
            index=0,
            train_end=spec.test_period.start_ts,
            test_start=spec.test_period.start_ts,
            test_end=spec.test_period.end_ts,
        )
        train: dict[int, pd.DataFrame] = {}
        test: dict[int, pd.DataFrame] = {}
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
                f"- costs {costs}"
            )
        # A holdout that made no trade has an identically-zero P&L series and
        # a 0/0 Sharpe; it is reported as 0.0 (module docs, step 5).  Any
        # other non-finite Sharpe is left for build_result to refuse.
        sharpe = 0.0 if result.trade_count == 0 else float(metrics["sharpe_ann"])
        return {
            "gross_return_bps": gross / capital * BPS,
            "transaction_cost_bps": costs / capital * BPS,
            "max_drawdown_bps": float(metrics["max_drawdown"]) / capital * BPS,
            "sharpe": sharpe,
        }

    def _looks(self, spec: ExperimentSpec) -> int:
        """Looks this spec's run debits (module docs, step 4)."""
        return self._methods(spec).looks(int(spec.configuration["n_folds"]))

    def _gate_looks(self, spec: ExperimentSpec) -> int:
        """The look count this spec's t threshold is derived from: recorded
        on its ledger entry when it has run before, else the ledger total
        once this run's looks are in it."""
        identity = (spec.alpha_id, LEDGER_KIND, spec.to_dict(), self._looks(spec))
        return self.ledger.gate_looks_for(
            spec.alpha_id, LEDGER_KIND, spec.to_dict(), self.ledger.batch_total([identity])
        )

    def _ledger_total_after(
        self, spec: ExperimentSpec, report: Mapping[str, Any], gate_looks: int | None
    ) -> int:
        return self.ledger.record(
            spec.alpha_id,
            LEDGER_KIND,
            config=spec.to_dict(),
            result={
                "experiment_id": spec.experiment_id,
                "oos_ic": report["oos_ic"],
                "nw_tstat": report["nw_tstat"],
                "gate_ic": report["gate_ic"],
                "gate_tstat": report["gate_tstat"],
                "verdict": report["verdict"],
            },
            count=self._looks(spec),
            gate_looks=gate_looks,
        )

    # -- persistence ----------------------------------------------------

    def experiment_dir(self, experiment_id: str) -> Path:
        return self.out_dir / experiment_id

    def _persist(
        self,
        spec: ExperimentSpec,
        result: ExperimentResult,
        eligibility: GateEligibility | None = None,
    ) -> None:
        if eligibility is None:  # no dataset at hand: configuration only
            eligibility = gate_eligibility(spec)
        spec_doc = validate_typed(spec)
        result_doc = validate_typed(result)
        docs = {
            SPEC_FILE: render_document(spec_doc),
            RESULT_FILE: render_document(result_doc),
            ELIGIBILITY_FILE: render_document(eligibility.to_dict(spec.experiment_id)),
        }
        target = self.experiment_dir(spec.experiment_id)
        if not target.exists() and self._claim_new_directory(target, docs):
            self.ledger.save()
            return
        existing = target / RESULT_FILE
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
                    "the directory deliberately if the evidence chain changed",
                    code="not_reproducible",
                )
            # Same numbers: keep the committed bytes (the last ulp of a BLAS
            # reduction is CPU-dependent; the artefact must not churn).
            reproduced = all(previous.get(k) == result_doc[k] for k in _PROVENANCE_FIELDS)
        target.mkdir(parents=True, exist_ok=True)
        for name, text in docs.items():
            if name == RESULT_FILE and existing.is_file() and reproduced:
                continue
            atomic_write_text(target / name, text, encoding="ascii", newline="\n")
        self.ledger.save()

    def _claim_new_directory(self, target: Path, docs: Mapping[str, str]) -> bool:
        """Assemble a complete experiment directory beside ``target`` and move
        it into place in one ``os.replace``.  ``True`` when this writer
        claimed the id; ``False`` when another writer got there first (the
        caller then takes the rerun path against that writer's documents).
        """
        self.out_dir.mkdir(parents=True, exist_ok=True)
        staging = self.out_dir / f"{STAGING_PREFIX}{target.name}-{os.getpid()}"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir()
        try:
            for name, text in docs.items():
                with open(staging / name, "w", encoding="ascii", newline="\n") as f:
                    f.write(text)
                    f.flush()
                    os.fsync(f.fileno())
            try:
                os.replace(str(staging), str(target))
            except OSError:
                return False
            return True
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    # -- protocol -------------------------------------------------------

    def run(self, spec: ExperimentSpec) -> ExperimentResult:
        """Run ``spec`` (see the module docs for the six steps)."""
        if self._gate is not None:
            self._gate(spec)
        self._check_spec(spec)
        factory = self._model_factory(spec)
        probe = factory()
        # Two windows, because they answer two different questions.
        #
        # ``full_window`` spans train..test end and belongs to the holdout
        # backtest, which masks train and test out of it itself.
        #
        # ``wf_window`` stops where the declared holdout begins and is the
        # only thing the walk-forward ever sees. It used to be the full
        # window, so every headline statistic (ic, rank_ic, t_stat,
        # hit_rate, turnover, fold_consistency, verdict) was a whole-dataset
        # number and the later folds trained on rows inside the holdout they
        # were supposed to be held out from: on the committed EQ03 spec,
        # fold 3 trained on 19.4% of the declared test period and fold 4 on
        # 59.3%, while ``validation_period`` was constructed,
        # contract-validated, printed, and then read by nothing. Splitting
        # the two makes the holdout genuinely held out and makes
        # ``validation_period`` the boundary it claims to be;
        # ``_assert_holdout_is_held_out`` stops it regressing.
        full_window = restrict_frames(
            self.frames(), spec.train_period.start_ts, spec.test_period.end_ts
        )
        universe = probe.universe(sorted(full_window))
        full_window = {iid: full_window[iid] for iid in universe}
        if not any(len(df) for df in full_window.values()):
            raise ResearchError(
                f"no rows for {spec.alpha_id}'s universe inside the experiment window"
            )
        wf_window = restrict_frames(
            full_window, spec.train_period.start_ts, spec.test_period.start_ts
        )
        if not any(len(df) for df in wf_window.values()):
            raise ResearchError(
                f"no rows for {spec.alpha_id}'s universe before the declared "
                "holdout — the walk-forward would have nothing to evaluate"
            )
        _assert_holdout_is_held_out(wf_window, spec.test_period.start_ts)
        cfg = spec.configuration
        bundle = self._methods(spec)
        gate_looks: int | None = None
        ledger_t: float | None = None
        if bundle.tstat_threshold == "ledger":
            # The run is judged against the denominator it contributes to,
            # and a rerun against the one its first run was judged at
            # (iap.validation.ledger, "Gate look count").
            gate_looks = self._gate_looks(spec)
            ledger_t = self.ledger.bonferroni_t_threshold_at(gate_looks)
        asset_class = "FX" if probe.asset_class == "FX" else "EQUITY"
        recompute = self._recompute.get(asset_class) if bundle.recompute_probe else None
        try:
            report = validate_alpha(
                factory,
                wf_window,
                self._backtester(spec, 1.0),
                self.meta,
                self.max_participation,
                n_folds=int(cfg["n_folds"]),
                embargo_ns=int(cfg["embargo_ns"]),
                ledger_t_threshold=ledger_t,
                ledger_looks=gate_looks,
                seed=int(spec.seed),
                recompute=recompute,
                **bundle.validate_kwargs(),
            )
        except ValueError as exc:  # splitter: too few rows / degenerate boundaries
            raise ResearchError(f"walk-forward validation impossible: {exc}") from exc
        self.last_report = report
        holdout = self._holdout(spec, full_window, factory)
        run_reasons: list[str] = []
        if bundle.recompute_probe and report["leakage"]["recompute_ok"] is None:
            run_reasons.append(
                "the recompute leakage probe did not run: no normalized event file for "
                f"{asset_class} beside the feature store"
            )
        if bundle.name == METHODS_V2 and not report["label_reopen_available"]:
            run_reasons.append(
                "the frames carry no label_reopen column: the default row policy could "
                "not score BLACKOUT rows (feature store written before v1.5.0)"
            )
        eligibility = gate_eligibility(
            spec,
            self.frames(),
            run_reasons=run_reasons,
            significance_threshold=float(report["gates"]["min_nw_tstat"]),
            threshold_looks=gate_looks,
        )
        total = self._ledger_total_after(spec, report, gate_looks)
        try:
            result = build_result(spec, report, holdout, total, tracker.git_commit(self.repo_root))
        except ResearchError:
            # The statistics were computed and the failure names one of
            # them: the looks are debited even though no result exists.
            self.ledger.save()
            raise
        self.last_eligibility = eligibility
        if self.dry_run:
            self.ledger.save()
        else:
            self._persist(spec, result, eligibility)
        return result
