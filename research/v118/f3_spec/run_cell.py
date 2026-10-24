"""F3 spec pilot: one (regime, G) cell.  EXPLORATORY, synthetic, not acceptance evidence.

python run_cell.py REGIME G N OUT_JSON      (resumable: skips if OUT_JSON exists)
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
from generators import NH, generate, seed_for
from procedures import p_p0, p_p1, p_p2, p_p3, wilson

ALPHAS = (0.025, 0.020)
KS = (1, 3, 6, 16)  # marginal levels alpha/k over all 16 columns
FAMS = (3, 6, 16)


def summarise(p: np.ndarray, n: int) -> dict:
    """p: (N, 16)."""
    fin = np.isfinite(p)
    out: dict = {"failures": int((~fin).sum()), "marginal": {}, "fwer": {}}
    pf = np.where(fin, p, 1.0)
    tot = p.size
    for k in KS:
        c = int((pf <= 0.025 / k).sum())
        out["marginal"][str(k)] = {
            "k": c,
            "n": tot,
            "rate": c / tot,
            "ratio": c / tot / (0.025 / k),
            "wilson95": wilson(c, tot),
        }
    for a in ALPHAS:
        for kf in FAMS:
            c = int((pf[:, :kf].min(axis=1) <= a / kf).sum())
            out["fwer"][f"{a}_K{kf}"] = {"k": c, "n": n, "rate": c / n, "wilson95": wilson(c, n)}
    return out


def main(regime: str, g: int, n: int, out: str) -> None:
    if Path(out).exists():
        print("exists, skip", out)
        return
    x3 = generate(regime, g, n)  # (N, G, 16)
    x = x3.transpose(1, 0, 2).reshape(g, n * NH)  # columns = dataset-major, 16 per dataset
    res: dict = {
        "label": "EXPLORATORY pilot, synthetic, NOT acceptance evidence",
        "regime": regime,
        "G": g,
        "datasets": n,
        "seed": seed_for(regime, g),
        "procedures": {},
    }
    print("START", regime, g, n, flush=True)
    for name, fn in (("P0", p_p0), ("P1", p_p1), ("P2", p_p2), ("P3", p_p3)):
        t0 = time.process_time()
        r = fn(x)
        info = {}
        if isinstance(r, tuple):
            r, info = r
        dt = time.process_time() - t0
        s = summarise(r.reshape(n, NH), n)
        s["cpu_s"] = dt
        s["cpu_s_per_1000_datasets"] = dt / n * 1000
        s["info"] = info
        res["procedures"][name] = s
        print(
            name,
            f"{dt:.1f}s",
            {k: round(v["ratio"], 3) for k, v in s["marginal"].items()},
            flush=True,
        )
    Path(out).write_text(json.dumps(res, indent=1))
    print("DONE", out, flush=True)


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4])
