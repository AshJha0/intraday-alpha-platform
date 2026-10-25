"""Synthetic null generators of the confirmation profiles PF-1 and PF-2 (U13; the
profiles specification ``CONFIRMATION_PROFILES.md`` section 4).

This module knows nothing about the gate.  For each profile it defines the cell table
(size cells: every generator of the profile's domain at every T of its grid; power
cells: the same with the planted effect of the profile's reference model on hypothesis
0) and draws each cell's ``(n, T, K)`` panel from the cell's own SplitMix64 stream
(:class:`r3_generators.Stream`, bit for bit the scalar ``iap.core.rng.SplitMix64``).

* cell seed: ``20320000 + 10000 * profile_index + 100 * kind + cell_index`` (kind 0 size,
  1 power; PF-1 cells at 20330000.., PF-2 at 20340000..); the development series that fixes a cell's block length is drawn from
  ``cell seed + 5000`` with the same generator; disjoint from R0's 20301000-20301500 and
  from the F3 streams;
* generators (all stationary, mean 0, unit unconditional variance): ``iid_normal``;
  ``ar1`` (stationary start ``x_1 ~ N(0, 1)``, ``x_t = rho x_{t-1} + sqrt(1 - rho^2) e_t``);
  ``garch11`` (``h_t = omega + alpha x_{t-1}^2 + beta h_{t-1}``, ``omega = 1 - alpha -
  beta``, ``h_1 = 1``, normal innovations, 300 burn-in sessions dropped); ``t3`` (Student
  t3 as the F3 b1 construction, ``z / sqrt(chi2_3 / 3) / sqrt(3)``); ``overlap_ma``
  (``x_t = sum_{j=0}^{h-1} e_{t+j} / sqrt(h)``: the dependence that a holding horizon
  of ``h`` sessions induces between overlapping windows, lag-k autocorrelation
  ``(h - k) / h``);
* draw order per chunk: the primary normals in row-major ``(n, T', K)`` order, then any
  auxiliary normals (the three chi-square normals of ``t3``), so a chunk is reproducible
  from the stream state at its start.

Thresholds (463 of 20,000; 4,047 of 5,000) are restated here from the profiles
specification so that the suite can be run on its own.  Nothing here reads data.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from r3_generators import Stream  # noqa: E402

K = 4
N_SIZE = 20_000
N_POWER = 5_000
ALPHA_OP = 0.020
SIZE_PASS_K = 463
POWER_PASS_DETECTIONS = 4_047
SEED_BASE = 20_320_000
DEV_SEED_OFFSET = 5_000
GARCH_BURN = 300
KIND_INDEX = {"size": 0, "power": 1}


@dataclass(frozen=True)
class Cell:
    profile: str
    profile_index: int
    kind: str  # size / power
    index: int
    generator: str
    params: dict[str, Any] = field(default_factory=dict)
    T: int = 250
    n: int = N_SIZE
    delta: float = 0.0  # planted mean on hypothesis 0 (power cells)

    @property
    def seed(self) -> int:
        return SEED_BASE + 10_000 * self.profile_index + 100 * KIND_INDEX[self.kind] + self.index

    @property
    def dev_seed(self) -> int:
        return self.seed + DEV_SEED_OFFSET

    @property
    def name(self) -> str:
        p = "_".join(
            f"{k}{v:g}" if isinstance(v, (int, float)) else f"{k}{v}"
            for k, v in self.params.items()
        )
        return f"{self.generator}{('_' + p) if p else ''}_T{self.T}"

    @property
    def id(self) -> str:
        return f"{self.profile}/{self.kind}/{self.index:02d}/{self.name}"


def cells_for(profile: Any, *, planted: Any) -> list[Cell]:
    """The cell table of a profile (``profile`` is ``iap.validation.profiles.Profile``;
    ``planted(T)`` gives the power cells' planted mean)."""
    out: list[Cell] = []
    i = 0
    for gen, params in profile.generators():
        for T in profile.T_grid:
            out.append(Cell(profile.id, profile.index, "size", i, gen, dict(params), T, N_SIZE))
            i += 1
    i = 0
    for gen, params in profile.generators():
        for T in profile.T_grid:
            out.append(
                Cell(
                    profile.id,
                    profile.index,
                    "power",
                    i,
                    gen,
                    dict(params),
                    T,
                    N_POWER,
                    float(planted(T)),
                )
            )
            i += 1
    return out


def draw(
    stream: Stream, generator: str, params: dict[str, Any], n: int, T: int, k: int = K
) -> np.ndarray:
    """``n`` independent datasets of shape ``(n, T, k)`` from ``stream`` (advanced)."""
    if generator == "iid_normal":
        return stream.normals(n, T, k)
    if generator == "ar1":
        rho = float(params["rho"])
        e = stream.normals(n, T, k)
        x = np.empty_like(e)
        x[:, 0, :] = e[:, 0, :]
        s = math.sqrt(1.0 - rho * rho)
        for t in range(1, T):
            x[:, t, :] = rho * x[:, t - 1, :] + s * e[:, t, :]
        return x
    if generator == "garch11":
        a, b = float(params["alpha"]), float(params["beta"])
        omega = 1.0 - a - b
        z = stream.normals(n, T + GARCH_BURN, k)
        x = np.empty_like(z)
        h = np.ones((n, k))
        x[:, 0, :] = z[:, 0, :]
        for t in range(1, T + GARCH_BURN):
            h = omega + a * x[:, t - 1, :] ** 2 + b * h
            x[:, t, :] = np.sqrt(h) * z[:, t, :]
        return np.ascontiguousarray(x[:, GARCH_BURN:, :])
    if generator == "t3":
        z = stream.normals(n, T, k)
        c = (stream.normals(n, T, k, 3) ** 2).sum(axis=3)
        return z / np.sqrt(c / 3.0) / math.sqrt(3.0)
    if generator == "overlap_ma":
        h = int(params["h"])
        e = stream.normals(n, T + h - 1, k)
        cs = np.cumsum(e, axis=1)
        cs = np.concatenate([np.zeros((n, 1, k)), cs], axis=1)
        return (cs[:, h : h + T, :] - cs[:, :T, :]) / math.sqrt(h)
    raise ValueError(f"unknown generator {generator!r}")


def development_series(cell: Cell) -> np.ndarray:
    """One series of length ``T`` from the cell's development stream (block-length diagnostic)."""
    return draw(Stream(cell.dev_seed), cell.generator, cell.params, 1, cell.T, 1)[0, :, 0]


def panel_chunks(cell: Cell, chunk: int):
    """Yield ``(start, panel)`` chunks of the cell's ``(n, T, K)`` panel from ONE stream
    consumed in order; a power cell's hypothesis 0 carries the planted mean."""
    stream = Stream(cell.seed)
    done = 0
    while done < cell.n:
        m = min(chunk, cell.n - done)
        x = draw(stream, cell.generator, cell.params, m, cell.T, K)
        if cell.delta:
            x[:, :, 0] += cell.delta
        yield done, x
        done += m
