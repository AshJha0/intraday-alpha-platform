"""Planted-signal power study: how often does the validation chain detect
an effect of KNOWN size, on how many sessions, and where does the power go?

Every other research artefact in this repository asks "is there signal in
the data?" of data whose truth nobody knows.  This module asks the question
the answers depend on: *when a signal of size s is really there, how often
does* :func:`iap.validation.validate.validate_alpha` *say so — and when
nothing is there, how often does it say so anyway?*  Without that curve a
REJECT is uninterpretable (no signal, or no power?) and so is a PROMOTE.

**Design (pinned, version :data:`POWER_VERSION`).**

1. *Planted data.*  The synthetic generator's opt-in ``planted`` block
   (:mod:`iap.marketdata.generator`) plants two independent effects into the
   equity efficient prices and order flow, and can reverse both mid-sample:

   * ``order_flow`` — informed aggressor flow whose sign leads the mid along
     an exponential impact kernel; detected by the trade-flow alpha **EQ04**
     (10 s trade imbalance);
   * ``lead_lag`` — every constituent follows the ETF's efficient return
     with a lag; detected by the index lead-lag alpha **EQ10** (ETF 1 s
     return);
   * ``break`` — the reversal of both from the middle of the run on;
     detected by :func:`iap.research.power_stats.slope_break_z`.

   The base generator config is ``research/power/generator_planted.json``;
   its ``planted`` block is the REFERENCE effect (level 1.0) and its
   ``sessions`` the number of sessions generated.  A grid *level* L scales
   both reference strengths by L; level 0 is the null (the false-positive
   row).  The reference strengths are never tuned to the result.
2. *Sessions.*  The run is generated once at the largest session count of
   the grid and evaluated on its first n sessions for every n of the grid.
   That is exact, not an approximation: without a break the generator and
   the feature pipeline are causal across sessions, so the first n sessions
   of an N-session run ARE the n-session run of the same seed
   (``python/tests/test_power_detection.py`` proves it on the bytes).  The
   study writes its own reference tree with a calendar of as many weekdays
   as it needs (:func:`extended_trading_days`); ``configs/`` and the pinned
   dataset are not touched.
3. *Detectors.*  Each effect is tested by its flagship alpha at the alpha's
   DECLARED label horizon, and — when the planted mechanism plays out over a
   longer time than that — also at the MATCHED horizon,
   :func:`matched_horizon`: the shortest pinned label horizon that covers
   the mechanism (order flow: the time by which half the impact kernel has
   arrived; lead-lag: the lag plus the one-second signal window).  The rule
   reads the planted block only; it was not chosen from detection rates, and
   every detector, session count and break test is counted in the study's
   own Bonferroni denominator.
4. *Pipeline.*  A reduced universe (:data:`DEFAULT_UNIVERSE`) and the REAL
   chain end to end — generator, raw->normalized QC, the feature engine and
   label sweep, the alpha's own fit, and ``validate_alpha`` with the pinned
   research execution model under the default method bundle — in a scratch
   directory that is removed afterwards.  The recompute leakage probe, which
   costs as much as the rest of a validation, runs on the first seed of
   every cell at the full session count.  Nothing under ``data/`` or the
   ledger is touched.
5. *Replication.*  ``n_seeds`` generator seeds per cell from
   SplitMix64(base seed).  Runs are independent processes (``jobs``); the
   document is assembled in grid order, so it does not depend on ``jobs``.
6. *Thresholds.*  A detection needs gate IC > 0 and the pooled-slope HAC t
   (the statistic the PROMOTE gate reads) at or above

   * ``fixed`` — 3.0, for comparison;
   * ``gate``  — the PROMOTE threshold in force: the Bonferroni |t| at the
     look count the committed promotion reports were judged at
     (:func:`gate_looks_in_force`);
   * ``study`` — the Bonferroni |t| of this study's own tests.

   The verdict of every run is computed at the largest of the three, so the
   study never judges itself by a looser rule than the platform.
7. *Beside the chain.*  For every evaluation the study also computes, on
   every scored row and without a walk-forward split, the pooled slope of
   the label on the alpha's raw signal with session-clustered HAC errors
   (:func:`iap.research.power_stats.pooled_slope_session_hac`), its
   effective sample size, and the break z.  These explain the chain's t;
   they are not a second chance to detect.

A cell is (detector, scenario, level, sessions); every rate carries its
Wilson 95 % interval.  ``diagnosis`` sets the analytical IC of each
mechanism beside the measured one and accounts for the t; ``power_model``
fits ``t ~ N((kappa0 + kappa * level) * sqrt(sessions), sd)`` and derives the minimum
detectable effect at 80 % power and the sessions the reference effect needs.

Outputs: ``research/power/POWER_REPORT.md`` and ``POWER_REPORT.json`` —
deterministic (no wall clock, sorted keys, rounded floats).
"""

from __future__ import annotations

import contextlib
import copy
import datetime as _dt
import io
import json
import math
import os
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from iap.alpha import build
from iap.alpha.data import load_features
from iap.backtest import Backtester, CostModel
from iap.core.rng import SplitMix64
from iap.experiment.locking import atomic_write_text
from iap.labels.frames import scored_labels
from iap.labels.labels import HORIZON_ORDER, HORIZONS_NS
from iap.marketdata.generator import MarketDataGenerator, load_generator_config
from iap.marketdata.normalize import normalize_run
from iap.reference.refdata import ReferenceData
from iap.research import power_stats
from iap.research.errors import ResearchError
from iap.research.runner import load_instrument_meta
from iap.research.specs import DEFAULT_CONFIGURATION
from iap.validation.leakage import RecomputeSources
from iap.validation.ledger import ExperimentLedger
from iap.validation.methods import methods
from iap.validation.metrics import nw_lags
from iap.validation.validate import GATES, validate_alpha

__all__ = [
    "BREAK_AT_FRACTION",
    "DEFAULT_BREAK_LEVELS",
    "DEFAULT_LEVELS",
    "DEFAULT_SEEDS",
    "DEFAULT_UNIVERSE",
    "EFFECT_ALPHAS",
    "POWER_VERSION",
    "SCENARIOS",
    "TARGET_POWER",
    "build_planted_frames",
    "cell_config",
    "default_jobs",
    "default_session_grid",
    "detector_id",
    "detectors_for",
    "diagnose",
    "direct_statistics",
    "evaluate_run",
    "extended_trading_days",
    "first_sessions",
    "gate_looks_in_force",
    "matched_horizon",
    "power_model",
    "render_markdown",
    "run_power_study",
    "study_seeds",
    "summarise",
    "write_reports",
]

#: ``x-version`` of ``POWER_REPORT.json``: 3 since the detection-power
#: rework of v1.5.0 (session grid, 20 seeds with Wilson intervals, declared
#: and matched detectors, the gate threshold in force, break test,
#: ``diagnosis`` and ``power_model`` blocks).  2 was the first v1.5.0 report
#: (default method bundle, 3 seeds, 2 sessions); 1 the v1.3.0 / v1.4.0 one.
POWER_VERSION = 3

#: Bootstrap resamples per run for the net-P&L interval (kept small: the
#: study runs it once per evaluation and detector).
BOOTSTRAP_RESAMPLES = 300

#: Reduced universe of the study: two constituents and the ETF (the leader).
DEFAULT_UNIVERSE = ("SYN.EQ.001", "SYN.EQ.002", "SYN.ETF.IDX")

#: Multipliers of the reference planted effect; 0 is the null row.
DEFAULT_LEVELS = (0.0, 0.5, 1.0)

#: Levels the ``break`` scenario runs at.
DEFAULT_BREAK_LEVELS = (1.0,)

#: Generator seeds per cell.  With 20 a rate of 0.8 has a Wilson 95 %
#: interval of [0.58, 0.92]: it cannot be mistaken for one in three.
DEFAULT_SEEDS = 20

#: The power at which the minimum detectable effect is quoted.
TARGET_POWER = 0.8

#: planted effect -> the flagship alpha expected to detect it.
EFFECT_ALPHAS: dict[str, str] = {"order_flow": "EQ04", "lead_lag": "EQ10"}

#: Where the ``break`` scenario reverses the effect, and where
#: :func:`iap.research.power_stats.slope_break_z` splits every run.
BREAK_AT_FRACTION = 0.5

#: scenario -> the ``planted.break`` block it runs under.
SCENARIOS: dict[str, dict[str, Any]] = {
    "stable": {"at_fraction": None, "post_multiplier": 1.0},
    "break": {"at_fraction": BREAK_AT_FRACTION, "post_multiplier": -1.0},
}

#: One-second grid of the generator's efficient price, and the trade window
#: and signal window (seconds) of the two detector alphas.
_STEP_NS = 1_000_000_000
_EQ04_WINDOW_STEPS = 10
_EQ10_SIGNAL_STEPS = 1

_ROUND = 6


def _fail(message: str) -> ResearchError:
    return ResearchError(message, code="power_study_error")


def study_seeds(base_seed: int, n_seeds: int) -> list[int]:
    """``n_seeds`` generator seeds from SplitMix64(``base_seed``) (31-bit, so
    they stay exact through JSON and every port's integer type)."""
    if n_seeds < 1:
        raise _fail("n_seeds must be >= 1")
    rng = SplitMix64(int(base_seed))
    return [int(rng.next_u64() >> 33) for _ in range(n_seeds)]


def default_session_grid(max_sessions: int) -> list[int]:
    """1, 2, 4, ... up to ``max_sessions`` (which is always the last entry)."""
    if max_sessions < 1:
        raise _fail("sessions must be >= 1")
    grid = []
    n = 1
    while n < max_sessions:
        grid.append(n)
        n *= 2
    return [*grid, int(max_sessions)]


def extended_trading_days(days: Sequence[str], n: int) -> list[str]:
    """The first ``n`` trading days of a calendar that starts with ``days``
    and continues on the weekdays after its last one (ISO dates).  The
    synthetic calendar has no holidays; a study that needs more sessions
    than ``configs/`` lists gets them from here, deterministically."""
    if n < 1:
        raise _fail("sessions must be >= 1")
    out = list(days)
    if not out:
        raise _fail("the calendar has no trading day to extend from")
    day = _dt.date.fromisoformat(out[-1])
    while len(out) < n:
        day += _dt.timedelta(days=1)
        if day.weekday() < 5:
            out.append(day.isoformat())
    return out[:n]


def cell_config(
    base: Mapping[str, Any], level: float, scenario: str, seed: int, sessions: int | None = None
) -> dict[str, Any]:
    """The generator config of one run: ``base`` with both reference planted
    strengths scaled by ``level``, the scenario's break block, ``seed`` and
    (when given) ``sessions``."""
    if scenario not in SCENARIOS:
        raise _fail(f"unknown scenario {scenario!r}; known: {sorted(SCENARIOS)}")
    if not (isinstance(level, (int, float)) and math.isfinite(level) and level >= 0.0):
        raise _fail(f"level must be a finite number >= 0, got {level!r}")
    cfg = copy.deepcopy(dict(base))
    planted = cfg["planted"]
    planted["order_flow"]["strength"] = float(planted["order_flow"]["strength"]) * level
    planted["lead_lag"]["beta"] = float(planted["lead_lag"]["beta"]) * level
    planted["break"] = dict(SCENARIOS[scenario])
    cfg["seed"] = int(seed)
    if sessions is not None:
        if sessions < 1:
            raise _fail("sessions must be >= 1")
        cfg["sessions"] = int(sessions)
    return cfg


# ---------------------------------------------------------------- detectors


def matched_horizon(effect: str, planted: Mapping[str, Any]) -> str:
    """Shortest pinned label horizon that covers the planted mechanism.

    * ``order_flow``: the number of one-second steps by which half of the
      impact kernel's weight has arrived (``decay^j`` over ``kernel_steps``);
    * ``lead_lag``: ``lag_steps`` plus the detector's one-second signal
      window — the follower has repeated the leader's move by then.

    A rule on the planted block alone (never on results): a label shorter
    than this ends before most of the planted move has happened.
    """
    if effect == "order_flow":
        cfg = planted["order_flow"]
        decay, steps = float(cfg["kernel_decay"]), int(cfg["kernel_steps"])
        weights = [decay**j for j in range(steps)]
        half, acc, need = 0.5 * sum(weights), 0.0, steps
        for j, w in enumerate(weights):
            acc += w
            if acc >= half:
                need = j + 1
                break
    elif effect == "lead_lag":
        need = int(planted["lead_lag"]["lag_steps"]) + _EQ10_SIGNAL_STEPS
    else:
        raise _fail(f"unknown effect {effect!r}; known: {sorted(EFFECT_ALPHAS)}")
    for name in HORIZON_ORDER:
        if HORIZONS_NS[name] >= need * _STEP_NS:
            return name
    return HORIZON_ORDER[-1]


def detector_id(effect: str, alpha_id: str, horizon: str) -> str:
    return f"{effect}:{alpha_id}@{horizon}"


def detectors_for(planted: Mapping[str, Any]) -> list[dict[str, str]]:
    """The study's detectors for a reference planted block, in report order:
    per effect the flagship alpha at its declared horizon (``role``
    ``"declared"``) and, when different, at the matched one (``"matched"``)."""
    out: list[dict[str, str]] = []
    for effect in sorted(EFFECT_ALPHAS):
        alpha_id = EFFECT_ALPHAS[effect]
        declared = build(alpha_id).horizon
        horizons = [("declared", declared)]
        matched = matched_horizon(effect, planted)
        if matched != declared:
            horizons.append(("matched", matched))
        for role, horizon in horizons:
            out.append(
                {
                    "id": detector_id(effect, alpha_id, horizon),
                    "effect": effect,
                    "alpha_id": alpha_id,
                    "horizon": horizon,
                    "role": role,
                }
            )
    return out


def _model(alpha_id: str, horizon: str):
    model = build(alpha_id)
    model.horizon = horizon
    return model


def gate_looks_in_force(reports_dir: Path) -> int:
    """Look count the committed promotion reports of the detector alphas
    were judged at (``ledger_looks`` of ``research/alpha_reports/<ID>.json``)
    — the count behind the PROMOTE t threshold in force."""
    looks = []
    for alpha_id in sorted(set(EFFECT_ALPHAS.values())):
        path = Path(reports_dir) / f"{alpha_id}.json"
        if not path.is_file():
            raise _fail(f"{path}: no promotion report to read the gate look count from")
        value = json.loads(path.read_text(encoding="utf-8")).get("ledger_looks")
        if not isinstance(value, int) or value < 1:
            raise _fail(f"{path}: ledger_looks missing — pass the gate look count explicitly")
        looks.append(value)
    return max(looks)


# ----------------------------------------------------------------- pipeline


def _reduced_configs(
    configs_dir: Path, universe: Sequence[str], out: Path, sessions: int = 0
) -> Path:
    """A configs tree under ``out`` whose instruments are exactly ``universe``
    and whose calendar has at least ``sessions`` trading days."""
    instruments = json.loads(
        (configs_dir / "instruments" / "instruments.json").read_text(encoding="utf-8")
    )
    rows = [r for r in instruments["instruments"] if r["symbol"] in set(universe)]
    missing = sorted(set(universe) - {r["symbol"] for r in rows})
    if missing:
        raise _fail(f"universe symbols not in instruments.json: {missing}")
    instruments["instruments"] = rows
    days = list(instruments["calendar"]["trading_days"])
    if sessions > len(days):
        instruments["calendar"]["trading_days"] = extended_trading_days(days, sessions)
    for sub, name, doc in (
        ("instruments", "instruments.json", instruments),
        (
            "venues",
            "venues.json",
            json.loads((configs_dir / "venues" / "venues.json").read_text(encoding="utf-8")),
        ),
    ):
        (out / sub).mkdir(parents=True, exist_ok=True)
        (out / sub / name).write_text(
            json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return out


def build_planted_frames(
    generator_cfg: Mapping[str, Any],
    configs_dir: Path,
    work_dir: Path,
    universe: Sequence[str] = DEFAULT_UNIVERSE,
) -> dict[int, pd.DataFrame]:
    """Generator -> normalise -> features + labels for one planted config
    (all of its ``sessions``), entirely inside ``work_dir``; returns the
    per-instrument feature frames."""
    from iap.features.__main__ import main as features_main

    work_dir = Path(work_dir)
    cfg_dir = _reduced_configs(
        Path(configs_dir), universe, work_dir / "configs", int(generator_cfg["sessions"])
    )
    refdata = ReferenceData.load(cfg_dir)
    raw, normalized, features = (work_dir / n for n in ("raw", "normalized", "features"))
    MarketDataGenerator(refdata, dict(generator_cfg)).generate_run(raw)
    for path in sorted(raw.glob("*.jsonl")):
        if path.stat().st_size == 0:  # no FX instruments -> empty FX files
            path.unlink()
    normalize_run(raw, normalized)
    sink = io.StringIO()
    with contextlib.redirect_stderr(sink), contextlib.redirect_stdout(sink):
        features_main(
            [
                "--data-dir",
                str(normalized),
                "--out-dir",
                str(features),
                "--configs",
                str(cfg_dir),
                "--registry-out",
                str(work_dir / "feature_registry.json"),
            ]
        )
    return load_features(features)


def first_sessions(frames: Mapping[int, pd.DataFrame], n: int) -> dict[int, pd.DataFrame]:
    """The rows of the first ``n`` sessions (UTC days present in any frame)
    of every frame."""
    if n < 1:
        raise _fail("sessions must be >= 1")
    days = sorted(
        {
            int(d)
            for df in frames.values()
            for d in np.unique(df["exchange_ts"].to_numpy(dtype=np.int64) // power_stats.DAY_NS)
        }
    )
    if n >= len(days):
        return dict(frames)
    cutoff = (days[n - 1] + 1) * power_stats.DAY_NS
    out = {}
    for iid, df in frames.items():
        keep = df["exchange_ts"].to_numpy(dtype=np.int64) < cutoff
        out[iid] = df.loc[keep].reset_index(drop=True)
    return out


def _backtester(configs_dir: Path) -> Backtester:
    cfg = DEFAULT_CONFIGURATION
    bundle = methods(str(cfg["methods"]))
    return Backtester(
        bundle.cost_model(CostModel.load(Path(configs_dir) / "execution" / "execution.json")),
        load_instrument_meta(Path(configs_dir)),
        bundle.backtest_config(
            latency_ns=int(cfg["latency_ns"]),
            max_decision_age_ns=int(cfg["max_decision_age_ns"]),
            flatten_at_session_end=bool(cfg["flatten_at_session_end"]),
        ),
    )


def direct_statistics(
    frames: Mapping[int, pd.DataFrame], alpha_id: str, horizon: str
) -> dict[str, Any]:
    """The full-sample statistics of one detector, beside the chain's.

    The alpha's oriented RAW signal (no fit, so no walk-forward split is
    needed) against the label the chain scores, pooled over the alpha's
    universe: the session-clustered pooled-slope t, its effective sample,
    the break z, and the row accounting — what share of the universe's rows
    carries a signal (``signal_coverage``), a scored label
    (``label_scored_frac``), and among the pairs an exactly-zero label
    (``label_zero_frac``: the mid did not move over the horizon).
    """
    model = _model(alpha_id, horizon)
    signals = model.signals(frames)
    ts_parts, x_parts, y_parts = [], [], []
    rows = scored = 0
    for iid in sorted(signals):
        df = frames[iid]
        labels, _ = scored_labels(df, horizon)
        x = signals[iid].to_numpy(dtype=float)
        rows += len(df)
        scored += int(np.isfinite(labels).sum())
        ts_parts.append(df["exchange_ts"].to_numpy(dtype=np.int64))
        x_parts.append(x)
        y_parts.append(labels)
    empty = {
        "ic_direct": None,
        "t_direct": None,
        "n_pairs": 0,
        "break_z": None,
        "signal_coverage": None,
        "label_scored_frac": None,
        "label_zero_frac": None,
    }
    if rows == 0:
        return empty
    ts = np.concatenate(ts_parts)
    x = np.concatenate(x_parts)
    y = np.concatenate(y_parts)
    lags = nw_lags(HORIZONS_NS[horizon])
    pooled = power_stats.pooled_slope_session_hac(ts, x, y, lags=lags)
    brk = power_stats.slope_break_z(ts, x, y, at_fraction=BREAK_AT_FRACTION, lags=lags)
    pair = np.isfinite(x) & np.isfinite(y)
    n_pairs = int(pair.sum())
    return {
        "ic_direct": pooled["ic"],
        "t_direct": pooled["t"],
        "n_pairs": n_pairs,
        "break_z": brk["z"],
        "signal_coverage": float(np.isfinite(x).sum()) / rows,
        "label_scored_frac": scored / rows,
        "label_zero_frac": float((y[pair] == 0.0).sum()) / n_pairs if n_pairs else None,
    }


def evaluate_run(
    frames: Mapping[int, pd.DataFrame],
    configs_dir: Path,
    promote_t_threshold: float,
    detectors: Sequence[Mapping[str, str]],
    seed: int = 0,
    normalized_dir: Path | None = None,
    engine_configs_dir: Path | None = None,
) -> dict[str, dict]:
    """``validate_alpha`` under the default method bundle, and the direct
    statistics, for every detector on one planted dataset.

    ``promote_t_threshold`` is the PROMOTE |t| of the run (never below 3.0);
    ``seed`` seeds the P&L bootstrap.  ``normalized_dir`` holds the run's
    normalized events for the recompute leakage probe and
    ``engine_configs_dir`` the configs tree the features were built with;
    without them the probe does not run.  Returns ``{detector id: row}``; a
    detector whose validation cannot run at all (too few rows for the
    splitter) yields ``{"error": ...}`` and counts as not detected — an
    under-powered sample is a miss, not a crash."""
    configs_dir = Path(configs_dir)
    bundle = methods(str(DEFAULT_CONFIGURATION["methods"]))
    backtester = _backtester(configs_dir)
    recompute = (
        RecomputeSources(normalized_dir, engine_configs_dir or configs_dir)
        if normalized_dir is not None and bundle.recompute_probe
        else None
    )
    meta = load_instrument_meta(configs_dir)
    exec_cfg = json.loads(
        (configs_dir / "execution" / "execution.json").read_text(encoding="utf-8")
    )
    max_participation = float(exec_cfg["defaults"]["max_participation"])
    out: dict[str, dict] = {}
    for det in detectors:
        alpha_id, horizon = det["alpha_id"], det["horizon"]

        def factory(alpha_id: str = alpha_id, horizon: str = horizon):
            return _model(alpha_id, horizon)

        try:
            report = validate_alpha(
                factory,
                frames,
                backtester,
                meta,
                max_participation,
                n_folds=int(DEFAULT_CONFIGURATION["n_folds"]),
                embargo_ns=int(DEFAULT_CONFIGURATION["embargo_ns"]),
                ledger_t_threshold=float(promote_t_threshold),
                seed=seed,
                n_boot=BOOTSTRAP_RESAMPLES,
                recompute=recompute.get("EQUITY") if recompute is not None else None,
                **bundle.validate_kwargs(),
            )
        except ValueError as exc:
            out[det["id"]] = {"alpha_id": alpha_id, "horizon": horizon, "error": str(exc)}
            continue
        out[det["id"]] = {
            **_run_row(alpha_id, horizon, report),
            **direct_statistics(frames, alpha_id, horizon),
        }
    return out


def _run_row(alpha_id: str, horizon: str, report: Mapping[str, Any]) -> dict[str, Any]:
    """One detector's chain row from its validation report."""
    t_within = report["nw_tstat_uncrossed"]
    if t_within is None:
        t_within = report["nw_tstat"]
    t_pooled = report["nw_tstat_pooled_uncrossed"]
    if t_pooled is None:
        t_pooled = report["nw_tstat_pooled"]
    boot = report["net_pnl_bootstrap"]
    return {
        "alpha_id": alpha_id,
        "horizon": horizon,
        "gate_ic": report["gate_ic"],
        "ic_vol_scaled": report["oos_ic_vol_scaled"],
        "t_pooled": t_pooled,
        "t_within": t_within,
        "fold_sign_consistency": report["fold_sign_consistency"],
        "hypothesis_confirmed": bool(report["hypothesis_confirmed"]),
        "leakage_passed": bool(report["leakage"]["passed"]),
        "recompute_ok": report["leakage"]["recompute_ok"],
        "trade_count_1x": int(report["trade_count_1x_cost"]),
        "n_folds_survive_1x_cost": int(report["n_folds_survive_1x_cost"]),
        "pnl_ci_positive": bool(boot["ci_low"] is not None and boot["ci_low"] > 0.0),
        "verdict": str(report["verdict"]),
    }


def _frame_rows(frames: Mapping[int, pd.DataFrame]) -> dict[str, int]:
    """Feature rows per instrument of one planted dataset (for the record)."""
    return {str(i): int(len(frames[i])) for i in sorted(frames)}


def _execute_run(task: Mapping[str, Any]) -> dict[str, Any]:
    """One run of the grid (a worker process): build the planted dataset at
    the task's session count, evaluate every detector on each session
    prefix, remove the scratch directory."""
    work = Path(task["work"])
    if work.exists():
        shutil.rmtree(work)
    try:
        frames = build_planted_frames(
            task["config"], Path(task["configs_dir"]), work, task["universe"]
        )
        evaluations = []
        full = max(task["session_grid"])
        for n in task["session_grid"]:
            probe = bool(task["probe"]) and n == full
            evaluations.append(
                {
                    "sessions": int(n),
                    "detectors": evaluate_run(
                        first_sessions(frames, n),
                        Path(task["configs_dir"]),
                        task["promote_t_threshold"],
                        task["detectors"],
                        seed=task["seed"],
                        normalized_dir=work / "normalized" if probe else None,
                        engine_configs_dir=work / "configs" if probe else None,
                    ),
                }
            )
        rows = _frame_rows(frames)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    # rounded BEFORE the cells are computed, so the cells are exactly
    # summarise(runs) of the document that is written
    return _rounded(
        {
            "scenario": task["scenario"],
            "level": task["level"],
            "seed": task["seed"],
            "rows": rows,
            "evaluations": evaluations,
        }
    )


# ------------------------------------------------------------------ summary


def _finite(values: Sequence[Any]) -> list[float]:
    return [float(v) for v in values if v is not None and math.isfinite(float(v))]


def _mean(values: Sequence[Any]) -> float | None:
    finite = _finite(values)
    return round(sum(finite) / len(finite), _ROUND) if finite else None


def _sd(values: Sequence[Any]) -> float | None:
    finite = _finite(values)
    if len(finite) < 2:
        return None
    mean = sum(finite) / len(finite)
    return round(math.sqrt(sum((v - mean) ** 2 for v in finite) / (len(finite) - 1)), _ROUND)


def _rate(successes: int, n: int) -> dict[str, Any]:
    lo, hi = power_stats.wilson_interval(successes, n)
    return {
        "k": successes,
        "rate": round(successes / n, _ROUND) if n else None,
        "lo": round(lo, _ROUND),
        "hi": round(hi, _ROUND),
    }


def _reaches(row: Mapping[str, Any], key: str, threshold: float, ic_key: str) -> bool:
    t, ic = row.get(key), row.get(ic_key)
    return t is not None and ic is not None and ic > 0.0 and t >= threshold


def _breaks(row: Mapping[str, Any], threshold: float) -> bool:
    z = row.get("break_z")
    return z is not None and abs(z) >= threshold


def summarise(
    runs: Sequence[Mapping[str, Any]], thresholds: Mapping[str, float]
) -> list[dict[str, Any]]:
    """One row per (detector, scenario, level, sessions): detection counts
    with Wilson 95 % intervals, and the means behind them, over the seeds.

    ``thresholds`` holds ``fixed``, ``gate`` and ``study`` (item 6 of the
    module docs).  A run that could not be validated counts as a miss in
    every rate and is left out of every mean."""
    cells: dict[tuple, list[Mapping[str, Any]]] = {}
    for run in runs:
        for evaluation in run["evaluations"]:
            for det, row in evaluation["detectors"].items():
                key = (det, run["scenario"], float(run["level"]), int(evaluation["sessions"]))
                cells.setdefault(key, []).append(row)
    out: list[dict[str, Any]] = []
    for det, scenario, level, sessions in sorted(cells):
        rows = cells[(det, scenario, level, sessions)]
        ok = [r for r in rows if "error" not in r]
        n = len(rows)

        def count(pred: Callable[[Mapping[str, Any]], bool]) -> dict[str, Any]:
            return _rate(sum(1 for r in ok if pred(r)), n)  # noqa: B023 (called within the iteration)

        def mean(key: str) -> float | None:
            return _mean([r[key] for r in ok])  # noqa: B023 (called within the iteration)

        effect, _, rest = det.partition(":")
        out.append(
            {
                "detector": det,
                "effect": effect,
                "alpha_id": rest.split("@")[0],
                "horizon": rest.split("@")[1],
                "scenario": scenario,
                "level": level,
                "sessions": sessions,
                "n_runs": n,
                "n_failed": n - len(ok),
                "sig_fixed": count(
                    lambda r: _reaches(r, "t_pooled", thresholds["fixed"], "gate_ic")
                ),
                "sig_gate": count(lambda r: _reaches(r, "t_pooled", thresholds["gate"], "gate_ic")),
                "sig_study": count(
                    lambda r: _reaches(r, "t_pooled", thresholds["study"], "gate_ic")
                ),
                "sig_direct_gate": count(
                    lambda r: _reaches(r, "t_direct", thresholds["gate"], "ic_direct")
                ),
                "evidence": count(lambda r: r["verdict"] in ("ITERATE", "PROMOTE")),
                "promote": count(lambda r: r["verdict"] == "PROMOTE"),
                "pnl_ci_positive": count(lambda r: bool(r["pnl_ci_positive"])),
                "break_fixed": count(lambda r: _breaks(r, thresholds["fixed"])),
                "break_gate": count(lambda r: _breaks(r, thresholds["gate"])),
                "n_recompute_failed": sum(1 for r in ok if r["recompute_ok"] is False),
                "mean_gate_ic": mean("gate_ic"),
                "mean_ic_vol_scaled": mean("ic_vol_scaled"),
                "mean_ic_direct": mean("ic_direct"),
                "mean_t_pooled": mean("t_pooled"),
                "sd_t_pooled": _sd([r["t_pooled"] for r in ok]),
                "mean_t_within": mean("t_within"),
                "mean_t_direct": mean("t_direct"),
                "mean_break_z": mean("break_z"),
                "sd_break_z": _sd([r["break_z"] for r in ok]),
                "mean_fold_sign_consistency": mean("fold_sign_consistency"),
                "mean_trades_1x": _mean([float(r["trade_count_1x"]) for r in ok]),
                "mean_folds_survive_1x_cost": _mean(
                    [float(r["n_folds_survive_1x_cost"]) for r in ok]
                ),
                "mean_n_pairs": _mean([float(r["n_pairs"]) for r in ok]),
                "mean_signal_coverage": mean("signal_coverage"),
                "mean_label_scored_frac": mean("label_scored_frac"),
                "mean_label_zero_frac": mean("label_zero_frac"),
            }
        )
    return out


def _ideal_ic(det: Mapping[str, str], planted: Mapping[str, Any], sigma_ratios) -> float:
    """Analytical IC of a detector at the reference effect, on the efficient
    price (:mod:`iap.research.power_stats`)."""
    steps = max(1, HORIZONS_NS[det["horizon"]] // _STEP_NS)
    if det["effect"] == "order_flow":
        cfg = planted["order_flow"]
        return power_stats.ideal_ic_order_flow(
            float(cfg["strength"]),
            float(cfg["kernel_decay"]),
            int(cfg["kernel_steps"]),
            _EQ04_WINDOW_STEPS,
            int(steps),
            sigma_ratios,
        )
    return power_stats.ideal_ic_lead_lag(
        float(planted["lead_lag"]["beta"]), int(planted["lead_lag"]["lag_steps"]), int(steps)
    )


def diagnose(
    cells: Sequence[Mapping[str, Any]],
    detectors: Sequence[Mapping[str, str]],
    planted: Mapping[str, Any],
    sigma_ratios: Sequence[float],
    n_folds: int,
) -> list[dict[str, Any]]:
    """Where the t of the reference effect goes, per detector and session
    count (stable scenario, level 1.0).  A chain of expected t-statistics,
    each removing one idealisation:

    * ``t_frictionless`` = ideal IC x sqrt(scored rows): every row carries a
      signal, the efficient price is observed, rows are independent;
    * ``t_signal_rows``  = ideal IC x sqrt(pairs): only rows with a signal;
    * ``t_observed_iid`` = measured IC x sqrt(pairs): the observed mid on
      its tick grid instead of the efficient price (``attenuation`` =
      measured IC / ideal IC);
    * ``t_direct``       = the measured session-clustered t: rows are not
      independent (``n_eff`` = (t_direct / measured IC)^2 independent
      observations, ``design_effect`` = pairs / n_eff rows for each);
    * ``t_oos_expected`` = ``t_direct`` x sqrt(n_folds / (n_folds + 1)): the
      walk-forward chain scores the last n_folds of n_folds + 1 blocks;
    * ``t_chain``        = the measured mean pooled-slope t of the chain.
    """
    by_key = {(c["detector"], c["scenario"], c["level"], c["sessions"]): c for c in cells}
    out = []
    for det in detectors:
        ideal = _ideal_ic(det, planted, sigma_ratios)
        for key in sorted(k for k in by_key if k[:3] == (det["id"], "stable", 1.0)):
            c = by_key[key]
            pairs, ic = c["mean_n_pairs"], c["mean_ic_direct"]
            if pairs is None or ic is None or not c["mean_signal_coverage"]:
                continue
            scored_rows = pairs / c["mean_signal_coverage"]
            t_direct = c["mean_t_direct"]
            n_eff = (t_direct / ic) ** 2 if t_direct is not None and abs(ic) > 1e-9 else None
            out.append(
                {
                    "detector": det["id"],
                    "sessions": c["sessions"],
                    "ideal_ic": ideal,
                    "ideal_r2": ideal * ideal,
                    "measured_ic": ic,
                    "measured_r2": ic * ic,
                    "attenuation": ic / ideal if ideal > 0.0 else None,
                    "signal_coverage": c["mean_signal_coverage"],
                    "label_scored_frac": c["mean_label_scored_frac"],
                    "label_zero_frac": c["mean_label_zero_frac"],
                    "n_pairs": pairs,
                    "n_eff": n_eff,
                    "design_effect": pairs / n_eff if n_eff else None,
                    "t_frictionless": ideal * math.sqrt(scored_rows),
                    "t_signal_rows": ideal * math.sqrt(pairs),
                    "t_observed_iid": ic * math.sqrt(pairs),
                    "t_direct": t_direct,
                    "t_oos_expected": (
                        t_direct * math.sqrt(n_folds / (n_folds + 1.0))
                        if t_direct is not None
                        else None
                    ),
                    "t_chain": c["mean_t_pooled"],
                }
            )
    return out


def power_model(
    runs: Sequence[Mapping[str, Any]],
    detectors: Sequence[Mapping[str, str]],
    thresholds: Mapping[str, float],
    session_grid: Sequence[int],
) -> list[dict[str, Any]]:
    """Per detector: the fit of ``t ~ N((kappa0 + kappa * level) *
    sqrt(sessions), sd)`` to the chain's pooled-slope t of every stable run
    (the null included: ``kappa0`` is what the detector reads with nothing
    planted), the null's mean and sd of t, the minimum detectable level at
    :data:`TARGET_POWER` for each session count, the sessions the reference
    effect needs — and the same for the break z of the ``break`` runs
    (``E[z] = kappa * level * sqrt(sessions)``, no intercept).

    The session prefixes of one run are nested, so the points are not
    independent: the coefficients are consistent point estimates, not
    quantities with a quoted error.  Session counts beyond the grid are
    extrapolation under the sqrt law and are labelled so by the report."""
    out = []
    for det in detectors:
        stable, null, brk = [], [], []
        for run in runs:
            for evaluation in run["evaluations"]:
                row = evaluation["detectors"].get(det["id"])
                if row is None or "error" in row:
                    continue
                point = (float(run["level"]), float(evaluation["sessions"]))
                if run["scenario"] == "stable":
                    stable.append((*point, row["t_pooled"]))
                    if run["level"] == 0.0:
                        null.append(row["t_pooled"])
                elif run["scenario"] == "break":
                    brk.append((*point, row["break_z"]))
        entry: dict[str, Any] = {"detector": det["id"]}
        for name, points, intercept in (("chain", stable, True), ("break", brk, False)):
            fit = power_stats.fit_t_model(points, intercept=intercept)
            block: dict[str, Any] = {**fit, "by_threshold": {}}
            usable = fit["kappa"] is not None and fit["sd"] is not None and fit["sd"] > 0.0
            for label in ("fixed", "gate"):
                thr = float(thresholds[label])
                args = (fit["kappa"], fit["sd"], thr)
                block["by_threshold"][label] = {
                    "threshold": thr,
                    "sessions_needed_at_reference": (
                        power_stats.sessions_needed(*args, power=TARGET_POWER, kappa0=fit["kappa0"])
                        if usable
                        else None
                    ),
                    "mde_level_by_sessions": {
                        str(n): (
                            power_stats.minimum_detectable_level(
                                *args, n, TARGET_POWER, kappa0=fit["kappa0"]
                            )
                            if usable
                            else None
                        )
                        for n in session_grid
                    },
                    "power_at_reference_by_sessions": {
                        str(n): (
                            power_stats.normal_power(
                                (fit["kappa0"] + fit["kappa"]) * math.sqrt(n), fit["sd"], thr
                            )
                            if usable
                            else None
                        )
                        for n in session_grid
                    },
                }
            entry[name] = block
        entry["null"] = {"mean_t": _mean(null), "sd_t": _sd(null), "n": len(_finite(null))}
        out.append(entry)
    return out


def _rounded(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        return round(value, _ROUND) if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(k): _rounded(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_rounded(v) for v in value]
    raise _fail(f"value {value!r} is not JSON-representable")


# -------------------------------------------------------------------- study


def run_power_study(
    generator_config_path: Path,
    configs_dir: Path,
    levels: Sequence[float] = DEFAULT_LEVELS,
    n_seeds: int = DEFAULT_SEEDS,
    scenarios: Sequence[str] = tuple(SCENARIOS),
    universe: Sequence[str] = DEFAULT_UNIVERSE,
    scratch_dir: Path | None = None,
    progress: Callable[[str], None] | None = None,
    sessions: Sequence[int] | None = None,
    break_levels: Sequence[float] = DEFAULT_BREAK_LEVELS,
    gate_looks: int | None = None,
    jobs: int = 1,
) -> dict[str, Any]:
    """Run the grid and return the ``POWER_REPORT.json`` document.

    ``sessions`` is the session grid (default: 1, 2, 4, ... up to the
    generator config's ``sessions``); the largest entry is the number of
    sessions generated.  ``break_levels`` are the non-zero levels the
    ``break`` scenario runs at (evaluated at the full session count only: a
    prefix of a break run has its break somewhere else).  ``gate_looks`` is
    the look count of the PROMOTE threshold in force (default:
    :func:`gate_looks_in_force` on ``research/alpha_reports`` beside
    ``configs_dir``).  ``jobs`` worker processes run the grid (1 = in this
    process); the document does not depend on it.

    ``scratch_dir`` (default: a fresh temporary directory) holds one
    sub-directory per run and is removed afterwards; ``progress`` receives
    one line per run.  Deterministic: same config, grid and code => same
    document.
    """
    generator_config_path = Path(generator_config_path)
    configs_dir = Path(configs_dir)
    if not generator_config_path.is_file():
        raise _fail(f"generator config not found: {generator_config_path}")
    base = load_generator_config(generator_config_path)
    reference = copy.deepcopy(base["planted"])
    if (
        float(reference["order_flow"]["strength"]) == 0.0
        and float(reference["lead_lag"]["beta"]) == 0.0
    ):
        raise _fail(
            f"{generator_config_path}: the planted block is all zero — the "
            "reference effect (level 1.0) must plant something"
        )
    levels = [float(v) for v in levels]
    if not levels or sorted(set(levels)) != levels:
        raise _fail("levels must be a non-empty strictly increasing list")
    for name in scenarios:
        if name not in SCENARIOS:
            raise _fail(f"unknown scenario {name!r}; known: {sorted(SCENARIOS)}")
    break_levels = [float(v) for v in break_levels]
    if sorted(set(break_levels)) != break_levels or any(v <= 0.0 for v in break_levels):
        raise _fail("break levels must be strictly increasing and > 0 (the null has no break)")
    session_grid = (
        default_session_grid(int(base["sessions"])) if sessions is None else list(sessions)
    )
    if (
        not session_grid
        or sorted(set(session_grid)) != session_grid
        or any((not isinstance(n, int)) or n < 1 for n in session_grid)
    ):
        raise _fail("sessions must be a non-empty strictly increasing list of integers >= 1")
    if jobs < 1:
        raise _fail("jobs must be >= 1")
    max_sessions = session_grid[-1]
    if gate_looks is None:
        gate_looks = gate_looks_in_force(configs_dir.parent / "research" / "alpha_reports")
    if gate_looks < 1:
        raise _fail("gate_looks must be >= 1")
    seeds = study_seeds(int(base["seed"]), n_seeds)
    detectors = detectors_for(reference)
    grid = [("stable", level, session_grid) for level in levels if "stable" in scenarios] + [
        ("break", level, [max_sessions]) for level in break_levels if "break" in scenarios
    ]
    if not grid:
        raise _fail("the grid is empty")
    # every chain t and every break z of the study is one test
    n_tests = 2 * len(detectors) * len(seeds) * sum(len(g) for _, _, g in grid)
    thresholds = {
        "fixed": float(GATES["min_nw_tstat"]),
        "gate": float(ExperimentLedger.bonferroni_t_threshold_at(gate_looks)),
        "study": float(ExperimentLedger.bonferroni_t_threshold_at(n_tests)),
    }
    promote_t = max(thresholds.values())
    owned = scratch_dir is None
    scratch = Path(tempfile.mkdtemp(prefix="iap-power-")) if owned else Path(scratch_dir)
    scratch.mkdir(parents=True, exist_ok=True)
    tasks = [
        {
            "scenario": scenario,
            "level": level,
            "seed": seed,
            "probe": index == 0,
            "session_grid": list(prefixes),
            "config": cell_config(base, level, scenario, seed, max_sessions),
            "configs_dir": str(configs_dir),
            "universe": tuple(universe),
            "work": str(scratch / f"{scenario}-{level:g}-{seed}"),
            "promote_t_threshold": promote_t,
            "detectors": detectors,
        }
        for scenario, level, prefixes in grid
        for index, seed in enumerate(seeds)
    ]

    def note(run: Mapping[str, Any]) -> None:
        if progress is None:
            return
        last = run["evaluations"][-1]
        progress(
            f"{run['scenario']} level {run['level']:g} seed {run['seed']} "
            f"({last['sessions']} sessions): "
            + ", ".join(
                f"{d}={row.get('verdict', 'ERROR')}" for d, row in sorted(last["detectors"].items())
            )
        )

    runs: list[dict[str, Any]] = []
    try:
        if jobs == 1:
            for task in tasks:
                runs.append(_execute_run(task))
                note(runs[-1])
        else:
            with ProcessPoolExecutor(max_workers=jobs) as pool:
                for run in pool.map(_execute_run, tasks):
                    runs.append(run)
                    note(run)
    finally:
        if owned:
            shutil.rmtree(scratch, ignore_errors=True)
    sigma = [float(v) for v in base["equities"]["vol_regimes"]["sigma_ticks_per_s"]]
    sigma_ratios = [v / sigma[0] for v in sigma]
    n_folds = int(DEFAULT_CONFIGURATION["n_folds"])
    thresholds = _rounded(thresholds)
    cells = summarise(runs, thresholds)
    return _rounded(
        {
            "x-version": POWER_VERSION,
            "description": (
                "Planted-signal power study (iap.research.power): detection rates "
                "of validate_alpha on synthetic data with effects of known size, by "
                "effect size and number of sessions. Deterministic: no wall clock; "
                "same config + grid + code => same document."
            ),
            "generator_config": generator_config_path.name,
            "base_seed": int(base["seed"]),
            "seeds": seeds,
            "universe": list(universe),
            "levels": levels,
            "break_levels": break_levels if "break" in scenarios else [],
            "sessions": session_grid,
            "scenarios": {name: SCENARIOS[name] for name in scenarios},
            "detectors": detectors,
            "reference_planted": {
                "order_flow": reference["order_flow"],
                "lead_lag": reference["lead_lag"],
            },
            "protocol": {
                **DEFAULT_CONFIGURATION,
                "thresholds": thresholds,
                "n_tests": n_tests,
                "gate_looks": int(gate_looks),
                "promote_t_threshold": promote_t,
                "target_power": TARGET_POWER,
                "bootstrap_resamples": BOOTSTRAP_RESAMPLES,
                "interval": "wilson_95",
            },
            "cells": cells,
            "diagnosis": diagnose(cells, detectors, reference, sigma_ratios, n_folds),
            "power_model": power_model(runs, detectors, thresholds, session_grid),
            "runs": runs,
        }
    )


def default_jobs() -> int:
    """Worker processes the CLI uses when ``--jobs`` is not given."""
    return max(1, min(os.cpu_count() or 1, 8))


# ---------------------------------------------------------------- rendering


def _cell(value: Any, spec: str) -> str:
    return "n/a" if value is None else format(value, spec)


def _rate_text(rate: Mapping[str, Any], n: int) -> str:
    return f"{rate['k']}/{n} [{rate['lo']:.2f}, {rate['hi']:.2f}]"


def _verdict_sentence(det: Mapping[str, str], cell: Mapping[str, Any], model: Mapping) -> str:
    """The plain statement for one detector at the reference effect on the
    largest session count."""
    rate = cell["sig_gate"]
    need = model["chain"]["by_threshold"]["gate"]["sessions_needed_at_reference"]
    head = (
        f"`{det['id']}` ({det['role']} horizon): {rate['k']} of {cell['n_runs']} runs "
        f"detected at the gate threshold on {cell['sessions']} sessions "
        f"(95 % interval {rate['lo']:.2f}–{rate['hi']:.2f}; mean t "
        f"{_cell(cell['mean_t_pooled'], '+.2f')})"
    )
    if rate["lo"] > 1.0 / 3.0 and rate["rate"] >= TARGET_POWER:
        tail = "**detected**: the rate is at or above the 80 % target and its interval excludes one in three"
    elif rate["lo"] > 1.0 / 3.0:
        tail = "**detected more often than one in three**, short of the 80 % target"
    elif rate["hi"] < 1.0 / 3.0:
        tail = "**not detected**: the interval lies below one in three"
    else:
        tail = "**not established**: the interval includes one in three"
    if need is None:
        more = "the fitted model gives no session count at which it would be"
    else:
        more = f"the fitted model puts 80 % power at about {math.ceil(need)} session(s)"
    return f"- {head} — {tail}; {more}."


def render_markdown(doc: Mapping[str, Any]) -> str:
    """``POWER_REPORT.md`` for a study document (deterministic)."""
    ref = doc["reference_planted"]
    protocol = doc["protocol"]
    thr = protocol["thresholds"]
    n_seeds = len(doc["seeds"])
    sessions = doc["sessions"]
    full = sessions[-1]
    detectors = doc["detectors"]
    cells = {(c["detector"], c["scenario"], c["level"], c["sessions"]): c for c in doc["cells"]}
    models = {m["detector"]: m for m in doc["power_model"]}
    levels = doc["levels"]
    lines = [
        "# Planted-signal power study",
        "",
        "Generated by `python -m iap.research power` (`iap.research.power`, "
        f"report version {doc['x-version']}). Do not edit by hand.",
        "",
        "How often does the validation chain (`iap.validation.validate_alpha`, "
        "the pinned research execution model under the "
        f"`{protocol['methods']}` method bundle) detect an effect of known "
        "size planted into the synthetic generator, on how many sessions, and "
        "how often does it report one that is not there?",
        "",
        "## Design",
        "",
        f"- Generator config: `research/power/{doc['generator_config']}` "
        f"(base seed {doc['base_seed']}); universe {', '.join(doc['universe'])}.",
        "- Reference effect (level 1.0): informed order flow "
        f"`strength={ref['order_flow']['strength']}`, kernel decay "
        f"{ref['order_flow']['kernel_decay']} over "
        f"{ref['order_flow']['kernel_steps']} one-second steps; ETF lead-lag "
        f"`beta={ref['lead_lag']['beta']}` at a lag of "
        f"{ref['lead_lag']['lag_steps']} step(s). A level scales both; the "
        "reference is fixed and is not tuned to this report.",
        f"- Levels: {', '.join(format(v, 'g') for v in levels)} (0 = null); "
        f"{n_seeds} generator seeds per cell; every rate is `detections/runs "
        "[Wilson 95 % interval]`.",
        f"- Sessions: each run is generated at {full} sessions and evaluated on "
        f"its first {', '.join(str(n) for n in sessions)} (the first n sessions "
        "of a run are exactly the n-session run of the same seed).",
        "- Scenarios: `stable` = the effect holds for the whole sample; "
        "`break` = it is reversed from the middle of the sample on "
        f"(levels {', '.join(format(v, 'g') for v in doc['break_levels']) or 'none'}, "
        f"{full} sessions).",
        "- Detectors: "
        + "; ".join(f"`{d['id']}` ({d['role']} horizon)" for d in detectors)
        + ". The matched horizon is the shortest pinned label horizon that covers "
        "the planted mechanism — a rule on the planted block, not on the results.",
        f"- Thresholds on the pooled-slope HAC t (gate IC > 0 required): `fixed` "
        f"{thr['fixed']:g}; `gate` {thr['gate']:.3f}, the PROMOTE threshold in "
        f"force (Bonferroni at the {protocol['gate_looks']:,} looks the committed "
        f"promotion reports were judged at); `study` {thr['study']:.3f}, "
        f"Bonferroni over this study's own {protocol['n_tests']:,} tests. "
        f"Verdicts are computed at {protocol['promote_t_threshold']:.3f}, the "
        "largest of the three.",
        "",
        "## Detection of the reference effect, by number of sessions",
        "",
        "Stable scenario, level 1.",
        "",
        "| detector | sessions | at gate | at fixed 3.0 | at study | mean t | sd t "
        "| evidence | promote |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for det in detectors:
        for n in sessions:
            c = cells.get((det["id"], "stable", 1.0, n))
            if c is None:
                continue
            lines.append(
                f"| {det['id']} | {n} | {_rate_text(c['sig_gate'], c['n_runs'])} "
                f"| {_rate_text(c['sig_fixed'], c['n_runs'])} "
                f"| {_rate_text(c['sig_study'], c['n_runs'])} "
                f"| {_cell(c['mean_t_pooled'], '+.2f')} | {_cell(c['sd_t_pooled'], '.2f')} "
                f"| {_rate_text(c['evidence'], c['n_runs'])} "
                f"| {_rate_text(c['promote'], c['n_runs'])} |"
            )
    lines += [
        "",
        "## False positives on the null",
        "",
        "Level 0: nothing is planted, so every detection is false. A detection "
        "needs a POSITIVE gate IC: a null whose mean t is below zero is the "
        "generator's own microstructure (its trade imbalance mean-reverts a "
        "little), not a false positive — but the planted effect has to overcome "
        "it, which the power model's `kappa0` accounts for.",
        "",
        "| detector | sessions | at gate | at fixed 3.0 | mean t | sd t | evidence "
        "| break z at fixed 3.0 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for det in detectors:
        for n in sessions:
            c = cells.get((det["id"], "stable", 0.0, n))
            if c is None:
                continue
            lines.append(
                f"| {det['id']} | {n} | {_rate_text(c['sig_gate'], c['n_runs'])} "
                f"| {_rate_text(c['sig_fixed'], c['n_runs'])} "
                f"| {_cell(c['mean_t_pooled'], '+.2f')} | {_cell(c['sd_t_pooled'], '.2f')} "
                f"| {_rate_text(c['evidence'], c['n_runs'])} "
                f"| {_rate_text(c['break_fixed'], c['n_runs'])} |"
            )
    lines += [
        "",
        f"## Detection by effect size ({full} sessions)",
        "",
        "| detector | level | at gate | at fixed 3.0 | mean t | gate IC | IC (vol-scaled) "
        "| fold consistency | trades at 1x | P&L CI > 0 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for det in detectors:
        for level in levels:
            c = cells.get((det["id"], "stable", level, full))
            if c is None:
                continue
            lines.append(
                f"| {det['id']} | {level:g} | {_rate_text(c['sig_gate'], c['n_runs'])} "
                f"| {_rate_text(c['sig_fixed'], c['n_runs'])} "
                f"| {_cell(c['mean_t_pooled'], '+.2f')} | {_cell(c['mean_gate_ic'], '+.4f')} "
                f"| {_cell(c['mean_ic_vol_scaled'], '+.4f')} "
                f"| {_cell(c['mean_fold_sign_consistency'], '.2f')} "
                f"| {_cell(c['mean_trades_1x'], '.1f')} "
                f"| {_rate_text(c['pnl_ci_positive'], c['n_runs'])} |"
            )
    lines += [
        "",
        "## The mid-sample break",
        "",
        "The effect is planted for the first half of the run and reversed for the "
        "second. The chain should NOT report evidence (a full-sample fit averages "
        "the two halves to nothing); the break z — the HAC z of the difference "
        "between the pooled slope of the raw signal before and after mid-sample — "
        "is what detects the reversal. The split point is the planted one, so "
        "this is the best case for a break test.",
        "",
        "| detector | scenario | level | evidence (chain) | fold consistency "
        "| mean t | break at gate | break at fixed 3.0 | mean break z |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for det in detectors:
        for scenario, level in [("break", v) for v in doc["break_levels"]] + [
            ("stable", v) for v in levels
        ]:
            c = cells.get((det["id"], scenario, level, full))
            if c is None:
                continue
            lines.append(
                f"| {det['id']} | {scenario} | {level:g} "
                f"| {_rate_text(c['evidence'], c['n_runs'])} "
                f"| {_cell(c['mean_fold_sign_consistency'], '.2f')} "
                f"| {_cell(c['mean_t_pooled'], '+.2f')} "
                f"| {_rate_text(c['break_gate'], c['n_runs'])} "
                f"| {_rate_text(c['break_fixed'], c['n_runs'])} "
                f"| {_cell(c['mean_break_z'], '+.2f')} |"
            )
    lines += [
        "",
        "The `stable` rows are the break test's false-alarm rate.",
        "",
        "## Where the power goes",
        "",
        "Reference effect, stable scenario. `ideal IC` is the analytical IC of the "
        "planted mechanism for a detector that observed the efficient price itself "
        "(`iap.research.power_stats.ideal_ic_*`; its square is the R² the "
        "reference effect explains at that horizon); `measured IC` is the "
        "full-sample IC of the alpha's raw signal on the observed mid. Each "
        "expected t removes one idealisation: every row has a signal and rows are "
        "independent (`frictionless`); only the rows that carry a signal (`signal "
        "rows`); the observed mid instead of the efficient price (`observed, "
        "iid`); serially dependent rows (`direct`, the measured session-clustered "
        "t); the walk-forward chain scores "
        f"{protocol['n_folds']} of {protocol['n_folds'] + 1} blocks (`OOS expected`). "
        "`chain` is the measured mean pooled-slope t the gate reads.",
        "",
        "| detector | sessions | ideal IC | measured IC | attenuation | signal coverage "
        "| labels scored | labels exactly 0 | pairs | effective n | rows per independent obs "
        "| t frictionless | t signal rows | t observed, iid | t direct | t OOS expected "
        "| t chain |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for d in doc["diagnosis"]:
        lines.append(
            f"| {d['detector']} | {d['sessions']} | {d['ideal_ic']:.4f} "
            f"| {d['measured_ic']:+.4f} | {_cell(d['attenuation'], '.2f')} "
            f"| {d['signal_coverage']:.2f} | {d['label_scored_frac']:.2f} "
            f"| {_cell(d['label_zero_frac'], '.2f')} | {d['n_pairs']:.0f} "
            f"| {_cell(d['n_eff'], '.0f')} | {_cell(d['design_effect'], '.2f')} "
            f"| {d['t_frictionless']:.1f} | {d['t_signal_rows']:.1f} "
            f"| {d['t_observed_iid']:+.2f} | {_cell(d['t_direct'], '+.2f')} "
            f"| {_cell(d['t_oos_expected'], '+.2f')} | {_cell(d['t_chain'], '+.2f')} |"
        )
    lines += [
        "",
        "## Minimum detectable effect and sessions needed",
        "",
        "Fit of `t ~ N((kappa0 + kappa * level) * sqrt(sessions), sd)` to the "
        "chain's t of every stable run, the null included (`kappa` = the t the "
        "reference effect adds on one session; `kappa0` = the t of the null on one "
        "session — where it is negative the generator's own microstructure works "
        "against the planted effect, which has to overcome it first). MDE = the "
        "level detected with "
        f"{protocol['target_power']:.0%} probability; `sessions for reference` = "
        f"sessions at which level 1 reaches {protocol['target_power']:.0%}. A session "
        f"count above {full} is an extrapolation under the sqrt law, not a "
        "measurement. The model is linear in the level; a mechanism whose "
        "response flattens with size (the lead-lag correlation is "
        "`beta / sqrt(1 + beta^2)`) is understated at level 1 when larger levels "
        "are in the fit, and the measured rate above is then the better number.",
        "",
        "| detector | kappa0 | kappa | sd | null mean t | null sd t | threshold "
        f"| MDE at {full} sessions | sessions for reference | modelled power at {full} |",
        "|---|---:|---:|---:|---:|---:|---|---:|---:|---:|",
    ]
    for det in detectors:
        m = models[det["id"]]
        for label in ("gate", "fixed"):
            b = m["chain"]["by_threshold"][label]
            lines.append(
                f"| {det['id']} | {_cell(m['chain']['kappa0'], '+.3f')} "
                f"| {_cell(m['chain']['kappa'], '+.3f')} "
                f"| {_cell(m['chain']['sd'], '.2f')} | {_cell(m['null']['mean_t'], '+.2f')} "
                f"| {_cell(m['null']['sd_t'], '.2f')} | {label} {b['threshold']:.3f} "
                f"| {_cell(b['mde_level_by_sessions'][str(full)], '.2f')} "
                f"| {_cell(b['sessions_needed_at_reference'], '.1f')} "
                f"| {_cell(b['power_at_reference_by_sessions'][str(full)], '.2f')} |"
            )
    lines += [
        "",
        "Break test (`E[z] = kappa * level * sqrt(sessions)`, fitted on the `break` runs):",
        "",
        "| detector | kappa | sd | threshold | sessions for reference |",
        "|---|---:|---:|---|---:|",
    ]
    for det in detectors:
        m = models[det["id"]]
        for label in ("gate", "fixed"):
            b = m["break"]["by_threshold"][label]
            lines.append(
                f"| {det['id']} | {_cell(m['break']['kappa'], '+.3f')} "
                f"| {_cell(m['break']['sd'], '.2f')} | {label} {b['threshold']:.3f} "
                f"| {_cell(b['sessions_needed_at_reference'], '.1f')} |"
            )
    lines += ["", "## What the platform can and cannot detect", ""]
    for det in detectors:
        c = cells.get((det["id"], "stable", 1.0, full))
        if c is not None:
            lines.append(_verdict_sentence(det, c, models[det["id"]]))
    failed = sum(c["n_failed"] for c in doc["cells"])
    leaks = sum(c["n_recompute_failed"] for c in doc["cells"])
    lines += [
        "",
        "## Reading it",
        "",
        "- `at gate` is the significance leg of PROMOTE as the platform applies "
        "it; `promote` additionally needs fold sign consistency, a confirmed "
        "hypothesis sign and net P&L > 0 at 1x costs. `evidence` = verdict "
        "ITERATE or PROMOTE.",
        "- `trades at 1x` is the mean number of trades the cost-aware backtest "
        "makes on the last fold: an alpha whose forecast never clears its "
        "round-trip cost makes none, and its net P&L of 0 does not pass the cost "
        "gate. A detected effect that is too small to trade is still not a "
        "PROMOTE — that is the cost gate working, not a loss of power.",
        "- Session prefixes of one run share data, so the rows of one detector "
        "at different session counts are not independent of each other; the "
        f"{n_seeds} runs within a row are."
        + (
            f" {failed} detector evaluation(s) could not be validated and count as misses."
            if failed
            else ""
        )
        + (
            f" The recompute leakage probe failed in {leaks} run(s)."
            if leaks
            else " The recompute leakage probe (first seed of every cell, full "
            "session count) passed wherever it ran."
        ),
        "- `research/power/POWER_REPORT.md` is the default grid, sized for the CI "
        "`regenerate` job; `research/power/extended/POWER_REPORT.md` is the larger "
        "one (`python -m iap.research power --sessions 1,2,4,8 --levels 0,0.5,1,2 "
        "--power-out-dir ../research/power/extended`, the opt-in `power_extended` "
        "step of `tools/regenerate_dataset_artifacts.py`). Any other grid runs the "
        "same way: `--seeds`, `--sessions`, `--levels`, `--break-levels`, `--jobs`.",
        "",
        "Per-run numbers are in `POWER_REPORT.json`.",
        "",
    ]
    return "\n".join(lines)


def write_reports(doc: Mapping[str, Any], out_dir: Path) -> dict[str, Path]:
    """Write ``POWER_REPORT.json`` and ``POWER_REPORT.md`` under ``out_dir``."""
    out_dir = Path(out_dir)
    paths = {"json": out_dir / "POWER_REPORT.json", "md": out_dir / "POWER_REPORT.md"}
    atomic_write_text(
        paths["json"],
        json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n",
        encoding="ascii",
        newline="\n",
    )
    atomic_write_text(paths["md"], render_markdown(doc), encoding="utf-8", newline="\n")
    return paths
