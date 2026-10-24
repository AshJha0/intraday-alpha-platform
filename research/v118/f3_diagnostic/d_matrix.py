"""F3 diagnostics D1.2, D1.4, D2.1, D2.2 on the matrices (no data read here; matrices.npz).

EXPLORATORY.  Step 1 is the VALIDATION GATE: the V0 matrices must reproduce the recorded A0 S2
records (counts exactly; mean, se, p to the stored 6-decimal rounding) or the script stops.
  python d_matrix.py MATRICES_NPZ RESULT_JSON OUT_JSON
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from fmat import ALPHA, additive_fit, derangement_array, family_rejections, inference_over

ROOT = Path(__file__).resolve().parents[3]


def main(npz: str, res_path: str, out_path: str) -> None:
    Z = np.load(npz, allow_pickle=False)
    ids = [str(x) for x in Z["ids"]]
    cfg = json.loads((ROOT / "research/v118/known_null_v3_config.json").read_text())
    fam = {
        k: [h.get("id") or f"{h['name']}@{h['horizon']}/{'+'.join(h['symbols'])}" for h in v]
        for k, v in cfg["variants"].items()
    }
    perms = derangement_array(7)
    stored = json.loads(Path(res_path).read_text())["size_trials"]["A0"]
    out: dict = {"label": "EXPLORATORY, matrix diagnostics", "ids": ids, "fam": fam}

    def infer(Mvar):
        return {hid: inference_over(Mvar[a], perms) for a, hid in enumerate(ids)}

    def fam_counts(inf):
        return {
            k: int(
                family_rejections(
                    np.stack([inf[h][0] for h in hs], 1), np.stack([inf[h][1] for h in hs], 1)
                ).sum()
            )
            for k, hs in fam.items()
        }

    inf0 = infer(Z["V0"])
    maxdm = maxds = maxdp = 0.0
    for k, hs in fam.items():
        for d in range(perms.shape[0]):
            r = stored[str(d)][k]["S2"]
            for j, h in enumerate(hs):
                maxdm = max(maxdm, abs(inf0[h][1][d] - r["mean_bps"][j]))
                maxds = max(maxds, abs(inf0[h][2][d] - r["se_bps"][j]))
                maxdp = max(maxdp, abs(inf0[h][0][d] - r["p"][j]))
    cnt0 = fam_counts(inf0)
    ok = maxdm < 2e-6 and maxds < 2e-6 and maxdp < 2e-6 and cnt0 == {"K3": 79, "K6": 80, "K16": 23}
    out["validation"] = {
        "max_abs_diff_mean": maxdm,
        "max_abs_diff_se": maxds,
        "max_abs_diff_p": maxdp,
        "counts": cnt0,
        "recorded": {"K3": 79, "K6": 80, "K16": 23},
        "passed": bool(ok),
    }
    print("validation", out["validation"], flush=True)
    if not ok:
        Path(out_path).write_text(json.dumps(out, indent=1))
        raise SystemExit(
            "VALIDATION GATE FAILED: matrices do not reproduce the recorded A0 records"
        )

    off = ~np.eye(7, dtype=bool)
    out["conditional"], out["decomposition"] = {}, {}
    for a, hid in enumerate(ids):
        p, m, se = inf0[hid]
        M0 = Z["V0"][a]
        mu, ai, bj, res = additive_fit(M0)
        tot = float(np.nanvar(M0[off], ddof=1))
        out["conditional"][hid] = {
            "E_m": float(np.nanmean(m)),
            "sd_m": float(np.nanstd(m, ddof=1)),
            "E_m_over_sd": float(np.nanmean(m) / np.nanstd(m, ddof=1)),
            "E_m_over_meanSE": float(np.nanmean(m) / np.nanmean(se)),
            "grand_offdiag_mean": mu,
            "diag_mean_real_pairing": float(np.nanmean(np.diag(M0))),
        }
        out["decomposition"][hid] = {
            "var_offdiag": tot,
            "share_row": float(np.var(ai) / tot),
            "share_col": float(np.var(bj) / tot),
            "share_resid": float(np.nanvar(res, ddof=1) / tot),
            "row_effects": ai.tolist(),
            "col_effects": bj.tolist(),
        }

    M3 = np.empty_like(Z["V0"])
    for a in range(len(ids)):
        _, _, _, res = additive_fit(Z["V0"][a])
        M3[a] = np.where(
            np.isnan(res), Z["V0"][a], res
        )  # diagonal untouched, never used by a derangement
    out["rerun"] = {"V0_recorded": cnt0}
    perhyp = {
        "V0": {
            h: {
                "E_m_over_sd": out["conditional"][h]["E_m_over_sd"],
                "raw_p_le_a": int((inf0[h][0] <= ALPHA).sum()),
                "raw_p_le_a_over_6": int((inf0[h][0] <= ALPHA / 6).sum()),
            }
            for h in ids
        }
    }
    for var, Mv in (
        ("V1_uncentred", Z["V1"]),
        ("V2_profile_removed", Z["V2"]),
        ("V3_double_centred", M3),
    ):
        inf = infer(Mv)
        out["rerun"][var] = fam_counts(inf)
        perhyp[var] = {
            h: {
                "E_m_over_sd": float(np.nanmean(inf[h][1]) / np.nanstd(inf[h][1], ddof=1)),
                "raw_p_le_a": int((inf[h][0] <= ALPHA).sum()),
                "raw_p_le_a_over_6": int((inf[h][0] <= ALPHA / 6).sum()),
            }
            for h in ids
        }
        print(var, out["rerun"][var], flush=True)
    out["rerun_per_hyp"] = perhyp

    p6 = derangement_array(6)
    out["loso_V0"] = {}
    for s in range(7):
        keep = [i for i in range(7) if i != s]
        inf = {hid: inference_over(Z["V0"][a][np.ix_(keep, keep)], p6) for a, hid in enumerate(ids)}
        out["loso_V0"][f"drop_{s}_{Z['sessions'][s]}"] = {
            "n_derangements": int(p6.shape[0]),
            **fam_counts(inf),
        }
        print("loso", s, out["loso_V0"][f"drop_{s}_{Z['sessions'][s]}"], flush=True)
    Path(out_path).write_text(json.dumps(out, indent=1))
    print("DONE")


if __name__ == "__main__":
    main(*sys.argv[1:4])
