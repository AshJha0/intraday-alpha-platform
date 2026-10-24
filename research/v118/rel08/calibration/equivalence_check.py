"""Full vs short-circuited REL-08 v2 evaluation on synthetic screens (implementation note, not a plan change).

For each case both modes run on the SAME data; every statistic, criterion and decision must be identical
except the two C6/C7 inputs of variants whose outcome was settled by C1-C4/C3.  Seeds are outside the plan's
range (20269300+).  Writes a small JSON summary; no calibration cell is touched.

    python equivalence_check.py --out equivalence.json [--workers 2]
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import run_calibration as rc
from iap.research import rel08_screen_v2 as v2

SKIPPED_FIELDS = ("influential_removed_net_bps", "alt_normalisation_net_bps")
DAYS = 3

CASES = (
    [("null", 0.0, sp, 20269300 + i) for i, sp in enumerate((1.0, 4.0, 1.0, 4.0))]
    + [("revert", a, 2.5, 20269310 + i) for i, a in enumerate((30.0, 60.0, 30.0, 60.0))]
    + [("lead", a, 2.5, 20269320 + i) for i, a in enumerate((0.3, 0.6))]
)


def _same(a, b) -> bool:
    if isinstance(a, float) and isinstance(b, float) and a != a and b != b:
        return True
    return a == b


def run_case(case):
    kind, amp, spread, seed = case
    data = rc.dr.make_data(kind, amp, seed, days=DAYS, spread=spread)
    rule = rc._rule(999)
    rule.doc["sufficiency_v2"] = {
        "min_rebalances_per_session": 2,
        "sessions_min_qualifying": DAYS,
        "sessions_total": DAYS,
    }
    days = sorted(int(d) for d in data.session_close())
    full = v2.run_screen_v2(data, rule, days)
    fast = v2.run_screen_v2(data, rule, days, short_circuit=True)
    diffs, skipped, c3_pass, continues = [], 0, 0, 0
    for a, b, da, db in zip(
        full["variants"], fast["variants"], full["decisions"], fast["decisions"], strict=True
    ):
        sk = not b["robustness_books_computed"]
        skipped += sk
        for k in a:
            if k == "robustness_books_computed" or (sk and k in SKIPPED_FIELDS):
                continue
            if not _same(a[k], b[k]):
                diffs.append((a["id"], k))
        if da["outcome"] != db["outcome"] or da["reason"] != db["reason"]:
            diffs.append((a["id"], "decision"))
        for c in ("C1", "C2", "C3", "C4", "C8"):
            if da["criteria"][c] != db["criteria"][c]:
                diffs.append((a["id"], c))
        c3_pass += bool(da["criteria"]["C3"])
        continues += da["outcome"] in ("continue", "passed_not_expanded")
        if not sk and not (
            _same(a["influential_removed_net_bps"], b["influential_removed_net_bps"])
            and _same(a["alt_normalisation_net_bps"], b["alt_normalisation_net_bps"])
        ):
            diffs.append((a["id"], "c6c7_when_computed"))
    return {
        "case": list(case),
        "diffs": diffs,
        "variants_skipped": skipped,
        "variants_c3_pass": int(c3_pass),
        "variants_continue": int(continues),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--workers", type=int, default=2)
    a = ap.parse_args(argv)
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        res = list(ex.map(run_case, CASES))
    summary = {
        "cases": len(res),
        "identical": all(not r["diffs"] for r in res),
        "total_variant_screens": 7 * len(res),
        "variants_skipped": sum(r["variants_skipped"] for r in res),
        "variants_c3_pass": sum(r["variants_c3_pass"] for r in res),
        "variants_continue": sum(r["variants_continue"] for r in res),
        "results": res,
    }
    a.out.write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print({k: v for k, v in summary.items() if k != "results"})
    return 0 if summary["identical"] else 1


if __name__ == "__main__":
    sys.exit(main())
