#!/usr/bin/env python3
"""Python MVP loop latency budget guard (v1.17, F4).

Runs ``iap.mvp.latency.measure_loop_latency`` on one symbol of the committed
real slice (``benchmarks/slices/itch50_20190130_AAPL``: Nasdaq ITCH, AAPL,
2019-01-30 from the 09:30 open, 65,132 events) and compares the per-event
p50 / p99 of ``MvpEngine.on_event`` with ``benchmarks/latency_budget.json``.

The budget file records a MEASURED figure with its provenance (CI run id,
runner CPU, slice ``dataset_version``) and the budget derived from it
(``measured x margin``, rounded up), the same rule as the F1 coverage floor
and the C++ bench guard: a guard is a threshold with recorded provenance,
never a number typed in.  Shared CI runners move with neighbour load, so
the margin is generous (the C++ p50/p99 guard uses 8x) and only p50 and p99
are compared — never p99.9 or max.  ``--advisory`` reports without failing
(used for the first measurement, before a budget exists); when the budget
file has no ``budget`` block the script is advisory whatever the flag.

    PYTHONPATH=python/src python3 tests/harness/check_mvp_latency_budget.py \
        [--events N] [--advisory] [--json out.json] [--title ...]

Exit status: 0 within budget (or advisory), 1 over budget, 2 cannot run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))

SLICE = ROOT / "benchmarks" / "slices" / "itch50_20190130_AAPL"
BUDGET = ROOT / "benchmarks" / "latency_budget.json"
DATE, SYMBOL = "2019-01-30", "AAPL"
GUARDED = ("p50", "p99")


def _load_budget() -> dict:
    if not BUDGET.is_file():
        return {}
    with open(BUDGET, encoding="utf-8") as fh:
        return json.load(fh)


def _markdown(
    res: dict, budget: dict, verdicts: list[tuple[str, int, int | None, str]], title: str
) -> str:
    out = [f"### {title}", ""]
    out.append(
        f"- slice `{SLICE.name}`, {res['events']} events; mean {res['mean_ns'] / 1e3:.1f} us, "
        f"total {res['total_s']:.1f} s; decisions {res['decisions']}, children "
        f"{res['children_submitted']}, fills {res['fills']}"
    )
    out.append(
        f"- hardware: {res['hardware']['cpu_model']} ({res['hardware']['cpu_count']} CPUs), "
        f"python {res['hardware']['python']}"
    )
    out += ["", "| quantile | measured us | budget us | verdict |", "|---|---:|---:|---|"]
    for name, got, limit, verdict in verdicts:
        lim = "-" if limit is None else f"{limit / 1e3:.0f}"
        out.append(f"| {name} | {got / 1e3:.1f} | {lim} | {verdict} |")
    for name in ("p90", "p99.9", "max"):
        out.append(f"| {name} (not guarded) | {res['quantiles_ns'][name] / 1e3:.1f} | - | - |")
    if budget.get("budget"):
        prov = budget.get("provenance", {})
        out.append("")
        out.append(
            f"- budget = measured CI p99 {budget['budget'].get('measured_p99_ns', 0) / 1e3:.0f} us "
            f"x {budget['budget'].get('margin')} (run {prov.get('run_id')}, {prov.get('cpu_model')})"
        )
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--events", type=int, default=None, help="stop after N events (default: all)")
    ap.add_argument("--advisory", action="store_true", help="report only, never fail")
    ap.add_argument("--json", default=None, help="write the measurement here")
    ap.add_argument("--title", default="Python MVP loop latency budget (coarse)")
    args = ap.parse_args(argv)

    if not SLICE.is_dir():
        print(f"missing slice {SLICE}", file=sys.stderr)
        return 2
    from iap.experiment.tracker import hardware_summary
    from iap.mvp.latency import measure_loop_latency
    from iap.mvp.real import prepare_symbol

    with tempfile.TemporaryDirectory(prefix="iap_latency_") as tmp:
        work = Path(tmp)
        prep = prepare_symbol(SLICE, DATE, SYMBOL, work / "stage")
        lat = measure_loop_latency(prep, work / "run", limit=args.events)
    res = lat.to_dict()
    res["slice"] = SLICE.name
    res["slice_dataset_version"] = prep.feed.data_version
    res["hardware"] = hardware_summary()
    res["run_id"] = os.environ.get("GITHUB_RUN_ID")

    budget = _load_budget()
    limits = (budget.get("budget") or {}).get("limits_ns") or {}
    advisory = args.advisory or not limits
    verdicts: list[tuple[str, int, int | None, str]] = []
    failed = False
    for name in GUARDED:
        got = int(res["quantiles_ns"][name])
        limit = limits.get(name)
        if limit is None:
            verdicts.append((name, got, None, "no budget (advisory)"))
            continue
        over = got > int(limit)
        failed = failed or over
        verdicts.append((name, got, int(limit), "OVER BUDGET" if over else "ok"))
    res["verdicts"] = {n: v for n, _, _, v in verdicts}
    md = _markdown(res, budget, verdicts, args.title)
    print(md)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(md + "\n")
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        with open(args.json, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(res, indent=2, sort_keys=True) + "\n")
    if failed and not advisory:
        print("MVP loop latency over budget", file=sys.stderr)
        return 1
    if failed:
        print("MVP loop latency over budget (advisory: not failing)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
