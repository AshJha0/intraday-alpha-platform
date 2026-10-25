"""Committed synthetic fixtures for the H1 reference implementation and the H1-equivalence test.

The fixture is a deterministic function of its ``seed`` and ``scenario``:
a self-contained SplitMix64 (the platform's pinned generator, copied here so
this folder imports nothing from ``iap``), uniform draws turned into
approximately normal shocks by a 12-fold Irwin-Hall sum (additions only), and
prices built multiplicatively (``mid *= 1 + r``), so no transcendental
function enters and the bytes are the same on every IEEE-754 platform.
``manifest.json`` records the sha256 of every array; ``test_h1_equivalence``
re-derives it before anything else runs.

Market (E0 section 3's model, planted): market factor ``r_m``, four sector
factors ``s_j`` (sector ETF return = 0.9 r_m + s_j, so the orthogonalisation
matters), and per name ``r_i = b_m r_m + b_s s_j + dx_i`` with ``x_i`` an
Ornstein-Uhlenbeck residual (``x <- (1 - kappa) x + sigma u``): a positive
``kappa`` plants reversal at every horizon; the ``no_reversal`` scenario sets
``kappa = 0`` so both horizons fail E0 section 4's quality rule and the
engine must trade nothing (fail closed).

Planted failure cases (E0 sections 3 and 9): a stale quote (7 s) for one name
at one decision; a quote that disappears before arrival (data gap); a halt
through three arrivals in life and, for another name, through the close
(a residual carried overnight); a displayed-depth collapse at one arrival; one name with missing bars in six of the estimation
sessions (below 15 valid sessions in the first trading session, eligible from
the second); thin displayed depth and thin 5-minute volume in a few names so
partial fills, participation scaling, `closing` residuals and a pre-close
depth exhaustion with an overnight carry occur.

Grid: ``bars_per_session`` 5-minute bars per session (a two-hour synthetic
session), 20 estimation sessions (the development subset) followed by 3
trading sessions.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np

_MASK64 = (1 << 64) - 1


class SplitMix64:
    """PLATFORM_CONVENTIONS section 3's pinned generator (copied; this folder does not import iap)."""

    def __init__(self, seed: int) -> None:
        self.state = seed & _MASK64

    def next_u64(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & _MASK64
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK64
        return z ^ (z >> 31)

    def uniform(self) -> float:
        return (self.next_u64() >> 11) * 2.0**-53

    def normal(self) -> float:
        """Irwin-Hall(12) - 6: approximately N(0, 1) using additions only (platform-exact)."""
        s = 0.0
        for _ in range(12):
            s += self.uniform()
        return s - 6.0


CONFIG: dict[str, Any] = {
    "capital": 1_000_000.0,
    "horizons": {
        "15m": {"formation_bars": 3, "holding_bars": 3, "live_cohorts": 3},
        "30m": {"formation_bars": 6, "holding_bars": 6, "live_cohorts": 6},
    },
    "bar_ns": 300 * 10**9,
    "session_ns": 86_400 * 10**9,
    "t0_ns": 1_793_025_000_000_000_000,  # 2026-10-26 14:30:00 UTC
    "bars_per_session": 24,
    "n_estimation": 20,
    "n_trading": 3,
    "beta_window_sessions": 20,
    "min_valid_sessions": 15,
    "beta_shrinkage": 0.3,
    "condition_bound": 1e8,
    "vol_floor_percentile": 5.0,
    "quote_stale_s": 5.0,
    "m_min": 1000,
    "tails": 0.2,
    "min_names_per_leg": 10,
    "beta_tol": 0.05,
    "sector_cap": 0.02,
    "name_cap": 0.02,
    "min_deployed_fraction": 0.5,
    "max_name_share_of_gross": 0.15,
    "participation_cap": 0.05,
    "min_ticket_shares": 10,
    "hurdle_multiple": 1.5,
    "band_bps": 2.0,
    "book_net_tol": 0.002,  # per live cohort: share rounding leaves |net| <= sum(mid / 2C); scaled by live cohorts
    "latency_ns": 500_000_000,
    "fee_per_share": 0.0005,
    "impact_c_depth": 0.0002,
    "impact_c_volume": 0.0010,
    "column": {"name": "conservative", "impact_multiplier": 1.5, "depth_haircut": 0.75},
    "stress_column": {"name": "stress", "impact_multiplier": 2.0, "depth_haircut": 0.5},
    "solver": {"method": "SLSQP", "constraint_tol": 1e-9, "ftol": 1e-15, "maxiter": 1000},
}

SCENARIOS = {"reversal": {"kappa": 0.5}, "no_reversal": {"kappa": 0.0}}
N_NAMES = 80
N_SECTORS = 4


def build(seed: int = 20261025, scenario: str = "reversal") -> dict[str, Any]:
    """The fixture as plain Python/numpy data (see the module docstring)."""
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}")
    kappa = SCENARIOS[scenario]["kappa"]
    rng = SplitMix64(seed)
    cfg = dict(CONFIG)
    B = cfg["bars_per_session"]
    S = cfg["n_estimation"] + cfg["n_trading"]
    names = [f"N{i:02d}" for i in range(N_NAMES)]
    sectors = {n: f"S{i % N_SECTORS}" for i, n in enumerate(names)}
    market = "MKT"
    sector_etfs = {f"S{j}": f"SEC{j}" for j in range(N_SECTORS)}
    b_m = {n: 0.6 + 0.8 * rng.uniform() for n in names}
    b_s = {n: 0.3 + 0.9 * rng.uniform() for n in names}
    sigma = {n: (25.0 + 20.0 * rng.uniform()) * 1e-4 for n in names}
    base = {n: 20.0 + 180.0 * rng.uniform() for n in names}
    base[market] = 400.0
    for e in sector_etfs.values():
        base[e] = 100.0 + 50.0 * rng.uniform()
    spread_bps = {n: 1.0 + 1.5 * rng.uniform() for n in names}
    depth = {n: 500 + int(5500 * rng.uniform()) for n in names}
    volume_5m = {n: 20_000 + int(180_000 * rng.uniform()) for n in names}
    for n in ("N05", "N23", "N41"):
        depth[n] = 120  # thin touch: partial fills, residuals
    for n in ("N08", "N30"):
        volume_5m[n] = 2_000  # thin volume: participation scaling (5 % = 100 shares)
    all_series = [market] + [sector_etfs[f"S{j}"] for j in range(N_SECTORS)] + names
    level = {n: base[n] for n in all_series}
    x = {n: 0.0 for n in names}
    mids = np.empty((S, B, len(all_series)))
    drift = np.empty((S, B, len(names)))
    idx = {n: k for k, n in enumerate(all_series)}
    for s in range(S):
        for k in range(B):
            if k == 0 and s > 0:
                # overnight: one larger market move, residuals carried
                r_m = 3.0e-3 * rng.normal()
            else:
                r_m = 1.0e-3 * rng.normal()
            s_j = [0.6e-3 * rng.normal() for _ in range(N_SECTORS)]
            level[market] *= 1.0 + r_m
            for j in range(N_SECTORS):
                level[sector_etfs[f"S{j}"]] *= 1.0 + (0.9 * r_m + s_j[j])
            for n in names:
                j = int(sectors[n][1])
                u = rng.normal()
                x_new = (1.0 - kappa) * x[n] + sigma[n] * u
                dx = x_new - x[n]
                x[n] = x_new
                r_i = b_m[n] * r_m + b_s[n] * s_j[j] + dx
                level[n] *= 1.0 + r_i
            for n in all_series:
                mids[s, k, idx[n]] = level[n]
            for n in names:
                drift[s, k, idx[n] - 1 - N_SECTORS] = 1.0e-4 * rng.normal()
    # planted data faults
    for s in range(6):
        mids[s, :, idx["N79"]] = np.nan  # below 15 valid sessions in trading session 20's window
    E = cfg["n_estimation"]
    stale = [[E + 0, 5, "N07", 7.0]]  # [session, bar, name, quote age in seconds]
    gaps = [[E + 1, 9, "N12"]]  # quote disappears before arrival
    halts = [
        [E + 2, 14, "N03"],
        [E + 2, 15, "N03"],
        [E + 2, 16, "N03"],
        [E + 0, 22, "N17"],
        [E + 0, 23, "N17"],
    ]
    depth_overrides = [[E + 1, 12, "N05", 20]]  # [session, bar, name, displayed depth at arrival]
    return {
        "config": cfg,
        "scenario": scenario,
        "seed": seed,
        "names": names,
        "sectors": sectors,
        "market": market,
        "sector_etfs": sector_etfs,
        "series": all_series,
        "mids": mids,  # (session, bar, series) mid prices; NaN = missing bar
        "arrival_drift": drift,  # (session, bar, name): mid_arrival = mid * (1 + drift)
        "spread_bps": spread_bps,
        "depth": depth,
        "volume_5m": volume_5m,
        "stale": stale,
        "gaps": gaps,
        "halts": halts,
        "depth_overrides": depth_overrides,
        "true_params": {"b_m": b_m, "b_s": b_s, "sigma": sigma, "kappa": kappa},
    }


def digest(fx: dict[str, Any]) -> dict[str, str]:
    """sha256 of every array and declared table of the fixture (what manifest.json records)."""
    h: dict[str, str] = {}
    for key in ("mids", "arrival_drift"):
        a = np.ascontiguousarray(fx[key], dtype="<f8")
        h[key] = hashlib.sha256(a.tobytes()).hexdigest()
    tables = {
        k: fx[k]
        for k in (
            "names",
            "sectors",
            "spread_bps",
            "depth",
            "volume_5m",
            "stale",
            "gaps",
            "halts",
            "depth_overrides",
            "config",
            "scenario",
            "seed",
        )
    }
    h["tables"] = hashlib.sha256(
        json.dumps(tables, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return h


def session_start_ns(fx: dict[str, Any], s: int) -> int:
    return fx["config"]["t0_ns"] + s * fx["config"]["session_ns"]


def bar_ts(fx: dict[str, Any], s: int, k: int) -> int:
    return session_start_ns(fx, s) + k * fx["config"]["bar_ns"]


MANIFEST = Path(__file__).resolve().parent / "fixtures" / "manifest.json"


def write_manifest() -> dict[str, Any]:
    doc = {sc: digest(build(scenario=sc)) for sc in SCENARIOS}
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_bytes((json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return doc


if __name__ == "__main__":
    print(json.dumps(write_manifest(), indent=2))
