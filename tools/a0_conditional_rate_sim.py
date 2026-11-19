#!/usr/bin/env python3
"""How much does the A0 conditional rejection share move between valid-null
worlds?  A data-free simulation for the REL-06 known-null v3 record.

The v3 study (research/v118/known_null_v3_RESULT.md) enumerated the 1,854
derangements of 7 real sessions and found 79 and 80 rejections (4.3 %)
against a 2.5 % target, and was recorded FAIL.  The 1,854 outcomes share
seven sessions, so the share is one draw of a random quantity, not an
estimate with a binomial error.  This script measures the spread of that
quantity when the test is exactly valid.

Model (a simplification, stated): a 7 x 7 payoff matrix ``M[i, j]``, the
session-mean payoff of decision session ``i`` paired with label session ``j``.
A derangement ``s`` gives the seven values ``M[i, s(i)]``; the test is the
one-sided one-sample Student t on them at level alpha (6 degrees of freedom),
which is exact when the seven values are i.i.d. normal.  Worlds:

* ``iid``: entries i.i.d. N(0, 1).  Every derangement's test is exactly valid.
* ``row``: ``M = r_i + e_ij`` with sd(r) = ``--row-sd``: a decision-session
  effect that survives the per-session label centring of A0.

This is not the S2 statistic or the K-family rule of the protocol; it bounds
what the derangement design alone can tell, nothing more.

    python tools/a0_conditional_rate_sim.py [--worlds 4000] [--seed 20261118] \\
        [--out research/round0/a0_conditional_rate_sim.json]
"""

from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path

import numpy as np
from scipy import stats

ROOT = Path(__file__).resolve().parents[1]
G = 7
OBSERVED = (79, 80)
N_DERANGEMENTS = 1854


def derangements(g: int = G) -> np.ndarray:
    idx = np.arange(g)
    out = [p for p in itertools.permutations(range(g)) if all(p[i] != i for i in idx)]
    return np.asarray(out, dtype=np.intp)


def conditional_shares(m: np.ndarray, ders: np.ndarray, alpha: float) -> np.ndarray:
    """Share of derangements whose t test rejects, per world (``m``: worlds x G x G)."""
    g = m.shape[1]
    vals = m[:, np.arange(g)[None, :], ders]  # worlds x derangements x G
    mean = vals.mean(axis=2)
    sd = vals.std(axis=2, ddof=1)
    t = mean / (sd / np.sqrt(g))
    crit = stats.t.ppf(1.0 - alpha, g - 1)
    return (t > crit).mean(axis=1)


def simulate(worlds: int, seed: int, alpha: float, row_sd: float) -> dict:
    ders = derangements()
    assert len(ders) == N_DERANGEMENTS
    rng = np.random.default_rng(seed)
    out: dict = {}
    for name, sd_r in (("iid", 0.0), ("row", row_sd)):
        shares = []
        for start in range(0, worlds, 500):
            n = min(500, worlds - start)
            m = rng.normal(size=(n, G, G))
            if sd_r:
                m = m + rng.normal(scale=sd_r, size=(n, G, 1))
            shares.append(conditional_shares(m, ders, alpha))
        s = np.concatenate(shares)
        counts = np.rint(s * N_DERANGEMENTS)
        out[name] = {
            "row_sd": sd_r,
            "mean_share": float(s.mean()),
            "sd_share": float(s.std(ddof=1)),
            "quantiles": {
                q: float(np.quantile(s, float(q))) for q in ("0.5", "0.9", "0.95", "0.99")
            },
            "worlds_with_zero_rejections": float((counts == 0).mean()),
            "p_count_at_least_observed": {str(k): float((counts >= k).mean()) for k in OBSERVED},
        }
    return {
        "schema": "iap.round0.a0_conditional_rate_sim/1",
        "worlds": worlds,
        "seed": seed,
        "alpha": alpha,
        "sessions": G,
        "derangements": N_DERANGEMENTS,
        "observed_counts": list(OBSERVED),
        "results": out,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--worlds", type=int, default=4000)
    ap.add_argument("--seed", type=int, default=20261118)
    ap.add_argument("--alpha", type=float, default=0.025)
    ap.add_argument("--row-sd", type=float, default=1.0)
    ap.add_argument(
        "--out", type=Path, default=ROOT / "research/round0/a0_conditional_rate_sim.json"
    )
    args = ap.parse_args(argv)
    res = simulate(args.worlds, args.seed, args.alpha, args.row_sd)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(res, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    print(json.dumps(res["results"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
