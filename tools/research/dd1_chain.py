#!/usr/bin/env python3
"""The research chain on a DD1 daily set: panel -> D1..D4 -> E5 reports -> M0 vs M1 (v2.0 stages 7-8).

    python tools/research/dd1_chain.py --synthetic --out <dir>
    python tools/research/dd1_chain.py --dataset <dev-root>/dd1_<Y> --out <dir> --ledger research/experiments.json

``--synthetic`` first writes a synthetic DD1 set with ``dd1_daily.dry_run``
(the same writer as the real job, fixture vendors, 100 names, a planted
five-session reversal and overnight drift) under ``<out>/dd1``, so that every
step below runs on files in the exact DD1 output format.  Its numbers are
plumbing checks, never findings.  ``--dataset`` points at a real DD1 record
(``dataset.json`` and its development-readable partitions; nothing sealed is
opened, :mod:`iap.research.dd1_panel` refuses it).

Steps, each timed:

1. ``panel``: :func:`iap.research.dd1_panel.write_panel` -> ``<out>/panel/``;
2. one development run per family (``research/v2/families/D1..D4``) through
   :func:`iap.research.loop.run_family`, each an E5 report with its sha256,
   recorded in the selection ledger;
3. ``ml``: the M0 vs M1 admission (:func:`iap.ml.pipeline.run_m1_demo`) for
   ``--ml-family``.  The frozen protocol (revision 1, placeholders unfilled)
   allows fits on the synthetic route only, so on a ``daily_pit`` panel the
   step records the protocol's refusal; ``--synthetic`` also runs it on the same
   panel relabelled ``synthetic`` (its own manifest, dataset id suffixed) to
   prove the plumbing.

Prints one JSON summary (timings, report hashes, screens, trade counts).
Exit 0 when every step ran (a protocol refusal on ``daily_pit`` is a recorded
outcome, not a failure); 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
for _p in (str(ROOT / "python" / "src"), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from iap.ml.protocol import ProtocolError  # noqa: E402
from iap.research import dd1_panel  # noqa: E402
from iap.research.loop import run_family  # noqa: E402

FAMILIES = ROOT / "research" / "v2" / "families"
SPECS = {
    "D1": FAMILIES / "D1_short_term_reversal.md",
    "D2": FAMILIES / "D2_residual_momentum.md",
    "D3": FAMILIES / "D3_overnight_asymmetry.md",
    "D4": FAMILIES / "D4_filing_date_drift.md",
}
#: The synthetic set: names beyond the fixture's twelve, planted structure (plumbing only).
SYNTHETIC = {"extra_symbols": 238, "reversal": 0.6, "overnight": 0.003, "volume_scale": 50.0}
SYNTHETIC_YEAR = 2026
#: The CI test's smaller set (fewer names, five years of history): the same steps in about a minute.
CI_ARGS = ["--names", "130", "--history-years", "6", "--ml-family", "D2"]


def synthetic_dataset(work: Path, **knobs: Any) -> Path:
    """A synthetic DD1 record written by the real job in dry-run mode; returns ``dd1_<Y>``."""
    from ingest import dd1_daily

    kw = {**SYNTHETIC, **knobs}
    n = 12 + int(kw["extra_symbols"])
    saved = dd1_daily.DRY_RUN_PLAUSIBLE
    dd1_daily.DRY_RUN_PLAUSIBLE = {"sp500": (8, n + 8), "ndx100": (5, n + 8)}
    try:
        rec = dd1_daily.dry_run(SYNTHETIC_YEAR, work / "dev", work / "sealed", **kw)
    finally:
        dd1_daily.DRY_RUN_PLAUSIBLE = saved
    if rec.get("status") != "complete":
        raise RuntimeError(f"synthetic DD1 dry run did not complete: {rec.get('status')}")
    return work / "dev" / f"dd1_{SYNTHETIC_YEAR}"


def _summary(result: dict[str, Any]) -> dict[str, Any]:
    rep = result["report"]
    run = result["run"]
    return {
        "report_sha256": result["sha256"],
        "model_passed": bool(getattr(result["model"], "passed", True)),
        "sessions": rep["sessions"],
        "cohorts_opened": run.cohorts_opened,
        "fills": run.fills,
        "screen": {k: rep["economic_screen"][k] for k in ("mean_bps", "se_bps", "passed")},
        "branch": rep["decision_tree"]["branch"],
        "refusals": dict(sorted(run.refusals.items(), key=lambda kv: -kv[1])[:3]),
    }


def run_chain(
    dataset: Path,
    out: Path,
    *,
    ledger: Path,
    families: list[str],
    ml_family: str | None,
    identity: str | None = None,
    synthetic_ml: bool = False,
) -> dict[str, Any]:
    timings: dict[str, float] = {}
    t = time.perf_counter()
    panel, manifest = dd1_panel.write_panel(dataset, out / "panel")
    timings["panel"] = time.perf_counter() - t
    man = json.loads(manifest.read_text(encoding="utf-8"))
    summary: dict[str, Any] = {
        "route": man["route"],
        "universe": man["universe"],
        "partitions": man["partitions"],
        "families": {},
        "timings_s": timings,
    }
    for fam in families:
        t = time.perf_counter()
        res = run_family(
            SPECS[fam],
            panel,
            manifest,
            partition="development",
            out_dir=out / "reports",
            ledger_path=ledger,
            identity=identity,
        )
        timings[fam] = time.perf_counter() - t
        summary["families"][fam] = _summary(res)
    if ml_family:
        from iap.ml.pipeline import run_m1_demo

        spec = SPECS[ml_family]
        t = time.perf_counter()
        try:
            run_m1_demo(
                spec,
                panel,
                manifest,
                out_dir=out / "ml",
                identity=identity,
                selection_ledger=ledger,
            )
            summary["ml"] = {"route": man["route"], "outcome": "ran"}
        except ProtocolError as exc:
            summary["ml"] = {"route": man["route"], "outcome": f"refused by the protocol: {exc}"}
        timings["ml"] = time.perf_counter() - t
        if synthetic_ml:
            relabelled = dict(
                man, route="synthetic", dataset_id=man["dataset_id"] + ".relabelled_synthetic"
            )
            mpath = out / "panel" / "manifest_synthetic.json"
            mpath.write_text(
                json.dumps(relabelled, indent=1, sort_keys=True) + "\n", encoding="utf-8"
            )
            t = time.perf_counter()
            rep = run_m1_demo(
                spec,
                panel,
                mpath,
                out_dir=out / "ml_synthetic",
                identity=identity,
                selection_ledger=out / "ml_synthetic" / "selection_ledger.json",
            )
            timings["ml_synthetic"] = time.perf_counter() - t
            summary["ml_synthetic"] = {
                "incumbent_u_mean_bps": rep["incumbent"]["u_mean_bps"],
                "n_fits": rep["n_fits"],
                "carried": rep["decision"]["carried"],
                "sessions_compared": rep["sessions_compared"],
            }
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--dataset", help="a DD1 record directory (<dev-root>/dd1_<Y>)")
    src.add_argument("--synthetic", action="store_true", help="write and use a synthetic DD1 set")
    ap.add_argument(
        "--out", required=True, help="working directory for the panel, reports and ledgers"
    )
    ap.add_argument("--ledger", default=None, help="selection ledger (required with --dataset)")
    ap.add_argument("--families", default="D1,D2,D3,D4")
    ap.add_argument("--ml-family", default="D1", help="family for M0 vs M1 ('' to skip)")
    ap.add_argument("--names", type=int, default=250, help="synthetic: index members (>= 12)")
    ap.add_argument(
        "--history-years", type=int, default=7, help="synthetic: years before the purchase year"
    )
    ap.add_argument("--identity", default=None, help=argparse.SUPPRESS)  # tests only
    args = ap.parse_args(argv)
    out = Path(args.out)
    if args.dataset and not args.ledger:
        print(
            "refused: a run on a real dataset is recorded in the selection ledger (--ledger)",
            file=sys.stderr,
        )
        return 2
    t = time.perf_counter()
    if args.synthetic:
        import datetime as _dt

        dataset = synthetic_dataset(
            out / "dd1",
            extra_symbols=args.names - 12,
            first=_dt.date(SYNTHETIC_YEAR - args.history_years, 1, 2),
        )
        ledger = Path(args.ledger) if args.ledger else out / "selection_ledger.json"
    else:
        dataset = Path(args.dataset)
        ledger = Path(args.ledger)
    built = time.perf_counter() - t
    try:
        summary = run_chain(
            dataset,
            out,
            ledger=ledger,
            families=[f.strip().upper() for f in args.families.split(",") if f.strip()],
            ml_family=args.ml_family.strip().upper() or None,
            identity=args.identity,
            synthetic_ml=args.synthetic,
        )
    except dd1_panel.PanelRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    summary["timings_s"]["dataset"] = built
    if args.synthetic:
        summary["evidence_class"] = "synthetic plumbing check; no claim about markets"
    print(json.dumps(summary, indent=2, sort_keys=True, default=float))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
