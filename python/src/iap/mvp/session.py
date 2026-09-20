"""One MVP session end to end: feed -> engine -> sinks -> store -> report.

:func:`run_session` drives :class:`~iap.mvp.engine.MvpEngine` over a
:class:`~iap.mvp.feed.FeedResult` into ``out_dir`` and writes every
artefact of the run:

``config.json``          the configuration document in force (+ versions)
``events.jsonl`` / ``.iap1`` / ``feed.json``   the captured stream (feed.py)
``traces.jsonl``         one canonical ``DecisionTrace`` per decision
``iap.sqlite``           the store (traces decomposed + MVP reference data)
``risk_audit.jsonl``     every ``RiskEvent`` of the hard risk engine
``report.json`` / ``report.md``   the report (report.py)
``paper_evidence.json``  ``PaperEvidence`` per alpha for ``iap.lifecycle``

The trace digest is computed twice (JSONL sink and store sink) and must
agree; :func:`compare_runs` is the diff used by ``replay`` and ``verify``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Union

from iap.alpha import ALPHA_CLASSES
from iap.contracts.versions import canonical_json
from iap.features.registry import registry_dicts
from iap.lifecycle.evidence import PaperEvidence
from iap.lifecycle.registry import AlphaRegistry
from iap.mvp.config import MvpConfig
from iap.mvp.engine import MvpEngine
from iap.mvp.feed import FeedResult, compose_reference_documents
from iap.mvp.report import build_report, render_markdown, report_json, research_ic_of
from iap.store.db import Store
from iap.trace.sinks import JsonlTraceSink, MultiSink, StoreTraceSink

__all__ = ["RunResult", "run_session", "compare_runs", "load_report",
           "CONFIG_FILE", "TRACES_FILE", "STORE_FILE", "REPORT_JSON", "REPORT_MD",
           "RISK_AUDIT_FILE", "PAPER_EVIDENCE_FILE"]

CONFIG_FILE = "config.json"
TRACES_FILE = "traces.jsonl"
STORE_FILE = "iap.sqlite"
REPORT_JSON = "report.json"
REPORT_MD = "report.md"
RISK_AUDIT_FILE = "risk_audit.jsonl"
PAPER_EVIDENCE_FILE = "paper_evidence.json"
_PAPER_EVIDENCE_VERSION = 2


@dataclass(frozen=True)
class RunResult:
    """What one session produced (the engine is kept for inspection)."""

    out_dir: Path
    config: MvpConfig
    feed: FeedResult
    report: Dict[str, Any]
    trace_digest: str
    engine: MvpEngine


def _import_reference(store: Store, cfg: MvpConfig, engine: MvpEngine,
                      registry: AlphaRegistry) -> None:
    """MVP reference data into the store (same column mapping as
    ``iap.store.importers.import_reference``, over the composed documents)."""
    instruments_cfg, venues_cfg = compose_reference_documents(cfg)
    for inst in sorted(instruments_cfg["instruments"], key=lambda d: d["instrument_id"]):
        store.upsert("instruments", {
            "instrument_id": inst["instrument_id"], "symbol": inst["symbol"],
            "asset_class": inst["asset_class"],
            "currency": inst.get("currency") or inst["quote_currency"],
            "base_currency": inst.get("base_currency"),
            "tick_size": float(inst["tick_size"]), "lot_size": int(inst["lot_size"]),
            "ref_price": float(inst["ref_price"]), "adv": float(inst["adv"]),
            "pip": None if inst.get("pip") is None else float(inst["pip"]),
            "venues_json": canonical_json(inst["venues"]),
            "underlying_json": (canonical_json(inst["underlying"])
                                if "underlying" in inst else None),
        })
    for venue in sorted(venues_cfg["venues"], key=lambda d: d["venue_id"]):
        store.upsert("venues", {
            "venue_id": venue["venue_id"], "venue": venue["venue"],
            "asset_class": venue["asset_class"],
            "taker_fee_per_share": venue.get("taker_fee_per_share"),
            "maker_rebate_per_share": venue.get("maker_rebate_per_share"),
            "commission_per_million": venue.get("commission_per_million"),
            "latency_mean_ns": int(venue["latency"]["mean_ns"]),
            "latency_jitter_ns": int(venue["latency"]["jitter_ns"]),
            "supports_json": canonical_json(venue["supports"]),
        })
    features = registry_dicts()
    store.upsert("feature_versions", {
        "feature_version": engine.feature_version, "registry_hash": engine.feature_version,
        "x_version": 1, "n_features": len(features), "registry_json": canonical_json(features),
    })
    for alpha in engine.alphas:
        cls = ALPHA_CLASSES[alpha.alpha_id]
        state = registry.get(alpha.alpha_id).state.name if alpha.alpha_id in registry \
            else "RESEARCH"
        store.insert_alpha(alpha.alpha_id, asset_class=cls.asset_class, family=cls.name,
                           horizon=cls.horizon, economic_rationale=cls.economic_rationale(),
                           current_state=state)


def _paper_evidence(engine: MvpEngine, report: Dict[str, Any],
                    registry: AlphaRegistry) -> Dict[str, Any]:
    """``PaperEvidence`` per alpha from this one paper session (n_sessions=1).

    ``net_pnl`` is the session's total P&L (the alphas share one book: an
    ensemble session cannot be split per alpha and is reported as such);
    ``realized_ic`` is the alpha's own realized IC at ITS FITTED HORIZON
    (the horizon the registry's ``research_ic`` was measured at — the
    ``paper_ic_tracking`` gate compares like with like; the IC at the MVP
    holding horizon sits next to it as ``realized_ic_at_mvp_horizon``) when
    defined, else 0.0 with ``ic_defined`` false alongside;
    ``tracking_error`` is 0.0 (a single session has no dispersion) — both
    facts are stated next to the numbers so a lifecycle gate never mistakes
    an undefined statistic for evidence.
    """
    alphas: Dict[str, Any] = {}
    kills = engine.n_kill_events()
    for alpha in engine.alphas:
        at_fit = engine.realized_ic(alpha.alpha_id, alpha.model.horizon)
        at_mvp = engine.realized_ic(alpha.alpha_id)
        research = research_ic_of(registry, alpha.alpha_id)
        record = registry.get(alpha.alpha_id) if alpha.alpha_id in registry else None
        evidence = PaperEvidence(
            n_sessions=1, realized_ic=at_fit.ic if at_fit.ic is not None else 0.0,
            research_ic=research if research is not None else 0.0,
            net_pnl=report["pnl"]["total"], n_kill_events=kills, tracking_error=0.0)
        alphas[alpha.alpha_id] = {
            "state_at_run": record.state.name if record is not None else None,
            "paper": evidence.to_dict(),
            "ic_defined": at_fit.ic is not None, "n_ic_samples": at_fit.n,
            "ic_horizon": at_fit.horizon,
            "realized_ic_at_mvp_horizon": at_mvp.ic, "mvp_horizon": at_mvp.horizon,
            "research_ic_defined": research is not None,
            "shared_book": True,
        }
    return {
        "x-version": _PAPER_EVIDENCE_VERSION,
        "session_id": engine.session_id,
        "run_id": engine.cfg.run_id,
        "config_version": engine.config_version,
        "data_version": engine.data_version,
        "event_ts": engine.last_ts,
        "policy": registry.policy,
        "alphas": alphas,
        "note": "PaperEvidence for iap.lifecycle (PAPER -> ACTIVE gates: paper_min_sessions, "
                "paper_ic_tracking, paper_net_pnl, no_kill_events); the registry is not "
                "mutated by the MVP.",
    }


def _write(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def run_session(cfg: MvpConfig, feed: FeedResult, out_dir: Union[str, Path]) -> RunResult:
    """Run one session (see module docstring) and write every artefact."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    registry = AlphaRegistry.load(cfg.reference_path("alpha_registry"))
    for aid in cfg.alphas:
        if aid not in registry:
            raise ValueError(f"{cfg.reference['alpha_registry']}: alpha {aid!r} not registered")
    db_path = out_dir / STORE_FILE
    if db_path.exists():
        db_path.unlink()
    store = Store.open(db_path)
    try:
        store.init()
        jsonl_sink = JsonlTraceSink(out_dir / TRACES_FILE)
        store_sink = StoreTraceSink(store)
        with MultiSink(jsonl_sink, store_sink) as sink:
            engine = MvpEngine(cfg, feed, sink)
            _import_reference(store, cfg, engine, registry)
            for ev in feed.events:
                engine.on_event(ev)
            engine.finish()
        digest = jsonl_sink.digest.hexdigest()
        if store_sink.digest.hexdigest() != digest:
            raise RuntimeError("trace digest mismatch between the JSONL and store sinks")
        store.insert_session(
            engine.session_id, data_version=engine.data_version,
            config_version=engine.config_version, seed=cfg.seed,
            start_ts=feed.first_ts, end_ts=feed.last_ts, n_events=len(feed.events))
        report = build_report(engine, feed, digest, registry)
    finally:
        store.close()
    _write(out_dir / REPORT_JSON, report_json(report))
    _write(out_dir / REPORT_MD, render_markdown(report))
    _write(out_dir / RISK_AUDIT_FILE, engine.risk_engine.audit_jsonl())
    _write(out_dir / PAPER_EVIDENCE_FILE,
           json.dumps(_paper_evidence(engine, report, registry), indent=2, sort_keys=True) + "\n")
    _write(out_dir / CONFIG_FILE, json.dumps({
        "x-version": 1,
        "run_id": cfg.run_id,
        "session_id": cfg.session_id,
        "seed": cfg.seed,
        "instrument": cfg.instrument,
        "config_version": engine.config_version,
        "data_version": engine.data_version,
        "feature_version": engine.feature_version,
        "model_version": engine.model_version,
        "trace_digest": digest,
        "document": cfg.document,
        "reference_documents": cfg.reference_documents(),
    }, indent=2, sort_keys=True) + "\n")
    return RunResult(out_dir=out_dir, config=cfg, feed=feed, report=report,
                     trace_digest=digest, engine=engine)


def load_report(run_dir: Union[str, Path]) -> Dict[str, Any]:
    """``report.json`` of a run directory."""
    with open(Path(run_dir) / REPORT_JSON, encoding="utf-8") as fh:
        return json.load(fh)


def _flatten(obj: Any, prefix: str, out: Dict[str, Any]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            _flatten(v, f"{prefix}.{k}" if prefix else str(k), out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _flatten(v, f"{prefix}[{i}]", out)
    else:
        out[prefix] = obj


def compare_runs(expected: Dict[str, Any], actual: Dict[str, Any],
                 *, tolerance: float = 0.0) -> List[str]:
    """Differences between two report documents as ``key: expected != actual``
    lines (empty = identical).  Floats compare within ``tolerance`` (absolute
    and relative), everything else exactly."""
    a: Dict[str, Any] = {}
    b: Dict[str, Any] = {}
    _flatten(expected, "", a)
    _flatten(actual, "", b)
    diffs: List[str] = []
    for key in sorted(set(a) | set(b)):
        if key not in a:
            diffs.append(f"{key}: <absent> != {b[key]!r}")
        elif key not in b:
            diffs.append(f"{key}: {a[key]!r} != <absent>")
        else:
            x, y = a[key], b[key]
            if isinstance(x, float) and isinstance(y, (int, float)) and not isinstance(y, bool):
                if abs(x - y) > tolerance * max(1.0, abs(x), abs(y)):
                    diffs.append(f"{key}: {x!r} != {y!r}")
            elif x != y or type(x) is not type(y):
                diffs.append(f"{key}: {x!r} != {y!r}")
    return diffs
