#!/usr/bin/env python3
"""Regenerate every committed artefact that derives from the bundled dataset
or from the research method defaults.

    python3 tools/regenerate_dataset_artifacts.py            # the whole chain
    python3 tools/regenerate_dataset_artifacts.py --list     # the steps, in order
    python3 tools/regenerate_dataset_artifacts.py --only goldens,tca

The seeded dataset (``data/raw``, ``data/normalized``, ``data/features``) is
never committed, but a long chain of committed files is computed from it.
When the dataset changes — a generator fix, a new seed, a feature-engine
change — or when the research method defaults change (v1.5.0), all of them
have to be regenerated together and in dependency order, or the repository
quotes numbers from two different datasets or two different rule sets. This
script is that order, written down once (schemas/MIGRATIONS.md: v1.4.0 has
the first use, for a dataset change; v1.5.0 the second, for the method
defaults on an unchanged dataset):

==============  ============================================================
step            writes
==============  ============================================================
dataset         data/raw, data/normalized (+ qc_report.json)   [git-ignored]
features        data/features                                  [git-ignored]
alpha_reports   research/alpha_reports/{<ID>.json,REPORT.md},
                configs/strategies/alpha_params.json, research/experiments.json
experiments     research/experiments/<id>/ (the five pinned runner
                experiments), research/experiments.json
combination     research/combination/{REPORT.md,COMBINATION.json,
                signal_correlation.json,reports/*.json},
                research/experiments.json
lifecycle       research/alpha_registry.json,
                research/lifecycle_transitions.jsonl (reads the alpha
                reports and research/combination/signal_correlation.json)
ml              research/ml_reports/*, research/models/run_NNNN_*/,
                research/models/ledger.json
adaptive        research/adaptive_reports/*, research/baselines/run_*.json,
                research/lifecycle_log.jsonl, research/experiments.json
power           research/power/POWER_REPORT.{md,json} (the default grid:
                three sizes, 20 seeds, 4 sessions; needs no dataset)
goldens         tests/golden/expected_*.json (every Python-owned generator;
                the ones that do not depend on the dataset or on
                alpha_params.json must come out byte-identical — that is
                the proof they are independent)
tca             research/tca/* (golden vectors only: must be unchanged)
execution       research/execution/{EXECUTION_REPORT.md,execution_study.json}
                (aggressive vs passive execution of the same parents on the
                bundled equities + the MVP session per child policy; needs
                only the `dataset` step)
configmaps      deployment/k8s/configmap-*.yaml (alpha_params.json is a
                projected config)
==============  ============================================================

One step is opt-in — it runs only when ``--only`` names it — because it
costs more than the rest of the chain together:

==============  ============================================================
power_extended  research/power/extended/POWER_REPORT.{md,json} (four sizes,
                20 seeds, 8 sessions; about three times the default grid)
==============  ============================================================

Rules the script enforces rather than documents:

* It starts from a ledger that has not seen this dataset under the default
  research methods. The report pipelines are ledgered per dataset and method
  bundle (``iap.validation.ledger``, "Dataset scope";
  ``iap.validation.methods``); running them twice on one dataset under one
  bundle would be recorded as reruns and would append a second set of model
  runs. ``--allow-rerun`` overrides.
* A step that fails stops the chain; nothing after it runs.
* ``--only`` runs a subset in the chain's order — how a change that touches
  only some artefacts regenerates them without re-running (and re-ledgering)
  the report pipelines: the signal-combination change of v1.5.0 ran
  ``--only combination,lifecycle,goldens`` (schemas/MIGRATIONS.md).
* Wall-clock time of every step is printed and written to ``--timings-out``.

The environment decides the last digits: the committed artefacts (v1.4.0,
v1.5.0) were produced by the ``regenerate`` job of ``.github/workflows/ci.yml``
(Ubuntu, Python 3.11, ``python/requirements-ci.txt`` plus the ``ml`` extra),
which is the environment the test suites then verify them in. A run on
another platform reproduces them to the documented tolerances, not to the
byte (docs/governance/REPRODUCIBILITY.md).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY = sys.executable

#: The five ExperimentRunner experiments kept under research/experiments/
#: for each dataset, in the order they are ledgered.
RUNNER_EXPERIMENTS = (
    ("EQ03", "1s"),
    ("EQ01", "1s"),
    ("EQ06", "1s"),
    ("EQ03", "5s"),
    ("EQ06", "10s"),
)

#: Steps that run only when ``--only`` names them.
OPT_IN_STEPS = ("power_extended",)

GOLDEN_TOOLS = (
    "make_golden.py",
    "make_golden_features.py",
    "make_golden_anomalies.py",
    "make_golden_alpha.py",
    "make_golden_adaptive.py",
    "make_golden_tca.py",
    "make_golden_markout.py",
    "make_golden_replay_passive.py",
    "make_golden_contracts.py",
    "make_golden_canonical_json.py",
    "make_golden_lifecycle.py",
    "make_golden_research.py",
    "make_golden_risk_edge.py",
    "make_golden_mvp.py",
)


def _steps() -> list[tuple[str, list[tuple[Path, list[str]]]]]:
    """``[(step name, [(cwd, argv), ...])]`` in dependency order."""
    py_dir = REPO / "python"
    tools = REPO / "python" / "tools"
    return [
        ("dataset", [(py_dir, [PY, "-m", "iap.marketdata"])]),
        ("features", [(py_dir, [PY, "-m", "iap.features"])]),
        ("alpha_reports", [(REPO, [PY, "research/alpha_reports/run_all.py"])]),
        (
            "experiments",
            [
                (
                    py_dir,
                    [
                        PY,
                        "-m",
                        "iap.research",
                        "run",
                        "--no-prereg",
                        "--alpha",
                        alpha,
                        "--horizon",
                        horizon,
                    ],
                )
                for alpha, horizon in RUNNER_EXPERIMENTS
            ],
        ),
        ("combination", [(py_dir, [PY, "-m", "iap.research", "combine"])]),
        ("lifecycle", [(py_dir, [PY, "-m", "iap.lifecycle", "bootstrap", "--force"])]),
        ("ml", [(REPO, [PY, "research/ml_reports/run_ml.py"])]),
        ("adaptive", [(REPO, [PY, "research/adaptive_reports/run_adaptive.py"])]),
        ("power", [(py_dir, [PY, "-m", "iap.research", "power"])]),
        (
            "power_extended",
            [
                (
                    py_dir,
                    [
                        PY,
                        "-m",
                        "iap.research",
                        "power",
                        "--sessions",
                        "1,2,4,8",
                        "--levels",
                        "0,0.5,1,2",
                        "--power-out-dir",
                        str(REPO / "research" / "power" / "extended"),
                    ],
                )
            ],
        ),
        ("goldens", [(REPO, [PY, str(tools / name), "--force"]) for name in GOLDEN_TOOLS]),
        ("tca", [(py_dir, [PY, "-m", "iap.tca"])]),
        ("execution", [(REPO, [PY, "research/execution/run_execution_study.py"])]),
        ("configmaps", [(REPO, [PY, "deployment/k8s/generate_configmaps.py"])]),
    ]


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"  # the generators write UTF-8 on every platform
    env["PYTHONPATH"] = str(REPO / "python" / "src")
    return env


def _ledger_has_seen_current_dataset() -> tuple[bool, str, str]:
    """``(seen, data_version, methods)`` — has the promotion pipeline already
    been ledgered for the dataset currently under ``data/normalized`` under
    the default method bundle?"""
    sys.path.insert(0, str(REPO / "python" / "src"))
    from iap.experiment.tracker import data_version
    from iap.validation.ledger import ExperimentLedger
    from iap.validation.methods import DEFAULT_METHODS

    version = data_version(REPO)
    ledger = ExperimentLedger(REPO / "research" / "experiments.json")
    seen = any(
        ExperimentLedger.entry_dataset_version(e) == version
        and e.get("kind") == "promotion_pipeline"
        and (e.get("config") or {}).get("methods") == DEFAULT_METHODS
        for e in ledger.entries
    )
    return seen, version, DEFAULT_METHODS


def _normalise_line_endings() -> int:
    """CRLF -> LF in every modified or untracked text file. The generators
    write the platform's line ending; the repository stores LF, and
    ``tests/golden`` is compared byte for byte (``.gitattributes``)."""
    out = subprocess.check_output(
        ["git", "status", "--porcelain", "-uall"], cwd=REPO, text=True, encoding="utf-8"
    )
    changed = 0
    for line in out.splitlines():
        rel = line[3:].strip().strip('"')
        if " -> " in rel:
            rel = rel.split(" -> ", 1)[1]
        path = REPO / rel
        if not path.is_file() or path.suffix in (".parquet", ".iap1", ".pkl", ".png"):
            continue
        data = path.read_bytes()
        if b"\r\n" in data:
            path.write_bytes(data.replace(b"\r\n", b"\n"))
            changed += 1
    return changed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--list", action="store_true", help="print the steps and exit")
    ap.add_argument("--only", default="", help="comma-separated step names to run")
    ap.add_argument("--from", dest="start", default="", help="first step to run")
    ap.add_argument(
        "--allow-rerun",
        action="store_true",
        help="run although the ledger already holds entries for this dataset",
    )
    ap.add_argument(
        "--timings-out",
        type=Path,
        default=REPO / "data" / "regeneration_timings.json",
        help="where the per-step wall-clock times are written",
    )
    args = ap.parse_args()

    steps = _steps()
    names = [name for name, _ in steps]
    if args.list:
        for name, commands in steps:
            opt_in = " (opt-in: --only)" if name in OPT_IN_STEPS else ""
            print(f"{name}: {len(commands)} command(s){opt_in}")
        return 0
    only = [s for s in args.only.split(",") if s]
    for name in only + ([args.start] if args.start else []):
        if name not in names:
            raise SystemExit(f"unknown step {name!r}; have {names}")
    selected = [
        (name, commands)
        for name, commands in steps
        if (name in only if only or name in OPT_IN_STEPS else True)
        and (not args.start or names.index(name) >= names.index(args.start))
    ]

    env = _env()
    timings: list[dict] = []
    t_all = time.perf_counter()
    for name, commands in selected:
        if name == "alpha_reports" and not args.allow_rerun:
            seen, version, bundle = _ledger_has_seen_current_dataset()
            if seen:
                raise SystemExit(
                    f"research/experiments.json already holds promotion-pipeline entries "
                    f"for dataset {version} under the {bundle!r} methods: the report "
                    "pipelines have run on it. Start from the ledger as it was before the "
                    "regeneration, or pass --allow-rerun."
                )
        t0 = time.perf_counter()
        for cwd, argv in commands:
            shown = " ".join(a if a != PY else "python" for a in argv)
            print(f"[{name}] {shown}", flush=True)
            rc = subprocess.run(argv, cwd=cwd, env=env).returncode
            if rc != 0:
                print(f"[{name}] FAILED with exit code {rc}: {shown}", file=sys.stderr)
                return rc
        seconds = round(time.perf_counter() - t0, 1)
        timings.append({"step": name, "seconds": seconds})
        print(f"[{name}] done in {seconds:.1f}s", flush=True)

    if os.linesep != "\n":
        print(f"normalised line endings in {_normalise_line_endings()} file(s)")
    total = round(time.perf_counter() - t_all, 1)
    print("\nstep            seconds")
    for row in timings:
        print(f"{row['step']:<15} {row['seconds']:>7.1f}")
    print(f"{'total':<15} {total:>7.1f}")
    args.timings_out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.timings_out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"steps": timings, "total_seconds": total}, fh, indent=2)
        fh.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
