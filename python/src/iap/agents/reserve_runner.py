"""The reserve runner: a candidate on a freshly generated session at a hidden seed.

``make_runner`` returns the ``(seed, candidate) -> bool`` the evaluator calls.
It generates the synthetic dataset with the generator config's ``seed``
replaced by the hidden seed (:func:`iap.research.power.build_planted_frames`,
inside a scratch directory that is deleted afterwards), runs the candidate
through the same ``validate_alpha`` chain the power study uses, and passes iff
the validation ran, the leakage check passed, the pooled t-statistic reaches
``t_threshold`` and the sign of the gate IC equals the candidate's
``expected_sign`` (the evaluator checks it against the pre-registration).
Nothing but the boolean leaves this function.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

DEFAULT_T = 3.0


def make_runner(
    configs_dir: Path,
    generator_config: Path,
    sessions: int = 2,
    t_threshold: float = DEFAULT_T,
    scratch_root: Path | None = None,
) -> Callable[[int, Mapping[str, Any]], bool]:
    from iap.research import power

    configs_dir = Path(configs_dir)
    base = json.loads(Path(generator_config).read_text(encoding="utf-8"))

    def run(seed: int, candidate: Mapping[str, Any]) -> bool:
        cfg = {**base, "seed": int(seed), "sessions": int(sessions)}
        det = {
            "id": "reserve",
            "alpha_id": str(candidate["alpha_id"]),
            "horizon": str(candidate["horizon"]),
        }
        work = Path(tempfile.mkdtemp(prefix="iap-reserve-", dir=scratch_root))
        try:
            frames = power.build_planted_frames(cfg, configs_dir, work)
            row = power.evaluate_run(frames, configs_dir, t_threshold, [det], seed=0)["reserve"]
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if "error" in row or not row.get("leakage_passed") or row.get("t_pooled") is None:
            return False
        ic = row.get("gate_ic")
        sign = int(candidate["expected_sign"])
        return bool(ic is not None and ic * sign > 0 and row["t_pooled"] * sign >= t_threshold)

    return run
