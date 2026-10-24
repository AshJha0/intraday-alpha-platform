"""F3 acceptance study: exact pass threshold and joint false-fail bound over all mandatory evaluations.

Union bound (no independence assumed; the K3/K6/K16 outcomes of one regime x G cell share datasets):
  P(any of the E mandatory evaluations fails) <= E * P(Binomial(n, f) > k_max)  when every true FWER <= f.
UNSIGNED DRAFT; pure arithmetic, reads no data.

  python joint_bound.py [declaration.json]      prints the markdown table
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from core import k_max
from scipy import stats

GRID = (0.018, 0.019, 0.020, 0.021, 0.0215, 0.022, 0.0225, 0.023, 0.025)


def evaluations(decl: dict) -> int:
    m = decl["mandatory"]
    return len(m["regimes"]) * len(m["G"]) * len(decl["families"])


def table(decl: dict) -> dict:
    n, target, conf = int(decl["n_per_cell"]), float(decl["fwer_target"]), float(decl["ucb_conf"])
    km = k_max(n, target, conf)
    e = evaluations(decl)
    rows = []
    for f in GRID:
        ff = float(stats.binom.sf(km, n, f))  # P(count > km)
        rows.append(
            {"true_fwer": f, "per_evaluation_false_fail": ff, "joint_union_bound": min(1.0, e * ff)}
        )
    lo, hi = 0.015, 0.025  # largest f with the union bound <= budget
    budget = float(decl["joint_false_fail_budget"])
    for _ in range(80):
        mid = (lo + hi) / 2
        if e * float(stats.binom.sf(km, n, mid)) <= budget:
            lo = mid
        else:
            hi = mid
    return {"n": n, "k_max": km, "max_passing_rate": km / n, "evaluations": e, "budget": budget,
            "max_true_fwer_within_budget": lo, "rows": rows}  # fmt: skip


def render(t: dict) -> str:
    out = [
        "# F3 acceptance study: joint false-fail bound (UNSIGNED DRAFT; arithmetic only)",
        "",
        f"n = {t['n']} datasets per cell; pass iff the one-sided 95 % Clopper-Pearson UCB <= FWER_target, i.e. "
        f"count <= {t['k_max']} (rate {t['max_passing_rate']:.5f}). Mandatory evaluations E = {t['evaluations']} "
        "(3 regimes x 3 G x 3 families). Union bound, no independence assumed.",
        "",
        "| true FWER (all evaluations) | P(one evaluation fails) | union bound over E |",
        "|---|---|---|",
    ]
    for r in t["rows"]:
        out.append(
            f"| {r['true_fwer']:.4f} | {r['per_evaluation_false_fail']:.3e} | {r['joint_union_bound']:.3e} |"
        )
    out += [
        "",
        f"Largest uniform true FWER with union bound <= {t['budget']}: {t['max_true_fwer_within_budget']:.5f}.",
    ]
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("declaration.json")
    d = json.loads(path.read_text(encoding="utf-8"))
    sys.stdout.write(render(table(d)))
