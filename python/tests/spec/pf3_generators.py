"""Synthetic null generators of PF-3, the combined multi-horizon book (U13; the protocol
``research/v2/U13_CONFIRMATION_PROTOCOL.md`` section 5).

A combined book sums a short-horizon component (1-5 session holdings) and a long-horizon
component (20-60 session holdings) on one capital.  Each component's daily return is the
overlapping-position moving average of :mod:`pf_generators` (``overlap_ma``: holding ``h``
sessions makes lag-k autocorrelation ``(h - k) / h``).  The generators combine them with
declared weights and add the structures neither PF-1 nor PF-2 covered:

* ``iid_normal``: the reference model; ``garch11`` (``z_t sqrt(g_t)``, the GARCH below) and
  ``t3`` (unit-variance Student t3) alone, the PF-1 tails and clustering without overlap;
* ``mix``: ``sqrt(w) S + sqrt(1 - w) Lg``, ``S = overlap_ma(h_short)``, ``Lg =
  overlap_ma(h_long)``, ``w = 0.5``, ``h_short = 3``,
  ``h_long`` in {10, 20, 40, 60} (the overlapping-exposure ladder);
* ``mix_garch``: ``mix`` times a common GARCH(1,1) volatility ``sqrt(g_t)`` (``g_t = omega +
  0.08 u_{t-1}^2 + 0.90 g_{t-1}``, ``u = sqrt(g) z`` from its own normals, 300 burn-in), the
  cross-component volatility co-movement;
* ``mix_t3``: ``mix`` whose innovations are Student t3 scaled to unit variance (heavy tails);
* ``mix_regime``: weights switch from ``w = 0.8`` to ``w = 0.2`` at a session drawn
  uniformly in ``[T/4, 3T/4)`` (a regime shift in the allocation);
* ``mix_ar``: ``sqrt(0.5) AR1(0.3) + sqrt(0.5) Lg`` (a short component with decaying
  dependence instead of a hard horizon).

The four structured mixtures use ``h_long = 10``; the ladder alone carries the longer horizons.

Every generator is stationary or piecewise stationary with mean 0 and unit variance at
every session.  Cell seeds ``20350000 + 100 * kind + index`` (``SEED_BASE + 10,000 * 3``),
development series at ``+ 5000``; disjoint from PF-1/PF-2 (20330000.., 20340000..), R0
(20301000-20301500) and F3.  Draw order per chunk: the generator's arrays in the order the
code below requests them, row-major ``(n, T', K)``.  Nothing here reads data.
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

K = 3  # K_C: the configurations C0, C1, C2 (independent: least favourable for Holm)
N_SIZE = 20_000
N_POWER = 5_000
ALPHA_OP = 0.020
SIZE_PASS_K = 463
POWER_PASS_DETECTIONS = 4_047
SEED_BASE = 20_350_000
DEV_SEED_OFFSET = 5_000
GARCH_ALPHA, GARCH_BETA, GARCH_BURN = 0.08, 0.90, 300
T_GRID = (250, 500)
KIND_INDEX = {"size": 0, "power": 1}

GENERATORS: list[tuple[str, dict[str, Any]]] = [
    ("iid_normal", {}),
    ("garch11", {}),
    ("t3", {}),
    ("mix", {"w": 0.5, "h_short": 3, "h_long": 10}),
    ("mix", {"w": 0.5, "h_short": 3, "h_long": 20}),
    ("mix", {"w": 0.5, "h_short": 3, "h_long": 40}),
    ("mix", {"w": 0.5, "h_short": 3, "h_long": 60}),
    ("mix_garch", {"w": 0.5, "h_short": 3, "h_long": 10}),
    ("mix_t3", {"w": 0.5, "h_short": 3, "h_long": 10}),
    ("mix_regime", {"w_from": 0.8, "w_to": 0.2, "h_short": 3, "h_long": 10}),
    ("mix_ar", {"w": 0.5, "rho": 0.3, "h_long": 10}),
]


@dataclass(frozen=True)
class Cell:
    kind: str
    index: int
    generator: str
    params: dict[str, Any] = field(default_factory=dict)
    T: int = 250
    n: int = N_SIZE
    delta: float = 0.0

    @property
    def seed(self) -> int:
        return SEED_BASE + 100 * KIND_INDEX[self.kind] + self.index

    @property
    def dev_seed(self) -> int:
        return self.seed + DEV_SEED_OFFSET

    @property
    def name(self) -> str:
        p = "_".join(f"{k}{v:g}" for k, v in self.params.items())
        return f"{self.generator}{('_' + p) if p else ''}_T{self.T}"

    @property
    def id(self) -> str:
        return f"PF-3/{self.kind}/{self.index:02d}/{self.name}"


def cells(*, planted: Any) -> list[Cell]:
    out: list[Cell] = []
    for kind, n in (("size", N_SIZE), ("power", N_POWER)):
        i = 0
        for g, params in GENERATORS:
            for T in T_GRID:
                d = float(planted(T)) if kind == "power" else 0.0
                out.append(Cell(kind, i, g, dict(params), T, n, d))
                i += 1
    return out


def _ma(e: np.ndarray, h: int, T: int) -> np.ndarray:
    cs = np.cumsum(e, axis=1)
    cs = np.concatenate([np.zeros_like(cs[:, :1, :]), cs], axis=1)
    return (cs[:, h : h + T, :] - cs[:, :T, :]) / math.sqrt(h)


def _t3(stream: Stream, *shape: int) -> np.ndarray:
    z = stream.normals(*shape)
    c = (stream.normals(*shape, 3) ** 2).sum(axis=-1)
    return z / np.sqrt(c / 3.0) / math.sqrt(3.0)


def _garch_sigma(stream: Stream, n: int, T: int, k: int) -> np.ndarray:
    """``sqrt(g_t)`` of a GARCH(1,1) driven by its own normals, unit unconditional variance."""
    z = stream.normals(n, T + GARCH_BURN, k)
    omega = 1.0 - GARCH_ALPHA - GARCH_BETA
    g = np.ones((n, k))
    sig = np.empty_like(z)
    sig[:, 0, :] = 1.0
    u_prev = z[:, 0, :]
    for i in range(1, T + GARCH_BURN):
        g = omega + GARCH_ALPHA * u_prev**2 + GARCH_BETA * g
        sig[:, i, :] = np.sqrt(g)
        u_prev = sig[:, i, :] * z[:, i, :]
    return sig[:, GARCH_BURN:, :]


def draw(
    stream: Stream, generator: str, p: dict[str, Any], n: int, T: int, k: int = K
) -> np.ndarray:
    """``n`` datasets of shape ``(n, T, k)`` from ``stream`` (advanced)."""
    if generator == "iid_normal":
        return stream.normals(n, T, k)
    if generator == "t3":
        return _t3(stream, n, T, k)
    if generator == "garch11":
        return stream.normals(n, T, k) * _garch_sigma(stream, n, T, k)
    hl = int(p["h_long"])
    if generator == "mix_ar":
        rho = float(p["rho"])
        e = stream.normals(n, T, k)
        s = np.empty_like(e)
        s[:, 0, :] = e[:, 0, :]
        f = math.sqrt(1.0 - rho * rho)
        for t in range(1, T):
            s[:, t, :] = rho * s[:, t - 1, :] + f * e[:, t, :]
        lg = _ma(stream.normals(n, T + hl - 1, k), hl, T)
        w = float(p["w"])
        return math.sqrt(w) * s + math.sqrt(1.0 - w) * lg
    hs = int(p["h_short"])
    if generator == "mix_t3":
        es, el = _t3(stream, n, T + hs - 1, k), _t3(stream, n, T + hl - 1, k)
    else:
        es, el = stream.normals(n, T + hs - 1, k), stream.normals(n, T + hl - 1, k)
    s, lg = _ma(es, hs, T), _ma(el, hl, T)
    if generator == "mix_regime":
        u = stream.uniforms(n * k).reshape(n, 1, k)
        tau = np.floor(T / 4 + u * (T / 2))
        t = np.arange(T).reshape(1, T, 1)
        w = np.where(t < tau, float(p["w_from"]), float(p["w_to"]))
        return np.sqrt(w) * s + np.sqrt(1.0 - w) * lg
    w = float(p["w"])
    x = math.sqrt(w) * s + math.sqrt(1.0 - w) * lg
    if generator == "mix_garch":
        return x * _garch_sigma(stream, n, T, k)
    if generator in ("mix", "mix_t3"):
        return x
    raise ValueError(f"unknown generator {generator!r}")


def development_series(cell: Cell) -> np.ndarray:
    """One series of length ``T`` from the cell's development stream (block-length rule)."""
    return draw(Stream(cell.dev_seed), cell.generator, cell.params, 1, cell.T, 1)[0, :, 0]


def panel_chunks(cell: Cell, chunk: int, n: int | None = None):
    """``(start, panel)`` chunks from ONE stream consumed in order; a power cell's
    hypothesis 0 carries the planted mean."""
    stream = Stream(cell.seed)
    total = int(n or cell.n)
    done = 0
    while done < total:
        m = min(chunk, total - done)
        x = draw(stream, cell.generator, cell.params, m, cell.T, K)
        if cell.delta:
            x[:, :, 0] += cell.delta
        yield done, x
        done += m
