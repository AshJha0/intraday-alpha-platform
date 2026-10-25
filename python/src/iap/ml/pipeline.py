"""The M1 demonstration (ML protocol tiers M0 -> M1) on the synthetic fixture, through the research loop's engine.

:func:`run_m1_demo` is the pipeline the protocol describes, run once on the
synthetic daily panel so that every mechanism exists and is tested before any
real fit: protocol freeze check -> trial ledger opened with the hash -> the
incumbent (M0) fitted on the development estimation window -> block length
fixed from the incumbent's **development** session series (before any
comparison) -> delta derived from the protocol -> each M1 grid point fitted on
the purged training rows, recorded as a trial, run through the same engine on
the **validation** partition as the incumbent -> paired Delta U, admission test,
Holm within the tier -> the carried candidate chosen lexicographically and
registered in the forecast registry.  The output is labelled synthetic: it is a
demonstration of the pipeline and **no claim** about any market.

The research loop refuses any partition but development and validation, so
nothing here can touch the sealed test (protocol section 10).
"""

from __future__ import annotations

import json
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

__all__ = ["run_m1_demo"]

SOFTWARE_VERSION = "iap v2.0 stage 8"


def _series(result: dict[str, Any]) -> tuple[list[float], list[str]]:
    s = result["run"].series
    return list(s.r_bps), list(s.session_ids)


def run_m1_demo(
    spec_path: str | Path,
    data_path: str | Path,
    manifest_path: str | Path,
    *,
    out_dir: str | Path,
    identity: str | None = None,
    protocol_path: str | Path | None = None,
    expected_protocol_sha256: str = FROZEN_SHA256,
    selection_ledger: str | Path | None = None,
) -> dict[str, Any]:
    """The module docstring's pipeline; returns the admission report (also written to ``out_dir``)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    proto_sha = require_frozen(protocol_path, expected=expected_protocol_sha256)
    proto = load_protocol(protocol_path)
    spec = loop.load_family_spec(spec_path)
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    route = manifest.get("route")
    if route != "synthetic" and not proto["placeholders_filled"]:
        raise ProtocolError(
            "the protocol's delta placeholders are unfilled (revision 1): fits run on the synthetic route only"
        )
    group = proto["feature_groups"].get(spec.family_id)
    if group is None:
        raise ProtocolError(f"family {spec.family_id!r} has no feature group in the protocol")
    seed = int(proto["seed"])
    trials = TrialLedger(out / "trial_ledger.json", proto)
    trials.freeze(proto_sha, int(proto["revision"]))

    # ------------------------------------------------------------ the incumbent (M0) and the split
    frame, data_sha = loop.load_panel(data_path)
    loop.check_partition("validation", manifest, data_path, identity=identity, data_sha256=data_sha)
    cal = sorted(frame["session"].unique().tolist())
    est, _trade_dev = loop.windows(frame, manifest, "development", spec)
    _est_val, trade_val = loop.windows(frame, manifest, "validation", spec)
    h_max = max(
        int(h[:-1]) if h.endswith("d") else 1 for h in spec.doc["horizons"]
    )  # the longest label horizon of the family
    split = ChronologicalSplit(tuple(cal), est[-1], spec.horizon_sessions, h_max, trade_val[-1])
    feats = loop.build_features(frame, group["features"] + list(spec.doc["features"]))
    label = loop.forward_label(frame, spec.horizon_sessions, exit_at_open=spec.exit_at_open)
    split.check_no_overlap(feats["session"])
    train = split.train_mask(feats["session"])
    assert set(feats.loc[train, "session"]).issubset(set(est)), (
        "training rows outside the estimation window"
    )

    incumbent_dev = loop.run_family(
        spec_path,
        data_path,
        manifest_path,
        partition="development",
        identity=identity,
        ledger_path=selection_ledger,
    )
    incumbent_val = loop.run_family(
        spec_path,
        data_path,
        manifest_path,
        partition="validation",
        identity=identity,
        ledger_path=selection_ledger,
    )
    m0 = incumbent_val["model"]
    # block length from the incumbent's development series, before any comparison
    bl = block_length_from_dependence(
        _series(incumbent_dev)[0], max_block=int(proto["admission"]["block_length"]["max"])
    )
    trials.record_note("block_length_fixed", source="incumbent development session series", **bl)
    delta = derive_delta(proto)
    trials.record_note("delta_derived", **delta)
    u_inc, ids_inc = _series(incumbent_val)

    # ------------------------------------------------------------ the M1 grid
    x_train = design(feats.loc[train], group["features"])
    y_train = label.to_numpy(dtype=float)[train]
    candidates: list[dict[str, Any]] = []
    grid_index = 0
    dataset_identity = f"{manifest.get('dataset_id', 'panel')}:{data_sha[:16]}"
    registry = ForecastRegistry()
    feature_set_hash = content_hash([lineage(f) for f in group["features"]])
    for model_class in proto["tiers"]["M1"]["classes"]:
        for hyper in proto["grids"][model_class]:
            name = f"{model_class}[{grid_index}]"
            if model_class == "M1.ridge":
                model: Any = RidgeModel(tuple(group["features"]), float(hyper["alpha"]))
            else:
                sol = proto["elastic_net_solver"]
                model = ElasticNetModel(
                    tuple(group["features"]),
                    float(hyper["alpha"]),
                    l1_ratio=float(hyper["l1_ratio"]),
                    max_iter=int(sol["max_iter"]),
                    tol=float(sol["tol"]),
                )
            model.fit(x_train, y_train)
            fit = trials.record_fit(
                family_id=spec.family_id,
                model_class=model_class,
                group_id=group["id"],
                hyper=hyper,
                split=split.to_dict(),
                seed=seed,
                complexity=model.complexity,
                fit_sha256=model.fit_hash(),
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
                identity=identity,
                model=model,
                ledger_path=selection_ledger,
                extra_features=group["features"],
                registry_entry=entry,
            )
            assert cand_val["model"].registry_id == model.registry_id
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
            trials.record_evaluation(fit["seq"], adm.to_dict())
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
            grid_index += 1

    # ------------------------------------------------------------ Holm within the tier, the carried candidate
    hol = holm_within_tier(
        {c["name"]: c["admission"]["p_value"] for c in candidates},
        alpha=float(proto["admission"]["holm_alpha"]),
    )
    for c in candidates:
        c["holm_adjusted_p"] = hol["adjusted"][c["name"]]
        c["admitted"] = bool(c["admission"]["admitted"] and hol["rejected"][c["name"]])
    carried = choose_candidate(candidates)
    decision = {
        "advanced": carried is not None,
        "carried": carried["name"] if carried else "M0 (incumbent stays)",
        "rule": proto["advancement"]["rule"],
        "holm": hol,
    }
    trials.record_decision("M1", spec.family_id, decision)
    trials.save()
    registry.save(out / "forecast_registry.json")
    report = {
        "title": f"U6 M1 admission demonstration: {spec.family_id} on the synthetic fixture",
        "evidence_class": "synthetic demonstration of the pipeline; no claim about markets",
        "protocol_sha256": proto_sha,
        "protocol_revision": int(proto["revision"]),
        "placeholders_filled": bool(proto["placeholders_filled"]),
        "family_id": spec.family_id,
        "feature_group": group,
        "split": split.to_dict(),
        "incumbent": {
            "model_class": m0.model_class,
            "registry_id": m0.registry_id,
            "u_mean_bps": float(np.mean(u_inc)),
            "development_report_sha256": incumbent_dev["sha256"],
            "validation_report_sha256": incumbent_val["sha256"],
        },
        "block_length": bl,
        "delta": delta,
        "candidates": candidates,
        "decision": decision,
        "trial_ledger_head": trials.chain_head,
        "n_fits": len([e for e in trials.entries if e["kind"] == "fit"]),
        "sessions_compared": len(u_inc),
    }
    report = loop._clean(report)
    (out / "admission_report.json").write_text(canonical_json(report) + "\n", encoding="ascii")
    if selection_ledger is not None:
        led = ExperimentLedger(selection_ledger, dataset_version=data_sha, data_family="synthetic")
        led.record(
            spec.family_id,
            "analysis_event",
            {
                "what": "U6 M1 admission",
                "protocol_sha256": proto_sha,
                "family_id": spec.family_id,
                "tier": "M1",
            },
            {
                "outcome_exposure": True,
                "decision": decision["carried"],
                "trial_ledger_head": trials.chain_head,
            },
        )
        led.save()
    return report
