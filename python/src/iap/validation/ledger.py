"""Multiple-testing ledger (spec §13: "control multiple testing and report
the number of experiments conducted").

Every evaluated alpha/configuration is one recorded experiment in
``research/experiments.json``.  The ledger is append-only within a run,
persisted as sorted JSON, and deliberately wall-clock-free (conventions §3:
reruns of the same code on the same data produce byte-identical ledgers).

Multiple-testing math reported with every batch:

- Bonferroni: a per-test significance threshold ``alpha / n_experiments``
  (alpha = 0.05 pinned) and the |t| threshold it implies under a normal
  approximation.
- Deflated-Sharpe-style note: with n independent trials the expected
  maximum |t| under the global null grows like sqrt(2 ln n); any observed
  t-stat below that is consistent with pure selection.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import List, Optional

PINNED_ALPHA = 0.05


def _norm_ppf(p: float) -> float:
    """Inverse standard normal CDF (Acklam's rational approximation,
    deterministic, |err| < 1.2e-8 — plenty for reporting thresholds)."""
    if not 0.0 < p < 1.0:
        raise ValueError("p must be in (0, 1)")
    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00)
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    if p > phigh:
        return -_norm_ppf(1 - p)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (
        ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1
    )


class ExperimentLedger:
    """Persistent experiment counter + entries (research/experiments.json)."""

    def __init__(self, path) -> None:
        self.path = Path(path)
        self.entries: List[dict] = []
        self.total_experiments = 0
        if self.path.exists():
            blob = json.loads(self.path.read_text())
            self.entries = list(blob.get("entries", []))
            self.total_experiments = int(
                blob.get("total_experiments", len(self.entries))
            )

    def record(
        self,
        alpha_id: str,
        kind: str,
        config: Optional[dict] = None,
        result: Optional[dict] = None,
        count: int = 1,
    ) -> int:
        """Record ``count`` experiments (count > 1 = a declared batch, e.g. a
        horizon scan) and return the running total."""
        if count < 1:
            raise ValueError("count must be >= 1")
        self.total_experiments += count
        self.entries.append(
            {
                "n": self.total_experiments,
                "alpha_id": alpha_id,
                "kind": kind,
                "count": count,
                "config": config or {},
                "result": result or {},
            }
        )
        return self.total_experiments

    # -- multiple-testing report ----------------------------------------

    def bonferroni_threshold(self) -> float:
        n = max(self.total_experiments, 1)
        return PINNED_ALPHA / n

    def bonferroni_t_threshold(self) -> float:
        """|t| needed for two-sided significance at the Bonferroni level."""
        p = self.bonferroni_threshold() / 2.0
        return abs(_norm_ppf(p))

    def expected_max_null_t(self) -> float:
        """Deflated-Sharpe-style yardstick: E[max |t|] under the global null
        with n independent trials ~ sqrt(2 ln n)."""
        n = max(self.total_experiments, 2)
        return math.sqrt(2.0 * math.log(n))

    def note(self) -> str:
        return (
            f"Multiple testing: {self.total_experiments} experiments recorded. "
            f"Bonferroni per-test p-threshold {self.bonferroni_threshold():.2e} "
            f"(|t| >= {self.bonferroni_t_threshold():.2f}); deflated-Sharpe-style "
            f"selection yardstick: expected max |t| under the global null is "
            f"~{self.expected_max_null_t():.2f} — any t below that is consistent "
            f"with pure selection over this many trials."
        )

    def save(self) -> None:
        blob = {
            "x-version": 1,
            "description": (
                "Multiple-testing ledger (spec §13). Deterministic: no "
                "wall-clock; identical rerun => identical file."
            ),
            "total_experiments": self.total_experiments,
            "pinned_alpha": PINNED_ALPHA,
            "bonferroni_p_threshold": self.bonferroni_threshold(),
            "bonferroni_t_threshold": self.bonferroni_t_threshold(),
            "expected_max_null_t": self.expected_max_null_t(),
            "entries": self.entries,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(blob, indent=2, sort_keys=True) + "\n")
