"""F3 diagnostic D1.1, D2.3, D3.1 (and the recorded half of D3.3): recorded result only, no data read.

Exploratory.  Reads the stored v3 result (hash checked), recomputes nothing but counts and
descriptive statistics from the stored per-trial records.  Usage:
  python d0_recorded.py RESULT_JSON OUT_JSON
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path

import numpy as np
from scipy import stats

ROOT = Path(__file__).resolve().parents[3]
EXPECT = "c489f28c63975e1b62c75fb6e092db1e774254c3f1783db868a659bc99e25210"
ALPHA = 0.025
DIV = (1, 3, 6, 16)


def main(res_path: str, out_path: str) -> None:
    raw = Path(res_path).read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    assert sha == EXPECT, sha
    d = json.loads(raw)
    cfg = json.loads((ROOT / "research/v118/known_null_v3_config.json").read_text())
    ids = {}
    for k, v in cfg["variants"].items():
        ids[k] = [h.get("id") or f"{h['name']}@{h['horizon']}/{'+'.join(h['symbols'])}" for h in v]
    out: dict = {"result_sha256": sha, "ids": ids, "label": "EXPLORATORY, recorded result only"}
    t6 = {q: float(stats.t.ppf(q, 6)) for q in (0.95, 0.975, 0.992)}
    for gen in ("A0", "B", "A_v2"):
        tr = d["size_trials"][gen]
        n = len(tr)
        g: dict = {"n": n, "cells": {}, "hyp": {}}
        for k in ("K3", "K6", "K16"):
            for s in ("S1", "S2"):
                recs = [tr[str(i)][k][s] for i in range(n)]
                cnt = sum(1 for r in recs if any(
                    p is not None and p <= ALPHA and m is not None and m > 0
                    for p, m in zip(r["p_holm"], r["mean_bps"])))
                g["cells"][f"{s}/{k}"] = cnt
                if s != "S2":
                    continue
                P = np.array([[np.nan if x is None else x for x in r["p"]] for r in recs])
                M = np.array([[np.nan if x is None else x for x in r["mean_bps"]] for r in recs])
                SE = np.array([[np.nan if x is None else x for x in r["se_bps"]] for r in recs])
                H = np.array([[np.nan if x is None else x for x in r["p_holm"]] for r in recs])
                T = M / SE
                rej = (H <= ALPHA) & (M > 0)
                union = int(rej.any(axis=1).sum())
                for j, hid in enumerate(ids[k]):
                    e = {"holm_rejections": int(rej[:, j].sum()),
                         "mean_m": float(M[:, j].mean()), "sd_m": float(M[:, j].std(ddof=1)),
                         "mean_se": float(SE[:, j].mean()),
                         "U": float(M[:, j].std(ddof=1) / math.sqrt((SE[:, j] ** 2).mean())),
                         "mean_over_sd": float(M[:, j].mean() / M[:, j].std(ddof=1)),
                         "t_quantiles": {str(q): float(np.quantile(T[:, j], q)) for q in t6},
                         "raw_p_le": {f"alpha/{dv}": int((P[:, j] <= ALPHA / dv).sum()) for dv in DIV},
                         "raw_p_min": float(np.nanmin(P[:, j]))}
                    g["hyp"][f"{k}|{hid}"] = e
                g["cells"][f"S2/{k}/sum_marginals"] = int(rej.sum())
                g["cells"][f"S2/{k}/union"] = union
        out[gen] = g
    out["t6_quantiles"] = t6
    out["nominal_counts"] = {gen: {f"alpha/{dv}": ALPHA / dv * len(d["size_trials"][gen]) for dv in DIV}
                             for gen in ("A0", "B", "A_v2")}
    Path(out_path).write_text(json.dumps(out, indent=1))
    for gen in ("A0", "B", "A_v2"):
        print(gen, out[gen]["n"], {k: v for k, v in out[gen]["cells"].items()})


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
