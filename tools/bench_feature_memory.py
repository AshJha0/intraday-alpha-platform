#!/usr/bin/env python3
"""Peak RSS + wall time of the serial feature build (v1.13, plan item E5).

Generates (once, cached) a multi-day synthetic dataset with the bundled
generator + normalizer, then runs ``python -m iap.features --workers 1`` in a
fresh child process per build and reports the child's PEAK resident set
(Windows: ``PeakWorkingSetSize``; POSIX: ``ru_maxrss``) and wall time.  No
third-party dependency (no psutil).

    PYTHONPATH=python/src python3 tools/bench_feature_memory.py \
        [--work DIR] [--slots 6000] [--sessions 3] [--cadence-ms 100] \
        [--src python/src] [--repeat 1] [-- extra build args...]

``--src`` selects the ``iap`` package the child imports, so a before/after
comparison is two runs: one with ``--src`` pointing at an export of the old
tree (``git archive origin/main python/src | tar -x -C old``), one with the
default.  Prints a markdown table row per build.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_CHILD = r"""
import json, sys, time
t0 = time.perf_counter()
from iap.features.__main__ import main
rc = main(sys.argv[1:])
wall = time.perf_counter() - t0
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class PMC(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t)]

    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    k32 = ctypes.WinDLL("kernel32")
    psapi = ctypes.WinDLL("psapi")
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
    peak = int(pmc.PeakWorkingSetSize)
else:
    import resource
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak = int(r if sys.platform == "darwin" else r * 1024)
print("BENCH " + json.dumps({"rc": rc, "peak_rss_bytes": peak, "wall_s": wall}))
"""


def make_dataset(work: Path, slots: int, sessions: int, seed: int) -> Path:
    """Generate + normalize the synthetic dataset once (cached by params)."""
    sys.path.insert(0, str(ROOT / "python" / "src"))
    from iap.marketdata.generator import MarketDataGenerator
    from iap.marketdata.normalize import normalize_run
    from iap.reference.refdata import ReferenceData

    tag = f"s{seed}_n{sessions}_slots{slots}"
    out = work / tag / "normalized"
    if out.is_dir() and any(out.glob("*.normalized.*")):
        return out
    raw = work / tag / "raw"
    cfg = {
        "seed": seed,
        "sessions": sessions,
        "equities": {"slots_per_stream": slots},
        "fx": {"slots_per_pair": max(1, slots * 3 // 4)},
    }
    MarketDataGenerator(ReferenceData.load(ROOT / "configs"), cfg).generate_run(raw)
    for path in sorted(raw.glob("*.jsonl")):
        if path.stat().st_size == 0:
            path.unlink()
    normalize_run(raw, out)
    return out


def run_build(src: Path, data: Path, out: Path, cadence_ms: int, extra: list[str]) -> dict:
    env = dict(os.environ, PYTHONPATH=str(src), PYTHONUTF8="1")
    args = [
        sys.executable,
        "-c",
        _CHILD,
        "--data-dir",
        str(data),
        "--out-dir",
        str(out),
        "--configs",
        str(ROOT / "configs"),
        "--registry-out",
        str(out / "feature_registry.json"),
        "--cadence-ms",
        str(cadence_ms),
        "--workers",
        "1",
        *extra,
    ]
    t0 = time.perf_counter()
    proc = subprocess.run(args, env=env, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise SystemExit(f"build failed ({proc.returncode}):\n{proc.stderr[-4000:]}")
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("BENCH "))
    res = json.loads(line[6:])
    res["process_wall_s"] = time.perf_counter() - t0
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--work", default=str(ROOT / "data" / "bench_feature_memory"))
    ap.add_argument("--slots", type=int, default=6000, help="flow slots per stream and session")
    ap.add_argument("--sessions", type=int, default=3)
    ap.add_argument("--seed", type=int, default=4242)
    ap.add_argument("--cadence-ms", type=int, default=100)
    ap.add_argument("--src", default=str(ROOT / "python" / "src"))
    ap.add_argument("--label", default="current")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--json", default=None, help="also write the results here")
    ap.add_argument("extra", nargs="*", help="extra build args (after --)")
    args = ap.parse_args(argv)

    work = Path(args.work)
    data = make_dataset(work, args.slots, args.sessions, args.seed)
    size_mb = sum(p.stat().st_size for p in data.glob("*.normalized.*")) / 2**20
    results = []
    for r in range(args.repeat):
        out = work / f"out_{args.label}_{r}"
        results.append(run_build(Path(args.src), data, out, args.cadence_ms, args.extra))
    best = min(results, key=lambda x: x["wall_s"])
    peak = max(x["peak_rss_bytes"] for x in results)
    print("| build | input MB | peak RSS MB | wall s |")
    print("|---|---:|---:|---:|")
    print(f"| {args.label} | {size_mb:.1f} | {peak / 2**20:.0f} | {best['wall_s']:.1f} |")
    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {"label": args.label, "input_mb": size_mb, "peak_rss_bytes": peak, "runs": results},
                indent=2,
            )
            + "\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
