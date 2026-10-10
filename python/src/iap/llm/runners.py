"""Gated-study runners: ``(alpha_id, horizon, dataset) -> metrics`` computed by
code, never by the model.

- :func:`planted_runner` - the ``synthetic:planted-v1`` dataset: a seeded,
  in-memory signal/label pair per (alpha, horizon) with a known planted
  effect on chosen pairs and pure noise elsewhere.  Milliseconds, no files;
  the evals and the CI tests use it.
- :func:`power_runner` - ``synthetic:seed=<n>``: the full synthetic pipeline
  (generator -> features -> ``validate_alpha``) as the power study and the
  reserve runner use it.  Minutes per run; for real research sessions.

A runner returns a flat dict of numbers/strings/bools; the caller decides the
verdict (:func:`verdict`) and writes the report.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import numpy as np

from iap.agents.blackboard import digest

Runner = Callable[[str, str, str], dict[str, Any]]
PLANTED_DATASET = "synthetic:planted-v1"
#: (alpha, horizon) -> planted sign; everything else is noise
DEFAULT_PLANTED: dict[tuple[str, str], int] = {("EQ02", "1s"): 1}
T_THRESHOLD = 3.0
_SEED_DS = re.compile(r"^synthetic:seed=(\d{1,9})$")


def planted_runner(
    planted: Mapping[tuple[str, str], int] | None = None,
    n_obs: int = 4000,
    effect: float = 0.08,
) -> Runner:
    planted = dict(DEFAULT_PLANTED if planted is None else planted)

    def run(alpha_id: str, horizon: str, dataset: str) -> dict[str, Any]:
        if dataset != PLANTED_DATASET:
            raise ValueError(f"planted runner serves only {PLANTED_DATASET}")
        seed = int(digest({"a": alpha_id, "h": horizon, "d": dataset})[:8], 16)
        rng = np.random.default_rng(seed)
        x = rng.standard_normal(n_obs)
        beta = effect * planted.get((alpha_id, horizon), 0)
        y = beta * x + rng.standard_normal(n_obs)
        ic = float(np.corrcoef(x, y)[0, 1])
        t = ic * math.sqrt((n_obs - 2) / max(1e-12, 1.0 - ic * ic))
        half = n_obs // 2
        ic_a = float(np.corrcoef(x[:half], y[:half])[0, 1])
        ic_b = float(np.corrcoef(x[half:], y[half:])[0, 1])
        return {
            "n_obs": n_obs,
            "gate_ic": round(ic, 6),
            "t_stat": round(t, 4),
            "ic_first_half": round(ic_a, 6),
            "ic_second_half": round(ic_b, 6),
            "leakage_passed": True,
        }

    return run


def power_runner(configs_dir: Path, generator_config: Path, sessions: int = 2) -> Runner:
    """The real synthetic pipeline at an explicit generator seed."""
    from iap.research import power

    configs_dir = Path(configs_dir)
    base = json.loads(Path(generator_config).read_text(encoding="utf-8"))

    def run(alpha_id: str, horizon: str, dataset: str) -> dict[str, Any]:
        m = _SEED_DS.match(dataset)
        if not m:
            raise ValueError("power runner serves only synthetic:seed=<n>")
        cfg = {**base, "seed": int(m.group(1)), "sessions": int(sessions)}
        det = {"id": "llm", "alpha_id": alpha_id, "horizon": horizon}
        work = Path(tempfile.mkdtemp(prefix="iap-llm-"))
        try:
            frames = power.build_planted_frames(cfg, configs_dir, work)
            row = power.evaluate_run(frames, configs_dir, T_THRESHOLD, [det], seed=0)["llm"]
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if "error" in row:
            raise ValueError(row["error"])
        out = {k: v for k, v in row.items() if k not in ("alpha_id", "horizon")}
        out["t_stat"] = out.get("t_pooled")
        return out

    return run


def verdict(metrics: Mapping[str, Any], expected_sign: int, t_threshold: float) -> str:
    """Code's verdict on a pre-registered hypothesis (never the model's)."""
    ic, t = metrics.get("gate_ic"), metrics.get("t_stat")
    if not metrics.get("leakage_passed", False) or ic is None or t is None:
        return "invalid"
    if ic * expected_sign > 0 and t * expected_sign >= t_threshold:
        return "confirmed"
    if ic * expected_sign > 0:
        return "weaker_not_confirmed"
    return "rejected"
