"""F3 power diagnostic core: planted-effect datasets, P1 + Holm, one deterministic stream per cell (UNSIGNED DRAFT).

Synthetic only; NO market data.  Reuses the acceptance study's generators and P1 p-values unchanged by importing
``research/v118/f3_accept/core.py`` (bound as a script in the identity; not modified).  Every parameter comes from
``declaration.json``.  Nothing here has been run for the study.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import math
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))
sys.path.insert(0, str(HERE))
import power_math as pm  # noqa: E402
from iap.core.rng import SplitMix64  # noqa: E402  (pinned RNG; no market-data module is imported)

ACC_CORE = ROOT / "research" / "v118" / "f3_accept" / "core.py"
_spec = importlib.util.spec_from_file_location("f3_accept_core_for_power", ACC_CORE)
assert _spec and _spec.loader
acc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(acc)


def regime_index(decl: Mapping[str, Any]) -> dict[str, int]:
    return dict(decl["seeds"]["regime_index"])


def seed_for(decl: Mapping[str, Any], regime: str, g: int, dry: bool = False) -> int:
    base = decl["seeds"]["dry_run_base"] if dry else decl["seeds"]["base"]
    return int(base) + 10 * regime_index(decl)[regime] + int(g)


def bootstrap_seed(decl: Mapping[str, Any], regime: str, g: int) -> int:
    return int(decl["seeds"]["bootstrap_base"]) + 10 * regime_index(decl)[regime] + int(g)


def sigma_rms(decl: Mapping[str, Any], regime: str, g: int) -> float:
    """Root-mean-square session scale: a0 has unit scale in every session, b0 / b1 use the declared scales."""
    if regime == "a0":
        return 1.0
    sc = acc.scales(decl, g)
    return float(math.sqrt(float(np.mean(sc**2))))


def shift_for(decl: Mapping[str, Any], regime: str, g: int, d: float) -> float:
    """mu = d * sigma_RMS / sqrt(G): the constant shift added to every session value of the planted hypothesis."""
    return d * sigma_rms(decl, regime, g) / math.sqrt(g)


def plant(decl: Mapping[str, Any], x: np.ndarray, regime: str, g: int, d: float) -> np.ndarray:
    """Copy of the null datasets with the planted shift added to hypothesis ``planted_index`` only (all sessions)."""
    y = x.copy()
    y[:, :, int(decl["planted_index"])] += shift_for(decl, regime, g, d)
    return y


def run_cell(
    decl: Mapping[str, Any], regime: str, g: int, n: int, *, dry: bool = False
) -> dict[str, Any]:
    """One stream cell: ONE SplitMix64 stream from the cell seed consumed chunk after chunk (never reseeded); every
    chunk's null datasets are reused for every d of the grid (common random numbers, frozen mapping: dataset i is the
    same null dataset for every d).  Returns per-(K, d) counts, the per-dataset rejection indicators (packed), and the
    stream evidence a resumed run recomputes and compares."""
    rng = SplitMix64(seed_for(decl, regime, g, dry))
    chunk = int(decl["chunk"])
    fams = list(decl["families"])
    grid = [float(v) for v in decl["grid"]]
    alpha = float(decl["alpha_op"])
    ind = {k: np.zeros((len(grid), n), dtype=bool) for k in fams}
    nullrej = {k: np.zeros(len(grid), dtype=np.int64) for k in fams}
    nan = 0
    per_chunk: list[list[int]] = []
    done = 0
    while done < n:
        m = min(chunk, n - done)
        x = acc.draw_chunk(decl, regime, g, m, rng)
        row = [m]
        for j, d in enumerate(grid):
            p = acc.p1_pvalues(plant(decl, x, regime, g, d))
            nan += int((~np.isfinite(p)).sum())
            for k in fams:
                planted, anynull = pm.holm_planted_rejected(p, alpha, k)
                ind[k][j, done : done + m] = planted
                nullrej[k][j] += int(anynull.sum())
                row.append(int(planted.sum()))
        per_chunk.append(row)
        done += m
    packed = {
        f"K{k}": base64.b64encode(np.packbits(ind[k], axis=1).tobytes()).decode("ascii")
        for k in fams
    }
    ind_sha = hashlib.sha256(
        b"".join(np.packbits(ind[k], axis=1).tobytes() for k in fams)
    ).hexdigest()
    return {
        "regime": regime,
        "G": g,
        "n": n,
        "seed": seed_for(decl, regime, g, dry),
        "chunk": chunk,
        "n_chunks": len(per_chunk),
        "nan_p": nan,
        "sigma_rms": sigma_rms(decl, regime, g),
        "counts": {f"K{k}": [int(v) for v in ind[k].sum(axis=1)] for k in fams},
        "null_rejection_datasets": {f"K{k}": [int(v) for v in nullrej[k]] for k in fams},
        "rng_final_state": rng.state,
        "chunk_counts_sha256": hashlib.sha256(repr(per_chunk).encode("ascii")).hexdigest(),
        "indicators_sha256": ind_sha,
        "indicators_packed_b64": packed,
    }


def unpack_indicators(rec: Mapping[str, Any], k: int, n_grid: int) -> np.ndarray:
    """(n_grid, n) boolean per-dataset rejection indicators of family ``k`` from a cell record."""
    raw = np.frombuffer(base64.b64decode(rec["indicators_packed_b64"][f"K{k}"]), dtype=np.uint8)
    bits = np.unpackbits(raw.reshape(n_grid, -1), axis=1)
    return bits[:, : int(rec["n"])].astype(bool)
