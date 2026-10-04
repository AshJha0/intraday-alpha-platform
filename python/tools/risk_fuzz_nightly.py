#!/usr/bin/env python3
"""The large differential-fuzz run of the three risk engines (backlog epic
E31) — the driver of the ``risk-fuzz`` CI job.

1. Generate ``--count`` fresh scripts from ``--seed`` (profiles in rotation,
   per-script seeds from one SplitMix64 stream; expectations recorded by the
   Python engine, the oracle).
2. Run every engine over them. An engine is a command that honours the
   replay protocol of ``python/tools/risk_fuzz.py``: it reads the scripts from
   ``IAP_RISK_FUZZ_DIR`` and writes ``<name>.tsv`` (one ``OK`` / ``DIVERGED``
   line per script) and the audit it produced into ``IAP_RISK_FUZZ_OUT``. The
   Rust engine is its own golden test (``cargo test -p risk --test
   golden_risk_fuzz``), the Java engine is ``RiskFuzzGoldenTest`` — no extra
   binaries.
3. No divergence: exit 0. Otherwise take the first diverging script, minimise
   it with the delta-debugging shrinker (each round writes the candidate
   scripts to a directory and starts each diverging engine once), write the
   original and the minimised script, their expected outputs, what each engine
   produced and a summary to ``--artifacts``, and exit 1.

Usage (from the repo root; Rust and Java already built)::

    python python/tools/risk_fuzz_nightly.py --seed 20261004 --count 400

``--engine NAME CWD COMMAND`` replaces the default engines (repeatable;
``COMMAND`` is split like a shell line, ``CWD`` is relative to the repo root).
Exit codes: 0 no divergence, 1 a divergence (artifacts written), 2 an engine
could not be run.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import make_risk_fuzz_corpus as corpus_tool  # noqa: E402
import risk_fuzz as rf  # noqa: E402

JUNIT_CP = "out/test:out/main:/usr/share/java/junit4.jar:/usr/share/java/hamcrest-core.jar"
DEFAULT_ENGINES = (
    (
        "rust",
        "rust",
        "cargo test --locked -q -p risk --test golden_risk_fuzz fuzz_corpus_replays_byte_for_byte",
    ),
    ("java", "java", f"java -cp {JUNIT_CP} org.junit.runner.JUnitCore com.iap.RiskFuzzGoldenTest"),
)


class EngineError(RuntimeError):
    """An engine command failed to run or did not report every script."""


def run_engine(
    engine: tuple[str, str, str], corpus_dir: Path, out_dir: Path, names: list[str]
) -> tuple[dict[str, str], float]:
    """Run one engine over ``corpus_dir``; returns its report and wall time."""
    name, cwd, command = engine
    out_dir.mkdir(parents=True, exist_ok=True)
    env = dict(
        os.environ,
        IAP_RISK_FUZZ_DIR=str(corpus_dir.resolve()),
        IAP_RISK_FUZZ_OUT=str(out_dir.resolve()),
    )
    started = time.monotonic()
    done = subprocess.run(
        shlex.split(command),
        cwd=rf.ROOT / cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    elapsed = time.monotonic() - started
    report_path = out_dir / f"{name}.tsv"
    tail = (done.stdout + done.stderr)[-4000:]
    if done.returncode != 0 or not report_path.is_file():
        raise EngineError(
            f"engine {name} exited {done.returncode} without a usable report:\n{tail}"
        )
    report = rf.read_report(report_path)
    if sorted(report) != sorted(names):
        raise EngineError(f"engine {name} reported {len(report)} of {len(names)} scripts:\n{tail}")
    return report, elapsed


def write_corpus(scripts: list[dict[str, Any]], base: dict[str, Any], out_dir: Path) -> None:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    files: dict[str, bytes] = {}
    for script in scripts:
        files.update(rf.script_files(script, base))
    rf.write_files(out_dir, files)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--seed", type=int, required=True, help="master seed of the fresh scripts")
    ap.add_argument("--count", type=int, default=400, help="number of fresh scripts")
    ap.add_argument("--work", type=Path, default=rf.ROOT / "risk_fuzz_work")
    ap.add_argument("--artifacts", type=Path, default=rf.ROOT / "risk_fuzz_artifacts")
    ap.add_argument(
        "--engine",
        nargs=3,
        action="append",
        metavar=("NAME", "CWD", "COMMAND"),
        help="an engine command (default: the Rust and Java golden replays)",
    )
    args = ap.parse_args(argv)
    engines = [tuple(e) for e in args.engine] if args.engine else list(DEFAULT_ENGINES)
    base = rf.load_base_config()
    summary: list[str] = []

    def say(line: str) -> None:
        print(line, flush=True)
        summary.append(line)

    started = time.monotonic()
    plan = corpus_tool.fresh_plan(args.seed, args.count)
    files, corpus = corpus_tool.build(plan)
    corpus_dir = args.work / "corpus"
    if args.work.exists():
        shutil.rmtree(args.work)
    rf.write_files(corpus_dir, files)
    names = [script["name"] for script, _, _ in corpus]
    cov = rf.coverage(corpus)
    say(
        f"risk differential fuzz: seed {args.seed}, {cov['scripts']} scripts, {cov['steps']} steps,"
    )
    say(
        f"{cov['audit_lines']} audit lines, {len(cov['branches_reached'])}/{len(rf.BRANCHES)} reason "
        f"branches reached (oracle: python, {time.monotonic() - started:.1f}s to generate)"
    )

    reports: dict[str, dict[str, str]] = {}
    try:
        for engine in engines:
            reports[engine[0]], elapsed = run_engine(engine, corpus_dir, args.work / "out", names)
            bad = sum(1 for why in reports[engine[0]].values() if why)
            say(f"engine {engine[0]}: {len(names) - bad} OK, {bad} diverged ({elapsed:.1f}s)")
    except EngineError as e:
        say(str(e))
        return 2

    diverging = [n for n in names if any(reports[e[0]][n] for e in engines)]
    exit_code = 0
    if not diverging:
        say("no divergence: every engine reproduced every decision, audit line and snapshot")
    else:
        exit_code = 1
        args.artifacts.mkdir(parents=True, exist_ok=True)
        for n in diverging:
            for e in engines:
                if reports[e[0]][n]:
                    say(f"DIVERGED {n} [{e[0]}]: {reports[e[0]][n]}")
        first = diverging[0]
        culprits = [e for e in engines if reports[e[0]][first]]
        script = next(s for s, _, _ in corpus if s["name"] == first)
        rounds = 0

        def diverges(cands: list[dict[str, Any]]) -> list[bool]:
            nonlocal rounds
            rounds += 1
            round_dir = args.work / "shrink" / f"r{rounds:03d}"
            write_corpus(cands, base, round_dir / "corpus")
            cand_names = [c["name"] for c in cands]
            verdicts = [False] * len(cands)
            for engine in culprits:
                report, _ = run_engine(engine, round_dir / "corpus", round_dir / "out", cand_names)
                verdicts = [v or bool(report[n]) for v, n in zip(verdicts, cand_names, strict=True)]
            return verdicts

        try:
            small = rf.shrink(script, base, diverges)
            final_dir = args.work / "shrink" / "final"
            write_corpus([small], base, final_dir / "corpus")
            say(
                f"minimised {first}: {len(script['steps'])} -> {len(small['steps'])} steps "
                f"in {rounds} rounds"
            )
            for engine in culprits:
                report, _ = run_engine(
                    engine, final_dir / "corpus", final_dir / "out", [small["name"]]
                )
                say(f"  [{engine[0]}] {report[small['name']] or 'OK (the reduction lost it)'}")
            for step in small["steps"]:
                say("  " + json.dumps(step, separators=(",", ":"), ensure_ascii=True))
            for path in sorted((final_dir / "corpus").iterdir()) + sorted(
                (final_dir / "out").iterdir()
            ):
                # the reduced case's own report must not shadow the full run's
                name = path.name if path.suffix != ".tsv" else f"{path.stem}_min.tsv"
                shutil.copy(path, args.artifacts / name)
        except EngineError as e:
            say(f"shrinking stopped: {e}")
        for path in sorted(corpus_dir.glob(f"{first}.*")) + sorted(
            (args.work / "out").glob(f"{first}.*")
        ):
            shutil.copy(path, args.artifacts / path.name)
        for e in engines:
            shutil.copy(args.work / "out" / f"{e[0]}.tsv", args.artifacts / f"{e[0]}.tsv")
        say(f"artifacts: {args.artifacts}")

    text = "\n".join(summary) + "\n"
    if exit_code:
        (args.artifacts / "SUMMARY.txt").write_text(text, encoding="utf-8", newline="\n")
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8", newline="\n") as f:
            f.write("```\n" + text + "```\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
