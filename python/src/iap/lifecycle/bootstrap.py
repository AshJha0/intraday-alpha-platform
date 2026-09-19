"""Bootstrap the registry from the research artefacts, plus ``status`` /
``retire`` helpers (the ``python -m iap.lifecycle`` commands).

Bootstrap reads, for each of the 24 flagship alphas (``iap.alpha.ALPHA_IDS``):

* ``research/alpha_reports/<ID>.json`` — the ``validate_alpha`` report;
* ``research/experiments.json`` — the multiple-testing ledger (the alpha's
  ``promotion_pipeline`` entry gives ``experiment_id`` and the ledger count);
* ``configs/strategies/alpha_params.json`` — the serialized model (its
  ``data_version`` / ``feature_version`` / ``git_commit`` provenance, and the
  alpha's parameter block whose content hash is the ``model_version``);

builds ``Evidence.research`` (an ``ExperimentResult``) and
``Evidence.capacity_usd``, registers every alpha at RESEARCH, advances each
one at the pinned bootstrap event time until it stops moving (at most
``STATE_COUNT`` steps; on the bundled data: RESEARCH -> CANDIDATE, then the
CANDIDATE -> VALIDATING evaluation holds), and writes
``research/alpha_registry.json`` + ``research/lifecycle_transitions.jsonl``.

**Report -> ExperimentResult mapping** (``iap.contracts`` §2 mapping,
refined for the crossed-book split of round 3):

==========================  ==================================================
ExperimentResult field      report key
==========================  ==================================================
experiment_id               ledger ``promotion_pipeline`` entry ``key[:16]``
dataset_version             ``alpha_params.json`` ``data_version``
feature_version             ``alpha_params.json`` ``feature_version``
model_version               ``content_hash(alpha_params.json params[ID])``
ic                          ``gate_ic`` (= ``oos_ic_uncrossed`` when finite,
                            else ``oos_ic`` — exactly what the PROMOTE gate
                            in ``iap.validation.validate`` reads)
rank_ic                     ``oos_rank_ic``
t_stat                      ``nw_tstat_uncrossed`` when finite, else
                            ``nw_tstat`` (the report's ``gate_t``)
nw_lags                     ``nw_lags``
hit_rate                    ``oos_hit_rate``
turnover                    ``turnover_flips_per_hour``
gross/transaction_cost/net  ``stress.cost.x1`` ``total_pnl`` / ``total_costs``
  _return_bps               (USD on the last fold) in bps of the pinned
                            ``REFERENCE_NOTIONAL_USD``; the only gate reading
                            them tests the sign, which is scale-free
max_drawdown_bps, sharpe    ``0.0`` — the per-alpha report does not carry
                            them and no gate reads them (placeholders until
                            the ExperimentRunner produces real results)
fold_consistency            ``fold_sign_consistency``
n_folds                     ``n_folds_run``
leakage_passed / _detail    ``leakage.passed`` / ``leakage``
hypothesis_sign_confirmed   ``hypothesis_confirmed``
verdict                     ``verdict``
n_experiments_in_ledger     ledger entry ``n`` (the count at the run)
git_commit                  ``alpha_params.json`` ``git_commit``
created_ts                  max ``folds[].test_end`` of the report
==========================  ==================================================

Using ``gate_ic`` rather than the pooled ``oos_ic`` is what makes the
lifecycle's failed-gate set agree with REPORT.md: FX01 is ITERATE there
because its uncrossed IC is 0.018, while its pooled IC is negative.

A report metric that is ``null`` / non-finite cannot enter an
``ExperimentResult`` (every metric is a finite double by contract); the
alpha is then bootstrapped with ``research = None`` and stays at RESEARCH
with the presence gates failed (``value = null``) — reported, never a crash.

**Bootstrap event time** (pinned, wall-clock free): the latest fold
``test_end`` across all reports, i.e. the last event timestamp the research
consumed (``1787691480577291027`` on the bundled data).  Every alpha is
registered and evaluated at that instant.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from iap.alpha import ALPHA_IDS
from iap.contracts.types import ExperimentResult, LifecycleState, Verdict
from iap.contracts.versions import content_hash
from iap.lifecycle.config import PolicyConfig, load_policy_config, repo_root
from iap.lifecycle.evidence import Evidence
from iap.lifecycle.machine import STATE_COUNT, AlphaLifecycle
from iap.lifecycle.registry import AlphaRegistry, LifecycleTransitionLog

__all__ = [
    "BootstrapRow",
    "BootstrapResult",
    "REFERENCE_NOTIONAL_USD",
    "REGISTRY_RELPATH",
    "TRANSITIONS_RELPATH",
    "bootstrap_event_ts",
    "capacity_from_report",
    "latest_event_ts",
    "load_ledger_entries",
    "load_params_document",
    "load_report",
    "render_status",
    "research_evidence",
    "run_bootstrap",
]

#: Reference notional for expressing the report's USD P&L in bps.
REFERENCE_NOTIONAL_USD = 1_000_000.0

REPORTS_RELPATH = Path("research") / "alpha_reports"
LEDGER_RELPATH = Path("research") / "experiments.json"
PARAMS_RELPATH = Path("configs") / "strategies" / "alpha_params.json"
REGISTRY_RELPATH = Path("research") / "alpha_registry.json"
TRANSITIONS_RELPATH = Path("research") / "lifecycle_transitions.jsonl"

_LEDGER_KIND = "promotion_pipeline"


def _finite(value: Any) -> Optional[float]:
    """``float(value)`` when it is a finite number, else ``None``."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    out = float(value)
    return out if math.isfinite(out) else None


def load_report(root: Path, alpha_id: str) -> Dict[str, Any]:
    path = root / REPORTS_RELPATH / f"{alpha_id}.json"
    if not path.is_file():
        raise ValueError(f"{path}: alpha report not found")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_ledger_entries(root: Path) -> Dict[str, Dict[str, Any]]:
    """``{alpha_id: promotion_pipeline ledger entry}``."""
    path = root / LEDGER_RELPATH
    if not path.is_file():
        raise ValueError(f"{path}: experiments ledger not found")
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    out: Dict[str, Dict[str, Any]] = {}
    for entry in doc["entries"]:
        if entry["kind"] == _LEDGER_KIND:
            if entry["alpha_id"] in out:
                raise ValueError(f"{path}: duplicate {_LEDGER_KIND} entry for "
                                 f"{entry['alpha_id']}")
            out[entry["alpha_id"]] = entry
    return out


def load_params_document(root: Path) -> Dict[str, Any]:
    path = root / PARAMS_RELPATH
    if not path.is_file():
        raise ValueError(f"{path}: alpha params not found")
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    for key in ("data_version", "feature_version", "git_commit", "params"):
        if key not in doc:
            raise ValueError(f"{path}: missing {key!r}")
    return doc


def latest_event_ts(report: Mapping[str, Any]) -> int:
    """The last event timestamp a report consumed: ``max(folds[].test_end)``."""
    ends = [int(f["test_end"]) for f in report["folds"]]
    if not ends:
        raise ValueError(f"{report.get('alpha_id')}: report has no folds")
    return max(ends)


def bootstrap_event_ts(reports: Mapping[str, Mapping[str, Any]]) -> int:
    """The pinned bootstrap instant: the latest ``test_end`` over all reports."""
    return max(latest_event_ts(r) for r in reports.values())


def capacity_from_report(report: Mapping[str, Any]) -> Optional[float]:
    """Aggregate deployable notional: the sum of the per-instrument capacity
    proxies (``None`` when any of them is missing or non-finite)."""
    values = [_finite(v) for _, v in sorted(report["capacity_usd_by_instrument"].items())]
    if not values or any(v is None for v in values):
        return None
    return float(sum(v for v in values if v is not None))


def research_evidence(
    alpha_id: str,
    report: Mapping[str, Any],
    ledger_entry: Optional[Mapping[str, Any]],
    params_doc: Mapping[str, Any],
) -> Tuple[Optional[ExperimentResult], List[str]]:
    """Build the alpha's ``ExperimentResult`` from its artefacts.

    Returns ``(result, missing)``: ``result`` is ``None`` when any required
    metric is absent or non-finite, and ``missing`` names those metrics.
    """
    missing: List[str] = []

    def num(key: str, value: Any) -> float:
        out = _finite(value)
        if out is None:
            missing.append(key)
            return 0.0
        return out

    gate_ic = num("gate_ic", report.get("gate_ic"))
    t_unc = _finite(report.get("nw_tstat_uncrossed"))
    t_stat = t_unc if t_unc is not None else num("nw_tstat", report.get("nw_tstat"))
    rank_ic = num("oos_rank_ic", report.get("oos_rank_ic"))
    hit_rate = num("oos_hit_rate", report.get("oos_hit_rate"))
    turnover = num("turnover_flips_per_hour", report.get("turnover_flips_per_hour"))
    fold_consistency = num("fold_sign_consistency", report.get("fold_sign_consistency"))
    cost_x1 = report.get("stress", {}).get("cost", {}).get("x1", {})
    net_usd = num("stress.cost.x1.total_pnl", cost_x1.get("total_pnl"))
    cost_usd = num("stress.cost.x1.total_costs", cost_x1.get("total_costs"))
    if missing:
        return None, missing

    scale = 1e4 / REFERENCE_NOTIONAL_USD
    net_bps = net_usd * scale
    cost_bps = cost_usd * scale
    gross_bps = net_bps + cost_bps

    if ledger_entry is None:
        experiment_id = f"{alpha_id}-unledgered"
        n_ledger = 0
    else:
        experiment_id = str(ledger_entry["key"])[:16]
        n_ledger = int(ledger_entry["n"])

    params = params_doc["params"]
    if alpha_id not in params:
        raise ValueError(f"alpha_params.json: no params block for {alpha_id}")
    result = ExperimentResult(
        experiment_id=experiment_id,
        alpha_id=alpha_id,
        dataset_version=str(params_doc["data_version"]),
        feature_version=str(params_doc["feature_version"]),
        model_version=content_hash(params[alpha_id]),
        ic=gate_ic,
        rank_ic=rank_ic,
        t_stat=t_stat,
        nw_lags=int(report["nw_lags"]),
        hit_rate=hit_rate,
        turnover=turnover,
        gross_return_bps=gross_bps,
        transaction_cost_bps=cost_bps,
        net_return_bps=net_bps,
        max_drawdown_bps=0.0,
        sharpe=0.0,
        fold_consistency=fold_consistency,
        n_folds=int(report["n_folds_run"]),
        leakage_passed=bool(report["leakage"]["passed"]),
        leakage_detail=dict(report["leakage"]),
        hypothesis_sign_confirmed=bool(report["hypothesis_confirmed"]),
        verdict=Verdict(report["verdict"]),
        n_experiments_in_ledger=n_ledger,
        git_commit=str(params_doc["git_commit"]),
        created_ts=latest_event_ts(report),
    )
    return result, missing


@dataclass(frozen=True)
class BootstrapRow:
    """Per-alpha bootstrap outcome (the summary table)."""

    alpha_id: str
    verdict: str
    state: LifecycleState
    failed_gates: Tuple[str, ...]
    missing_metrics: Tuple[str, ...]


@dataclass
class BootstrapResult:
    event_ts: int
    registry: AlphaRegistry
    machine: AlphaLifecycle
    rows: List[BootstrapRow]

    def count_by_state(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for row in self.rows:
            out[row.state.name] = out.get(row.state.name, 0) + 1
        return dict(sorted(out.items()))


def run_bootstrap(root: Optional[Path] = None, *, config: Optional[PolicyConfig] = None,
                  write: bool = True,
                  alpha_ids: Optional[List[str]] = None) -> BootstrapResult:
    """Bootstrap (see module docstring).  ``write=False`` computes without
    touching ``research/``; ``alpha_ids`` defaults to ``iap.alpha.ALPHA_IDS``."""
    root = Path(root) if root is not None else repo_root()
    cfg = config if config is not None else load_policy_config(
        root / "configs" / "strategies" / "lifecycle.json",
        root / "configs" / "strategies" / "strategies.json")
    ids = sorted(alpha_ids if alpha_ids is not None else ALPHA_IDS)
    reports = {aid: load_report(root, aid) for aid in ids}
    ledger = load_ledger_entries(root)
    params_doc = load_params_document(root)
    event_ts = bootstrap_event_ts(reports)

    registry = AlphaRegistry(cfg.policy)
    log = (LifecycleTransitionLog(root / TRANSITIONS_RELPATH, truncate=True)
           if write else None)
    machine = AlphaLifecycle(cfg, registry, log)
    rows: List[BootstrapRow] = []
    for aid in ids:
        report = reports[aid]
        result, missing = research_evidence(aid, report, ledger.get(aid), params_doc)
        evidence = Evidence(research=result, capacity_usd=capacity_from_report(report),
                            validation=None, paper=None, live=None)
        machine.register(
            aid, event_ts,
            experiment_id=None if result is None else result.experiment_id,
            data_version=None if result is None else result.dataset_version,
            feature_version=None if result is None else result.feature_version,
            model_version=None if result is None else result.model_version)
        for _ in range(STATE_COUNT):
            if machine.advance(aid, event_ts, evidence) is None:
                break
        rec = registry.get(aid)
        failed = tuple(rec.last_evaluation.failed_gates) if rec.last_evaluation else ()
        rows.append(BootstrapRow(aid, str(report["verdict"]), rec.state, failed,
                                 tuple(missing)))
    if write:
        registry.save(root / REGISTRY_RELPATH)
    return BootstrapResult(event_ts=event_ts, registry=registry, machine=machine, rows=rows)


def render_status(registry: AlphaRegistry) -> str:
    """The ``status`` table: alpha, state, since, failed gates of the last
    evaluation (``-`` when none)."""
    header = ("alpha", "state", "since_ts", "failed gates")
    lines = [" | ".join(header), " | ".join("-" * len(h) for h in header)]
    for rec in registry.records():
        failed = rec.last_evaluation.failed_gates if rec.last_evaluation else []
        lines.append(" | ".join((
            rec.alpha_id, rec.state.name, str(rec.since_ts),
            ", ".join(failed) if failed else "-")))
    return "\n".join(lines)
