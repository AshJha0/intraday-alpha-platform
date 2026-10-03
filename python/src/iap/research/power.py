"""Planted-signal power study: how often does the validation chain detect
an effect of KNOWN size?

Every other research artefact in this repository asks "is there signal in
the data?" of data whose truth nobody knows.  This module asks the question
the answers depend on: *when a signal of size s is really there, how often
does* :func:`iap.validation.validate.validate_alpha` *say so — and when
nothing is there, how often does it say so anyway?*  Without that curve a
REJECT is uninterpretable (no signal, or no power?) and so is a PROMOTE.

**Design (pinned, version :data:`POWER_VERSION`).**

1. *Planted data.*  The synthetic generator's opt-in ``planted`` block
   (:mod:`iap.marketdata.generator`) plants two independent effects into the
   equity efficient prices and order flow:

   * ``order_flow`` — informed aggressor flow whose sign leads the mid along
     an exponential impact kernel; the detector is the trade-flow alpha
     **EQ04** (10 s trade imbalance against the 5 s label);
   * ``lead_lag`` — every constituent follows the ETF's efficient return
     with a lag; the detector is the index lead-lag alpha **EQ10** (ETF 1 s
     return against the constituent's 1 s label).

   The base generator config is ``research/power/generator_planted.json``;
   its ``planted`` block is the REFERENCE effect (level 1.0).  A grid
   *level* L scales both reference strengths (``order_flow.strength`` and
   ``lead_lag.beta``) by L; level 0 is the null (no effect planted, the
   false-positive row).
2. *Scenarios.*  ``stable`` plants the effect for the whole sample.
   ``break`` plants it for the first half and REVERSES it for the second
   (``planted.break``: ``at_fraction`` 0.5, ``post_multiplier`` -1): the
   effect a naive full-sample fit would average to nothing, and exactly
   what fold sign consistency exists to catch.  The break scenario is run
   only at non-zero levels (the null has nothing to break).
3. *Universe and pipeline.*  A reduced universe (:data:`DEFAULT_UNIVERSE`:
   two constituents and the ETF, the bundled venues, no FX) keeps one cell
   to tens of seconds.  Each cell runs the REAL pipeline end to end —
   generator, raw->normalized QC, the feature engine and label sweep, the
   alpha's own fit, and ``validate_alpha`` with the pinned research
   execution model (4 folds, 60 s embargo, 1 s latency, 60 s decision age,
   session flattening, 1x costs) — into a scratch directory that is removed
   afterwards.  Nothing under ``data/`` or the ledger is touched: a planted
   dataset is not the research dataset, and its looks are not looks at it.
4. *Replication.*  Each (scenario, level) cell is generated under
   ``n_seeds`` generator seeds drawn from SplitMix64 seeded with the base
   config's seed, so the whole study is a pure function of the config, the
   grid and the code.
5. *Detection.*  Per run and detector, from the validation report:

   * ``sig_within``  — gate IC > 0 and the gate's Newey-West t (mean of
     WITHIN-bucket ICs, the statistic the PROMOTE gate reads) >= 3.0;
   * ``sig_pooled``  — gate IC > 0 and the POOLED-slope HAC t
     (:func:`iap.validation.metrics.pooled_slope_hac_tstat`) >= 3.0;
   * ``sig_ledger``  — gate IC > 0 and the within-bucket t >= the
     multiple-testing threshold of THIS study: the Bonferroni |t| at
     ``n_tests`` = runs x detectors
     (``ExperimentLedger.bonferroni_t_threshold_at``), i.e. the opt-in
     ``tstat_threshold="ledger"`` policy applied to the study's own number
     of looks;
   * ``evidence``    — verdict ITERATE or PROMOTE;
   * ``promote``     — verdict PROMOTE (needs cost survival too);
   * ``pnl_ci_positive`` — the lower end of the 95 % stationary-bootstrap
     interval of the pooled walk-forward net P&L at 1x costs is above zero
     (:func:`iap.validation.diagnostics.fold_diagnostics`, seeded with the
     run's generator seed).

   A cell reports each as a rate over its seeds, with the mean gate IC, the
   mean vol-scaled IC (:func:`iap.validation.metrics.instrument_ics` — each
   instrument on a unit scale, so the most volatile one cannot dominate the
   pool), the two mean t-statistics, the mean fold sign consistency and the
   mean number of folds that survive 1x costs.

**Reading the table.**  The level-0 rows are the size of the test (the
false-positive rate at |t| >= 3 should be near zero).  The stable rows are
its power.  The break rows are a robustness check: a reversed effect should
NOT be reported as evidence, and fold sign consistency should fall.  With a
handful of seeds per cell a rate is coarse (steps of 1 / n_seeds); the
table is a calibration of the chain, not a publication-grade power curve.

Outputs: ``research/power/POWER_REPORT.md`` and ``POWER_REPORT.json`` —
deterministic (no wall clock, sorted keys, rounded floats).
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import math
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

import pandas as pd

from iap.alpha import build
from iap.alpha.data import load_features
from iap.backtest import Backtester, BacktestConfig, CostModel
from iap.core.rng import SplitMix64
from iap.experiment.locking import atomic_write_text
from iap.marketdata.generator import MarketDataGenerator, load_generator_config
from iap.marketdata.normalize import normalize_run
from iap.reference.refdata import ReferenceData
from iap.research.errors import ResearchError
from iap.research.runner import load_instrument_meta
from iap.research.specs import DEFAULT_CONFIGURATION
from iap.validation.diagnostics import fold_diagnostics
from iap.validation.ledger import ExperimentLedger
from iap.validation.stress import STRESS_VERSION_CARRY
from iap.validation.validate import GATES, validate_alpha

__all__ = [
    "DEFAULT_LEVELS",
    "DEFAULT_SEEDS",
    "DEFAULT_UNIVERSE",
    "DETECTORS",
    "POWER_VERSION",
    "SCENARIOS",
    "build_planted_frames",
    "cell_config",
    "evaluate_run",
    "render_markdown",
    "run_power_study",
    "study_seeds",
    "summarise",
    "write_reports",
]

#: ``x-version`` of ``POWER_REPORT.json``.
POWER_VERSION = 1

#: Bootstrap resamples per run for the net-P&L interval (kept small: the
#: study runs it once per run and detector).
BOOTSTRAP_RESAMPLES = 300

#: Reduced universe of the study: two constituents and the ETF (the leader).
DEFAULT_UNIVERSE = ("SYN.EQ.001", "SYN.EQ.002", "SYN.ETF.IDX")

#: Multipliers of the reference planted effect; 0 is the null row.
DEFAULT_LEVELS = (0.0, 0.5, 1.0, 2.0)

#: Generator seeds per cell.
DEFAULT_SEEDS = 3

#: planted effect -> the flagship alpha expected to detect it.
DETECTORS: Dict[str, str] = {"order_flow": "EQ04", "lead_lag": "EQ10"}

#: scenario -> the ``planted.break`` block it runs under.
SCENARIOS: Dict[str, Dict[str, Any]] = {
    "stable": {"at_fraction": None, "post_multiplier": 1.0},
    "break": {"at_fraction": 0.5, "post_multiplier": -1.0},
}

_ROUND = 6


def _fail(message: str) -> ResearchError:
    return ResearchError(message, code="power_study_error")


def study_seeds(base_seed: int, n_seeds: int) -> List[int]:
    """``n_seeds`` generator seeds from SplitMix64(``base_seed``) (31-bit, so
    they stay exact through JSON and every port's integer type)."""
    if n_seeds < 1:
        raise _fail("n_seeds must be >= 1")
    rng = SplitMix64(int(base_seed))
    return [int(rng.next_u64() >> 33) for _ in range(n_seeds)]


def cell_config(base: Mapping[str, Any], level: float, scenario: str,
                seed: int) -> Dict[str, Any]:
    """The generator config of one run: ``base`` with both reference planted
    strengths scaled by ``level``, the scenario's break block and ``seed``."""
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
    return cfg


def _reduced_configs(configs_dir: Path, universe: Sequence[str], out: Path) -> Path:
    """A configs tree under ``out`` whose instruments are exactly ``universe``."""
    instruments = json.loads(
        (configs_dir / "instruments" / "instruments.json").read_text(encoding="utf-8"))
    rows = [r for r in instruments["instruments"] if r["symbol"] in set(universe)]
    missing = sorted(set(universe) - {r["symbol"] for r in rows})
    if missing:
        raise _fail(f"universe symbols not in instruments.json: {missing}")
    instruments["instruments"] = rows
    for sub, name, doc in (
        ("instruments", "instruments.json", instruments),
        ("venues", "venues.json",
         json.loads((configs_dir / "venues" / "venues.json").read_text(encoding="utf-8"))),
    ):
        (out / sub).mkdir(parents=True, exist_ok=True)
        (out / sub / name).write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n",
                                      encoding="utf-8")
    return out


def build_planted_frames(
    generator_cfg: Mapping[str, Any],
    configs_dir: Path,
    work_dir: Path,
    universe: Sequence[str] = DEFAULT_UNIVERSE,
) -> Dict[int, pd.DataFrame]:
    """Generator -> normalise -> features + labels for one planted config,
    entirely inside ``work_dir``; returns the per-instrument feature frames."""
    from iap.features.__main__ import main as features_main

    work_dir = Path(work_dir)
    cfg_dir = _reduced_configs(Path(configs_dir), universe, work_dir / "configs")
    refdata = ReferenceData.load(cfg_dir)
    raw, normalized, features = (work_dir / n for n in ("raw", "normalized", "features"))
    MarketDataGenerator(refdata, dict(generator_cfg)).generate_run(raw)
    for path in sorted(raw.glob("*.jsonl")):
        if path.stat().st_size == 0:      # no FX instruments -> empty FX files
            path.unlink()
    normalize_run(raw, normalized)
    sink = io.StringIO()
    with contextlib.redirect_stderr(sink), contextlib.redirect_stdout(sink):
        features_main([
            "--data-dir", str(normalized), "--out-dir", str(features),
            "--configs", str(cfg_dir),
            "--registry-out", str(work_dir / "feature_registry.json"),
        ])
    return load_features(features)


def _backtester(configs_dir: Path) -> Backtester:
    cfg = DEFAULT_CONFIGURATION
    return Backtester(
        CostModel.load(Path(configs_dir) / "execution" / "execution.json"),
        load_instrument_meta(Path(configs_dir)),
        BacktestConfig(
            latency_ns=int(cfg["latency_ns"]),
            max_decision_age_ns=int(cfg["max_decision_age_ns"]),
            flatten_at_session_end=bool(cfg["flatten_at_session_end"]),
        ),
    )


def evaluate_run(frames: Mapping[int, pd.DataFrame], configs_dir: Path,
                 ledger_t_threshold: Optional[float] = None,
                 seed: int = 0) -> Dict[str, dict]:
    """``validate_alpha`` (and the per-fold diagnostics) for every detector
    on one planted dataset.

    ``ledger_t_threshold`` is the study's multiple-testing |t| (``None``:
    the fixed gate); ``seed`` seeds the P&L bootstrap.  Returns
    ``{effect: row}``; a detector whose validation cannot run at all (too
    few rows for the splitter) yields ``{"error": ...}`` and counts as not
    detected — an under-powered sample is a miss, not a crash."""
    configs_dir = Path(configs_dir)
    backtester = _backtester(configs_dir)
    meta = load_instrument_meta(configs_dir)
    exec_cfg = json.loads(
        (configs_dir / "execution" / "execution.json").read_text(encoding="utf-8"))
    max_participation = float(exec_cfg["defaults"]["max_participation"])
    out: Dict[str, dict] = {}
    for effect in sorted(DETECTORS):
        alpha_id = DETECTORS[effect]

        def factory(alpha_id: str = alpha_id):
            return build(alpha_id)

        try:
            report = validate_alpha(
                factory, frames, backtester, meta, max_participation,
                n_folds=int(DEFAULT_CONFIGURATION["n_folds"]),
                embargo_ns=int(DEFAULT_CONFIGURATION["embargo_ns"]),
                stress_version=STRESS_VERSION_CARRY,
            )
            diagnostics = fold_diagnostics(
                factory, frames, backtester,
                n_folds=int(DEFAULT_CONFIGURATION["n_folds"]),
                embargo_ns=int(DEFAULT_CONFIGURATION["embargo_ns"]),
                seed=seed, n_boot=BOOTSTRAP_RESAMPLES,
            )
        except ValueError as exc:
            out[effect] = {"alpha_id": alpha_id, "error": str(exc)}
            continue
        out[effect] = _run_row(alpha_id, report, diagnostics, ledger_t_threshold)
    return out


def _run_row(alpha_id: str, report: Mapping[str, Any],
             diagnostics: Mapping[str, Any],
             ledger_t_threshold: Optional[float] = None) -> Dict[str, Any]:
    gate_ic = report["gate_ic"]
    t_within = report["nw_tstat_uncrossed"]
    if t_within is None:
        t_within = report["nw_tstat"]
    t_pooled = report["nw_tstat_pooled_uncrossed"]
    if t_pooled is None:
        t_pooled = report["nw_tstat_pooled"]
    positive = gate_ic is not None and gate_ic > 0.0
    threshold = float(GATES["min_nw_tstat"])
    ledger_threshold = max(threshold, float(ledger_t_threshold)) \
        if ledger_t_threshold is not None else threshold
    boot = diagnostics["net_pnl_bootstrap"]
    return {
        "alpha_id": alpha_id,
        "gate_ic": gate_ic,
        "ic_vol_scaled": report["oos_ic_vol_scaled"],
        "ic_instrument_mean": report["oos_ic_instrument_mean"],
        "n_folds_survive_1x_cost": int(diagnostics["n_folds_survive_1x_cost"]),
        "net_pnl_1x_pooled": diagnostics["net_pnl_1x_pooled"],
        "net_pnl_ci_low": boot["ci_low"],
        "net_pnl_ci_high": boot["ci_high"],
        "sig_ledger": bool(positive and t_within is not None
                           and t_within >= ledger_threshold),
        "pnl_ci_positive": bool(boot["ci_low"] is not None and boot["ci_low"] > 0.0),
        "t_within": t_within,
        "t_pooled": t_pooled,
        "fold_sign_consistency": report["fold_sign_consistency"],
        "hypothesis_confirmed": bool(report["hypothesis_confirmed"]),
        "leakage_passed": bool(report["leakage"]["passed"]),
        "verdict": str(report["verdict"]),
        "sig_within": bool(positive and t_within is not None and t_within >= threshold),
        "sig_pooled": bool(positive and t_pooled is not None and t_pooled >= threshold),
        "evidence": report["verdict"] in ("ITERATE", "PROMOTE"),
        "promote": report["verdict"] == "PROMOTE",
    }


def _frame_rows(frames: Mapping[int, pd.DataFrame]) -> Dict[str, int]:
    """Feature rows per instrument of one planted dataset (for the record)."""
    return {str(i): int(len(frames[i])) for i in sorted(frames)}


def _mean(values: Sequence[Optional[float]]) -> Optional[float]:
    finite = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    return round(sum(finite) / len(finite), _ROUND) if finite else None


def summarise(runs: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """One row per (effect, scenario, level): rates and means over seeds."""
    cells: Dict[tuple, List[Mapping[str, Any]]] = {}
    for run in runs:
        for effect, row in run["detectors"].items():
            cells.setdefault((effect, run["scenario"], float(run["level"])), []).append(row)
    out: List[Dict[str, Any]] = []
    for effect, scenario, level in sorted(cells):
        rows = cells[(effect, scenario, level)]
        ok = [r for r in rows if "error" not in r]
        n = len(rows)

        def rate(key: str) -> float:
            return round(sum(1 for r in ok if r[key]) / n, _ROUND)

        out.append({
            "effect": effect,
            "alpha_id": DETECTORS[effect],
            "scenario": scenario,
            "level": level,
            "n_runs": n,
            "n_failed": n - len(ok),
            "rate_sig_within": rate("sig_within"),
            "rate_sig_pooled": rate("sig_pooled"),
            "rate_sig_ledger": rate("sig_ledger"),
            "rate_pnl_ci_positive": rate("pnl_ci_positive"),
            "mean_ic_vol_scaled": _mean([r["ic_vol_scaled"] for r in ok]),
            "mean_folds_survive_1x_cost": _mean(
                [float(r["n_folds_survive_1x_cost"]) for r in ok]),
            "rate_evidence": rate("evidence"),
            "rate_promote": rate("promote"),
            "mean_gate_ic": _mean([r["gate_ic"] for r in ok]),
            "mean_t_within": _mean([r["t_within"] for r in ok]),
            "mean_t_pooled": _mean([r["t_pooled"] for r in ok]),
            "mean_fold_sign_consistency": _mean(
                [r["fold_sign_consistency"] for r in ok]),
        })
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


def run_power_study(
    generator_config_path: Path,
    configs_dir: Path,
    levels: Sequence[float] = DEFAULT_LEVELS,
    n_seeds: int = DEFAULT_SEEDS,
    scenarios: Sequence[str] = tuple(SCENARIOS),
    universe: Sequence[str] = DEFAULT_UNIVERSE,
    scratch_dir: Optional[Path] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Run the grid and return the ``POWER_REPORT.json`` document.

    ``scratch_dir`` (default: a fresh temporary directory) holds one
    sub-directory per run and is removed afterwards; ``progress`` receives
    one line per run.  Deterministic: same config, grid and code => same
    document.
    """
    generator_config_path = Path(generator_config_path)
    if not generator_config_path.is_file():
        raise _fail(f"generator config not found: {generator_config_path}")
    base = load_generator_config(generator_config_path)
    reference = copy.deepcopy(base["planted"])
    if float(reference["order_flow"]["strength"]) == 0.0 and \
            float(reference["lead_lag"]["beta"]) == 0.0:
        raise _fail(
            f"{generator_config_path}: the planted block is all zero — the "
            "reference effect (level 1.0) must plant something")
    levels = [float(v) for v in levels]
    if not levels or sorted(set(levels)) != levels:
        raise _fail("levels must be a non-empty strictly increasing list")
    for name in scenarios:
        if name not in SCENARIOS:
            raise _fail(f"unknown scenario {name!r}; known: {sorted(SCENARIOS)}")
    seeds = study_seeds(int(base["seed"]), n_seeds)
    grid = [(scenario, level) for scenario in scenarios for level in levels
            if scenario == "stable" or level != 0.0]     # the null has nothing to break
    n_tests = len(grid) * len(seeds) * len(DETECTORS)
    ledger_t = ExperimentLedger.bonferroni_t_threshold_at(n_tests)
    owned = scratch_dir is None
    scratch = Path(tempfile.mkdtemp(prefix="iap-power-")) if owned else Path(scratch_dir)
    scratch.mkdir(parents=True, exist_ok=True)
    runs: List[Dict[str, Any]] = []
    try:
        for scenario, level in grid:
            for seed in seeds:
                work = scratch / f"{scenario}-{level:g}-{seed}"
                if work.exists():
                    shutil.rmtree(work)
                try:
                    frames = build_planted_frames(
                        cell_config(base, level, scenario, seed),
                        Path(configs_dir), work, universe)
                    detectors = evaluate_run(frames, Path(configs_dir),
                                             ledger_t_threshold=ledger_t, seed=seed)
                finally:
                    shutil.rmtree(work, ignore_errors=True)
                # rounded BEFORE the cells are computed, so the cells are
                # exactly summarise(runs) of the document that is written
                runs.append(_rounded({"scenario": scenario, "level": level, "seed": seed,
                                      "rows": _frame_rows(frames),
                                      "detectors": detectors}))
                if progress is not None:
                    progress(
                        f"{scenario} level {level:g} seed {seed}: " + ", ".join(
                            f"{e}={d.get('verdict', 'ERROR')}"
                            for e, d in sorted(detectors.items())))
    finally:
        if owned:
            shutil.rmtree(scratch, ignore_errors=True)
    return _rounded({
        "x-version": POWER_VERSION,
        "description": (
            "Planted-signal power study (iap.research.power): detection rates "
            "of validate_alpha on synthetic data with effects of known size. "
            "Deterministic: no wall clock; same config + grid + code => same "
            "document."),
        "generator_config": generator_config_path.name,
        "base_seed": int(base["seed"]),
        "seeds": seeds,
        "universe": list(universe),
        "levels": levels,
        "scenarios": {name: SCENARIOS[name] for name in scenarios},
        "detectors": dict(DETECTORS),
        "reference_planted": {
            "order_flow": reference["order_flow"],
            "lead_lag": reference["lead_lag"],
        },
        "protocol": {**DEFAULT_CONFIGURATION, "stress_version": STRESS_VERSION_CARRY,
                     "min_nw_tstat": float(GATES["min_nw_tstat"]),
                     "n_tests": n_tests, "ledger_t_threshold": ledger_t,
                     "bootstrap_resamples": BOOTSTRAP_RESAMPLES},
        "cells": summarise(runs),
        "runs": runs,
    })


def _cell(value: Any, spec: str) -> str:
    return "n/a" if value is None else format(value, spec)


def render_markdown(doc: Mapping[str, Any]) -> str:
    """``POWER_REPORT.md`` for a study document (deterministic)."""
    ref = doc["reference_planted"]
    lines = [
        "# Planted-signal power study",
        "",
        "Generated by `python -m iap.research power` (`iap.research.power`, "
        f"report version {doc['x-version']}). Do not edit by hand.",
        "",
        "How often does the validation chain (`iap.validation.validate_alpha`, "
        "the pinned research execution model) detect an effect of known size "
        "planted into the synthetic generator, and how often does it report "
        "one that is not there?",
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
        f"{ref['lead_lag']['lag_steps']} step(s). A level scales both.",
        f"- Levels: {', '.join(format(v, 'g') for v in doc['levels'])} "
        f"(0 = null); {len(doc['seeds'])} generator seeds per cell "
        f"({', '.join(str(s) for s in doc['seeds'])}).",
        "- Scenarios: `stable` = the effect holds for the whole sample; "
        "`break` = it is reversed from the middle of the sample on.",
        "- Detectors: " + ", ".join(
            f"{effect} -> {alpha}" for effect, alpha in sorted(doc["detectors"].items()))
        + ".",
        f"- Significance: gate IC > 0 and t >= {doc['protocol']['min_nw_tstat']:g}. "
        "`within` is the Newey-West t of within-bucket ICs (what the PROMOTE "
        "gate reads); `pooled` is the pooled-slope HAC t; `ledger` is the "
        "within-bucket t against the multiple-testing threshold of this "
        f"study's own {doc['protocol']['n_tests']} tests "
        f"(t >= {doc['protocol']['ledger_t_threshold']:.2f}).",
        "- `P&L CI > 0`: the 95 % stationary-bootstrap interval "
        f"({doc['protocol']['bootstrap_resamples']} resamples) of the pooled "
        "walk-forward net P&L at 1x costs lies above zero.",
        "",
        "## Detection rates",
        "",
        "| effect | alpha | scenario | level | runs | sig (within) | sig (pooled) "
        "| sig (ledger) | evidence | promote | P&L CI > 0 |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for c in doc["cells"]:
        lines.append(
            f"| {c['effect']} | {c['alpha_id']} | {c['scenario']} | {c['level']:g} "
            f"| {c['n_runs']} | {c['rate_sig_within']:.2f} | {c['rate_sig_pooled']:.2f} "
            f"| {c['rate_sig_ledger']:.2f} | {c['rate_evidence']:.2f} "
            f"| {c['rate_promote']:.2f} | {c['rate_pnl_ci_positive']:.2f} |")
    lines += [
        "",
        "## Statistics behind the rates (means over the seeds of a cell)",
        "",
        "| effect | alpha | scenario | level | gate IC (pooled) | IC (vol-scaled) "
        "| t within | t pooled | fold consistency | folds surviving 1x cost |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for c in doc["cells"]:
        lines.append(
            f"| {c['effect']} | {c['alpha_id']} | {c['scenario']} | {c['level']:g} "
            f"| {_cell(c['mean_gate_ic'], '+.4f')} "
            f"| {_cell(c['mean_ic_vol_scaled'], '+.4f')} "
            f"| {_cell(c['mean_t_within'], '+.2f')} "
            f"| {_cell(c['mean_t_pooled'], '+.2f')} "
            f"| {_cell(c['mean_fold_sign_consistency'], '.2f')} "
            f"| {_cell(c['mean_folds_survive_1x_cost'], '.2f')} |")
    failed = sum(c["n_failed"] for c in doc["cells"])
    lines += [
        "",
        "`evidence` = verdict ITERATE or PROMOTE; `promote` additionally needs "
        "fold consistency, a confirmed hypothesis sign and net P&L > 0 at 1x "
        "costs. Rates are over the seeds of the cell"
        + (f"; {failed} detector run(s) could not be validated and count as "
           "misses." if failed else "."),
        "",
        "## Reading it",
        "",
        "- Level 0 is the false-positive row: nothing is planted, so every "
        "non-zero rate there is a false detection.",
        "- The `stable` rows are the power of the chain at each effect size.",
        "- `sig (pooled)` and `sig (ledger)` against `sig (within)` show what "
        "the corrected statistics change: the pooled t keeps between-bucket "
        "signal the within-bucket t discards, and the ledger threshold "
        "charges the study for the number of things it tried.",
        "- The `break` rows plant an effect that reverses mid-sample. A chain "
        "that reports `evidence` there is averaging over a regime change; "
        "fold sign consistency is the statistic that should fall.",
        f"- With {len(doc['seeds'])} seeds a rate moves in steps of "
        f"{1 / len(doc['seeds']):.2f}: this calibrates the chain, it is not a "
        "precise power curve.",
        "",
        "Per-run numbers are in `POWER_REPORT.json`.",
        "",
    ]
    return "\n".join(lines)


def write_reports(doc: Mapping[str, Any], out_dir: Path) -> Dict[str, Path]:
    """Write ``POWER_REPORT.json`` and ``POWER_REPORT.md`` under ``out_dir``."""
    out_dir = Path(out_dir)
    paths = {"json": out_dir / "POWER_REPORT.json", "md": out_dir / "POWER_REPORT.md"}
    atomic_write_text(
        paths["json"],
        json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n",
        encoding="ascii", newline="\n")
    atomic_write_text(paths["md"], render_markdown(doc), encoding="utf-8", newline="\n")
    return paths
