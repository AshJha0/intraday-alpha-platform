"""REL-06 known-null protocol v3 (revision 4) runner (``research/v118/NULL_PROTOCOL_v3.md``).

The full run is a registered analysis: it refuses unless ``--prereg`` names a prereg that is
signed under the trust root and binds this protocol hash, that prereg, this runner
configuration and this code version (:func:`iap.research.known_null_v3.verify_binding`),
and the runner declares an actor (``IAP_RUN_ACTOR``) that is not the signer::

    IAP_RUN_ACTOR=<runner> python tools/rel06_known_null_v3.py \\
        --prereg research/v118/null_protocol_v3_prereg.json --workers 2

``--timing N`` (N <= 20) times N trials of every generator without a prereg and prints only
wall-clock figures.  ``--blocker-calibration OUT`` reads no data: it measures the B
residual-mean blocker (false flags at the verdict's aggregation level, injected bias) through
the runner's own statistic / SE / threshold / aggregation / verdict path and writes OUT.

Before any trial the A0 centring invariant is checked on the centred labels (63 groups) and
the derangement input is verified to be the checked array; a failure aborts the run
(``CentringInvariantError``).  The holdout registry is consulted first; the ledger is never
touched.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
import rel06_known_null as base  # noqa: E402
from iap.research import known_null as kn  # noqa: E402
from iap.research import known_null_diag as diag  # noqa: E402
from iap.research import known_null_v3 as v3  # noqa: E402

CONFIG = ROOT / v3.CONFIG_PATH
OUT = ROOT / "research" / "v118" / "known_null_v3_result.json"
MAX_TIMING_TRIALS = 20
MAX_WORKERS = 2

_STUDY: kn.Study | None = None
_SRC: v3.A0Source | None = None
_CFG: dict[str, Any] = {}

Job = tuple[str, str, list[int], dict[int, list[int]], list[float]]  # arm, gen, idx, perms, deltas


def build(data_dir: Path, cfg: dict[str, Any]) -> tuple[kn.Study, v3.A0Source]:
    study = base.build_study(data_dir, cfg)
    return study, v3.build_a0_source(study, cfg)  # raises CentringInvariantError


def _init_worker(data_dir: str, cfg: dict[str, Any]) -> None:
    global _STUDY, _SRC, _CFG
    _CFG = cfg
    _STUDY, _SRC = build(Path(data_dir), cfg)


def _seed(cfg: dict[str, Any], arm: str, gen: str) -> int:
    if arm == "size":
        return int(cfg["size_arm"][gen]["base_seed"])
    return int(cfg["power_arm"]["generators"][gen]["base_seed"])


def _run(study: kn.Study, src: v3.A0Source, cfg: dict[str, Any], job: Job):
    arm, gen, idx, perms, deltas = job
    res = v3.run_trials_v3(
        study,
        src,
        gen,
        idx,
        perms,
        base_seed=_seed(cfg, arm, gen),
        alpha=float(cfg["alpha"]),
        deltas=deltas,
    )
    return arm, gen, res


def _work(job: Job):
    assert _STUDY is not None and _SRC is not None
    return _run(_STUDY, _SRC, _CFG, job)


def permutations(cfg: dict[str, Any], g: int) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Per (arm, generator) the permutation of every trial: A0 size = every derangement once
    (lexicographic), A_v2 size and A0 power = random derangements without replacement."""
    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    n0 = int(cfg["size_arm"]["A0"]["n_trials"])
    all_d = kn.derangements(g)
    assert len(all_d) == n0
    out[("size", "A0")] = [{"trial": i, "permutation": list(p)} for i, p in enumerate(all_d)]
    n2 = int(cfg["size_arm"]["A_v2"]["n_trials"])
    out[("size", "A_v2")] = kn.draw_permutations(g, n2, _seed(cfg, "size", "A_v2"))
    npow = int(cfg["power_arm"]["n_trials"])
    out[("power", "A0")] = kn.draw_permutations(g, npow, _seed(cfg, "power", "A0"))
    return out


def jobs_for(cfg: dict[str, Any], g: int, workers: int, n_cap: int | None = None) -> list[Job]:
    perms = permutations(cfg, g)
    deltas = [float(d) for d in cfg["power_arm"]["delta_bps_per_trip"]]
    plan = [
        ("size", "A0", int(cfg["size_arm"]["A0"]["n_trials"]), []),
        ("size", "B", int(cfg["size_arm"]["B"]["n_trials"]), []),
        ("size", "A_v2", int(cfg["size_arm"]["A_v2"]["n_trials"]), []),
        ("power", "A0", int(cfg["power_arm"]["n_trials"]), deltas),
        ("power", "B", int(cfg["power_arm"]["n_trials"]), deltas),
    ]
    jobs: list[Job] = []
    for arm, gen, n, dl in plan:
        n = n if n_cap is None else min(n, n_cap)
        pmap = {p["trial"]: p["permutation"] for p in perms.get((arm, gen), [])}
        chunk = max(1, -(-n // max(1, workers * 4)))
        for lo in range(0, n, chunk):
            idx = list(range(lo, min(n, lo + chunk)))
            jobs.append((arm, gen, idx, {i: pmap[i] for i in idx if i in pmap}, dl))
    return jobs


def run_all(data_dir: Path, cfg: dict[str, Any], workers: int, n_cap: int | None = None):
    study, src = build(data_dir, cfg)
    jobs = jobs_for(cfg, len(study.panel.sessions), workers, n_cap)
    if workers <= 1:
        done = [_run(study, src, cfg, j) for j in jobs]
    else:
        with ProcessPoolExecutor(
            workers, initializer=_init_worker, initargs=(str(data_dir), cfg)
        ) as ex:
            done = list(ex.map(_work, jobs))
    results: dict[str, dict[str, dict[int, Any]]] = {"size": {}, "power": {}}
    for arm, gen, res in done:
        results[arm].setdefault(gen, {}).update(res)
    return study, src, results


def diagnostics(
    cfg: dict[str, Any], study: kn.Study, results: dict[str, dict[str, dict[int, Any]]]
) -> dict[str, Any]:
    """Family rates per generator and cell: R1 (alpha 0.025, primary) and R0 (Holm 0.05,
    diagnostic, same draws); per-hypothesis marginal counts of R1.  Descriptive only."""
    ok = bool(study.eligible["gate_eligible"])
    out: dict[str, Any] = {}
    for gen, trials in sorted(results["size"].items()):
        n = len(trials)
        block: dict[str, Any] = {"n_trials": n, "cells": {}, "per_hypothesis_R1": {}}
        for k in v3.KS:
            ids = v3.hypothesis_ids(cfg, k)
            for s in v3.STATISTICS:
                r1 = v3.cell_count(trials, k, s)
                r0 = v3.cell_count(trials, k, s, ids, v3.R0_ALPHA, ok)
                block["cells"][f"{s}/{k}"] = {
                    "R1_count": r1,
                    "R1": kn._rate(r1, n),
                    "R0_count": r0,
                    "R0": kn._rate(r0, n),
                }
                block["per_hypothesis_R1"][f"{s}/{k}"] = {
                    hid: sum(1 for i in trials if hid in trials[i][k][s]["confirmed"])
                    for hid in ids
                }
        out[gen] = block
    return out


def power_summary(
    cfg: dict[str, Any], study: kn.Study, results: dict[str, dict[str, dict[int, Any]]]
) -> dict[str, Any]:
    ok = bool(study.eligible["gate_eligible"])
    out: dict[str, Any] = {}
    for gen, trials in sorted(results["power"].items()):
        idx = sorted(trials)
        gb: dict[str, Any] = {}
        for k in v3.KS:
            fam = study.variants[k]
            ids = [h.id for h in fam]
            for d in cfg["power_arm"]["delta_bps_per_trip"]:
                dk = kn.delta_key(float(d))
                for s in v3.STATISTICS:
                    recs = [trials[i][k][dk][s] for i in idx]
                    r0 = [v3.at_alpha(r, ids, v3.R0_ALPHA, ok) for r in recs]
                    gb[f"{k}/{dk}/{s}"] = {
                        "R1": kn.summarise_power(recs, fam),
                        "R0": kn.summarise_power(r0, fam),
                    }
        out[gen] = gb
    return out


def calibration_report(out: Path) -> dict[str, Any]:
    """B blocker calibration at the verdict's aggregation level; no market data."""
    reps_big, reps_mid, reps_bias = 20000, 5000, 2000
    fa = [
        diag.measure_b_blocker(reps_big, model="iid", seed=101),
        diag.measure_b_blocker(reps_big, model="factor", rho=0.5, seed=102),
        diag.measure_b_blocker(reps_big, model="factor", rho=0.9, seed=103),
        diag.measure_b_blocker(reps_mid, model="t3", seed=104),
        diag.measure_b_blocker(reps_mid, model="skew", seed=105),
        diag.measure_b_blocker(reps_mid, model="factor", rho=0.9, seed=106),
    ]
    bias = [
        {
            **diag.measure_b_blocker(reps_bias, bias_sd=b, biased_hypotheses=nb, seed=200 + nb),
        }
        for nb in (1, 16)
        for b in (0.05, 0.09, 0.15, 0.20)
    ]
    doc = {
        "what": "false flags and detection of the generator B residual-mean blocker, measured "
        "through b_residual_blocker + primary_verdict (the runner's own path), 16 hypotheses x "
        "2000 independent trials per dataset; verdict_b_flag_rate = P(at least one false flag "
        "across the 16 hypotheses) for the valid-null rows",
        "analytic": diag.b_blocker_union_bound(),
        "false_flag_valid_null": fa,
        "injected_bias": bias,
        "detects": "a mean of the S2 session-mean statistic across B trials further than 4 SE "
        "from zero in at least one of the 16 hypotheses; a non-flag is not proof of "
        "unbiasedness",
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="ascii")
    return doc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="REL-06 known-null protocol v3 runner")
    ap.add_argument("--config", type=Path, default=CONFIG)
    ap.add_argument("--data-root", type=Path, default=ROOT)
    ap.add_argument("--prereg", type=Path, help="signed prereg binding this analysis")
    ap.add_argument(
        "--timing", type=int, metavar="N", help=f"timing only, N <= {MAX_TIMING_TRIALS}"
    )
    ap.add_argument("--blocker-calibration", type=Path, metavar="OUT")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args(argv)
    if a.blocker_calibration is not None:
        doc = calibration_report(a.blocker_calibration)
        print(
            json.dumps(doc["analytic"]),
            [r["verdict_b_flag_rate"] for r in doc["false_flag_valid_null"]],
        )
        return 0
    if not 1 <= a.workers <= MAX_WORKERS:
        raise SystemExit(f"--workers must be 1..{MAX_WORKERS} (machine rule)")
    cfg = v3.load_config(a.config)
    data_dir = (a.data_root / cfg["dataset"]["dir"]).resolve()
    from iap.research.holdout_registry import guard_or_exit

    rec = guard_or_exit(data_dir, purpose="REL-06 known-null v3 (null labels only)")
    if rec.get("state") != "in_sample":
        raise SystemExit(
            f"{data_dir} is {rec.get('state')!r}, the protocol reads in-sample data only"
        )
    if rec.get("dataset_version") not in (None, cfg["dataset"]["dataset_version"]):
        raise SystemExit(f"dataset_version {rec.get('dataset_version')} != configured")
    if a.timing is not None:
        if not 1 <= a.timing <= MAX_TIMING_TRIALS:
            raise SystemExit(f"--timing takes 1..{MAX_TIMING_TRIALS} trials")
        t0 = time.perf_counter()
        run_all(data_dir, cfg, a.workers, a.timing)
        per = (time.perf_counter() - t0) / a.timing
        print(
            json.dumps(
                {
                    "trials_timed": a.timing,
                    "seconds_per_trial_all": round(per, 2),
                    "note": "timing only; no trial outcome was printed or kept",
                }
            )
        )
        return 0
    if a.prereg is None:
        raise SystemExit("the full run needs --prereg (a signed prereg binding this analysis)")
    prov = v3.verify_binding(ROOT, a.prereg, a.config, cfg)
    study, src, results = run_all(data_dir, cfg, a.workers)
    series_ok = src.series_used_ok(study.panel)  # re-check after the trials
    centring_ok = bool(src.invariant["ok"] and series_ok)
    verdict = v3.evaluate_size_arm(
        cfg, results["size"]["A0"], results["size"]["B"], centring_ok=centring_ok
    )
    g = len(study.panel.sessions)
    doc = {
        "x-version": 1,
        "kind": "rel06_known_null_v3",
        "protocol": cfg["protocol"],
        "small_g_statement": kn.small_g_statement(g),
        "sessions": study.panel.sessions,
        "alpha": cfg["alpha"],
        "verdict": verdict,
        "centring_invariant": {k: src.invariant[k] for k in ("ok", "n_groups", "max_abs_mean")}
        | {"series_used_ok": series_ok, "grid_collapse": src.grid_collapse},
        "diagnostics": diagnostics(cfg, study, results),
        "power_arm_descriptive": power_summary(cfg, study, results),
        "cbar_bps": {hid: t.mean_fixed_bps for hid, t in sorted(study.trips.items())},
        "size_trials": {
            gen: {str(i): res[i] for i in sorted(res)} for gen, res in results["size"].items()
        },
        **prov,
    }
    from iap.research.runner import render_document

    a.out.parent.mkdir(parents=True, exist_ok=True)
    with a.out.open("w", encoding="ascii", newline="\n") as fh:
        fh.write(render_document(kn.round_floats(doc)))
    print(f"wrote {a.out}: {verdict['verdict']}: {verdict['label']}")
    print(diag.CLAIM_SCOPE)
    return 0


if __name__ == "__main__":
    np.seterr(all="ignore")
    os.environ.setdefault("PYTHONUTF8", "1")
    raise SystemExit(main())
