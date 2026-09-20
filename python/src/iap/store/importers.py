"""Importers: flat-file research artefacts -> the store.

Each importer is total over its input: a malformed or non-finite record is
skipped and reported in :attr:`ImportReport.warnings`, never raised, and
every write is an upsert, so importing twice leaves the counts unchanged.
The artefacts stay the source of truth; the store is the index.

Import order used by :func:`import_all` (and ``python -m iap.store build``):
reference -> experiments ledger -> alpha reports -> experiment documents ->
lifecycle log -> lifecycle transitions -> alpha registry -> TCA orders ->
model runs -> drift baselines.  Only the lifecycle-transitions and
alpha-registry steps write ``alphas.current_state``, so the alpha rows must
exist before they run.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from iap.contracts.types import (
    Actor,
    ContractError,
    ExperimentResult,
    ExperimentSpec,
    GateResult,
    LifecycleState,
    LifecycleTransition,
    Verdict,
)
from iap.contracts.validate import ContractValidationError
from iap.contracts.versions import canonical_json, content_hash
from iap.store.db import Store

__all__ = [
    "ImportReport",
    "UNVERSIONED",
    "default_repo_root",
    "import_all",
    "import_alpha_registry",
    "import_alpha_reports",
    "import_baselines",
    "import_experiment_documents",
    "import_experiments_ledger",
    "import_lifecycle_log",
    "import_lifecycle_transitions",
    "import_model_runs",
    "import_reference",
    "import_tca_orders",
]

PathLike = Union[str, Path]

#: The platform's pinned sentinel for "no git commit recorded"
#: (docs/governance/REPRODUCIBILITY.md).
UNVERSIONED = "unversioned-workspace"

#: Pinned lifecycle gates of ``configs/strategies/strategies.json``
#: ``adaptive.lifecycle`` (API_ADAPTIVE): the thresholds the research
#: lifecycle log was produced under.
WATCH_IC_GATE = 0.0
REACTIVATE_IC_GATE = 0.005

@dataclass(frozen=True)
class ImportReport:
    """What one importer wrote: ``{table: rows written}`` plus the records
    it skipped, each described by one warning line."""

    inserted: Dict[str, int] = field(default_factory=dict)
    warnings: Tuple[str, ...] = ()

    @property
    def n_warnings(self) -> int:
        return len(self.warnings)


class _Collector:
    """Accumulates per-table write counts and warning lines."""

    def __init__(self) -> None:
        self.inserted: Dict[str, int] = {}
        self.warnings: List[str] = []

    def wrote(self, table: str, n: int = 1) -> None:
        self.inserted[table] = self.inserted.get(table, 0) + n

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def report(self) -> ImportReport:
        return ImportReport(dict(sorted(self.inserted.items())), tuple(self.warnings))


def default_repo_root() -> Path:
    """``<repo>`` resolved from this file (``python/src/iap/store``)."""
    return Path(__file__).resolve().parents[4]


def _load_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def _num_or_none(value: Any) -> Optional[float]:
    """A finite number, else ``None`` (NULL) — for nullable columns."""
    return float(value) if _finite(value) else None


def _read_jsonl(path: Path, col: _Collector) -> List[Tuple[int, Dict[str, Any]]]:
    out: List[Tuple[int, Dict[str, Any]]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            text = line.strip()
            if not text:
                continue
            try:
                doc = json.loads(text)
            except ValueError as exc:
                col.warn(f"{path.name}:{lineno}: not JSON ({exc})")
                continue
            if not isinstance(doc, dict):
                col.warn(f"{path.name}:{lineno}: not an object")
                continue
            out.append((lineno, doc))
    return out


# --------------------------------------------------------------------------
# Reference data
# --------------------------------------------------------------------------

def import_reference(store: Store, configs_dir: PathLike,
                     feature_registry: Optional[PathLike] = None) -> ImportReport:
    """``configs/instruments/instruments.json`` -> instruments,
    ``configs/venues/venues.json`` -> venues, and the feature registry
    (default ``<repo>/data/reference/feature_registry.json`` next to
    ``configs/``; skipped with a warning when absent) -> feature_versions."""
    configs = Path(configs_dir)
    col = _Collector()

    for inst in sorted(_load_json(configs / "instruments" / "instruments.json")["instruments"],
                       key=lambda d: d["instrument_id"]):
        store.upsert("instruments", {
            "instrument_id": inst["instrument_id"],
            "symbol": inst["symbol"],
            "asset_class": inst["asset_class"],
            "currency": inst.get("currency") or inst["quote_currency"],
            "base_currency": inst.get("base_currency"),
            "tick_size": float(inst["tick_size"]),
            "lot_size": int(inst["lot_size"]),
            "ref_price": float(inst["ref_price"]),
            "adv": float(inst["adv"]),
            "pip": _num_or_none(inst.get("pip")),
            "venues_json": canonical_json(inst["venues"]),
            "underlying_json": (canonical_json(inst["underlying"])
                                if "underlying" in inst else None),
        })
        col.wrote("instruments")

    for venue in sorted(_load_json(configs / "venues" / "venues.json")["venues"],
                        key=lambda d: d["venue_id"]):
        store.upsert("venues", {
            "venue_id": venue["venue_id"],
            "venue": venue["venue"],
            "asset_class": venue["asset_class"],
            "taker_fee_per_share": _num_or_none(venue.get("taker_fee_per_share")),
            "maker_rebate_per_share": _num_or_none(venue.get("maker_rebate_per_share")),
            "commission_per_million": _num_or_none(venue.get("commission_per_million")),
            "latency_mean_ns": int(venue["latency"]["mean_ns"]),
            "latency_jitter_ns": int(venue["latency"]["jitter_ns"]),
            "supports_json": canonical_json(venue["supports"]),
        })
        col.wrote("venues")

    registry_path = (Path(feature_registry) if feature_registry is not None
                     else configs.parent / "data" / "reference" / "feature_registry.json")
    if registry_path.is_file():
        registry = _load_json(registry_path)
        features = registry["features"]
        store.upsert("feature_versions", {
            "feature_version": registry["registry_hash"],
            "registry_hash": registry["registry_hash"],
            "x_version": int(registry["x-version"]),
            "n_features": len(features),
            "registry_json": canonical_json(features),
        })
        col.wrote("feature_versions")
    else:
        col.warn(f"feature registry absent: {registry_path}")
    return col.report()


# --------------------------------------------------------------------------
# Multiple-testing ledger
# --------------------------------------------------------------------------

def import_experiments_ledger(store: Store, path: PathLike) -> ImportReport:
    """``research/experiments.json`` entries -> ledger_entries (one row per
    distinct key; ``count`` looks each)."""
    col = _Collector()
    doc = _load_json(Path(path))
    for entry in sorted(doc["entries"], key=lambda e: e["key"]):
        result = entry.get("result") or {}
        verdict = result.get("verdict")
        if verdict is not None and verdict not in Verdict.__members__:
            col.warn(f"ledger {entry['key'][:12]}: unknown verdict {verdict!r}")
            continue
        for name in ("oos_ic", "nw_tstat"):
            if name in result and not _finite(result[name]):
                col.warn(f"ledger {entry['key'][:12]}: {name} non-finite, stored NULL")
        store.upsert("ledger_entries", {
            "ledger_key": entry["key"],
            "alpha_id": entry["alpha_id"],
            "kind": entry["kind"],
            "config_json": canonical_json(entry["config"]),
            "count": int(entry["count"]),
            "n": int(entry["n"]),
            "reruns": int(entry.get("reruns", 0)),
            "oos_ic": _num_or_none(result.get("oos_ic")),
            "nw_tstat": _num_or_none(result.get("nw_tstat")),
            "verdict": verdict,
            "result_json": canonical_json(result),
        })
        col.wrote("ledger_entries")
    return col.report()


# --------------------------------------------------------------------------
# Alpha reports -> alphas + experiments + experiment_results
# --------------------------------------------------------------------------

def _ledger_entries(ledger_path: Optional[PathLike]) -> Dict[str, Dict[str, Any]]:
    """``{alpha_id: promotion_pipeline ledger entry}`` (empty without a ledger)."""
    if ledger_path is None or not Path(ledger_path).is_file():
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for entry in _load_json(Path(ledger_path))["entries"]:
        if entry["kind"] == "promotion_pipeline":
            out[entry["alpha_id"]] = entry
    return out


def _rationales() -> Dict[str, str]:
    """``{alpha_id: economic rationale}`` from the flagship registry."""
    from iap.alpha import ALPHA_CLASSES
    return {aid: cls.economic_rationale() for aid, cls in ALPHA_CLASSES.items()}


def _params_document(repo_root: Path, dataset_version: Optional[str],
                     feature_version: Optional[str], col: _Collector) -> Optional[Dict[str, Any]]:
    """``configs/strategies/alpha_params.json`` — the provenance document
    the registry's mapping reads (``data_version``, ``feature_version``,
    ``git_commit`` and the per-alpha parameter blocks whose hash is the
    ``model_version``).  Explicit ``dataset_version`` / ``feature_version``
    arguments override the document's (for a tree that has none: the
    arguments must then both be given, and the model hash / commit are the
    ``unversioned`` sentinels)."""
    from iap.lifecycle.bootstrap import PARAMS_RELPATH, load_params_document

    path = repo_root / PARAMS_RELPATH
    doc: Optional[Dict[str, Any]] = None
    if path.is_file():
        doc = load_params_document(repo_root)
    elif dataset_version is None or feature_version is None:
        col.warn(f"alpha reports: {PARAMS_RELPATH.as_posix()} not found and no explicit "
                 "dataset_version / feature_version; experiment rows skipped")
        return None
    else:
        doc = {"data_version": dataset_version, "feature_version": feature_version,
               "git_commit": UNVERSIONED, "params": {}}
    if dataset_version is not None:
        doc = dict(doc, data_version=dataset_version)
    if feature_version is not None:
        doc = dict(doc, feature_version=feature_version)
    return doc


def _report_to_contracts(report: Mapping[str, Any], source: str,
                         ledger_entry: Optional[Mapping[str, Any]],
                         params_doc: Mapping[str, Any]) -> Tuple[ExperimentSpec, ExperimentResult]:
    """The pinned alpha-report -> (ExperimentSpec, ExperimentResult) mapping —
    ``iap.lifecycle.bootstrap.research_evidence`` / ``alpha_report_spec``,
    the one mapping the registry is built from, so the store row and the
    registry evidence of an alpha carry the same ``experiment_id`` and
    numbers (docs/DATA_MODEL.md section 6).  Raises ``ValueError`` on a
    report that cannot be mapped honestly (a non-finite metric)."""
    from iap.lifecycle.bootstrap import alpha_report_spec, research_evidence

    alpha_id = str(report["alpha_id"])
    params = dict(params_doc)
    if alpha_id not in params.get("params", {}):
        # No fitted block for this alpha (a tree without alpha_params.json):
        # the model hash is the unversioned sentinel, never a fake hash.
        params["params"] = dict(params.get("params", {}), **{alpha_id: None})
    result, missing = research_evidence(alpha_id, report, ledger_entry, params)
    if result is None:
        raise ValueError(f"non-finite report metric(s): {', '.join(missing)}")
    if params["params"][alpha_id] is None:
        result = ExperimentResult.from_dict(dict(result.to_dict(), model_version=None))
    spec = alpha_report_spec(alpha_id, report, result, source)
    return spec, result


def import_alpha_reports(store: Store, reports_dir: PathLike, *,
                         dataset_version: Optional[str] = None,
                         feature_version: Optional[str] = None,
                         ledger_path: Optional[PathLike] = None,
                         repo_root: Optional[PathLike] = None) -> ImportReport:
    """``research/alpha_reports/<ID>.json`` -> alphas (id, asset class,
    family = the report's ``name``, horizon, economic rationale from
    ``iap.alpha``) and, per report, one ExperimentSpec + ExperimentResult
    under the pinned mapping shared with the lifecycle registry
    (``iap.lifecycle.bootstrap.research_evidence`` + ``alpha_report_spec``:
    ``experiment_id`` = the ledger entry's ``key[:16]``, versions and the
    model hash from ``configs/strategies/alpha_params.json``, P&L in bps of
    the 1e6 USD reference notional).  A report with a non-finite metric
    still registers its alpha but contributes no experiment rows (warning).
    ``ledger_path`` supplies the ledger entry (``<ID>-unledgered`` and
    ``n_experiments_in_ledger = 0`` without it).  An existing alpha row
    keeps its ``current_state``."""
    reports = Path(reports_dir)
    root = Path(repo_root) if repo_root is not None else default_repo_root()
    col = _Collector()
    params_doc = _params_document(root, dataset_version, feature_version, col)
    ledger = _ledger_entries(ledger_path)
    rationales = _rationales()
    states = {r["alpha_id"]: r["current_state"]
              for r in store.query("SELECT alpha_id, current_state FROM alphas")}

    for path in sorted(reports.glob("*.json")):
        report = _load_json(path)
        if not isinstance(report, dict) or "alpha_id" not in report:
            col.warn(f"{path.name}: not an alpha report")
            continue
        alpha_id = report["alpha_id"]
        store.insert_alpha(
            alpha_id, asset_class=report["asset_class"], family=report["name"],
            horizon=report["horizon"], economic_rationale=rationales.get(alpha_id, ""),
            current_state=states.get(alpha_id, "RESEARCH"))
        col.wrote("alphas")
        if params_doc is None:
            continue
        try:
            spec, result = _report_to_contracts(
                report, f"research/alpha_reports/{path.name}", ledger.get(alpha_id), params_doc)
        except (ValueError, KeyError, TypeError) as exc:
            col.warn(f"{path.name}: experiment rows skipped ({exc})")
            continue
        store.insert_experiment_spec(spec)
        store.insert_experiment_result(result)
        col.wrote("experiments")
        col.wrote("experiment_results")
    return col.report()


# --------------------------------------------------------------------------
# Experiment documents (research/experiments/<experiment_id>/{spec,result}.json)
# --------------------------------------------------------------------------

def import_experiment_documents(store: Store, experiments_dir: PathLike) -> ImportReport:
    """``research/experiments/<id>/spec.json`` (ExperimentSpec) and
    ``result.json`` (ExperimentResult), the ExperimentRunner's own
    documents -> experiments + experiment_results.  A directory whose id
    does not match its spec, or a document that fails its contract, is
    skipped with a warning; a spec without a result imports alone."""
    col = _Collector()
    base = Path(experiments_dir)
    for spec_path in sorted(base.glob("*/spec.json")):
        run_dir = spec_path.parent
        try:
            spec = ExperimentSpec.from_dict(_load_json(spec_path))
        except (ContractError, ValueError, TypeError) as exc:
            col.warn(f"{run_dir.name}/spec.json: skipped ({exc})")
            continue
        if spec.experiment_id != run_dir.name:
            col.warn(f"{run_dir.name}/spec.json: experiment_id {spec.experiment_id!r} "
                     "does not match its directory, skipped")
            continue
        store.insert_experiment_spec(spec)
        col.wrote("experiments")
        result_path = run_dir / "result.json"
        if not result_path.is_file():
            col.warn(f"{run_dir.name}: result.json absent")
            continue
        try:
            result = ExperimentResult.from_dict(_load_json(result_path))
        except (ContractError, ValueError, TypeError) as exc:
            col.warn(f"{run_dir.name}/result.json: skipped ({exc})")
            continue
        if result.experiment_id != spec.experiment_id:
            col.warn(f"{run_dir.name}/result.json: experiment_id mismatch, skipped")
            continue
        store.insert_experiment_result(result)
        col.wrote("experiment_results")
    return col.report()


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------

def import_lifecycle_log(store: Store, path: PathLike, *,
                         watch_ic_gate: float = WATCH_IC_GATE,
                         reactivate_ic_gate: float = REACTIVATE_IC_GATE) -> ImportReport:
    """``research/lifecycle_log.jsonl`` (the ACTIVE/WATCH/RETIRED
    sub-machine of ``iap.adaptive.lifecycle``, one simulated deployment
    per policy) -> lifecycle_transitions with ``source = 'lifecycle_log'``.

    Gate mapping: a move to a lower state (WATCH -> ACTIVE, RETIRED ->
    WATCH) passed the ``reactivate_ic`` gate; a move to a higher state
    (ACTIVE -> WATCH, WATCH -> RETIRED) failed the ``watch_ic`` gate.  The
    gate value is the row's ``rolling_ic`` and the thresholds are the
    pinned config gates.  ``actor`` is SYSTEM (the tracker is automatic).
    This artefact never changes ``alphas.current_state``: it is a policy
    comparison, not the alpha's ledger.
    """
    col = _Collector()
    src = Path(path)
    if not src.is_file():
        col.warn(f"absent: {src}")
        return col.report()
    for lineno, doc in _read_jsonl(src, col):
        try:
            from_state = LifecycleState[doc["from"]]
            to_state = LifecycleState[doc["to"]]
            recovered = to_state < from_state
            rolling_ic = doc.get("rolling_ic")
            if rolling_ic is not None and not _finite(rolling_ic):
                col.warn(f"{src.name}:{lineno}: rolling_ic non-finite, gate value dropped")
                rolling_ic = None
            gate_name = "reactivate_ic" if recovered else "watch_ic"
            threshold = reactivate_ic_gate if recovered else watch_ic_gate
            transition = LifecycleTransition(
                alpha_id=doc["alpha_id"], from_state=from_state, to_state=to_state,
                event_ts=int(doc["event_ts"]), reason=str(doc["reason"]),
                gates={gate_name: GateResult(passed=recovered,
                                             value=None if rolling_ic is None else float(rolling_ic),
                                             threshold=threshold)},
                policy=str(doc["policy"]), actor=Actor.SYSTEM)
            store.insert_lifecycle_transition(
                transition, source="lifecycle_log",
                eval_index=int(doc["eval_index"]) if "eval_index" in doc else None)
        except (KeyError, ValueError, TypeError, ContractValidationError) as exc:
            col.warn(f"{src.name}:{lineno}: skipped ({exc})")
            continue
        col.wrote("lifecycle_transitions")
    return col.report()


def import_lifecycle_transitions(store: Store, path: PathLike) -> ImportReport:
    """``research/lifecycle_transitions.jsonl`` (LifecycleTransition
    documents written by the lifecycle service) -> lifecycle_transitions
    with ``source = 'lifecycle_transitions'``, then ``alphas.current_state``
    := the latest ``to_state`` per alpha (by ``event_ts``, then file
    order).  An absent file is a warning, not an error."""
    col = _Collector()
    src = Path(path)
    if not src.is_file():
        col.warn(f"absent: {src}")
        return col.report()
    latest: Dict[str, Tuple[int, int, str]] = {}
    for lineno, doc in _read_jsonl(src, col):
        try:
            transition = LifecycleTransition.from_dict(doc)
            store.insert_lifecycle_transition(transition, source="lifecycle_transitions")
        except (ContractError, ContractValidationError) as exc:
            col.warn(f"{src.name}:{lineno}: skipped ({exc})")
            continue
        col.wrote("lifecycle_transitions")
        key = (transition.event_ts, lineno, transition.to_state.name)
        if transition.alpha_id not in latest or key > latest[transition.alpha_id]:
            latest[transition.alpha_id] = key
    for alpha_id in sorted(latest):
        if store.set_alpha_state(alpha_id, latest[alpha_id][2]):
            col.wrote("alphas.current_state")
        else:
            col.warn(f"{src.name}: alpha {alpha_id} not registered; state not applied")
    return col.report()


def import_alpha_registry(store: Store, path: PathLike) -> ImportReport:
    """``research/alpha_registry.json`` (the lifecycle service's registry:
    ``alphas.<id>.state``) -> ``alphas.current_state``.  The registry is
    the authoritative state, so it is applied after the transitions.  An
    absent file is a warning; an unregistered alpha or unknown state is a
    warning for that alpha."""
    col = _Collector()
    src = Path(path)
    if not src.is_file():
        col.warn(f"absent: {src}")
        return col.report()
    doc = _load_json(src)
    for alpha_id, entry in sorted(doc.get("alphas", {}).items()):
        state = entry.get("state") if isinstance(entry, dict) else None
        if state not in LifecycleState.__members__:
            col.warn(f"{src.name}: alpha {alpha_id} has unknown state {state!r}")
            continue
        if store.set_alpha_state(alpha_id, state):
            col.wrote("alphas.current_state")
        else:
            col.warn(f"{src.name}: alpha {alpha_id} not registered; state not applied")
    return col.report()


# --------------------------------------------------------------------------
# Research TCA harness
# --------------------------------------------------------------------------

_TCA_REQUIRED: Tuple[str, ...] = (
    "decision_mid", "arrival_mid", "end_mid", "arrival_slippage_bps",
    "spread_cost", "impact_cost", "timing_cost")
_TCA_PEROLD: Tuple[str, ...] = (
    "total_is_bps", "delay_bps", "trading_bps", "opportunity_bps", "total_is",
    "delay_cost", "trading_cost", "opportunity_cost", "qty_target", "qty_filled",
    "fill_rate")


def import_tca_orders(store: Store, path: PathLike) -> ImportReport:
    """``research/tca/tca_orders.json`` -> tca_orders (research-layer
    doubles, real price units).  Nullable markout columns store ``NULL``
    for the harness's ``null``; a non-finite required metric skips the
    order with a warning."""
    col = _Collector()
    doc = _load_json(Path(path))
    for key in sorted(doc, key=int):
        for order in sorted(doc[key]["orders"], key=lambda o: int(o["order_id"])):
            perold = order["perold"]
            bad = [n for n in _TCA_REQUIRED if not _finite(order.get(n))]
            bad += [f"perold.{n}" for n in _TCA_PEROLD if not _finite(perold.get(n))]
            if bad:
                col.warn(f"tca order {order['order_id']}: non-finite {bad}, skipped")
                continue
            store.upsert("tca_orders", {
                "order_id": int(order["order_id"]),
                "instrument_id": int(order["instrument_id"]),
                "side": order["side"],
                "qty_target": float(perold["qty_target"]),
                "qty_filled": float(perold["qty_filled"]),
                "fill_rate": float(perold["fill_rate"]),
                "n_fills": int(order["n_fills"]),
                "decision_mid": float(order["decision_mid"]),
                "arrival_mid": float(order["arrival_mid"]),
                "end_mid": float(order["end_mid"]),
                "fill_vwap": _num_or_none(order.get("fill_vwap")),
                "total_is_bps": float(perold["total_is_bps"]),
                "delay_bps": float(perold["delay_bps"]),
                "trading_bps": float(perold["trading_bps"]),
                "opportunity_bps": float(perold["opportunity_bps"]),
                "total_is": float(perold["total_is"]),
                "delay_cost": float(perold["delay_cost"]),
                "trading_cost": float(perold["trading_cost"]),
                "opportunity_cost": float(perold["opportunity_cost"]),
                "spread_cost": float(order["spread_cost"]),
                "impact_cost": float(order["impact_cost"]),
                "timing_cost": float(order["timing_cost"]),
                "arrival_slippage_bps": float(order["arrival_slippage_bps"]),
                "vwap_slippage_bps": _num_or_none(order.get("vwap_slippage_bps")),
                "twap_slippage_bps": _num_or_none(order.get("twap_slippage_bps")),
                "execution_alpha_vs_vwap_bps": _num_or_none(
                    order.get("execution_alpha_vs_vwap_bps")),
                "adverse_selection_json": canonical_json({
                    "bps": order.get("adverse_selection_bps", {}),
                    "n": order.get("adverse_selection_n", {})}),
            })
            col.wrote("tca_orders")
    return col.report()


# --------------------------------------------------------------------------
# Model runs
# --------------------------------------------------------------------------

def import_model_runs(store: Store, models_dir: PathLike) -> ImportReport:
    """``research/models/ledger.json`` + ``<run_id>/manifest.json``
    (+ ``metrics.json`` when present) -> model_runs.  A ledger run without
    a manifest is skipped with a warning."""
    col = _Collector()
    models = Path(models_dir)
    ledger = _load_json(models / "ledger.json")
    for run in sorted(ledger["runs"], key=lambda r: r["run_id"]):
        run_id = run["run_id"]
        manifest_path = models / run_id / "manifest.json"
        if not manifest_path.is_file():
            col.warn(f"model run {run_id}: manifest.json absent, skipped")
            continue
        manifest = _load_json(manifest_path)
        metrics_path = models / run_id / "metrics.json"
        metrics = _load_json(metrics_path) if metrics_path.is_file() else None
        m = metrics or {}
        git_dirty = manifest.get("git_dirty")
        store.upsert("model_runs", {
            "run_id": run_id,
            "name": run["name"],
            "model_version": str(manifest["model_version"]),
            "data_version": str(manifest["data_version"]),
            "feature_version": str(manifest["feature_version"]),
            "git_commit": str(manifest["git_commit"]),
            "git_dirty": None if git_dirty is None else int(bool(git_dirty)),
            "train_start_ts": int(manifest["train_window"]["start_ts"]),
            "train_end_ts": int(manifest["train_window"]["end_ts"]),
            "test_start_ts": int(manifest["test_window"]["start_ts"]),
            "test_end_ts": int(manifest["test_window"]["end_ts"]),
            "hyperparams_json": canonical_json(manifest.get("hyperparams", {})),
            "hardware_json": canonical_json(manifest.get("hardware", {})),
            "manifest_json": canonical_json(manifest),
            "metrics_json": None if metrics is None else canonical_json(metrics),
            "mean_ic": _num_or_none(m.get("mean_ic")),
            "mean_rank_ic": _num_or_none(m.get("mean_rank_ic")),
            "ic_tstat": _num_or_none(m.get("ic_tstat")),
            "pooled_ic": _num_or_none(m.get("pooled_ic")),
            "auc_test": _num_or_none(m.get("auc_test")),
            "brier_test": _num_or_none(m.get("brier_test")),
        })
        col.wrote("model_runs")
    return col.report()


# --------------------------------------------------------------------------
# Drift baselines
# --------------------------------------------------------------------------

def import_baselines(store: Store, baselines_dir: PathLike) -> ImportReport:
    """``research/baselines/*.json`` -> drift_baselines (PSI histograms for
    ``feature`` / ``signal`` kinds, the rolling-IC baseline for ``ic``)."""
    col = _Collector()
    for path in sorted(Path(baselines_dir).glob("*.json")):
        doc = _load_json(path)
        kind = doc.get("kind")
        if kind not in ("feature", "signal", "ic"):
            col.warn(f"{path.name}: unknown baseline kind {kind!r}, skipped")
            continue
        row: Dict[str, Any] = {
            "name": doc["name"], "alpha_id": doc["alpha_id"], "kind": kind,
            "x_version": int(doc["x-version"]), "feature_version": doc["feature_version"],
            "source": str(doc.get("source", "")),
            "n": None, "n_buckets": None, "mean": None, "std": None,
            "min_value": None, "max_value": None, "psi_eps": None,
            "edges_json": None, "expected_frac_json": None, "horizon": None,
            "baseline_kind": None, "bucket_ns": None, "ic_mean": None,
            "ic_std": None, "n_buckets_baseline": None,
        }
        if kind == "ic":
            row.update(horizon=doc.get("horizon"), baseline_kind=doc.get("baseline_kind"),
                       bucket_ns=int(doc["bucket_ns"]),
                       ic_mean=_num_or_none(doc.get("ic_mean")),
                       ic_std=_num_or_none(doc.get("ic_std")),
                       n_buckets_baseline=int(doc["n_buckets_baseline"]))
        else:
            row.update(n=int(doc["n"]), n_buckets=int(doc["n_buckets"]),
                       mean=_num_or_none(doc.get("mean")), std=_num_or_none(doc.get("std")),
                       min_value=_num_or_none(doc.get("min")),
                       max_value=_num_or_none(doc.get("max")),
                       psi_eps=_num_or_none(doc.get("psi_eps")),
                       edges_json=canonical_json(doc["edges"]),
                       expected_frac_json=canonical_json(doc["expected_frac"]))
        store.upsert("drift_baselines", row)
        col.wrote("drift_baselines")
    return col.report()


# --------------------------------------------------------------------------
# Everything
# --------------------------------------------------------------------------

def import_all(store: Store, repo_root: Optional[PathLike] = None
               ) -> Dict[str, ImportReport]:
    """Run every importer over the repository artefacts that exist, in the
    pinned order; ``{step: report}`` in that order."""
    root = Path(repo_root) if repo_root is not None else default_repo_root()
    research = root / "research"
    ledger = research / "experiments.json"
    steps: Sequence[Tuple[str, Path, Any]] = (
        ("reference", root / "configs",
         lambda: import_reference(store, root / "configs")),
        ("experiments_ledger", ledger,
         lambda: import_experiments_ledger(store, ledger)),
        ("alpha_reports", research / "alpha_reports",
         lambda: import_alpha_reports(store, research / "alpha_reports",
                                      ledger_path=ledger, repo_root=root)),
        ("experiment_documents", research / "experiments",
         lambda: import_experiment_documents(store, research / "experiments")),
        ("lifecycle_log", research / "lifecycle_log.jsonl",
         lambda: import_lifecycle_log(store, research / "lifecycle_log.jsonl")),
        ("lifecycle_transitions", research / "lifecycle_transitions.jsonl",
         lambda: import_lifecycle_transitions(store, research / "lifecycle_transitions.jsonl")),
        ("alpha_registry", research / "alpha_registry.json",
         lambda: import_alpha_registry(store, research / "alpha_registry.json")),
        ("tca_orders", research / "tca" / "tca_orders.json",
         lambda: import_tca_orders(store, research / "tca" / "tca_orders.json")),
        ("model_runs", research / "models" / "ledger.json",
         lambda: import_model_runs(store, research / "models")),
        ("baselines", research / "baselines",
         lambda: import_baselines(store, research / "baselines")),
    )
    out: Dict[str, ImportReport] = {}
    for name, required, run in steps:
        out[name] = run() if required.exists() else ImportReport({}, (f"absent: {required}",))
    return out
