"""R3 adversarial Monte Carlo: the independent generator script (R0 section 5).

This module knows nothing about the gate.  It defines the cell table of the frozen R0
specification (sections 5.1-5.5 and the two power cells of 5.3) and generates every
cell's ``(n, G, 16)`` panel from its own SplitMix64 stream:

* random stream: SplitMix64 exactly as ``iap.core.rng.SplitMix64`` and
  ``research/v118/f3_spec/generators.py`` (Box-Muller normals, cosine branch, two
  uniforms per normal, draws in row-major panel order), vectorised here from the
  closed form ``state + k * gamma`` of the stream; a test compares the first draws
  with the scalar generator bit for bit;
* cell seed: ``20301000 + 100 * case_index + cell_index`` (R0 section 2) with the
  case indices A = 0, B = 1, C = 2, D = 3, E = 4 and the cell indices enumerated
  in the order of :data:`CELLS` within each case;
* generators: the exact AR(1) of 5.1 (stationary start, unit stationary variance),
  the equicorrelated-within-session normals of 5.2, the F3 b0 / b1 scales and
  distributions of 5.3 (log-spaced 1..5 cyclic session scales; Student t3 and
  centred lognormal(sigma = 1) scaled to unit variance), the calibration /
  confirmation pair of 5.4 (i) and the dropped-session panels of 5.5.

Thresholds (463 of 20,000; 4,047 of 5,000) are restated here from R0 so that the
suite can be run on its own::

    PYTHONPATH=python/src python python/tests/spec/r3_suite.py --write

Nothing here reads data.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

K = 16
N_SIZE = 20_000
N_POWER = 5_000
ALPHA_OP = 0.020
SIZE_PASS_K = 463  # one-sided 95 % Clopper-Pearson upper bound 0.02498 <= 0.025
POWER_PASS_DETECTIONS = 4_047  # one-sided 95 % lower bound 0.800
SEED_BASE = 20_301_000
CASE_INDEX = {"R3-A": 0, "R3-B": 1, "R3-C": 2, "R3-D": 3, "R3-E": 4}
SCALE_HI = 5.0

_MASK = np.uint64((1 << 64) - 1)
_GAMMA = np.uint64(0x9E3779B97F4A7C15)
_MIX1 = np.uint64(0xBF58476D1CE4E5B9)
_MIX2 = np.uint64(0x94D049BB133111EB)


# ------------------------------------------------------------------ stream --
class Stream:
    """Vectorised SplitMix64 (PLATFORM_CONVENTIONS.md section 3); the k-th output
    after ``state`` is ``mix(state + k * gamma)``, so a block of ``n`` draws is one
    array expression and the state advances by ``n * gamma``."""

    def __init__(self, seed: int) -> None:
        self.state = int(seed) & ((1 << 64) - 1)

    def uniforms(self, n: int) -> np.ndarray:
        if n <= 0:
            return np.empty(0)
        k = np.arange(1, n + 1, dtype=np.uint64)
        with np.errstate(over="ignore"):
            z = np.uint64(self.state) + k * _GAMMA
            z = (z ^ (z >> np.uint64(30))) * _MIX1
            z = (z ^ (z >> np.uint64(27))) * _MIX2
            z = z ^ (z >> np.uint64(31))
        self.state = (self.state + n * 0x9E3779B97F4A7C15) & ((1 << 64) - 1)
        return (z >> np.uint64(11)).astype(np.float64) * 2.0**-53

    def normals(self, *shape: int) -> np.ndarray:
        n = int(np.prod(shape)) if shape else 1
        u = self.uniforms(2 * n)
        u1, u2 = u[0::2], u[1::2]
        if np.any(u1 == 0.0):  # pragma: no cover - probability 2**-53 per draw
            raise ArithmeticError("SplitMix64 drew u1 == 0; the scalar generator would redraw")
        return (np.sqrt(-2.0 * np.log(u1)) * np.cos(2.0 * math.pi * u2)).reshape(shape)


# ------------------------------------------------------------------- cells --
@dataclass(frozen=True)
class Cell:
    case: str  # R3-A .. R3-E
    index: int  # cell index within the case (seed offset)
    name: str
    G: int
    generator: str
    procedure: str  # P-IND / P-BLK
    domain_class: str  # claimed / candidate / stress
    n: int = N_SIZE
    params: dict[str, Any] = field(default_factory=dict)
    kind: str = "size"  # size / power
    note: str = ""

    @property
    def seed(self) -> int:
        return SEED_BASE + 100 * CASE_INDEX[self.case] + self.index

    @property
    def id(self) -> str:
        return f"{self.case}/{self.index:02d}/{self.name}"


def scales(g: int) -> np.ndarray:
    """Session scales log-spaced from 1 to 5 in the fixed cyclic order (F3 b0 / b1)."""
    return np.exp(np.linspace(0.0, math.log(SCALE_HI), g))


def _cells() -> list[Cell]:
    out: list[Cell] = []
    # R3-A serially correlated sessions.  One panel per (rho, G); both procedures read it.
    i = 0
    for rho in (0.0, 0.1, 0.2, 0.3, 0.5):
        for g in (10, 20, 30, 60):
            for proc in ("P-IND", "P-BLK"):
                if proc == "P-BLK" and g == 10:
                    continue  # B = 2 < 4: refused by the gate, no cell
                if proc == "P-IND":
                    cls = "claimed" if rho == 0.0 else "stress"
                else:
                    cls = "candidate" if rho <= 0.3 else "stress"
                out.append(
                    Cell(
                        "R3-A",
                        i,
                        f"ar1_rho{rho:g}_G{g}_{proc}",
                        g,
                        "ar1_gaussian_stationary",
                        proc,
                        cls,
                        params={"rho": rho},
                    )
                )
                i += 1
    # R3-B common-factor dependence across hypotheses (all D-IND, must pass).
    i = 0
    for rho_h in (0.0, 0.3, 0.6, 0.9):
        for g in (10, 30):
            out.append(
                Cell(
                    "R3-B",
                    i,
                    f"equicorr_rhoh{rho_h:g}_G{g}",
                    g,
                    "normal_equicorrelated",
                    "P-IND",
                    "claimed",
                    params={"rho_h": rho_h},
                )
            )
            i += 1
    # R3-C heteroskedastic and heavy-tailed outcomes.
    i = 0
    for g in (10, 15, 20, 30, 45, 60, 90):
        out.append(
            Cell(
                "R3-C",
                i,
                f"normal_homoskedastic_G{g}",
                g,
                "normal_homoskedastic",
                "P-IND",
                "claimed",
                note="tier 1 (a)",
            )
        )
        i += 1
    for g in (10, 15, 20, 30, 45, 60, 90):
        out.append(
            Cell(
                "R3-C",
                i,
                f"normal_heteroskedastic_G{g}",
                g,
                "normal_heteroskedastic_logspaced_1_5_cyclic",
                "P-IND",
                "claimed",
                note="tier 1 (b): claimed for this generator, arrangement and G only",
            )
        )
        i += 1
    for dist in ("t3", "lognormal"):
        for g in (10, 30):
            out.append(
                Cell(
                    "R3-C",
                    i,
                    f"{dist}_heteroskedastic_G{g}",
                    g,
                    f"{dist}_heteroskedastic_logspaced_1_5_cyclic",
                    "P-IND",
                    "stress",
                    note="tier 2, withdrawn for v1.19 (R0-1): exploratory, no claim",
                )
            )
            i += 1
    for g in (10, 30):
        out.append(
            Cell(
                "R3-C",
                i,
                f"power_normal_homoskedastic_G{g}",
                g,
                "normal_homoskedastic",
                "P-IND",
                "claimed",
                n=N_POWER,
                kind="power",
                params={"planted_hypothesis": 0, "theoretical_power": 0.90},
                note="power acceptance applies to these two cells only",
            )
        )
        i += 1
    # R3-D (i) clean selection: 16 candidates on a calibration panel, best 3 registered,
    # tested on an independent confirmation panel at G = 20.
    out.append(
        Cell(
            "R3-D",
            0,
            "clean_selection_best3_G20",
            20,
            "normal_homoskedastic",
            "P-IND",
            "claimed",
            params={"select": 3},
            note="R3-D (i): calibration and confirmation panels independent; family K = 3",
        )
    )
    # R3-E malformed, missing and unequal samples: the two size cells at G = 20.
    out.append(
        Cell(
            "R3-E",
            0,
            "missing_10pct_dropped_G20",
            20,
            "normal_homoskedastic",
            "P-IND",
            "claimed",
            params={"attempted": 22, "missing": 2},
            note="R3-E: 22 sessions attempted, 2 fail m_min and are dropped by the rule; G = 20 reached",
        )
    )
    out.append(
        Cell(
            "R3-E",
            1,
            "unequal_usable_counts_G20",
            20,
            "normal_homoskedastic",
            "P-IND",
            "claimed",
            params={"usable_units_range": [60, 180]},
            note="R3-E: unequal usable counts, all above m_min",
        )
    )
    return out


CELLS: tuple[Cell, ...] = tuple(_cells())


def cell_by_id(cell_id: str) -> Cell:
    for c in CELLS:
        if c.id == cell_id:
            return c
    raise KeyError(cell_id)


# ------------------------------------------------------------- generators --
def _ar1(z: np.ndarray, rho: float) -> np.ndarray:
    """x_1 = z_1; x_t = rho x_{t-1} + sqrt(1 - rho^2) z_t (stationary, unit variance)."""
    if rho == 0.0:
        return z
    x = np.empty_like(z)
    x[:, 0] = z[:, 0]
    c = math.sqrt(1.0 - rho * rho)
    for t in range(1, z.shape[1]):
        x[:, t] = rho * x[:, t - 1] + c * z[:, t]
    return x


def _t3_unit(stream: Stream, z: np.ndarray) -> np.ndarray:
    """Student t3 scaled to unit variance: z / sqrt(chi2_3 / 3) / sqrt(3) (F3 b1)."""
    c = (stream.normals(*z.shape, 3) ** 2).sum(axis=-1)
    return z / np.sqrt(c / 3.0) / math.sqrt(3.0)


def _lognormal_unit(z: np.ndarray) -> np.ndarray:
    """Centred lognormal(sigma = 1) scaled to unit variance (F3 b1)."""
    return (np.exp(z) - math.exp(0.5)) / math.sqrt((math.e - 1.0) * math.e)


def generate(cell: Cell) -> dict[str, Any]:
    """The cell's panel(s) and session metadata.  Returns ``panel`` of shape
    ``(n, G_attempted, K)`` (true mean 0 everywhere except the planted hypothesis
    of a power cell), ``sessions`` (the session records without values: id, and the
    m_min fields for R3-E) and, for R3-D (i), the ``calibration`` panel."""
    s = Stream(cell.seed)
    n, g = cell.n, cell.G
    gen = cell.generator
    out: dict[str, Any] = {"seed": cell.seed}
    if cell.case == "R3-E" and cell.index == 0:
        attempted = int(cell.params["attempted"])
        z = s.normals(n, attempted, K)
        missing = int(cell.params["missing"])
        # the missing sessions are the fixed positions 7 and 15 of the attempted
        # window (they fail the registered usable-unit count and are dropped)
        drop = {7, 15}
        assert len(drop) == missing
        sessions = [
            {
                "session": f"2026-03-{t + 1:02d}",
                "usable_units": 30 if t in drop else 120,
                "coverage_time_min": 300.0,
                "coverage_grid": 0.8,
            }
            for t in range(attempted)
        ]
        out.update(panel=z, sessions=sessions, dropped_positions=sorted(drop))
        return out
    if cell.case == "R3-E" and cell.index == 1:
        z = s.normals(n, g, K)
        lo, hi = cell.params["usable_units_range"]
        counts = np.linspace(lo, hi, g).round().astype(int)
        sessions = [
            {
                "session": f"2026-03-{t + 1:02d}",
                "usable_units": int(counts[(t * 7) % g]),
                "coverage_time_min": 300.0,
                "coverage_grid": 0.8,
            }
            for t in range(g)
        ]
        out.update(panel=z, sessions=sessions)
        return out
    if cell.case == "R3-D":
        cal = s.normals(n, g, K)
        con = s.normals(n, g, K)
        out.update(calibration=cal, panel=con, sessions=_plain_sessions(g))
        return out
    z = s.normals(n, g, K)
    if gen == "ar1_gaussian_stationary":
        x = _ar1(z, float(cell.params["rho"]))
    elif gen == "normal_equicorrelated":
        rho_h = float(cell.params["rho_h"])
        f = s.normals(n, g, 1)
        x = math.sqrt(rho_h) * f + math.sqrt(1.0 - rho_h) * z
    elif gen == "normal_homoskedastic":
        x = z
    elif gen == "normal_heteroskedastic_logspaced_1_5_cyclic":
        x = z * scales(g)[None, :, None]
    elif gen == "t3_heteroskedastic_logspaced_1_5_cyclic":
        x = _t3_unit(s, z) * scales(g)[None, :, None]
    elif gen == "lognormal_heteroskedastic_logspaced_1_5_cyclic":
        x = _lognormal_unit(z) * scales(g)[None, :, None]
    else:
        raise ValueError(gen)
    if cell.kind == "power":
        j = int(cell.params["planted_hypothesis"])
        x = x.copy()
        x[:, :, j] += planted_effect(cell)
    out.update(panel=x, sessions=_plain_sessions(g))
    return out


def _plain_sessions(g: int) -> list[dict[str, Any]]:
    return [{"session": f"2026-03-{t + 1:02d}"} for t in range(g)]


def planted_effect(cell: Cell) -> float:
    """The planted mean (sd units) of a power cell: the effect at which the one-sided
    Student t on G values at the Holm floor ``alpha_op / K`` has the cell's
    theoretical power (0.90) under the reference model, by inversion of the
    noncentral t.  The power acceptance counts detections of that hypothesis."""
    from scipy import optimize, stats

    g = cell.G
    target = float(cell.params["theoretical_power"])
    tcrit = float(stats.t.isf(ALPHA_OP / K, g - 1))

    def f(delta: float) -> float:
        return float(stats.nct.sf(tcrit, g - 1, delta * math.sqrt(g))) - target

    return float(optimize.brentq(f, 1e-9, 50.0, xtol=1e-13))


def clopper_pearson_upper(k: int, n: int, level: float = 0.95) -> float:
    """One-sided upper bound ``scipy.stats.beta.ppf(level, k + 1, n - k)`` (R0 section 2)."""
    from scipy import stats

    if k >= n:
        return 1.0
    return float(stats.beta.ppf(level, k + 1, n - k))


def clopper_pearson_lower(k: int, n: int, level: float = 0.95) -> float:
    from scipy import stats

    if k <= 0:
        return 0.0
    return float(stats.beta.ppf(1.0 - level, k, n - k + 1))
