"""Pinned lifecycle policy configuration (``configs/strategies/lifecycle.json``).

The policy has two parts, loaded from two files and merged into one
:class:`PolicyConfig`:

* the **promotion gates** (RESEARCH -> CANDIDATE -> VALIDATING -> PAPER ->
  ACTIVE) and the demotion counter, from ``configs/strategies/lifecycle.json``
  (``x-version`` 2).  Its defaults equal the spec section 20 gates pinned in
  ``iap.validation.validate.GATES`` (OOS IC >= 0.01, t >= 3.0 as the floor,
  fold sign consistency >= 0.70, >= 3 folds, positive net P&L at 1x cost) so
  the lifecycle can never disagree with a REPORT.md verdict about *which*
  gate an alpha fails.  ``tstat_threshold`` (new in ``x-version`` 2) names
  how the ``statistical_significance`` gate gets its threshold:
  ``"ledger"`` — the default — reads the multiple-testing threshold the
  research result was judged at from the evidence
  (``Evidence.significance_threshold``) and applies
  ``max(min_nw_tstat, that)``; evidence that carries no such threshold
  FAILS the gate.  ``"fixed"`` — the legacy rule — applies ``min_nw_tstat``
  alone and ignores the evidence field;
* the **live sub-machine** (ACTIVE <-> WATCH -> RETIRED), from
  ``configs/strategies/strategies.json`` ``adaptive.lifecycle`` — the existing
  :class:`iap.adaptive.lifecycle.LifecycleConfig` (gates, the retirement
  rule and its CUSUM parameters), reused unchanged and deliberately not
  duplicated (one pin, one file).

Loading is fail-fast: a wrong ``x-version``, a missing or unknown key, a
non-finite number, a negative count or an inconsistent threshold raises
``ValueError`` naming the file and the key.  ``PolicyConfig.to_dict()`` is
the merged, JSON-ready view embedded in ``tests/golden/expected_lifecycle.json``
so a port can run the golden from the golden alone.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from iap.adaptive.lifecycle import LifecycleConfig

__all__ = [
    "DEFAULT_LIFECYCLE_PATH",
    "DEFAULT_STRATEGIES_PATH",
    "LIFECYCLE_CONFIG_VERSION",
    "TSTAT_THRESHOLD_POLICIES",
    "GateThresholds",
    "PolicyConfig",
    "load_policy_config",
    "repo_root",
]

#: ``x-version`` of ``configs/strategies/lifecycle.json``: 2 since v1.5.0
#: (``tstat_threshold``).
LIFECYCLE_CONFIG_VERSION = 2

#: How ``statistical_significance`` gets its threshold (module docs): the
#: default, then the legacy rule.  The names are those of
#: ``iap.validation.validate.TSTAT_THRESHOLD_POLICIES``.
TSTAT_THRESHOLD_POLICIES = ("ledger", "fixed")


def repo_root() -> Path:
    """The repository root (``python/src/iap/lifecycle/config.py`` -> 4 up)."""
    return Path(__file__).resolve().parents[4]


DEFAULT_LIFECYCLE_PATH = repo_root() / "configs" / "strategies" / "lifecycle.json"
DEFAULT_STRATEGIES_PATH = repo_root() / "configs" / "strategies" / "strategies.json"

_GATE_KEYS_FLOAT = (
    "min_oos_ic",
    "min_nw_tstat",
    "min_fold_sign_consistency",
    "min_net_return_bps",
    "min_capacity_usd",
    "max_ic_rank_gap",
    "ic_rank_gap_eps",
    "max_holdout_ic_gap",
    "max_paper_ic_gap",
    "min_paper_net_pnl",
)
_GATE_KEYS_INT = (
    "min_experiments_in_ledger",
    "min_folds",
    "min_paper_sessions",
    "max_kill_events",
)


#: Keys of the merged view's ``live`` block (``LifecycleConfig.to_dict``).
_LIVE_KEYS = (
    "watch_ic_gate",
    "reactivate_ic_gate",
    "retire_breach_evals",
    "reactivate_evals",
    "breach_rule",
    "cusum_k",
    "cusum_h",
)


def _as_policy(doc: Mapping[str, Any], where: str) -> str:
    value = doc["tstat_threshold"]
    if not isinstance(value, str) or value not in TSTAT_THRESHOLD_POLICIES:
        raise ValueError(
            f"{where}.tstat_threshold: expected one of {list(TSTAT_THRESHOLD_POLICIES)}, "
            f"got {value!r}"
        )
    return value


def _require_keys(block: Mapping[str, Any], expected: tuple, where: str) -> None:
    missing = sorted(set(expected) - set(block))
    unknown = sorted(set(block) - set(expected))
    if missing:
        raise ValueError(f"{where}: missing keys {missing}")
    if unknown:
        raise ValueError(f"{where}: unknown keys {unknown}")


def _as_float(block: Mapping[str, Any], key: str, where: str) -> float:
    value = block[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where}.{key}: expected a number, got {type(value).__name__}")
    out = float(value)
    if not math.isfinite(out):
        raise ValueError(f"{where}.{key}: non-finite number")
    return out


def _as_int(block: Mapping[str, Any], key: str, where: str, minimum: int) -> int:
    value = block[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{where}.{key}: expected an integer, got {type(value).__name__}")
    if value < minimum:
        raise ValueError(f"{where}.{key}: {value} < {minimum}")
    return value


@dataclass(frozen=True)
class GateThresholds:
    """Every promotion-gate threshold, one attribute per config key
    (``configs/strategies/lifecycle.json`` ``gates``)."""

    min_experiments_in_ledger: int
    min_oos_ic: float
    min_nw_tstat: float
    min_fold_sign_consistency: float
    min_folds: int
    min_net_return_bps: float
    min_capacity_usd: float
    max_ic_rank_gap: float
    ic_rank_gap_eps: float
    max_holdout_ic_gap: float
    min_paper_sessions: int
    max_paper_ic_gap: float
    min_paper_net_pnl: float
    max_kill_events: int

    def __post_init__(self) -> None:
        if not (0.0 <= self.min_fold_sign_consistency <= 1.0):
            raise ValueError("gates.min_fold_sign_consistency must lie in [0, 1]")
        if self.ic_rank_gap_eps <= 0.0:
            raise ValueError("gates.ic_rank_gap_eps must be > 0")
        if self.max_ic_rank_gap < 0.0:
            raise ValueError("gates.max_ic_rank_gap must be >= 0")
        if self.max_holdout_ic_gap < 0.0 or self.max_paper_ic_gap < 0.0:
            raise ValueError("gates.max_*_ic_gap must be >= 0")
        if self.min_capacity_usd < 0.0:
            raise ValueError("gates.min_capacity_usd must be >= 0")

    @staticmethod
    def from_block(block: Mapping[str, Any], where: str) -> GateThresholds:
        _require_keys(block, _GATE_KEYS_FLOAT + _GATE_KEYS_INT, where)
        values: dict[str, Any] = {k: _as_float(block, k, where) for k in _GATE_KEYS_FLOAT}
        values["min_experiments_in_ledger"] = _as_int(block, "min_experiments_in_ledger", where, 1)
        values["min_folds"] = _as_int(block, "min_folds", where, 1)
        values["min_paper_sessions"] = _as_int(block, "min_paper_sessions", where, 1)
        values["max_kill_events"] = _as_int(block, "max_kill_events", where, 0)
        return GateThresholds(**values)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready view in config key order."""
        return {
            "min_experiments_in_ledger": self.min_experiments_in_ledger,
            "min_oos_ic": self.min_oos_ic,
            "min_nw_tstat": self.min_nw_tstat,
            "min_fold_sign_consistency": self.min_fold_sign_consistency,
            "min_folds": self.min_folds,
            "min_net_return_bps": self.min_net_return_bps,
            "min_capacity_usd": self.min_capacity_usd,
            "max_ic_rank_gap": self.max_ic_rank_gap,
            "ic_rank_gap_eps": self.ic_rank_gap_eps,
            "max_holdout_ic_gap": self.max_holdout_ic_gap,
            "min_paper_sessions": self.min_paper_sessions,
            "max_paper_ic_gap": self.max_paper_ic_gap,
            "min_paper_net_pnl": self.min_paper_net_pnl,
            "max_kill_events": self.max_kill_events,
        }


@dataclass(frozen=True)
class PolicyConfig:
    """The merged lifecycle policy: name, promotion gates, demotion counter
    and the live (ACTIVE/WATCH/RETIRED) sub-machine gates."""

    policy: str
    gates: GateThresholds
    max_consecutive_failures: int
    live: LifecycleConfig
    #: "ledger" (default) or "fixed" (legacy) — module docs
    tstat_threshold: str = "ledger"

    def __post_init__(self) -> None:
        if not self.policy:
            raise ValueError("policy name must not be empty")
        if self.max_consecutive_failures < 1:
            raise ValueError("demotion.max_consecutive_failures must be >= 1")
        if self.tstat_threshold not in TSTAT_THRESHOLD_POLICIES:
            raise ValueError(
                f"tstat_threshold {self.tstat_threshold!r} unknown; "
                f"known: {TSTAT_THRESHOLD_POLICIES}"
            )

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready merged view (embedded in the lifecycle golden)."""
        return {
            "policy": self.policy,
            "tstat_threshold": self.tstat_threshold,
            "gates": self.gates.to_dict(),
            "demotion": {"max_consecutive_failures": self.max_consecutive_failures},
            "live": self.live.to_dict(),
        }

    @staticmethod
    def from_dict(doc: Mapping[str, Any]) -> PolicyConfig:
        """Inverse of :meth:`to_dict` (used to run a golden from its own
        embedded config)."""
        _require_keys(doc, ("policy", "tstat_threshold", "gates", "demotion", "live"), "config")
        _require_keys(doc["demotion"], ("max_consecutive_failures",), "config.demotion")
        _require_keys(doc["live"], _LIVE_KEYS, "config.live")
        return PolicyConfig(
            policy=str(doc["policy"]),
            gates=GateThresholds.from_block(doc["gates"], "config.gates"),
            max_consecutive_failures=_as_int(
                doc["demotion"], "max_consecutive_failures", "config.demotion", 1
            ),
            live=LifecycleConfig.from_config(doc["live"]),
            tstat_threshold=_as_policy(doc, "config"),
        )


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"{path}: config file not found")
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return doc


def load_policy_config(
    lifecycle_path: Path | None = None, strategies_path: Path | None = None
) -> PolicyConfig:
    """Load and validate the policy from the two pinned config files.

    Raises ``ValueError`` naming the file and key on any inconsistency.
    """
    lc_path = Path(lifecycle_path) if lifecycle_path is not None else DEFAULT_LIFECYCLE_PATH
    st_path = Path(strategies_path) if strategies_path is not None else DEFAULT_STRATEGIES_PATH
    doc = _read_json(lc_path)
    where = str(lc_path)
    if doc.get("x-version") != LIFECYCLE_CONFIG_VERSION:
        raise ValueError(
            f"{where}: x-version {doc.get('x-version')!r} != {LIFECYCLE_CONFIG_VERSION} "
            "(a v1.4.0 document must be upgraded: name 'tstat_threshold' — 'ledger', or "
            "'fixed' to keep its 3.0 gate)"
        )
    _require_keys(
        doc, ("x-version", "description", "policy", "tstat_threshold", "gates", "demotion"), where
    )
    tstat_threshold = _as_policy(doc, where)
    policy = doc["policy"]
    if not isinstance(policy, str) or not policy:
        raise ValueError(f"{where}.policy: expected a non-empty string")
    _require_keys(doc["demotion"], ("max_consecutive_failures",), f"{where}.demotion")
    gates = GateThresholds.from_block(doc["gates"], f"{where}.gates")
    max_failures = _as_int(doc["demotion"], "max_consecutive_failures", f"{where}.demotion", 1)

    strategies = _read_json(st_path)
    try:
        live_block = strategies["adaptive"]["lifecycle"]
    except (KeyError, TypeError):
        raise ValueError(f"{st_path}: missing adaptive.lifecycle block") from None
    try:
        live = LifecycleConfig.from_config(live_block)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{st_path}: adaptive.lifecycle: {exc}") from None
    return PolicyConfig(
        policy=policy,
        gates=gates,
        max_consecutive_failures=max_failures,
        live=live,
        tstat_threshold=tstat_threshold,
    )
