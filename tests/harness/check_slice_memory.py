#!/usr/bin/env python3
"""Peak-RSS guard of ingest and feature build on the committed real slice (v1.17, F5).

Runs, each in a fresh child process so the peak is the job's own:

* ``ingest``: the normaliser (``iap.marketdata.normalize.normalize_run``,
  the per-session path every ITCH / IEX ingest ends in) over a raw JSONL of
  the slice's events — the slice is a fixed point of the normaliser (its
  output bytes equal the slice's ``.iap1``, checked here);
* ``features``: ``python -m iap.features --workers 1`` over the slice
  (48 native features, the default cadence; the streaming E5 path).

and reports each child's PEAK resident set (Windows ``PeakWorkingSetSize``,
POSIX ``ru_maxrss``; no psutil) next to ``benchmarks/memory_budget.json``.
The budget file holds MEASURED CI figures with provenance (run id, runner
CPU, slice ``dataset_version``) and ``budget = measured x margin``: the
guard catches a path that starts holding the day in memory again (the
v1.16 failure mode, docs/research/MEMORY_REVIEW_v116.md), not drift of a
few percent.  Without a ``budget`` block, or with ``--advisory``, it reports
and never fails.

    PYTHONPATH=python/src python3 tests/harness/check_slice_memory.py \
        [--advisory] [--json out.json] [--title ...]

Exit status: 0 within budget (or advisory), 1 over budget, 2 cannot run.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "python" / "src"
SLICE = ROOT / "benchmarks" / "slices" / "itch50_20190130_AAPL"
SLICE_FILE = SLICE / "normalized" / "eq_20190130.normalized.iap1"
BUDGET = ROOT / "benchmarks" / "memory_budget.json"
JOBS = ("ingest", "features")

# Runs inside the child: the job, then its own peak RSS, printed as one line.
_CHILD = r"""
import json, sys, time
from pathlib import Path
job, slice_file, work = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
t0 = time.perf_counter()
extra = {}
if job == "ingest":
    from iap.core.codec import read_iap1, sha256_file, write_jsonl
    from iap.marketdata.normalize import normalize_run
    raw = work / "raw"; raw.mkdir(parents=True)
    write_jsonl(raw / slice_file.name.replace(".normalized.iap1", ".jsonl"), read_iap1(slice_file))
    qc = normalize_run(raw, work / "normalized")
    out = work / "normalized" / slice_file.name
    extra = {"events_out": qc["totals"]["events_out"],
             "fixed_point": sha256_file(out) == sha256_file(slice_file)}
elif job == "features":
    from iap.features.__main__ import main
    rc = main(["--data-dir", str(slice_file.parent), "--out-dir", str(work / "features"),
               "--configs", str(slice_file.parent.parent / "configs"),
               "--registry-out", str(work / "features" / "feature_registry.json"),
               "--workers", "1"])
    if rc:
        raise SystemExit(rc)
    summary = json.loads((work / "features" / "features_summary.json").read_text())
    extra = {"rows": summary.get("rows")}
else:
    raise SystemExit(f"unknown job {job}")
wall = time.perf_counter() - t0
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class PMC(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

    pmc = PMC(); pmc.cb = ctypes.sizeof(PMC)
    k32 = ctypes.WinDLL("kernel32"); psapi = ctypes.WinDLL("psapi")
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
    peak = int(pmc.PeakWorkingSetSize)
else:
    import resource
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak = int(r if sys.platform == "darwin" else r * 1024)
print("MEM " + json.dumps({"peak_rss_bytes": peak, "wall_s": wall, **extra}))
"""


def run_job(job: str, work: Path) -> dict:
    env = dict(os.environ, PYTHONPATH=str(SRC), PYTHONUTF8="1")
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, job, str(SLICE_FILE), str(work / job)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{job} failed ({proc.returncode}):\n{proc.stderr[-4000:]}")
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("MEM "))
    return json.loads(line[4:])


def _load_budget() -> dict:
    if not BUDGET.is_file():
        return {}
    with open(BUDGET, encoding="utf-8") as fh:
        return json.load(fh)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--advisory", action="store_true", help="report only, never fail")
    ap.add_argument("--json", default=None, help="write the measurements here")
    ap.add_argument("--title", default="Peak RSS on the real slice (coarse)")
    args = ap.parse_args(argv)
    if not SLICE_FILE.is_file():
        print(f"missing slice {SLICE_FILE}", file=sys.stderr)
        return 2
    sys.path.insert(0, str(SRC))
    from iap.experiment.tracker import hardware_summary

    budget = _load_budget()
    limits = (budget.get("budget") or {}).get("limits_bytes") or {}
    advisory = args.advisory or not limits
    results: dict[str, dict] = {}
    failed = False
    rows = ["| job | peak RSS MB | budget MB | wall s | verdict |", "|---|---:|---:|---:|---|"]
    with tempfile.TemporaryDirectory(prefix="iap_slice_mem_") as tmp:
        for job in JOBS:
            t = time.perf_counter()
            r = run_job(job, Path(tmp))
            r["process_wall_s"] = round(time.perf_counter() - t, 2)
            if job == "ingest" and not r.get("fixed_point"):
                raise RuntimeError("normaliser output differs from the slice bytes")
            limit = limits.get(job)
            if limit is None:
                verdict = "no budget (advisory)"
            elif r["peak_rss_bytes"] > int(limit):
                verdict, failed = "OVER BUDGET", True
            else:
                verdict = "ok"
            r["verdict"] = verdict
            results[job] = r
            lim = "-" if limit is None else f"{int(limit) / 2**20:.0f}"
            rows.append(
                f"| {job} | {r['peak_rss_bytes'] / 2**20:.1f} | {lim} | {r['wall_s']:.1f} | {verdict} |"
            )
    hw = hardware_summary()
    md = [
        f"### {args.title}",
        "",
        f"- slice `{SLICE.name}` ({SLICE_FILE.stat().st_size} bytes); "
        f"hardware {hw['cpu_model']} ({hw['cpu_count']} CPUs), python {hw['python']}",
        "",
    ] + rows
    if budget.get("budget"):
        prov = budget.get("provenance", {})
        md += [
            "",
            f"- budget = measured CI peak x {budget['budget'].get('margin')} "
            f"(run {prov.get('run_id')}, {prov.get('cpu_model')})",
        ]
    text = "\n".join(md) + "\n"
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    if args.json:
        doc = {
            "slice": SLICE.name,
            "hardware": hw,
            "run_id": os.environ.get("GITHUB_RUN_ID"),
            "jobs": results,
        }
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        with open(args.json, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    if failed and not advisory:
        print("peak RSS over budget", file=sys.stderr)
        return 1
    if failed:
        print("peak RSS over budget (advisory: not failing)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
