"""Planted-signal power study on REAL data: how often does the validation
chain detect an effect of known size when the noise is a real market's?

:mod:`iap.research.power` answers the power question on synthetic data,
whose noise the generator chose.  This module asks it of an ingested real
dataset (docs/REAL_DATA.md), so that a REJECT on real data can be read as
"no signal" or "no power" — the question a handful of real sessions makes
pressing.

**Design (pinned, version :data:`REAL_POWER_VERSION`).**

1. *Planting in labels.*  Real prices cannot be re-generated, so the effect
   is planted in the stored LABELS of the feature rows: for detector alpha
   ``A`` at horizon ``h`` with raw signal ``x`` on one instrument,

       y'  =  y  +  c * z(x),      c = rho * sd(y) / sqrt(1 - rho^2)

   where ``y`` is the scored mid label, ``z`` the instrument's standardised
   signal over its scored rows and ``rho`` the grid LEVEL — the planted
   information coefficient.  When ``y`` carries nothing about ``x`` (the
   ``shifted`` background) the planted IC is ``rho`` exactly in
   expectation.  The same ``c * z(x)`` is added to ``label_cost_<h>`` and,
   where finite, ``label_reopen_<h>``, so the scored label is moved
   consistently whatever row policy reads it.  Levels are IC values,
   pinned before any result was seen (:data:`DEFAULT_LEVELS`).
2. *Backgrounds.*
   * ``shifted`` — every label column of an (instrument, session) is
     circularly shifted by a seeded offset in [1/4, 3/4] of the session's
     rows.  This keeps the real labels' distribution, tails,
     autocorrelation and blackout structure and breaks their alignment with
     the features: the null is a real-noise null.  Replicated over
     ``n_seeds`` offsets; every rate carries its Wilson 95 % interval.
   * ``real`` — the labels as they are.  One run per level (nothing is
     random); its level-0 row IS the real-data result of the detector, and
     a planted effect adds to whatever the market already holds.
3. *Scenarios.*  ``stable`` plants at every session prefix of the grid;
   ``break`` reverses the planted sign from the middle session on and is
   evaluated at the full session count only (as in :mod:`power`).
4. *Detectors.*  EQ04 (trade imbalance) and EQ10 (ETF lead-lag) at their
   DECLARED horizons, planted independently at those horizons.  There is no
   matched horizon: the effect is planted at the horizon it is tested at.
5. *Chain.*  :func:`iap.research.power.evaluate_run` — ``validate_alpha``
   under the default method bundle and the same three thresholds
   (``fixed`` 3.0, ``gate`` in force, ``study`` Bonferroni of this study's
   own tests), detection = gate IC > 0 and pooled HAC t >= threshold.
   The recompute leakage probe does NOT run: it rebuilds labels from the
   normalized events and would rightly flag the planted labels.

**What it does not show.**  The backtester prices fills at the real mids,
which the planted labels do not move, so the P&L gates, the PROMOTE verdict
and the P&L bootstrap measure the real market, not the planted effect: the
study measures STATISTICAL detection power only.  Planting into labels also
skips how a real effect would move features (impact on the book).  On a
handful of sessions the shifted offsets are not fully independent, so the
intervals are optimistic about their own width.

Outputs ``REAL_POWER_REPORT.md`` / ``.json`` — deterministic.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from iap.alpha import configure_universe
from iap.alpha.data import load_features
from iap.core.rng import SplitMix64
from iap.experiment.locking import atomic_write_text
from iap.labels.frames import scored_labels
from iap.research import power_stats
from iap.research.errors import ResearchError
from iap.research.power import (
    BREAK_AT_FRACTION,
    EFFECT_ALPHAS,
    _model,
    _rounded,
    detector_id,
    evaluate_run,
    first_sessions,
    gate_looks_in_force,
    study_seeds,
    summarise,
)
from iap.validation.ledger import ExperimentLedger
from iap.validation.validate import GATES

__all__ = [
    "BACKGROUNDS",
    "DEFAULT_LEVELS",
    "DEFAULT_SEEDS",
    "REAL_POWER_VERSION",
    "plant",
    "render_markdown",
    "run_real_power_study",
    "session_days",
    "shift_labels",
    "write_reports",
]

REAL_POWER_VERSION = 1

#: planted information coefficients (level 0 = the null row)
DEFAULT_LEVELS = (0.0, 0.005, 0.01, 0.02, 0.04)

#: the break scenario's planted IC
DEFAULT_BREAK_LEVELS = (0.02,)

DEFAULT_SEEDS = 20

BACKGROUNDS = ("shifted", "real")

_ROUND = 6

_FEATURE_COL = re.compile(r"_v\d+$")

#: feature columns the backtester / validator read besides the alphas' own
_MARKET_COLS = frozenset(
    {
        "mid_price_v1",
        "spread_ticks_v1",
        "vol_regime_flag_v1",
        "depth_bid_l1_v1",
        "depth_ask_l1_v1",
    }
)


def _fail(message: str) -> ResearchError:
    return ResearchError(message, code="invalid_spec")


def detectors() -> list[dict[str, str]]:
    """EQ04 and EQ10 at their declared horizons."""
    out = []
    for effect, alpha_id in sorted(EFFECT_ALPHAS.items()):
        horizon = _model(alpha_id, _declared(alpha_id)).horizon
        out.append(
            {
                "id": detector_id(effect, alpha_id, horizon),
                "effect": effect,
                "alpha_id": alpha_id,
                "horizon": horizon,
                "kind": "declared",
            }
        )
    return out


def _declared(alpha_id: str) -> str:
    from iap.alpha import build

    return build(alpha_id).horizon


def session_days(frames: Mapping[int, pd.DataFrame]) -> list[int]:
    """Sorted UTC day indices present in any frame."""
    days: set[int] = set()
    for df in frames.values():
        ts = df["exchange_ts"].to_numpy(dtype=np.int64)
        days.update(int(d) for d in np.unique(ts // power_stats.DAY_NS))
    return sorted(days)


def _label_columns(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if c.startswith("label_")]


def shift_labels(frames: Mapping[int, pd.DataFrame], seed: int) -> dict[int, pd.DataFrame]:
    """The ``shifted`` background: every label column of each (instrument,
    session) circularly shifted by one seeded offset in [n/4, 3n/4]."""
    rng = SplitMix64(seed)
    out: dict[int, pd.DataFrame] = {}
    for iid in sorted(frames):
        df = frames[iid].copy()
        cols = _label_columns(df)
        day = df["exchange_ts"].to_numpy(dtype=np.int64) // power_stats.DAY_NS
        for d in np.unique(day):
            idx = np.flatnonzero(day == d)
            n = len(idx)
            if n < 4:
                continue
            offset = n // 4 + rng.below(max(1, n // 2))
            for c in cols:
                values = df[c].to_numpy().copy()
                values[idx] = np.roll(values[idx], offset)
                df[c] = values
        out[iid] = df
    return out


def plant(
    frames: Mapping[int, pd.DataFrame],
    dets: Sequence[Mapping[str, str]],
    level: float,
    break_from_day: int | None = None,
) -> dict[int, pd.DataFrame]:
    """Add ``c * z(x)`` to every detector's labels (item 1 of the module
    docs); with ``break_from_day`` the sign is reversed on that UTC day and
    after."""
    if not 0.0 <= level < 1.0:
        raise _fail(f"level must be an IC in [0, 1): {level}")
    out = {iid: frames[iid].copy() for iid in frames}
    if level == 0.0:
        return out
    scale = level / math.sqrt(1.0 - level * level)
    for det in dets:
        h = det["horizon"]
        model = _model(det["alpha_id"], h)
        signals = model.signals(out)
        for iid in sorted(signals):
            df = out[iid]
            x = signals[iid].to_numpy(dtype=float)
            y, _ = scored_labels(df, h)
            both = np.isfinite(x) & np.isfinite(y)
            if both.sum() < 3:
                continue
            sx, sy = float(np.std(x[both])), float(np.std(y[both]))
            if sx == 0.0 or sy == 0.0:
                continue
            z = (x - float(np.mean(x[both]))) / sx
            delta = np.where(np.isfinite(z), scale * sy * z, 0.0)
            if break_from_day is not None:
                day = df["exchange_ts"].to_numpy(dtype=np.int64) // power_stats.DAY_NS
                delta = np.where(day >= break_from_day, -delta, delta)
            for col in (f"label_mid_{h}", f"label_cost_{h}", f"label_reopen_{h}"):
                if col in df.columns:
                    v = df[col].to_numpy(dtype=float)
                    df[col] = np.where(np.isfinite(v), v + delta, v)
    return out


def run_real_power_study(
    dataset_dir: Path,
    levels: Sequence[float] = DEFAULT_LEVELS,
    break_levels: Sequence[float] = DEFAULT_BREAK_LEVELS,
    n_seeds: int = DEFAULT_SEEDS,
    sessions: Sequence[int] | None = None,
    base_seed: int = 20261004,
    gate_looks: int | None = None,
    reports_dir: Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run the grid on ``dataset_dir`` and return the report document.

    ``sessions`` defaults to 2, 4, ... and the dataset's session count.
    ``gate_looks`` defaults to the count the committed promotion reports
    were judged at (``reports_dir``, default research/alpha_reports of the
    checkout)."""
    dataset_dir = Path(dataset_dir)
    configs_dir = dataset_dir / "configs"
    manifest = json.loads((dataset_dir / "dataset.json").read_text(encoding="utf-8"))
    configure_universe(configs_dir / "instruments" / "instruments.json")
    dets = detectors()
    needed = _MARKET_COLS | {f for d in dets for f in _model(d["alpha_id"], d["horizon"]).features}

    def _slim(names):
        # drop feature columns no detector reads; keep ids, labels, market columns
        return [n for n in names if not _FEATURE_COL.search(n) or n in needed]

    frames = load_features(dataset_dir / "features", columns=_slim)
    days = session_days(frames)
    if len(days) < 2:
        raise _fail(f"{dataset_dir}: needs >= 2 sessions, has {len(days)}")
    levels = [float(v) for v in levels]
    break_levels = [float(v) for v in break_levels]
    if not levels or sorted(set(levels)) != levels:
        raise _fail("levels must be a non-empty strictly increasing list")
    grid_sessions = (
        sorted({*range(2, len(days), 2), len(days)}) if sessions is None else list(sessions)
    )
    if not grid_sessions or grid_sessions[-1] > len(days) or grid_sessions[0] < 2:
        raise _fail(f"sessions must be within [2, {len(days)}]")
    full = grid_sessions[-1]
    if gate_looks is None:
        repo = Path(__file__).resolve().parents[4]
        gate_looks = gate_looks_in_force(reports_dir or repo / "research" / "alpha_reports")
    seeds = study_seeds(base_seed, n_seeds)
    # (background, scenario, level, seeds, session prefixes)
    grid: list[tuple[str, str, float, list[int], list[int]]] = []
    for level in levels:
        grid.append(("shifted", "stable", level, seeds, grid_sessions))
        grid.append(("real", "stable", level, [0], grid_sessions))
    for level in break_levels:
        grid.append(("shifted", "break", level, seeds, [full]))
    n_tests = 2 * len(dets) * sum(len(s) * len(p) for _, _, _, s, p in grid)
    thresholds = _rounded(
        {
            "fixed": float(GATES["min_nw_tstat"]),
            "gate": float(ExperimentLedger.bonferroni_t_threshold_at(gate_looks)),
            "study": float(ExperimentLedger.bonferroni_t_threshold_at(n_tests)),
        }
    )
    promote_t = max(thresholds.values())
    break_day = days[min(len(days) - 1, int(math.floor(full * BREAK_AT_FRACTION)))]

    runs: list[dict[str, Any]] = []
    for background, scenario, level, run_seeds, prefixes in grid:
        for seed in run_seeds:
            base = shift_labels(frames, seed) if background == "shifted" else frames
            planted = plant(
                base, dets, level, break_from_day=break_day if scenario == "break" else None
            )
            evaluations = [
                {
                    "sessions": int(n),
                    "detectors": evaluate_run(
                        first_sessions(planted, n), configs_dir, promote_t, dets, seed=seed
                    ),
                }
                for n in prefixes
            ]
            run = _rounded(
                {
                    "scenario": f"{background}:{scenario}",
                    "level": level,
                    "seed": seed,
                    "evaluations": evaluations,
                }
            )
            runs.append(run)
            if progress is not None:
                last = run["evaluations"][-1]
                progress(
                    f"{run['scenario']} ic {level:g} seed {seed} ({last['sessions']} sessions): "
                    + ", ".join(
                        f"{d} t={row.get('t_pooled')}"
                        for d, row in sorted(last["detectors"].items())
                    )
                )
    cells = summarise(runs, thresholds)
    return _rounded(
        {
            "x-version": REAL_POWER_VERSION,
            "description": (
                "Planted-signal power study on real data (iap.research.power_real): "
                "detection rates of validate_alpha when an effect of known IC is planted "
                "in the labels of an ingested real dataset. Statistical detection only: "
                "P&L gates see the real market. Deterministic."
            ),
            "dataset_version": manifest["dataset_version"],
            "session_dates": [str(np.datetime64(d, "D")) for d in days],
            "instruments": sorted(int(i) for i in frames),
            "levels": levels,
            "break_levels": break_levels,
            "sessions": grid_sessions,
            "base_seed": base_seed,
            "seeds": seeds,
            "backgrounds": list(BACKGROUNDS),
            "detectors": dets,
            "protocol": {
                "thresholds": thresholds,
                "n_tests": n_tests,
                "gate_looks": int(gate_looks),
                "promote_t_threshold": promote_t,
                "break_from": str(np.datetime64(break_day, "D")),
                "recompute_probe": False,
                "interval": "wilson_95",
            },
            "cells": cells,
            "runs": runs,
        }
    )


def _rate(rate: Mapping[str, Any], n: int) -> str:
    if rate["rate"] is None:
        return "-"
    return f"{rate['k']}/{n} ({rate['lo']:.2f}-{rate['hi']:.2f})"


def _num(v: Any, spec: str = ".2f") -> str:
    return "-" if v is None else format(v, spec)


def render_markdown(doc: Mapping[str, Any]) -> str:
    p = doc["protocol"]
    th = p["thresholds"]
    lines = [
        "# Planted-signal power on real data",
        "",
        f"Dataset `{doc['dataset_version'][:12]}`: {len(doc['session_dates'])} sessions "
        f"({doc['session_dates'][0]} .. {doc['session_dates'][-1]}), instruments "
        f"{doc['instruments']}. Generated by `python -m iap.research power-real`; "
        "method in `iap.research.power_real`.",
        "",
        "An effect of known information coefficient (IC) is planted in the labels and "
        "the validation chain is asked to find it. `shifted` = real labels circularly "
        "shifted within each session (real noise, no real signal), replicated over "
        f"{len(doc['seeds'])} seeds; `real` = the labels as they are, one run, so its "
        "IC 0 row is the detector's real-data result.",
        "",
        f"Detection: gate IC > 0 and pooled HAC t >= threshold. Thresholds: fixed "
        f"{th['fixed']:.2f}, gate in force {th['gate']:.2f} ({p['gate_looks']:,} looks), "
        f"study {th['study']:.2f} ({p['n_tests']:,} tests).",
        "",
        "**Read with:** statistical detection only — fills are priced at real mids the "
        "planted labels do not move, so verdicts and P&L gates are about the real market. "
        "The recompute leakage probe is off (it would flag the planted labels).",
        "",
        "| detector | background | scenario | IC | sessions | detected @gate | "
        "@fixed | @study | mean t | mean gate IC |",
        "|---|---|---|---:|---:|---|---|---|---:|---:|",
    ]
    for c in doc["cells"]:
        background, scenario = c["scenario"].split(":")
        n = c["n_runs"]
        lines.append(
            f"| {c['detector']} | {background} | {scenario} | {c['level']:g} | "
            f"{c['sessions']} | {_rate(c['sig_gate'], n)} | {_rate(c['sig_fixed'], n)} | "
            f"{_rate(c['sig_study'], n)} | {_num(c['mean_t_pooled'])} | "
            f"{_num(c['mean_gate_ic'], '.4f')} |"
        )
    lines += [
        "",
        "Break rows: the planted sign reverses from "
        f"{p['break_from']}; `break_gate` counts |break z| >= the gate threshold "
        "(see REAL_POWER_REPORT.json).",
        "",
    ]
    return "\n".join(lines)


def write_reports(doc: Mapping[str, Any], out_dir: Path) -> dict[str, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {"md": out_dir / "REAL_POWER_REPORT.md", "json": out_dir / "REAL_POWER_REPORT.json"}
    atomic_write_text(paths["md"], render_markdown(doc))
    atomic_write_text(
        paths["json"],
        json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n",
    )
    return paths
