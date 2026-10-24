"""F3 diagnostic (real data, in sample): the 7 x 7 per-session matrices M[h][i, j] behind A0.

EXPLORATORY.  M[i, j] = per-session mean gross P&L per trip (bps, the S2 statistic) of decision
session i traded against the labels of session j.  Seven cyclic shifts pi_k(s) = (s+k) % 7 fill all
49 cells.  k = 0 is the REAL pairing (diagonal): kept for decomposition only, never a null draw.
Variants: V0 recorded A0 (centred per name/horizon/session); V1 uncentred (v2 generator A);
V2 = V0 with the common intraday label profile removed (per name, horizon, time-of-day cell,
mean over sessions).  Reads data/real/multi7 through the runner's own guarded builder.
  python build_matrices.py DATA_ROOT OUT_NPZ
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "python" / "src"))
sys.path.insert(0, str(ROOT / "tools"))
import rel06_known_null as base  # noqa: E402
from iap.research import known_null as kn  # noqa: E402
from iap.research import known_null_v3 as v3  # noqa: E402
from iap.research.holdout_registry import guard_or_exit  # noqa: E402

G = 7


def labels_from_grid(panel: kn.Panel, grid: np.ndarray, perm) -> np.ndarray:
    pi = np.asarray(perm, dtype=np.int64)
    out = np.full(panel.n_rows, np.nan)
    on = panel.cell >= 0
    out[on] = grid[panel.name[on], pi[panel.sess[on]], panel.cell[on]]
    return out


def per_session(trips: kn.TripTable, labels: np.ndarray) -> np.ndarray:
    net, ok = kn.trip_values(trips, labels, "gross", 0.0)
    cnt = np.bincount(trips.sess[ok], minlength=G).astype(float)
    tot = np.bincount(trips.sess[ok], weights=net[ok], minlength=G)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(cnt > 0, tot / cnt, np.nan)


def main(data_root: str, out: str) -> None:
    cfg = v3.load_config(ROOT / v3.CONFIG_PATH)
    data_dir = (Path(data_root) / cfg["dataset"]["dir"]).resolve()
    rec = guard_or_exit(data_dir, purpose="F3 diagnostic review (null labels only, matrices)")
    if rec.get("state") != "in_sample":
        raise SystemExit(f"{data_dir} is {rec.get('state')!r}; the plan reads in-sample data only")
    t0 = time.perf_counter()
    study = base.build_study(data_dir, cfg)
    src = v3.build_a0_source(study, cfg)
    print("START built", round(time.perf_counter() - t0, 1), "s", flush=True)
    panel = study.panel
    hyps = {h.id: h for fam in study.variants.values() for h in fam}
    ids = sorted(hyps)
    grids = {
        "V0": src.grids,
        "V1": study.grids,
    }
    prof = {}
    for h, gr in src.grids.items():
        p = np.nanmean(gr, axis=1, keepdims=True)  # (name, 1, cell)
        prof[h] = gr - np.nan_to_num(p)
    grids["V2"] = prof
    res = {}
    for var, gr in grids.items():
        M = np.full((len(ids), G, G), np.nan)
        for k in range(G):
            perm = [(s + k) % G for s in range(G)]
            labs = {
                h: (
                    kn.labels_a(
                        panel, src.centred[h] if var == "V0" else study.real[h], perm, gr[h]
                    )
                    if var in ("V0", "V1")
                    else labels_from_grid(panel, gr[h], perm)
                )
                for h in study.horizons
            }
            for a, hid in enumerate(ids):
                x = per_session(study.trips[hid], labs[hyps[hid].horizon])
                for i in range(G):
                    M[a, i, (i + k) % G] = x[i]
        res[var] = M
        print("variant", var, "done", round(time.perf_counter() - t0, 1), "s", flush=True)
    np.savez(out, ids=np.array(ids), sessions=np.array(panel.sessions), **res)
    print("DONE", out, flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
