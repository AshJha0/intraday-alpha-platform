"""F3 diagnostic D1.3: sampling distribution of the A0-style rate under a parametric null at the
matrix level.  EXPLORATORY, synthetic only (no market data).

Each draw is one 7 x 7 matrix M with true mean 0 in every cell (H0 holds by construction).  All 1,854
derangements are enumerated and the unmodified kn.cluster_inference (CR1 + Webb G=7) is applied.
Per matrix: the rate of raw p <= a, a/3, a/6 with a = 0.025 (single hypothesis, marginal).
Models: a  iid N(0,1)                                          seed 20271001
        b  r_i + c_j + s_i e_ij, sd(r)=sd(c)=0.5, s=(.5,.7,1,1,1.4,2,2.8)   seed 20271002
        c  as b with sd(r), sd(c), s_i fitted to the real V0 matrix of one hypothesis  seed 20271003
  python d13_null_matrices.py MODEL N_MATRICES OUT_JSON [MATRICES_NPZ HYPOTHESIS_ID]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

from fmat import ALPHA, additive_fit, derangement_array, inference_over, kn

SEEDS = {"a": 20271001, "b": 20271002, "c": 20271003}
S_B = np.array([0.5, 0.7, 1.0, 1.0, 1.4, 2.0, 2.8])


def draw_matrix(model: str, rng, params=None) -> np.ndarray:
    z = kn.splitmix_normals(rng, 7 * 7 + 14)
    e = z[:49].reshape(7, 7)
    r, c = z[49:56], z[56:63]
    if model == "a":
        return e
    if model == "b":
        return 0.5 * r[:, None] + 0.5 * c[None, :] + S_B[:, None] * e
    sr, sc, s = params
    return sr * r[:, None] + sc * c[None, :] + s[:, None] * e


def main(model: str, n: int, out: str, npz: str | None = None, hid: str | None = None) -> None:
    perms = derangement_array(7)
    params = None
    if model == "c":
        Z = np.load(npz, allow_pickle=False)
        a = [str(x) for x in Z["ids"]].index(hid)
        _, ai, bj, res = additive_fit(Z["V0"][a])
        params = (float(np.std(ai, ddof=1)), float(np.std(bj, ddof=1)),
                  np.array([np.nanstd(res[i], ddof=1) for i in range(7)]))
    seed = SEEDS[model]
    rng = kn.SplitMix64(seed)
    rates = []
    shifts = []
    for k in range(n):
        M = draw_matrix(model, rng, params)
        p, m, se = inference_over(M, perms)
        rates.append([float((p <= ALPHA).mean()), float((p <= ALPHA / 3).mean()), float((p <= ALPHA / 6).mean())])
        shifts.append(float(np.mean(m) / np.std(m, ddof=1)))
        if (k + 1) % 10 == 0:
            print(model, k + 1, "of", n, flush=True)
    R = np.array(rates)
    q = lambda col: {str(x): float(np.quantile(R[:, col], x)) for x in (0.05, 0.25, 0.5, 0.75, 0.9, 0.95)}
    doc = {"label": "EXPLORATORY synthetic matrix-level null", "model": model, "seed": seed, "n_matrices": n,
           "hypothesis_fitted": hid, "params": None if params is None else [params[0], params[1], params[2].tolist()],
           "thresholds": ["a", "a/3", "a/6"], "nominal": [ALPHA, ALPHA / 3, ALPHA / 6],
           "mean_rate": R.mean(axis=0).tolist(), "sd_rate": R.std(axis=0, ddof=1).tolist(),
           "quantiles": {t: q(i) for i, t in enumerate(["a", "a/3", "a/6"])},
           "frac_matrices_rate_gt_2.97pct": [float((R[:, i] > 55 / 1854).mean()) for i in range(3)],
           "mean_abs_E_m_over_sd": float(np.mean(np.abs(shifts))), "rates": R.tolist(), "shift_E_m_over_sd": shifts}
    Path(out).write_text(json.dumps(doc, indent=1))
    print("DONE", doc["mean_rate"], doc["sd_rate"])


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]), sys.argv[3], *(sys.argv[4:6]))
