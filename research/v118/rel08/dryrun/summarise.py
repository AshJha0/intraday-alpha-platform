"""Aggregate the raw dry-run JSON into markdown tables (synthetic data only)."""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np


def agg(runs, keyf):
    g = defaultdict(list)
    for r in runs:
        for v in r["variants"]:
            if "crit" in v:
                g[keyf(r, v)].append(v)
    return g


def m(xs, k):
    a = np.array([x[k] for x in xs if x.get(k) is not None], float)
    return float(np.nanmean(a)) if a.size else float("nan")


def rate(xs, c):
    return sum(1 for x in xs if x["crit"][c]) / max(1, len(xs))


def grid_table(runs):
    g = agg(
        runs,
        lambda r, v: (
            r["spread"],
            r["book"].get("solver_iters", 20),
            r["book"]["alpha_bps"],
            r["book"]["risk_aversion"],
            v["id"],
        ),
    )
    out = [
        "| spread x | solver iters | alpha bps | risk aversion | variant | turnover (frac of $1M / rebalance) | traded $ / rebalance | MDE bps | C2 pass | net bps (null) | C3 pass (null) | n |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for k in sorted(g):
        xs = g[k]
        t = m(xs, "mean_turnover")
        out.append(
            f"| {k[0]:g} | {k[1]} | {k[2]:g} | {k[3]:g} | {k[4]} | {t:.4f} | {t * 1e6:,.0f} | {m(xs, 'mde'):.3f} | {rate(xs, 'C2'):.2f} | {m(xs, 'net_mean'):.3f} | {rate(xs, 'C3'):.2f} | {len(xs)} |"
        )
    return "\n".join(out)


def check_table(runs):
    g = agg(runs, lambda r, v: (r["spread"], r["mode"], r["amp"], v["id"]))
    out = [
        "| spread x | case | amp | variant | n | mean net bps | MDE | C1 | C2 | C3 | C4 | C5 | C6 | C7 | C8 | continue | rank IC |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for k in sorted(g):
        xs = g[k]
        cont = sum(1 for x in xs if x["outcome"] == "continue") / len(xs)
        out.append(
            f"| {k[0]:g} | {k[1]} | {k[2]:g} | {k[3]} | {len(xs)} | {m(xs, 'net_mean'):.3f} | {m(xs, 'mde'):.3f} | "
            + " | ".join(
                f"{rate(xs, c):.2f}" for c in ("C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8")
            )
            + f" | {cont:.2f} | {m(xs, 'ic_mean'):.4f} |"
        )
    return "\n".join(out)


if __name__ == "__main__":
    runs = json.loads(Path(sys.argv[2]).read_text())
    print(grid_table(runs) if sys.argv[1] in ("grid", "grid2") else check_table(runs))
