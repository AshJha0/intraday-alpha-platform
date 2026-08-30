"""Refit policies as first-class objects (adaptability layer).

A refit policy answers exactly one question, deterministically and without
wall-clock: *given what is knowable at event time T, should the alpha's
parameters be re-fitted now?*  Policies never see future data — the engine
(:mod:`iap.backtest.adaptive`) hands them a :class:`RefitContext` built
solely from information available strictly before T.

Pinned decision rules (mirrored in /API_ADAPTIVE.md):

- :class:`StaticPolicy` — fit once at deployment, never again.
- :class:`ScheduledPolicy(period_ns)` — calendar-aligned cadence: refit at
  the first evaluation after an epoch-aligned period boundary is crossed,
  i.e. when ``now_ns // period_ns > last_fit_ns // period_ns`` (pinned).
  With period_ns = 1 day this is the classic "refit before the next
  session on everything seen so far"; a boundary landing exactly on an
  evaluation time fires at that evaluation.
- :class:`DriftTriggeredPolicy(psi_threshold, ic_z_threshold,
  min_refit_gap_ns)`` — refit when
  ``now_ns - last_fit_ns >= min_refit_gap_ns`` AND
  (``max monitored PSI > psi_threshold``  [strict >, pinned]  OR
  ``ic_z < ic_z_threshold``               [strict <, pinned]).
  A PSI of None (window too thin) or an ic_z of None (too few matured
  buckets) can never trigger — monitors without data are silent.

Thresholds are pinned in ``configs/strategies.json`` under the
``adaptive`` block; :func:`load_adaptive_config` validates it.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass(frozen=True)
class RefitContext:
    """Everything a policy may look at — knowable strictly before now_ns."""

    now_ns: int                       # decision event time (block boundary)
    last_fit_ns: int                  # event time of the last (re)fit
    psi_by_series: Dict[str, Optional[float]] = field(default_factory=dict)
    ic_z: Optional[float] = None      # rolling realized-IC z vs research

    @property
    def elapsed_ns(self) -> int:
        return self.now_ns - self.last_fit_ns

    @property
    def psi_max(self) -> Optional[float]:
        vals = [v for v in self.psi_by_series.values() if v is not None]
        return max(vals) if vals else None


@dataclass(frozen=True)
class RefitDecision:
    refit: bool
    reasons: List[str] = field(default_factory=list)


class RefitPolicy(ABC):
    """First-class refit policy: pure function of a RefitContext."""

    name: str = ""

    @abstractmethod
    def should_refit(self, ctx: RefitContext) -> RefitDecision:
        """Deterministic refit decision at ctx.now_ns."""

    def describe(self) -> dict:
        """JSON-serializable policy description (for reports/logs)."""
        return {"policy": self.name}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} {self.describe()}>"


class StaticPolicy(RefitPolicy):
    """Fit once at deployment; never refit (the rigid-system control arm)."""

    name = "static"

    def should_refit(self, ctx: RefitContext) -> RefitDecision:
        return RefitDecision(refit=False)


class ScheduledPolicy(RefitPolicy):
    """Refit on an epoch-aligned event-time cadence (rule pinned above)."""

    name = "scheduled"

    def __init__(self, period_ns: int) -> None:
        if period_ns <= 0:
            raise ValueError("ScheduledPolicy period_ns must be positive")
        self.period_ns = int(period_ns)

    def should_refit(self, ctx: RefitContext) -> RefitDecision:
        if ctx.now_ns // self.period_ns > ctx.last_fit_ns // self.period_ns:
            return RefitDecision(
                refit=True,
                reasons=[
                    f"scheduled: period boundary crossed "
                    f"({ctx.last_fit_ns} -> {ctx.now_ns}, period {self.period_ns})"
                ],
            )
        return RefitDecision(refit=False)

    def describe(self) -> dict:
        return {"policy": self.name, "period_ns": self.period_ns}


class DriftTriggeredPolicy(RefitPolicy):
    """Refit when distribution drift or realized-IC decay says the fitted
    world no longer matches the deployed world (rules pinned above)."""

    name = "drift_triggered"

    def __init__(
        self,
        psi_threshold: float,
        ic_z_threshold: float,
        min_refit_gap_ns: int = 0,
    ) -> None:
        if psi_threshold < 0.0:
            raise ValueError("psi_threshold must be >= 0")
        if min_refit_gap_ns < 0:
            raise ValueError("min_refit_gap_ns must be >= 0")
        self.psi_threshold = float(psi_threshold)
        self.ic_z_threshold = float(ic_z_threshold)
        self.min_refit_gap_ns = int(min_refit_gap_ns)

    def should_refit(self, ctx: RefitContext) -> RefitDecision:
        if ctx.elapsed_ns < self.min_refit_gap_ns:
            return RefitDecision(refit=False)
        reasons: List[str] = []
        for series in sorted(ctx.psi_by_series):
            v = ctx.psi_by_series[series]
            if v is not None and v > self.psi_threshold:
                reasons.append(
                    f"psi[{series}]={v:.6f} > threshold {self.psi_threshold}"
                )
        if ctx.ic_z is not None and ctx.ic_z < self.ic_z_threshold:
            reasons.append(
                f"ic_z={ctx.ic_z:.4f} < threshold {self.ic_z_threshold}"
            )
        return RefitDecision(refit=bool(reasons), reasons=reasons)

    def describe(self) -> dict:
        return {
            "policy": self.name,
            "psi_threshold": self.psi_threshold,
            "ic_z_threshold": self.ic_z_threshold,
            "min_refit_gap_ns": self.min_refit_gap_ns,
        }


# ---------------------------------------------------------------------------
# adaptive config (configs/strategies.json "adaptive" block)
# ---------------------------------------------------------------------------

_REQ_INT_FIELDS = (
    "block_ns", "warmup_ns", "train_window_ns", "embargo_ns",
    "monitor_window_ns", "ic_window_ns", "ic_bucket_ns",
    "min_psi_samples", "min_ic_buckets",
)
_REQ_LIFECYCLE = (
    "watch_ic_gate", "reactivate_ic_gate", "retire_breach_evals",
    "reactivate_evals",
)


def validate_adaptive_config(block: dict) -> dict:
    """Validate the ``adaptive`` config block; returns it unchanged.

    Raises ValueError with a precise message on any missing or out-of-range
    field — a misconfigured adaptability layer must fail loudly, not adapt
    quietly to garbage.
    """
    if not isinstance(block, dict):
        raise ValueError("adaptive config block must be an object")
    if int(block.get("x-version", 0)) != 1:
        raise ValueError("adaptive config: x-version must be 1")
    for f in _REQ_INT_FIELDS:
        if f not in block:
            raise ValueError(f"adaptive config: missing field {f!r}")
        v = block[f]
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            raise ValueError(f"adaptive config: {f} must be a positive integer, got {v!r}")
    if block["warmup_ns"] < block["block_ns"]:
        raise ValueError("adaptive config: warmup_ns must be >= block_ns")
    pol = block.get("policies")
    if not isinstance(pol, dict) or "drift_triggered" not in pol:
        raise ValueError("adaptive config: policies.drift_triggered required")
    dt = pol["drift_triggered"]
    for f in ("psi_threshold", "ic_z_threshold", "min_refit_gap_ns"):
        if f not in dt:
            raise ValueError(f"adaptive config: policies.drift_triggered missing {f!r}")
    if not (isinstance(dt["psi_threshold"], (int, float)) and dt["psi_threshold"] >= 0):
        raise ValueError("adaptive config: psi_threshold must be a number >= 0")
    if not isinstance(dt["ic_z_threshold"], (int, float)):
        raise ValueError("adaptive config: ic_z_threshold must be a number")
    for pname, p in pol.items():
        if pname.startswith("scheduled"):
            if not (isinstance(p.get("period_ns"), int) and p["period_ns"] > 0):
                raise ValueError(
                    f"adaptive config: policies.{pname}.period_ns must be a positive integer"
                )
    lc = block.get("lifecycle")
    if not isinstance(lc, dict):
        raise ValueError("adaptive config: lifecycle block required")
    for f in _REQ_LIFECYCLE:
        if f not in lc:
            raise ValueError(f"adaptive config: lifecycle missing {f!r}")
    for f in ("retire_breach_evals", "reactivate_evals"):
        if not isinstance(lc[f], int) or isinstance(lc[f], bool) or lc[f] < 1:
            raise ValueError(f"adaptive config: lifecycle.{f} must be an integer >= 1")
    if float(lc["reactivate_ic_gate"]) < float(lc["watch_ic_gate"]):
        raise ValueError(
            "adaptive config: reactivate_ic_gate must be >= watch_ic_gate "
            "(hysteresis, not oscillation)"
        )
    return block


def load_adaptive_config(strategies_json_path) -> dict:
    """Load + validate the ``adaptive`` block of configs/strategies.json."""
    blob = json.loads(Path(strategies_json_path).read_text())
    if "adaptive" not in blob:
        raise ValueError(f"{strategies_json_path}: no 'adaptive' block")
    return validate_adaptive_config(blob["adaptive"])


def build_policy(name: str, cfg: dict) -> RefitPolicy:
    """Instantiate a configured policy by name from the adaptive block."""
    policies = cfg["policies"]
    if name == "static":
        return StaticPolicy()
    if name in policies and name.startswith("scheduled"):
        return ScheduledPolicy(period_ns=int(policies[name]["period_ns"]))
    if name == "drift_triggered":
        dt = policies["drift_triggered"]
        return DriftTriggeredPolicy(
            psi_threshold=float(dt["psi_threshold"]),
            ic_z_threshold=float(dt["ic_z_threshold"]),
            min_refit_gap_ns=int(dt["min_refit_gap_ns"]),
        )
    raise ValueError(f"unknown adaptive policy {name!r}")
