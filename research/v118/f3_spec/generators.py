"""F3 spec pilot: synthetic null generators.  All have true mean 0.  Returns array (N, G, 16)."""

from __future__ import annotations

import math

import numpy as np
from procedures import kn

NH = 16
REGIMES = ("a0", "a1", "b0", "b1", "c0", "c1")
REGIME_INDEX = {r: i for i, r in enumerate(REGIMES)}
RHO_A1 = 0.6
PHI = {"c0": 0.3, "c1": 0.6}


def seed_for(regime: str, g: int) -> int:
    return 20281100 + 10 * REGIME_INDEX[regime] + g


def scales(g: int) -> np.ndarray:
    return np.exp(np.linspace(0.0, math.log(5.0), g))


def _normals(rng, *shape):
    return kn.splitmix_normals(rng, int(np.prod(shape))).reshape(shape)


def generate(regime: str, g: int, n: int) -> np.ndarray:
    rng = kn.SplitMix64(seed_for(regime, g))
    z = _normals(rng, n, g, NH)
    if regime == "a0":
        return z
    if regime == "a1":
        f = _normals(rng, n, g, 1)
        return math.sqrt(RHO_A1) * f + math.sqrt(1 - RHO_A1) * z
    sc = scales(g)[None, :, None]
    if regime == "b0":
        return z * sc
    if regime == "b1":
        c = (_normals(rng, n, g, NH, 3) ** 2).sum(axis=3)
        t3 = z / np.sqrt(c / 3) / math.sqrt(3.0)
        ln = (np.exp(z) - math.exp(0.5)) / math.sqrt((math.e - 1) * math.e)
        kind = (np.arange(g) % 3)[None, :, None]
        return np.where(kind == 0, z, np.where(kind == 1, t3, ln)) * sc
    if regime in PHI:
        phi = PHI[regime]
        x = np.empty_like(z)
        x[:, 0] = z[:, 0]
        for i in range(1, g):
            x[:, i] = phi * x[:, i - 1] + math.sqrt(1 - phi * phi) * z[:, i]
        return x
    raise ValueError(regime)
