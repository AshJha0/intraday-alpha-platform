"""F3 diagnostic D3.2: size of the unmodified CR1 + Webb (G=7, one-sided, full enumeration) test.

EXPLORATORY, synthetic only (no market data).  Independent session means x_1..x_7 with true mean 0.
Seed 20271004 (SplitMix64, kn.SplitMix64).  Raw-p rejection rate at 0.025, 0.025/3, 0.025/6, 0.025/16.
  python d32_synthetic_size.py OUT_JSON [REPS]
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "python" / "src"))
from iap.research import known_null as kn  # noqa: E402

G = 7
THR = {"a": 0.025, "a/3": 0.025 / 3, "a/6": 0.025 / 6, "a/16": 0.025 / 16}
SDS_HET = np.array([1, 1, 1, 2, 3, 4, 5], float)


def wilson(k, n, z=1.96):
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(c - h, 5), round(c + h, 5)]


def draw(kind: str, rng, n: int) -> np.ndarray:
    z = kn.splitmix_normals(rng, n * G).reshape(n, G)
    if kind == "normal":
        return z
    if kind == "normal_het":
        return z * SDS_HET
    if kind == "t3":
        c = (kn.splitmix_normals(rng, n * G * 3).reshape(n, G, 3) ** 2).sum(axis=2)
        return z / np.sqrt(c / 3)
    if kind == "t3_het":
        return draw("t3", rng, n) * SDS_HET
    if kind == "skew":  # lognormal sigma 1, mean removed
        return np.exp(z) - math.exp(0.5)
    if kind == "skew_het":
        return (np.exp(z) - math.exp(0.5)) * SDS_HET
    raise ValueError(kind)


def main(out: str, reps: int = 20000) -> None:
    rng = kn.SplitMix64(20271004)
    res = {"seed": 20271004, "reps": reps, "label": "EXPLORATORY synthetic, independent sessions, true mean 0",
           "scenarios": {}}
    for kind in ("normal", "normal_het", "t3", "t3_het", "skew", "skew_het"):
        X = draw(kind, rng, reps)
        ps = np.array([kn.cluster_inference(x, 0.025)["p"] for x in X])
        t = np.array([(lambda r: r["t"] if r["t"] is not None else np.nan)(kn.cluster_inference(x, 0.025)) for x in X[:2000]])
        res["scenarios"][kind] = {
            nm: {"k": int((ps <= a).sum()), "rate": float((ps <= a).mean()), "wilson95": wilson(int((ps <= a).sum()), reps),
                 "nominal": a}
            for nm, a in THR.items()
        }
        res["scenarios"][kind]["t_q975_first2000"] = float(np.nanquantile(t, 0.975))
        print(kind, {nm: v["rate"] for nm, v in res["scenarios"][kind].items() if isinstance(v, dict) and "rate" in v}, flush=True)
    Path(out).write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 20000)
