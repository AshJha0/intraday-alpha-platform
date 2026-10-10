"""Importers: flat-file research artefacts -> the store.

Each importer is total over its input: a malformed or non-finite record is
skipped and reported in :attr:`ImportReport.warnings`, never raised, and
every write is an upsert, so importing twice leaves the counts unchanged.
The artefacts stay the source of truth; the store is the index.

Import order used by :func:`import_all` (and ``python -m iap.store build``):
current scope -> reference -> experiments ledger (plus any extra ledgers) ->
alpha reports -> experiment documents -> lifecycle log -> lifecycle
transitions -> lifecycle archive -> alpha registry -> TCA orders -> model
runs -> drift baselines.  Only the lifecycle-transitions and alpha-registry
steps write ``alphas.current_state``, so the alpha rows must exist before
they run.

**Scope (data model x-version 2).**  Every experiment, result, ledger entry
and lifecycle transition is filed under the ``(dataset_version, methods)``
it was computed in, and the scorecard views never pool two scopes:

* a ledger entry's dataset is its stamp, else the one inside its config
  (``experiment_runner``), else ``"unstamped"``; its bundle is the one its
  config names, else ``legacy_v1`` (an entry recorded before v1.5.0 names
  none) — :func:`ledger_entry_scope`;
* an alpha report's experiment takes the bundle of the ledger entry it is
  the record of, else the bundle its ``methods`` block matches;
* a runner experiment takes ``configuration["methods"]``;
* the live lifecycle artefacts are filed under the current scope, an
  archived ledger (``research/archive``) under the scope its file name
  states, and neither the archive nor an earlier scope's ledger entries
  touch ``alphas.current_state``.

The CURRENT scope (:func:`resolve_current_scope`) is the dataset of
``configs/strategies/alpha_params.json`` and the default bundle of
``iap.validation.methods``; ``store_scope`` records it and the ``*_current``
views filter to it.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
from iap.contracts.versions import canonical_json
from iap.store.db import LEGACY_METHODS, Store, methods_of

__all__ = [
    "ImportReport",
    "LIFECYCLE_ARCHIVE_GLOB",
    "UNSTAMPED",
    "UNVERSIONED",
    "default_repo_root",
    "import_all",
    "import_alpha_registry",
    "import_alpha_reports",
    "import_baselines",
    "import_experiment_documents",
    "import_experiments_ledger",
    "import_lifecycle_archive",
    "import_lifecycle_log",
    "import_lifecycle_transitions",
    "import_model_runs",
    "import_reference",
    "import_tca_orders",
    "ledger_entry_scope",
    "resolve_current_scope",
]

PathLike = str | Path

#: The platform's pinned sentinel for "no git commit recorded"
#: (docs/governance/REPRODUCIBILITY.md).
UNVERSIONED = "unversioned-workspace"

#: ``dataset_version`` of a ledger entry that carries no dataset at all.
UNSTAMPED = "unstamped"

#: Archived lifecycle ledgers: ``lifecycle_transitions.dataset-<hash
#: prefix>[.methods-<bundle>].jsonl`` under ``research/archive``.
LIFECYCLE_ARCHIVE_GLOB = "lifecycle_transitions.dataset-*.jsonl"

#: Ledger kinds whose entries carry a PROMOTE verdict judged at a t threshold.
_GATED_KINDS = ("promotion_pipeline", "experiment_runner")

#: Pinned lifecycle gates of ``configs/strategies/strategies.json``
#: ``adaptive.lifecycle`` (API_ADAPTIVE): the thresholds the research
#: lifecycle log was produced under.
WATCH_IC_GATE = 0.0
REACTIVATE_IC_GATE = 0.005


@dataclass(frozen=True)
class ImportReport:
    """What one importer wrote: ``{table: rows written}`` plus the records
    it skipped, each described by one warning line."""

    inserted: dict[str, int] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    @property
    def n_warnings(self) -> int:
        return len(self.warnings)


class _Collector:
    """Accumulates per-table write counts and warning lines."""

    def __init__(self) -> None:
        self.inserted: dict[str, int] = {}
        self.warnings: list[str] = []

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
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _num_or_none(value: Any) -> float | None:
    """A finite number, else ``None`` (NULL) — for nullable columns."""
    return float(value) if _finite(value) else None


def _read_jsonl(path: Path, col: _Collector) -> list[tuple[int, dict[str, Any]]]:
    out: list[tuple[int, dict[str, Any]]] = []
    with open(path, encoding="utf-8") as fh:
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


def import_reference(
    store: Store, configs_dir: PathLike, feature_registry: PathLike | None = None
) -> ImportReport:
    """``configs/instruments/instruments.json`` -> instruments,
    ``configs/venues/venues.json`` -> venues, and the feature registry
    (default ``<repo>/data/reference/feature_registry.json`` next to
    ``configs/``; skipped with a warning when absent) -> feature_versions."""
    configs = Path(configs_dir)
    col = _Collector()

    for inst in sorted(
        _load_json(configs / "instruments" / "instruments.json")["instruments"],
        key=lambda d: d["instrument_id"],
    ):
        store.upsert(
            "instruments",
            {
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
                "underlying_json": (
                    canonical_json(inst["underlying"]) if "underlying" in inst else None
                ),
            },
        )
        col.wrote("instruments")

    for venue in sorted(
        _load_json(configs / "venues" / "venues.json")["venues"], key=lambda d: d["venue_id"]
    ):
        store.upsert(
            "venues",
            {
                "venue_id": venue["venue_id"],
                "venue": venue["venue"],
                "asset_class": venue["asset_class"],
                "taker_fee_per_share": _num_or_none(venue.get("taker_fee_per_share")),
                "maker_rebate_per_share": _num_or_none(venue.get("maker_rebate_per_share")),
                "commission_per_million": _num_or_none(venue.get("commission_per_million")),
                "latency_mean_ns": int(venue["latency"]["mean_ns"]),
                "latency_jitter_ns": int(venue["latency"]["jitter_ns"]),
                "supports_json": canonical_json(venue["supports"]),
            },
        )
        col.wrote("venues")

    registry_path = (
        Path(feature_registry)
        if feature_registry is not None
        else configs.parent / "data" / "reference" / "feature_registry.json"
    )
    if registry_path.is_file():
        registry = _load_json(registry_path)
        features = registry["features"]
        store.upsert(
            "feature_versions",
            {
                "feature_version": registry["registry_hash"],
                "registry_hash": registry["registry_hash"],
                "x_version": int(registry["x-version"]),
                "n_features": len(features),
                "registry_json": canonical_json(features),
            },
        )
        col.wrote("feature_versions")
    else:
        col.warn(f"feature registry absent: {registry_path}")
    return col.report()


# --------------------------------------------------------------------------
# Multiple-testing ledger
# --------------------------------------------------------------------------


def ledger_entry_scope(entry: Mapping[str, Any]) -> tuple[str, str]:
    """``(dataset_version, methods)`` of a ledger entry (module docs, Scope)."""
    from iap.validation.ledger import ExperimentLedger

    config = entry.get("config") or {}
    dataset = ExperimentLedger.entry_dataset_version(dict(entry))
    bundle = config.get("methods")
    if bundle is None:
        bundle = (config.get("configuration") or {}).get("methods")
    return (
        UNSTAMPED if dataset is None else dataset,
        LEGACY_METHODS if bundle is None else str(bundle),
    )


def _entry_experiment_id(entry: Mapping[str, Any]) -> str | None:
    """The ``experiments`` row a ledger entry is the record of."""
    if entry["kind"] == "promotion_pipeline":
        return str(entry["key"])[:16]
    if entry["kind"] == "experiment_runner":
        experiment_id = (entry.get("config") or {}).get("experiment_id")
        return None if experiment_id is None else str(experiment_id)
    return None


def _entry_promote_threshold(entry: Mapping[str, Any], bundle: str) -> float | None:
    """The PROMOTE |t| an entry was judged at: ``max(3.0, Bonferroni |t| at
    gate_looks)`` when it records its look count; the fixed gate for a gated
    entry of a fixed-threshold bundle; ``None`` otherwise."""
    from iap.validation.ledger import ExperimentLedger
    from iap.validation.methods import METHODS
    from iap.validation.validate import LEGACY_TSTAT_THRESHOLD, effective_gates

    if entry.get("gate_looks") is not None:
        at = ExperimentLedger.bonferroni_t_threshold_at(int(entry["gate_looks"]))
        return float(effective_gates("ledger", at)["min_nw_tstat"])
    known = METHODS.get(bundle)
    if (
        entry["kind"] in _GATED_KINDS
        and known is not None
        and known.tstat_threshold == LEGACY_TSTAT_THRESHOLD
    ):
        return float(effective_gates(LEGACY_TSTAT_THRESHOLD)["min_nw_tstat"])
    return None


def _rebuild_ledger_scopes(store: Store, col: _Collector) -> None:
    """``ledger_scopes`` := one row per (dataset_version, methods) of
    ``ledger_entries`` with its looks and the Bonferroni |t| at that count
    (rewritten whole, so it always describes every ledger imported)."""
    from iap.validation.ledger import ExperimentLedger

    rows = store.query(
        "SELECT dataset_version, methods, COUNT(*) AS n_entries, SUM(count) AS looks "
        "FROM ledger_entries GROUP BY dataset_version, methods "
        "ORDER BY dataset_version, methods"
    )
    store.replace_rows(
        "ledger_scopes",
        [
            {
                "dataset_version": row["dataset_version"],
                "methods": row["methods"],
                "n_entries": int(row["n_entries"]),
                "looks": int(row["looks"]),
                "bonferroni_t_threshold": float(
                    ExperimentLedger.bonferroni_t_threshold_at(int(row["looks"]))
                ),
            }
            for row in rows
        ],
    )
    col.wrote("ledger_scopes", len(rows))


def import_experiments_ledger(store: Store, path: PathLike) -> ImportReport:
    """A multiple-testing ledger (``research/experiments.json``, or the
    ledger kept beside an ingested dataset) -> ledger_entries, one row per
    distinct key (``count`` looks each) filed under its scope
    (:func:`ledger_entry_scope`), then ``ledger_scopes`` rebuilt over every
    entry in the store.  Importing a second ledger adds its scopes; it never
    merges them into another dataset's."""
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
        dataset, bundle = ledger_entry_scope(entry)
        if dataset == UNSTAMPED:
            col.warn(f"ledger {entry['key'][:12]}: no dataset_version, filed as {UNSTAMPED!r}")
        gate_looks = entry.get("gate_looks")
        store.upsert(
            "ledger_entries",
            {
                "ledger_key": entry["key"],
                "alpha_id": entry["alpha_id"],
                "kind": entry["kind"],
                "dataset_version": dataset,
                "methods": bundle,
                "experiment_id": _entry_experiment_id(entry),
                "gate_looks": None if gate_looks is None else int(gate_looks),
                "promote_t_threshold": _entry_promote_threshold(entry, bundle),
                "config_json": canonical_json(entry["config"]),
                "count": int(entry["count"]),
                "n": int(entry["n"]),
                "reruns": int(entry.get("reruns", 0)),
                "oos_ic": _num_or_none(result.get("oos_ic")),
                "nw_tstat": _num_or_none(result.get("nw_tstat")),
                "verdict": verdict,
                "result_json": canonical_json(result),
            },
        )
        col.wrote("ledger_entries")
    _rebuild_ledger_scopes(store, col)
    return col.report()


# --------------------------------------------------------------------------
# Alpha reports -> alphas + experiments + experiment_results
# --------------------------------------------------------------------------


def _ledger_entries(
    ledger_path: PathLike | None, dataset_version: str | None = None
) -> dict[str, dict[str, Any]]:
    """``{alpha_id: promotion_pipeline ledger entry}`` (empty without a ledger).

    The ledger keeps one entry per alpha, dataset AND method bundle
    (``iap.validation.ledger``, "Dataset scope"): the entry stamped with
    ``dataset_version`` under the default bundle wins, then one of that
    dataset under another bundle, so the row agrees with the registry
    (``iap.lifecycle.bootstrap.select_pipeline_entries``); an alpha with no
    entry on that dataset falls back to its latest entry."""
    from iap.validation.methods import DEFAULT_METHODS

    if ledger_path is None or not Path(ledger_path).is_file():
        return {}
    latest: dict[str, dict[str, Any]] = {}
    exact: dict[str, dict[str, Any]] = {}
    default: dict[str, dict[str, Any]] = {}
    for entry in _load_json(Path(ledger_path))["entries"]:
        if entry["kind"] == "promotion_pipeline":
            latest[entry["alpha_id"]] = entry
            if dataset_version is not None and entry.get("dataset_version") == dataset_version:
                exact[entry["alpha_id"]] = entry
                if ledger_entry_scope(entry)[1] == DEFAULT_METHODS:
                    default[entry["alpha_id"]] = entry
    return {**latest, **exact, **default}


#: The report ``methods`` keys that identify a bundle (``block_rows`` is the
#: horizon-specific label of ``block_invalid_label_rows``).
_REPORT_METHOD_FIELDS = (
    "ic_rows",
    "stress_version",
    "position_policy",
    "cap_fills_at_l1",
    "impact_model",
    "significance",
    "tstat_threshold",
    "capacity",
    "fold_diagnostics",
    "recompute_probe",
)
#: v1.9 method keys: a report states them only when they differ from the
#: default, so an absent key means the default value.
_REPORT_V19_METHOD_FIELDS = {
    "split_mode": "row_mass",
    "gate_ic_source": "pooled",
    "validity_diagnostics": False,
    # v1.12 (``v4``): the report-only R7 blocks.
    "cpcv": False,
    "deflated_sharpe": False,
}


def _report_methods(report: Mapping[str, Any], ledger_entry: Mapping[str, Any] | None) -> str:
    """The bundle an alpha report was produced under: that of the ledger
    entry it is the record of; without one, the bundle whose choices its
    ``methods`` block states (``legacy_v1`` for a report written before the
    block existed).  A block that matches no bundle raises ``ValueError``."""
    from iap.validation.methods import METHODS

    if ledger_entry is not None:
        return ledger_entry_scope(ledger_entry)[1]
    block = report.get("methods")
    if block is None:
        return LEGACY_METHODS
    if isinstance(block, str):
        return block
    for name, bundle in sorted(METHODS.items()):
        if all(block.get(key) == getattr(bundle, key) for key in _REPORT_METHOD_FIELDS) and all(
            block.get(key, default) == getattr(bundle, key)
            for key, default in _REPORT_V19_METHOD_FIELDS.items()
        ):
            return name
    raise ValueError("the report's methods block matches no known bundle")


def _rationales() -> dict[str, str]:
    """``{alpha_id: economic rationale}`` from the flagship registry."""
    from iap.alpha import ALPHA_CLASSES

    return {aid: cls.economic_rationale() for aid, cls in ALPHA_CLASSES.items()}


def _params_document(
    repo_root: Path, dataset_version: str | None, feature_version: str | None, col: _Collector
) -> dict[str, Any] | None:
    """``configs/strategies/alpha_params.json`` — the provenance document
    the registry's mapping reads (``data_version``, ``feature_version``,
    ``git_commit`` and the per-alpha parameter blocks whose hash is the
    ``model_version``).  Explicit ``dataset_version`` / ``feature_version``
    arguments override the document's (for a tree that has none: the
    arguments must then both be given, and the model hash / commit are the
    ``unversioned`` sentinels)."""
    from iap.lifecycle.bootstrap import PARAMS_RELPATH, load_params_document

    path = repo_root / PARAMS_RELPATH
    doc: dict[str, Any] | None = None
    if path.is_file():
        doc = load_params_document(repo_root)
    elif dataset_version is None or feature_version is None:
        col.warn(
            f"alpha reports: {PARAMS_RELPATH.as_posix()} not found and no explicit "
            "dataset_version / feature_version; experiment rows skipped"
        )
        return None
    else:
        doc = {
            "data_version": dataset_version,
            "feature_version": feature_version,
            "git_commit": UNVERSIONED,
            "params": {},
        }
    if dataset_version is not None:
        doc = dict(doc, data_version=dataset_version)
    if feature_version is not None:
        doc = dict(doc, feature_version=feature_version)
    return doc


def _report_to_contracts(
    report: Mapping[str, Any],
    source: str,
    ledger_entry: Mapping[str, Any] | None,
    params_doc: Mapping[str, Any],
) -> tuple[ExperimentSpec, ExperimentResult]:
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


def import_alpha_reports(
    store: Store,
    reports_dir: PathLike,
    *,
    dataset_version: str | None = None,
    feature_version: str | None = None,
    ledger_path: PathLike | None = None,
    repo_root: PathLike | None = None,
) -> ImportReport:
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
    ``n_experiments_in_ledger = 0`` without it).  Both rows are filed under
    the report's method bundle (:func:`_report_methods`) and the dataset of
    the parameter document.  An existing alpha row keeps its
    ``current_state``."""
    reports = Path(reports_dir)
    root = Path(repo_root) if repo_root is not None else default_repo_root()
    col = _Collector()
    params_doc = _params_document(root, dataset_version, feature_version, col)
    ledger = _ledger_entries(
        ledger_path, None if params_doc is None else str(params_doc["data_version"])
    )
    rationales = _rationales()
    states = {
        r["alpha_id"]: r["current_state"]
        for r in store.query("SELECT alpha_id, current_state FROM alphas")
    }

    for path in sorted(reports.glob("*.json")):
        report = _load_json(path)
        if not isinstance(report, dict) or "alpha_id" not in report:
            col.warn(f"{path.name}: not an alpha report")
            continue
        alpha_id = report["alpha_id"]
        store.insert_alpha(
            alpha_id,
            asset_class=report["asset_class"],
            family=report["name"],
            horizon=report["horizon"],
            economic_rationale=rationales.get(alpha_id, ""),
            current_state=states.get(alpha_id, "RESEARCH"),
        )
        col.wrote("alphas")
        if params_doc is None:
            continue
        try:
            spec, result = _report_to_contracts(
                report, f"research/alpha_reports/{path.name}", ledger.get(alpha_id), params_doc
            )
            bundle = _report_methods(report, ledger.get(alpha_id))
        except (ValueError, KeyError, TypeError) as exc:
            col.warn(f"{path.name}: experiment rows skipped ({exc})")
            continue
        store.insert_experiment_spec(spec, methods=bundle)
        store.insert_experiment_result(result, methods=bundle)
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
    skipped with a warning; a spec without a result imports alone.  Both
    rows are filed under the bundle ``configuration["methods"]`` names
    (``legacy_v1`` for a spec written before the bundles existed)."""
    col = _Collector()
    base = Path(experiments_dir)
    for spec_path in sorted(base.glob("*/spec.json")):
        if spec_path.parent.name.startswith("."):
            continue  # a run directory still being staged by the runner
        run_dir = spec_path.parent
        try:
            spec = ExperimentSpec.from_dict(_load_json(spec_path))
            bundle = methods_of(spec.configuration)
        except (ContractError, ValueError, TypeError) as exc:
            col.warn(f"{run_dir.name}/spec.json: skipped ({exc})")
            continue
        if spec.experiment_id != run_dir.name:
            col.warn(
                f"{run_dir.name}/spec.json: experiment_id {spec.experiment_id!r} "
                "does not match its directory, skipped"
            )
            continue
        store.insert_experiment_spec(spec, methods=bundle)
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
        store.insert_experiment_result(result, methods=bundle)
        col.wrote("experiment_results")
    return col.report()


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


def import_lifecycle_log(
    store: Store,
    path: PathLike,
    *,
    watch_ic_gate: float = WATCH_IC_GATE,
    reactivate_ic_gate: float = REACTIVATE_IC_GATE,
    scope: tuple[str, str] | None = None,
) -> ImportReport:
    """``research/lifecycle_log.jsonl`` (the ACTIVE/WATCH/RETIRED
    sub-machine of ``iap.adaptive.lifecycle``, one simulated deployment
    per policy) -> lifecycle_transitions with ``source = 'lifecycle_log'``.

    Gate mapping: a move to a lower state (WATCH -> ACTIVE, RETIRED ->
    WATCH) passed the ``reactivate_ic`` gate; a move to a higher state
    (ACTIVE -> WATCH, WATCH -> RETIRED) failed the ``watch_ic`` gate.  The
    gate value is the row's ``rolling_ic`` and the thresholds are the
    pinned config gates.  ``actor`` is SYSTEM (the tracker is automatic).
    This artefact never changes ``alphas.current_state``: it is a policy
    comparison, not the alpha's ledger.  ``scope`` is the
    ``(dataset_version, methods)`` the log was produced in (the current
    scope for the live file; ``None`` leaves the columns NULL).
    """
    dataset_version, bundle = scope if scope is not None else (None, None)
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
                alpha_id=doc["alpha_id"],
                from_state=from_state,
                to_state=to_state,
                event_ts=int(doc["event_ts"]),
                reason=str(doc["reason"]),
                gates={
                    gate_name: GateResult(
                        passed=recovered,
                        value=None if rolling_ic is None else float(rolling_ic),
                        threshold=threshold,
                    )
                },
                policy=str(doc["policy"]),
                actor=Actor.SYSTEM,
            )
            store.insert_lifecycle_transition(
                transition,
                source="lifecycle_log",
                eval_index=int(doc["eval_index"]) if "eval_index" in doc else None,
                dataset_version=dataset_version,
                methods=bundle,
            )
        except (KeyError, ValueError, TypeError, ContractValidationError) as exc:
            col.warn(f"{src.name}:{lineno}: skipped ({exc})")
            continue
        col.wrote("lifecycle_transitions")
    return col.report()


def import_lifecycle_transitions(
    store: Store,
    path: PathLike,
    *,
    scope: tuple[str, str] | None = None,
    source: str = "lifecycle_transitions",
    apply_state: bool = True,
) -> ImportReport:
    """``research/lifecycle_transitions.jsonl`` (LifecycleTransition
    documents written by the lifecycle service) -> lifecycle_transitions
    with ``source = 'lifecycle_transitions'``, then ``alphas.current_state``
    := the latest ``to_state`` per alpha (by ``event_ts``, then file
    order).  An absent file is a warning, not an error.

    ``scope`` is the ``(dataset_version, methods)`` the transitions were
    decided in.  ``apply_state=False`` imports the rows only — what an
    archived ledger of an earlier scope gets (:func:`import_lifecycle_archive`):
    its transitions are history and must not set today's state."""
    col = _Collector()
    src = Path(path)
    if not src.is_file():
        col.warn(f"absent: {src}")
        return col.report()
    dataset_version, bundle = scope if scope is not None else (None, None)
    latest: dict[str, tuple[int, int, str]] = {}
    for lineno, doc in _read_jsonl(src, col):
        try:
            transition = LifecycleTransition.from_dict(doc)
            store.insert_lifecycle_transition(
                transition, source=source, dataset_version=dataset_version, methods=bundle
            )
        except (ContractError, ContractValidationError) as exc:
            col.warn(f"{src.name}:{lineno}: skipped ({exc})")
            continue
        col.wrote("lifecycle_transitions")
        key = (transition.event_ts, lineno, transition.to_state.name)
        if transition.alpha_id not in latest or key > latest[transition.alpha_id]:
            latest[transition.alpha_id] = key
    if not apply_state:
        return col.report()
    for alpha_id in sorted(latest):
        if store.set_alpha_state(alpha_id, latest[alpha_id][2]):
            col.wrote("alphas.current_state")
        else:
            col.warn(f"{src.name}: alpha {alpha_id} not registered; state not applied")
    return col.report()


def _archive_scope(name: str, datasets: Sequence[str]) -> tuple[str, str]:
    """``(dataset_version, methods)`` an archive file name states:
    ``lifecycle_transitions.dataset-<prefix>[.methods-<bundle>].jsonl``.  The
    prefix is resolved to the one known dataset it starts (the ledger's);
    an unknown prefix stays as written, an ambiguous one is an error.  A
    name without a ``methods`` part predates the bundles: ``legacy_v1``."""
    parts = name[: -len(".jsonl")].split(".")[1:]
    fields = dict(part.split("-", 1) for part in parts if "-" in part)
    prefix = fields.get("dataset")
    if not prefix:
        raise ValueError("the file name states no dataset")
    matches = sorted({d for d in datasets if d.startswith(prefix)})
    if len(matches) > 1:
        raise ValueError(f"dataset prefix {prefix!r} is ambiguous: {matches}")
    return (matches[0] if matches else prefix, fields.get("methods", LEGACY_METHODS))


def import_lifecycle_archive(store: Store, archive_dir: PathLike) -> ImportReport:
    """``research/archive/lifecycle_transitions.dataset-*.jsonl`` — the
    lifecycle ledgers of earlier datasets and bundles, kept when the ledger
    was regenerated -> lifecycle_transitions with ``source =
    'archive/<file>'`` under the scope the file name states
    (:func:`_archive_scope`; the dataset prefix is resolved against the
    datasets of ``ledger_entries``).  ``alphas.current_state`` is never
    touched."""
    col = _Collector()
    base = Path(archive_dir)
    datasets = [
        r["dataset_version"]
        for r in store.query("SELECT DISTINCT dataset_version FROM ledger_entries")
    ]
    for path in sorted(base.glob(LIFECYCLE_ARCHIVE_GLOB)):
        try:
            scope = _archive_scope(path.name, datasets)
        except ValueError as exc:
            col.warn(f"{path.name}: skipped ({exc})")
            continue
        report = import_lifecycle_transitions(
            store, path, scope=scope, source=f"archive/{path.name}", apply_state=False
        )
        for table, n in report.inserted.items():
            col.wrote(table, n)
        for warning in report.warnings:
            col.warn(warning)
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

_TCA_REQUIRED: tuple[str, ...] = (
    "decision_mid",
    "arrival_mid",
    "end_mid",
    "arrival_slippage_bps",
    "spread_cost",
    "impact_cost",
    "timing_cost",
)
_TCA_PEROLD: tuple[str, ...] = (
    "total_is_bps",
    "delay_bps",
    "trading_bps",
    "opportunity_bps",
    "total_is",
    "delay_cost",
    "trading_cost",
    "opportunity_cost",
    "qty_target",
    "qty_filled",
    "fill_rate",
)


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
            store.upsert(
                "tca_orders",
                {
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
                        order.get("execution_alpha_vs_vwap_bps")
                    ),
                    "adverse_selection_json": canonical_json(
                        {
                            "bps": order.get("adverse_selection_bps", {}),
                            "n": order.get("adverse_selection_n", {}),
                        }
                    ),
                },
            )
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
        store.upsert(
            "model_runs",
            {
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
            },
        )
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
        row: dict[str, Any] = {
            "name": doc["name"],
            "alpha_id": doc["alpha_id"],
            "kind": kind,
            "x_version": int(doc["x-version"]),
            "feature_version": doc["feature_version"],
            "source": str(doc.get("source", "")),
            "n": None,
            "n_buckets": None,
            "mean": None,
            "std": None,
            "min_value": None,
            "max_value": None,
            "psi_eps": None,
            "edges_json": None,
            "expected_frac_json": None,
            "horizon": None,
            "baseline_kind": None,
            "bucket_ns": None,
            "ic_mean": None,
            "ic_std": None,
            "n_buckets_baseline": None,
        }
        if kind == "ic":
            row.update(
                horizon=doc.get("horizon"),
                baseline_kind=doc.get("baseline_kind"),
                bucket_ns=int(doc["bucket_ns"]),
                ic_mean=_num_or_none(doc.get("ic_mean")),
                ic_std=_num_or_none(doc.get("ic_std")),
                n_buckets_baseline=int(doc["n_buckets_baseline"]),
            )
        else:
            row.update(
                n=int(doc["n"]),
                n_buckets=int(doc["n_buckets"]),
                mean=_num_or_none(doc.get("mean")),
                std=_num_or_none(doc.get("std")),
                min_value=_num_or_none(doc.get("min")),
                max_value=_num_or_none(doc.get("max")),
                psi_eps=_num_or_none(doc.get("psi_eps")),
                edges_json=canonical_json(doc["edges"]),
                expected_frac_json=canonical_json(doc["expected_frac"]),
            )
        store.upsert("drift_baselines", row)
        col.wrote("drift_baselines")
    return col.report()


# --------------------------------------------------------------------------
# Everything
# --------------------------------------------------------------------------


def resolve_current_scope(
    repo_root: PathLike | None = None,
    *,
    dataset_version: str | None = None,
    methods: str | None = None,
) -> tuple[str, str] | None:
    """The CURRENT research scope ``(dataset_version, methods)``: the
    ``data_version`` of ``configs/strategies/alpha_params.json`` (the dataset
    the deployed parameters were fitted on) and the default method bundle.
    Either part can be named explicitly (``python -m iap.store build
    --dataset-version / --methods``); ``None`` when no dataset is named and
    the parameter document is absent."""
    from iap.lifecycle.bootstrap import PARAMS_RELPATH, load_params_document
    from iap.validation.methods import DEFAULT_METHODS

    root = Path(repo_root) if repo_root is not None else default_repo_root()
    if dataset_version is None:
        if not (root / PARAMS_RELPATH).is_file():
            return None
        dataset_version = str(load_params_document(root)["data_version"])
    return (str(dataset_version), DEFAULT_METHODS if methods is None else str(methods))


def import_all(
    store: Store,
    repo_root: PathLike | None = None,
    *,
    dataset_version: str | None = None,
    methods: str | None = None,
    extra_ledgers: Sequence[PathLike] = (),
) -> dict[str, ImportReport]:
    """Run every importer over the repository artefacts that exist, in the
    pinned order; ``{step: report}`` in that order.

    ``dataset_version`` / ``methods`` name the current scope
    (:func:`resolve_current_scope` supplies what is not given);
    ``extra_ledgers`` are further multiple-testing ledgers to index beside
    ``research/experiments.json`` — the ledger an ingested dataset keeps in
    its own directory — each under the scopes its entries state."""
    root = Path(repo_root) if repo_root is not None else default_repo_root()
    research = root / "research"
    ledger = research / "experiments.json"
    scope = resolve_current_scope(root, dataset_version=dataset_version, methods=methods)

    def set_scope() -> ImportReport:
        col = _Collector()
        if scope is None:
            col.warn("absent: no current scope (no alpha_params.json and no --dataset-version)")
        else:
            store.set_current_scope(*scope)
            col.wrote("store_scope")
        return col.report()

    extra_steps = tuple(
        (
            f"extra_ledger:{Path(extra).as_posix()}",
            Path(extra),
            lambda extra=extra: import_experiments_ledger(store, extra),
        )
        for extra in extra_ledgers
    )
    steps: Sequence[tuple[str, Path, Any]] = (
        ("current_scope", root, set_scope),
        ("reference", root / "configs", lambda: import_reference(store, root / "configs")),
        ("experiments_ledger", ledger, lambda: import_experiments_ledger(store, ledger)),
        *extra_steps,
        (
            "alpha_reports",
            research / "alpha_reports",
            lambda: import_alpha_reports(
                store, research / "alpha_reports", ledger_path=ledger, repo_root=root
            ),
        ),
        (
            "experiment_documents",
            research / "experiments",
            lambda: import_experiment_documents(store, research / "experiments"),
        ),
        (
            "lifecycle_log",
            research / "lifecycle_log.jsonl",
            lambda: import_lifecycle_log(store, research / "lifecycle_log.jsonl", scope=scope),
        ),
        (
            "lifecycle_transitions",
            research / "lifecycle_transitions.jsonl",
            lambda: import_lifecycle_transitions(
                store, research / "lifecycle_transitions.jsonl", scope=scope
            ),
        ),
        (
            "lifecycle_archive",
            research / "archive",
            lambda: import_lifecycle_archive(store, research / "archive"),
        ),
        (
            "alpha_registry",
            research / "alpha_registry.json",
            lambda: import_alpha_registry(store, research / "alpha_registry.json"),
        ),
        (
            "tca_orders",
            research / "tca" / "tca_orders.json",
            lambda: import_tca_orders(store, research / "tca" / "tca_orders.json"),
        ),
        (
            "model_runs",
            research / "models" / "ledger.json",
            lambda: import_model_runs(store, research / "models"),
        ),
        (
            "baselines",
            research / "baselines",
            lambda: import_baselines(store, research / "baselines"),
        ),
    )
    out: dict[str, ImportReport] = {}
    for name, required, run in steps:
        out[name] = run() if required.exists() else ImportReport({}, (f"absent: {required}",))
    return out
