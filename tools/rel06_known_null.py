"""REL-06 known-null study runner (``research/v118/NULL_PROTOCOL.md``).

The full run is a registered analysis: it refuses unless ``--prereg`` names
a prereg JSON that is signed under the trust root and whose
``analysis_identity`` matches this checkout (scripts, ``python/src/iap``
tree, commit ancestry, data version, family, metrics, thresholds), and the
runner declares an actor (``IAP_RUN_ACTOR``) that is not the signer::

    IAP_RUN_ACTOR=<runner> python tools/rel06_known_null.py \\
        --prereg research/preregs/rel06_known_null_prereg.json --workers 2

``--timing N`` (N <= 20, protocol parameter 10.9) runs N trials of every
generator and variant WITHOUT a prereg and reports only wall-clock time and
the projected full-run time: no rate, p-value or confirmation is printed or
written, so no result is examined before the signature.

Both modes call the holdout registry first (REL-02b; the dataset must be
``in_sample``), read only the configured dataset, and never touch the ledger.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python" / "src"))

import numpy as np  # noqa: E402
from iap.research import known_null as kn  # noqa: E402

CONFIG = ROOT / "research" / "v118" / "known_null_config.json"
OUT = ROOT / "research" / "v118" / "known_null_result.json"
MAX_TIMING_TRIALS = 20
MAX_WORKERS = 2

_STUDY: kn.Study | None = None


def needed_columns(config: dict[str, Any]) -> list[str]:
    cols = {
        "exchange_ts",
        "mid_price_v1",
        "half_spread_cost_bps_v1",
        "depth_bid_l1_v1",
        "depth_ask_l1_v1",
    }
    for fam in config["variants"].values():
        for h in fam:
            cols.add(h["signal"])
            cols.update(f"label_{k}_{h['horizon']}" for k in ("mid", "cost", "valid"))
    return sorted(cols)


def load_panel(data_dir: Path, config: dict[str, Any]) -> kn.Panel:
    import pyarrow.parquet as pq

    inst = json.loads(
        (data_dir / "configs" / "instruments" / "instruments.json").read_text("utf-8")
    )
    sym = {
        int(r["instrument_id"]): str(r.get("symbol", r["instrument_id"]))
        for r in inst["instruments"]
    }
    want = config["dataset"]["symbols"]
    cols = needed_columns(config)
    frames = []
    for iid in sorted(sym):
        if sym[iid] not in want:
            continue
        t = pq.read_table(data_dir / "features" / f"features_{iid}.parquet", columns=cols)
        frames.append((sym[iid], iid, {c: t.column(c).to_numpy() for c in cols}))
        del t
    found = [f[0] for f in frames]
    if sorted(found) != sorted(want):
        raise SystemExit(f"symbols {found} != configured {want}")
    panel = kn.make_panel(frames)
    if panel.sessions != list(config["dataset"]["sessions"]):
        raise SystemExit(f"sessions {panel.sessions} != configured {config['dataset']['sessions']}")
    return panel


def make_pricer(data_dir: Path, config: dict[str, Any]) -> kn.SimulatorPricer:
    from iap.execution.config import load_exec_config

    ex = config["execution"]
    cfg = load_exec_config(
        data_dir / "configs",
        adv_table=True if ex["adv_table"] == "session" else None,
    )
    cfg = replace(
        cfg, impact_model=ex["impact_model"], impact_exponent=float(ex["impact_exponent"])
    )
    if ex["regulatory_fees"]:
        # the dated Section 31 + TAF schedule is repo reference data, not per dataset
        from iap.execution.regulatory import load_default_schedule

        cfg = replace(cfg, regulatory_fees=load_default_schedule(ROOT / "configs"))
    return kn.SimulatorPricer(cfg, int(ex["venue_id"]))


def build_study(data_dir: Path, config: dict[str, Any]) -> kn.Study:
    panel = load_panel(data_dir, config)
    return kn.prepare(panel, config, pricer=make_pricer(data_dir, config))


def _init_worker(data_dir: str, config: dict[str, Any]) -> None:
    global _STUDY
    _STUDY = build_study(Path(data_dir), config)


def _work(job: tuple[str, list[int], dict[int, list[int]]]) -> tuple[str, dict]:
    gen, trials, perms = job
    assert _STUDY is not None
    return gen, kn.run_trials(_STUDY, gen, trials, perms)


def run_all(
    data_dir: Path, config: dict[str, Any], n_trials: int, workers: int, study: kn.Study | None
) -> tuple[dict[str, dict[int, dict]], list[dict[str, Any]], kn.Study]:
    """Every generator x trial; deterministic whatever ``workers`` is (A's
    permutations are drawn up front, B's seeds depend on the trial only)."""
    study = study or build_study(data_dir, config)
    g = len(study.panel.sessions)
    perms = kn.draw_permutations(g, n_trials, int(config["generators"]["A"]["base_seed"]))
    pmap = {p["trial"]: p["permutation"] for p in perms}
    jobs = []
    chunk = max(1, -(-n_trials // max(1, workers * 4)))
    for gen in sorted(config["generators"]):
        for lo in range(0, n_trials, chunk):
            idx = list(range(lo, min(n_trials, lo + chunk)))
            jobs.append((gen, idx, {i: pmap[i] for i in idx} if gen == "A" else {}))
    results: dict[str, dict[int, dict]] = {gen: {} for gen in config["generators"]}
    if workers <= 1:
        for gen, idx, pm in jobs:
            results[gen].update(kn.run_trials(study, gen, idx, pm))
    else:
        with ProcessPoolExecutor(
            workers, initializer=_init_worker, initargs=(str(data_dir), config)
        ) as ex:
            for gen, res in ex.map(_work, jobs):
                results[gen].update(res)
    return results, perms, study


def verify_prereg(prereg: Path, config: dict[str, Any]) -> dict[str, Any]:
    from iap.agents.analysis_identity import (
        require_independent_runner,
        verify_identity,
        verify_signed_document,
    )

    doc = json.loads(prereg.read_text(encoding="utf-8"))
    verify_signed_document(ROOT, doc)
    args = kn.identity_args(config)
    verify_identity(
        ROOT,
        doc.get("analysis_identity"),
        data_versions=args["data_versions"],
        family=args["family"],
        metrics=args["metrics"],
        thresholds=args["thresholds"],
    )
    actor = require_independent_runner(doc)
    return {
        "prereg": prereg.resolve().relative_to(ROOT).as_posix(),
        "runner_actor": actor,
        "commit_bound": doc["analysis_identity"]["commit"],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="REL-06 known-null study")
    ap.add_argument("--config", type=Path, default=CONFIG)
    ap.add_argument(
        "--data-root",
        type=Path,
        default=ROOT,
        help="directory the config's dataset.dir is relative to (default: the repo)",
    )
    ap.add_argument("--prereg", type=Path, help="signed prereg binding this analysis (full run)")
    ap.add_argument(
        "--timing", type=int, metavar="N", help=f"timing only, N <= {MAX_TIMING_TRIALS}"
    )
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args(argv)
    if not 1 <= a.workers <= MAX_WORKERS:
        raise SystemExit(f"--workers must be 1..{MAX_WORKERS} (machine rule)")
    config = json.loads(a.config.read_text(encoding="utf-8"))
    data_dir = (a.data_root / config["dataset"]["dir"]).resolve()
    from iap.research.holdout_registry import guard_or_exit

    rec = guard_or_exit(data_dir, purpose="REL-06 known-null study (null labels only)")
    if rec.get("state") != "in_sample":
        raise SystemExit(
            f"{data_dir} is {rec.get('state')!r}, the protocol reads in-sample data only"
        )
    if rec.get("dataset_version") not in (None, config["dataset"]["dataset_version"]):
        raise SystemExit(f"dataset_version {rec.get('dataset_version')} != configured")
    if a.timing is not None:
        if not 1 <= a.timing <= MAX_TIMING_TRIALS:
            raise SystemExit(f"--timing takes 1..{MAX_TIMING_TRIALS} trials")
        t0 = time.perf_counter()
        study = build_study(data_dir, config)
        t1 = time.perf_counter()
        run_all(data_dir, config, a.timing, a.workers, study if a.workers == 1 else None)
        t2 = time.perf_counter()
        per = (t2 - t1) / a.timing
        full = int(config["n_trials"])
        doc = {
            "trials_timed": a.timing,
            "workers": a.workers,
            "setup_seconds": round(t1 - t0, 1),
            "seconds_per_trial_all_generators_and_variants": round(per, 2),
            "projected_full_run_hours": round(((t1 - t0) + per * full) / 3600.0, 2),
            "budget_hours": config["compute_budget_hours"],
            "within_budget": ((t1 - t0) + per * full) / 3600.0
            <= float(config["compute_budget_hours"]),
            "note": "timing only (parameter 10.9); no trial outcome was printed or kept",
        }
        print(json.dumps(doc, indent=2, sort_keys=True))
        return 0
    if a.prereg is None:
        raise SystemExit("the full run needs --prereg (a signed prereg binding this analysis)")
    prov = verify_prereg(a.prereg, config)
    prov["config_sha256"] = kn.config_hash(a.config)
    prov["dataset_version"] = config["dataset"]["dataset_version"]
    results, perms, study = run_all(data_dir, config, int(config["n_trials"]), a.workers, None)
    doc = kn.study_document(study, results, perms, provenance=prov)
    from iap.research.runner import render_document

    a.out.parent.mkdir(parents=True, exist_ok=True)
    with a.out.open("w", encoding="ascii", newline="\n") as fh:
        fh.write(render_document(doc))
    print(f"wrote {a.out}: verdict {doc['verdict']}")
    print(doc["small_g_statement"])
    return 0


if __name__ == "__main__":
    np.seterr(all="ignore")
    os.environ.setdefault("PYTHONUTF8", "1")
    raise SystemExit(main())
