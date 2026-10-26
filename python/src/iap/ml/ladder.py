"""The tier ladder M0 -> M1 -> M2 -> M3 (ML protocol sections 4-9), through the research loop's engine.

:func:`run_ladder` extends the M1 demonstration (:mod:`iap.ml.pipeline`) to the
bounded nonlinear tiers: protocol freeze check -> trial ledger opened with the
hash -> block length fixed once from the M0 incumbent's **development** session
series and delta derived, both before any comparison -> per tier, every grid
point fitted on the purged training rows of the development estimation window
(one ledger row each: config hash, data partition, dataset identity, seed,
result hash), registered, run through the same engine on the **validation**
partition as the tier's incumbent -> paired Delta U, admission test, Holm within
the tier (M3 counts its early-stopping evaluations in K) -> :func:`decide_tier`
-> the carried candidate becomes the next tier's incumbent.

**Admission refuses promotion without incremental value**: a tier advances only
if a candidate's ``CI_lower(Delta U) > delta`` *and* its Holm-adjusted p <=
alpha against the incumbent carried into the tier.  If no candidate is admitted
the ladder stops (protocol section 8) and the next tier is not fitted: the
refusal is a ``ladder_stopped`` ledger entry.  ``demonstrate_all_tiers=True``
(synthetic route only) fits the later tiers anyway against the unchanged
incumbent so the mechanics are exercised; its decisions are labelled
``protocol_effect: none`` and the carried candidate does not change.

The research loop refuses any partition but development and validation, so
nothing here can touch the sealed test (protocol section 10).  Runtimes are
written to ``timings.json`` beside the report, never into it, so reports and
ledgers stay byte-identical across reruns.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from iap.alpha.registry import ForecastRegistry, RegistryEntry
from iap.contracts.versions import canonical_json, content_hash
from iap.data.contracts import lineage
from iap.ml.admission import (
    admission_test,
    block_length_from_dependence,
    choose_candidate,
    holm_within_tier,
    paired_delta,
)
from iap.ml.models import ElasticNetModel, RidgeModel, design
from iap.ml.nonlinear import GAMModel, GBTModel
from iap.ml.protocol import (
    FROZEN_SHA256,
    ProtocolError,
    derive_delta,
    load_protocol,
    require_frozen,
)
from iap.ml.splits import ChronologicalSplit
from iap.ml.trials import TrialLedger
from iap.research import loop
from iap.validation.ledger import ExperimentLedger

__all__ = ["LADDER", "build_candidate", "check_feature_group", "decide_tier", "run_ladder"]

SOFTWARE_VERSION = "iap v2.0 stage 8"
LADDER = ("M1", "M2", "M3")


def check_feature_group(features: Sequence[str], group: Mapping[str, Any]) -> None:
    """Protocol section 3: a model that reads a feature outside its family's closed group is not a candidate."""
    outside = sorted(set(features) - set(group["features"]))
    if outside:
        raise ProtocolError(
            f"features {outside} are outside the closed group {group['id']!r}: not a candidate"
        )


def build_candidate(
    model_class: str, hyper: Mapping[str, Any], group: Mapping[str, Any], proto: Mapping[str, Any]
) -> Any:
    """The unfitted model of one grid point, reading exactly the family's group."""
    feats = tuple(group["features"])
    check_feature_group(feats, group)
    seed = int(proto["seed"])
    if model_class == "M1.ridge":
        return RidgeModel(feats, float(hyper["alpha"]))
    if model_class == "M1.elastic_net":
        sol = proto["elastic_net_solver"]
        return ElasticNetModel(
            feats,
            float(hyper["alpha"]),
            l1_ratio=float(hyper["l1_ratio"]),
            max_iter=int(sol["max_iter"]),
            tol=float(sol["tol"]),
        )
    if model_class == "M2.gam":
        return GAMModel(feats, float(hyper["lambda"]))
    if model_class == "M3.gbt":
        es = proto["early_stopping"]
        return GBTModel(
            feats,
            int(hyper["max_leaves"]),
            float(hyper["learning_rate"]),
            float(hyper["l2_leaf"]),
            max_trees=int(es["max_trees"]),
            seed=seed,
            evaluate_every=int(es["evaluate_every_trees"]),
            patience=int(es["patience_trees"]),
            min_rel_improvement=float(es["min_relative_improvement"]),
        )
    raise ProtocolError(f"no model for class {model_class!r}")


def decide_tier(
    candidates: list[dict[str, Any]],
    *,
    alpha: float,
    extra_looks: int = 0,
    rule: str = "",
) -> dict[str, Any]:
    """Holm within the tier, then the lexicographic carried candidate among those admitted.

    Each candidate carries ``name``, ``model_class``, ``complexity``,
    ``grid_index`` and ``admission`` (an :class:`AdmissionResult` dict); this
    adds ``holm_adjusted_p`` and ``admitted`` to each.  ``advanced`` is False
    (promotion refused) unless some candidate clears both the margin and Holm.
    """
    hol = (
        holm_within_tier(
            {c["name"]: c["admission"]["p_value"] for c in candidates},
            alpha=alpha,
            extra_looks=extra_looks,
        )
        if candidates
        else {"K": 0, "alpha": alpha, "adjusted": {}, "rejected": {}}
    )
    for c in candidates:
        c["holm_adjusted_p"] = hol["adjusted"][c["name"]]
        c["admitted"] = bool(c["admission"]["admitted"] and hol["rejected"][c["name"]])
    carried = choose_candidate(candidates)
    reasons = {
        c["name"]: (
            "admitted"
            if c["admitted"]
            else "CI_lower <= delta: no incremental value over the incumbent"
            if not c["admission"]["admitted"]
            else "Holm-adjusted p > alpha"
        )
        for c in candidates
    }
    return {
        "advanced": carried is not None,
        "carried": carried["name"] if carried else None,
        "rule": rule,
        "holm": hol,
        "early_stopping_looks_in_K": int(extra_looks),
        "reasons": reasons,
    }


def _series(result: dict[str, Any]) -> tuple[list[float], list[str]]:
    s = result["run"].series
    return list(s.r_bps), list(s.session_ids)


def run_ladder(
    spec_path: str | Path,
    data_path: str | Path,
    manifest_path: str | Path,
    *,
    out_dir: str | Path,
    tiers: Sequence[str] = LADDER,
    identity: str | None = None,
    protocol_path: str | Path | None = None,
    expected_protocol_sha256: str = FROZEN_SHA256,
    selection_ledger: str | Path | None = None,
    demonstrate_all_tiers: bool = False,
) -> dict[str, Any]:
    """The module docstring's ladder; returns the report (also written to ``out_dir``)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if tuple(tiers) != LADDER[: len(tiers)]:
        raise ProtocolError(
            f"tiers must be climbed in order from M1: {list(LADDER)}, got {list(tiers)}"
        )
    proto_sha = require_frozen(protocol_path, expected=expected_protocol_sha256)
    proto = load_protocol(protocol_path)
    spec = loop.load_family_spec(spec_path)
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    route = manifest.get("route")
    if route != "synthetic" and not proto["placeholders_filled"]:
        raise ProtocolError(
            "the protocol's delta placeholders are unfilled (revision 1): fits run on the synthetic route only"
        )
    if demonstrate_all_tiers and route != "synthetic":
        raise ProtocolError("demonstrate_all_tiers is a synthetic-route demonstration only")
    group = proto["feature_groups"].get(spec.family_id)
    if group is None:
        raise ProtocolError(f"family {spec.family_id!r} has no feature group in the protocol")
    seed = int(proto["seed"])
    alpha = float(proto["admission"]["holm_alpha"])
    trials = TrialLedger(out / "trial_ledger.json", proto)
    trials.freeze(proto_sha, int(proto["revision"]))
    timings: dict[str, float] = {}
    t0 = time.perf_counter()

    # ------------------------------------------------------------ data, split, M0 incumbent
    frame, data_sha = loop.load_panel(data_path)
    loop.check_partition("validation", manifest, data_path, identity=identity, data_sha256=data_sha)
    cal = sorted(frame["session"].unique().tolist())
    est, _trade_dev = loop.windows(frame, manifest, "development", spec)
    _est_val, trade_val = loop.windows(frame, manifest, "validation", spec)
    h_max = max(int(h[:-1]) if h.endswith("d") else 1 for h in spec.doc["horizons"])
    split = ChronologicalSplit(tuple(cal), est[-1], spec.horizon_sessions, h_max, trade_val[-1])
    feats = loop.build_features(frame, group["features"] + list(spec.doc["features"]))
    label = loop.forward_label(frame, spec.horizon_sessions, exit_at_open=spec.exit_at_open)
    split.check_no_overlap(feats["session"])
    train = split.train_mask(feats["session"])
    if not set(feats.loc[train, "session"]).issubset(set(est)):
        raise ProtocolError("training rows outside the estimation window")
    # validation rows for M3 early stopping: inside the validation window and with the label
    # window closed by validation_end (purged on the far side too)
    pos = {s: i for i, s in enumerate(cal)}
    sess_idx = feats["session"].map(pos).to_numpy()
    valid_es = split.validation_mask(feats["session"]) & (
        sess_idx + spec.horizon_sessions <= pos[trade_val[-1]]
    )
    x_train = design(feats.loc[train], group["features"])
    y_train = label.to_numpy(dtype=float)[train]
    x_val = design(feats.loc[valid_es], group["features"])
    y_val = label.to_numpy(dtype=float)[valid_es]

    common = {"identity": identity, "ledger_path": selection_ledger}
    incumbent_dev = loop.run_family(
        spec_path, data_path, manifest_path, partition="development", **common
    )
    incumbent_val = loop.run_family(
        spec_path, data_path, manifest_path, partition="validation", **common
    )
    m0 = incumbent_val["model"]
    bl = block_length_from_dependence(
        _series(incumbent_dev)[0], max_block=int(proto["admission"]["block_length"]["max"])
    )
    trials.record_note("block_length_fixed", source="M0 incumbent development session series", **bl)
    delta = derive_delta(proto)
    trials.record_note("delta_derived", **delta)
    timings["setup_and_m0_s"] = time.perf_counter() - t0

    dataset_identity = f"{manifest.get('dataset_id', 'panel')}:{data_sha[:16]}"
    registry = ForecastRegistry()
    feature_set_hash = content_hash([lineage(f) for f in group["features"]])
    n_ok = int((np.all(np.isfinite(x_train), axis=1) & np.isfinite(y_train)).sum())

    incumbent: dict[str, Any] = {
        "name": "M0.pooled_linear",
        "model_class": m0.model_class,
        "registry_id": m0.registry_id,
        "u_val": _series(incumbent_val),
    }
    tier_reports: list[dict[str, Any]] = []
    stopped_at: str | None = None
    for tier in tiers:
        t_tier = time.perf_counter()
        if stopped_at is not None and not demonstrate_all_tiers:
            trials.record_note(
                "ladder_stopped",
                tier=tier,
                reason=f"no candidate admitted at {stopped_at}: {tier} is not fitted (protocol section 8)",
            )
            tier_reports.append(
                {"tier": tier, "fitted": False, "refused": f"ladder stopped at {stopped_at}"}
            )
            continue
        u_inc, ids_inc = incumbent["u_val"]
        candidates: list[dict[str, Any]] = []
        models: dict[str, Any] = {}
        early_looks = 0
        grid_index = 0
        for model_class in proto["tiers"][tier]["classes"]:
            for hyper in proto["grids"][model_class]:
                name = f"{model_class}[{grid_index}]"
                model = build_candidate(model_class, hyper, group, proto)
                try:
                    if tier == "M3":
                        model.fit(x_train, y_train, x_val, y_val)
                        early_looks += model.evaluations
                    else:
                        model.fit(x_train, y_train)
                except ValueError as exc:
                    # too little purged history for the group: recorded, never silently skipped
                    trials.record_note(
                        f"{tier.lower()}_not_fitted",
                        model_class=model_class,
                        rows=n_ok,
                        reason=str(exc),
                    )
                    grid_index += 1
                    continue
                config = {
                    "model_class": model_class,
                    "hyper": hyper,
                    "group_id": group["id"],
                    "split": split.to_dict(),
                    "seed": seed,
                    "protocol_sha256": proto_sha,
                }
                fit = trials.record_fit(
                    family_id=spec.family_id,
                    model_class=model_class,
                    group_id=group["id"],
                    hyper=hyper,
                    split=split.to_dict(),
                    seed=seed,
                    complexity=model.complexity,
                    fit_sha256=model.fit_hash(),
                    extra={
                        "config_sha256": content_hash(config),
                        "data_partition": "development",
                        "dataset_identity": dataset_identity,
                        "train_rows": n_ok,
                        "early_stopping_partition": "validation" if tier == "M3" else None,
                        "validation_reused_for_early_stopping": tier == "M3",
                        "early_stopping_evaluations": getattr(model, "evaluations", 0),
                    },
                )
                entry = RegistryEntry(
                    spec.family_id,
                    spec.primary_horizon,
                    model_class,
                    model.parameters(),
                    feature_set_hash,
                    (est[0], est[-1]),
                    dataset_identity,
                    SOFTWARE_VERSION,
                    selection_history={
                        "kind": "candidate configuration",
                        "trial_seq": fit["seq"],
                        "protocol_sha256": proto_sha,
                        "trial_ledger_head_before": trials.chain_head,
                    },
                )
                model.registry_id = registry.register(entry)
                cand_val = loop.run_family(
                    spec_path,
                    data_path,
                    manifest_path,
                    partition="validation",
                    model=model,
                    extra_features=group["features"],
                    registry_entry=entry,
                    **common,
                )
                u_cand, ids_cand = _series(cand_val)
                du = paired_delta(u_cand, u_inc, ids_cand, ids_inc)
                adm = admission_test(
                    du,
                    delta_bps=float(delta["delta_bps"]),
                    block_length=int(bl["block_length"]),
                    resamples=int(proto["admission"]["bootstrap"]["resamples"]),
                    seed=seed,
                    level=float(proto["admission"]["ci_level_one_sided"]),
                )
                trials.record_evaluation(
                    fit["seq"],
                    {
                        **adm.to_dict(),
                        "incumbent": incumbent["name"],
                        "report_sha256": cand_val["sha256"],
                    },
                )
                candidates.append(
                    {
                        "name": name,
                        "model_class": model_class,
                        "hyper": hyper,
                        "grid_index": grid_index,
                        "complexity": model.complexity,
                        "registry_id": model.registry_id,
                        "report_sha256": cand_val["sha256"],
                        "u_mean_bps": float(np.mean(u_cand)),
                        "admission": adm.to_dict(),
                    }
                )
                models[name] = (model, (u_cand, ids_cand))
                grid_index += 1
        decision = decide_tier(
            candidates,
            alpha=alpha,
            extra_looks=early_looks if tier == "M3" else 0,
            rule=proto["advancement"]["rule"],
        )
        decision["incumbent"] = incumbent["name"]
        effective = stopped_at is None
        decision["protocol_effect"] = (
            "binding"
            if effective
            else f"none (ladder stopped at {stopped_at}; synthetic demonstration)"
        )
        if candidates:
            trials.record_decision(tier, spec.family_id, decision)
        else:
            trials.record_note("tier_empty", tier=tier, family_id=spec.family_id)
        if decision["advanced"] and effective:
            model, series = models[decision["carried"]]
            incumbent = {
                "name": decision["carried"],
                "model_class": model.model_class,
                "registry_id": model.registry_id,
                "u_val": series,
            }
        elif effective:
            stopped_at = tier
        tier_reports.append(
            {
                "tier": tier,
                "fitted": bool(candidates),
                "candidates": candidates,
                "decision": decision,
            }
        )
        timings[f"{tier}_s"] = time.perf_counter() - t_tier

    report_doc = {
        "title": f"U6 ladder M0 -> {tiers[-1]}: {spec.family_id}",
        "evidence_class": (
            "synthetic demonstration of the pipeline; no claim about markets"
            if route == "synthetic"
            else f"development/validation admission on route {route}"
        ),
        "protocol_sha256": proto_sha,
        "protocol_revision": int(proto["revision"]),
        "placeholders_filled": bool(proto["placeholders_filled"]),
        "family_id": spec.family_id,
        "feature_group": group,
        "split": split.to_dict(),
        "early_stopping_rows": int(valid_es.sum()),
        "m0": {
            "registry_id": m0.registry_id,
            "development_report_sha256": incumbent_dev["sha256"],
            "validation_report_sha256": incumbent_val["sha256"],
        },
        "block_length": bl,
        "delta": delta,
        "tiers": tier_reports,
        "carried": incumbent["name"],
        "stopped_at": stopped_at,
        "demonstrate_all_tiers": demonstrate_all_tiers,
        "trial_ledger_head": trials.chain_head,
        "n_fits": len([e for e in trials.entries if e["kind"] == "fit"]),
        "sessions_compared": len(incumbent["u_val"][0]),
    }
    trials.check_complete()
    trials.save()
    registry.save(out / "forecast_registry.json")
    report = loop._clean(report_doc)
    (out / "ladder_report.json").write_text(canonical_json(report) + "\n", encoding="ascii")
    timings["total_s"] = time.perf_counter() - t0
    (out / "timings.json").write_text(
        json.dumps({k: round(v, 2) for k, v in timings.items()}, indent=2) + "\n", encoding="utf-8"
    )
    if selection_ledger is not None:
        led = ExperimentLedger(
            selection_ledger,
            dataset_version=data_sha,
            data_family="synthetic" if route == "synthetic" else "real",
        )
        led.record(
            spec.family_id,
            "analysis_event",
            {
                "what": f"U6 ladder M0 -> {tiers[-1]}",
                "protocol_sha256": proto_sha,
                "family_id": spec.family_id,
                "tiers": list(tiers),
            },
            {
                "outcome_exposure": True,
                "decision": incumbent["name"],
                "trial_ledger_head": trials.chain_head,
            },
        )
        led.save()
    return report
