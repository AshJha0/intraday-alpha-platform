"""Lifecycle gates — pure, deterministic ``(alpha_id, evidence) -> GateResult``.

Every gate is one row of :data:`GATE_SPECS`: a name, the evidence block it
reads, the metric it extracts, the comparison and the config key holding its
threshold.  A port implements the table, not eighteen classes.

Comparison kinds (pinned):

* ``min``  — ``value >= threshold`` (inclusive);
* ``max``  — ``value <= threshold`` (inclusive);
* ``gt``   — ``value >  threshold`` (strict; the net-P&L gate mirrors
  ``iap.validation.validate``'s ``net_pnl_1x > 0.0``);
* ``bool`` — the metric itself; ``value`` and ``threshold`` are ``null``.

A gate whose evidence block is absent, or whose metric is ``None``, FAILS
with ``value = null`` (a missing number never passes); the machine decides
separately whether an absent block counts as a failure or as silence
(``iap.lifecycle.machine``).

Gate inventory (config key in ``configs/strategies/lifecycle.json``):

==========================  ==========  =========================================  ==========================
gate                        block       metric                                     threshold key
==========================  ==========  =========================================  ==========================
ledger_entry_exists         research    n_experiments_in_ledger (min)              min_experiments_in_ledger
leakage_clean               research    leakage_passed (bool)                      —
oos_ic                      research    ic (min)                                   min_oos_ic
statistical_significance    research    t_stat (min)                               min_nw_tstat
fold_consistency            research    fold_consistency (min)                     min_fold_sign_consistency
fold_count                  research    n_folds (min)                              min_folds
hypothesis_sign             research    hypothesis_sign_confirmed is True (bool)   —
net_pnl_after_costs         research    net_return_bps (gt)                        min_net_return_bps
capacity                    capacity    capacity_usd (min)                         min_capacity_usd
stability                   research    |ic - rank_ic| / max(|ic|, eps) (max)      max_ic_rank_gap
holdout_ic_tracks_research  validation  |holdout_ic - research_ic| (max)           max_holdout_ic_gap
replay_reproducible         validation  replay_hash_match (bool)                   —
cross_language_parity       validation  parity (bool)                              —
paper_min_sessions          paper       n_sessions (min)                           min_paper_sessions
paper_ic_tracking           paper       |realized_ic - research_ic| (max)          max_paper_ic_gap
paper_net_pnl               paper       net_pnl (min)                              min_paper_net_pnl
no_kill_events              paper       n_kill_events (max)                        max_kill_events
rolling_ic                  live        rolling_ic (min)                           watch_ic_gate (strategies)
==========================  ==========  =========================================  ==========================

**Why the Pearson/rank gap is the stability rule.**  Everything a research
result carries is a point estimate over one out-of-sample window; the one
pair of numbers that measures the *shape* of the signal-label relation
rather than its strength is (``ic``, ``rank_ic``): Pearson weights every row
by its magnitude, Spearman weights every row equally.  A rank IC far above
the Pearson IC means the monotone relation exists but the linear score is
diluted by outliers; a rank IC near zero under a healthy Pearson IC means
the IC comes from a handful of extreme rows.  Both are instabilities a live
book will feel first.  ``|ic - rank_ic| / max(|ic|, eps) <= 1.0`` therefore
requires the rank IC to lie in ``[0, 2 * ic]`` for a positive IC (same sign,
at most twice the linear IC) — computable from any ``ExperimentResult``,
scale-free, and with a threshold a port can state in one line.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Tuple, Union

from iap.contracts.types import GateResult
from iap.lifecycle.config import PolicyConfig
from iap.lifecycle.evidence import Evidence

__all__ = [
    "GATE_SPECS",
    "Gate",
    "GateSpec",
    "build_gates",
    "ic_rank_gap",
]

Metric = Union[float, int, bool, None]


def ic_rank_gap(ic: float, rank_ic: float, eps: float) -> float:
    """``|ic - rank_ic| / max(|ic|, eps)`` — the pinned stability statistic."""
    return abs(ic - rank_ic) / max(abs(ic), eps)


def _research(field: str) -> Callable[[Evidence, PolicyConfig], Metric]:
    def read(ev: Evidence, cfg: PolicyConfig) -> Metric:
        return None if ev.research is None else getattr(ev.research, field)
    return read


def _hypothesis(ev: Evidence, cfg: PolicyConfig) -> Metric:
    if ev.research is None:
        return None
    return ev.research.hypothesis_sign_confirmed is True


def _capacity(ev: Evidence, cfg: PolicyConfig) -> Metric:
    return ev.capacity_usd


def _stability(ev: Evidence, cfg: PolicyConfig) -> Metric:
    if ev.research is None:
        return None
    return ic_rank_gap(ev.research.ic, ev.research.rank_ic, cfg.gates.ic_rank_gap_eps)


def _validation(field: str) -> Callable[[Evidence, PolicyConfig], Metric]:
    def read(ev: Evidence, cfg: PolicyConfig) -> Metric:
        return None if ev.validation is None else getattr(ev.validation, field)
    return read


def _holdout_gap(ev: Evidence, cfg: PolicyConfig) -> Metric:
    if ev.validation is None:
        return None
    return abs(ev.validation.holdout_ic - ev.validation.research_ic)


def _paper(field: str) -> Callable[[Evidence, PolicyConfig], Metric]:
    def read(ev: Evidence, cfg: PolicyConfig) -> Metric:
        return None if ev.paper is None else getattr(ev.paper, field)
    return read


def _paper_gap(ev: Evidence, cfg: PolicyConfig) -> Metric:
    if ev.paper is None:
        return None
    return abs(ev.paper.realized_ic - ev.paper.research_ic)


def _rolling_ic(ev: Evidence, cfg: PolicyConfig) -> Metric:
    if ev.live is None or not ev.live.informative:
        return None
    return ev.live.rolling_ic


def _threshold_from_gates(key: str) -> Callable[[PolicyConfig], Optional[float]]:
    def read(cfg: PolicyConfig) -> Optional[float]:
        return float(getattr(cfg.gates, key))
    return read


def _threshold_none(cfg: PolicyConfig) -> Optional[float]:
    return None


def _threshold_watch_gate(cfg: PolicyConfig) -> Optional[float]:
    return cfg.live.watch_ic_gate


@dataclass(frozen=True)
class GateSpec:
    """One row of the gate table (see module docstring)."""

    name: str
    block: str
    kind: str
    threshold_key: Optional[str]
    metric: Callable[[Evidence, PolicyConfig], Metric]
    threshold: Callable[[PolicyConfig], Optional[float]]

    def __post_init__(self) -> None:
        if self.kind not in ("min", "max", "gt", "bool"):
            raise ValueError(f"gate {self.name}: unknown comparison {self.kind!r}")
        if (self.kind == "bool") != (self.threshold_key is None):
            raise ValueError(f"gate {self.name}: bool gates have no threshold key")


def _spec(name: str, block: str, kind: str, key: Optional[str],
          metric: Callable[[Evidence, PolicyConfig], Metric]) -> GateSpec:
    if key is None:
        threshold = _threshold_none
    elif key == "watch_ic_gate":
        threshold = _threshold_watch_gate
    else:
        threshold = _threshold_from_gates(key)
    return GateSpec(name=name, block=block, kind=kind, threshold_key=key,
                    metric=metric, threshold=threshold)


#: The complete gate table, in a fixed order (the machine evaluates the
#: subset of an edge in the edge's own pinned order).
GATE_SPECS: Tuple[GateSpec, ...] = (
    _spec("ledger_entry_exists", "research", "min", "min_experiments_in_ledger",
          _research("n_experiments_in_ledger")),
    _spec("leakage_clean", "research", "bool", None, _research("leakage_passed")),
    _spec("oos_ic", "research", "min", "min_oos_ic", _research("ic")),
    _spec("statistical_significance", "research", "min", "min_nw_tstat",
          _research("t_stat")),
    _spec("fold_consistency", "research", "min", "min_fold_sign_consistency",
          _research("fold_consistency")),
    _spec("fold_count", "research", "min", "min_folds", _research("n_folds")),
    _spec("hypothesis_sign", "research", "bool", None, _hypothesis),
    _spec("net_pnl_after_costs", "research", "gt", "min_net_return_bps",
          _research("net_return_bps")),
    _spec("capacity", "capacity", "min", "min_capacity_usd", _capacity),
    _spec("stability", "research", "max", "max_ic_rank_gap", _stability),
    _spec("holdout_ic_tracks_research", "validation", "max", "max_holdout_ic_gap",
          _holdout_gap),
    _spec("replay_reproducible", "validation", "bool", None,
          _validation("replay_hash_match")),
    _spec("cross_language_parity", "validation", "bool", None, _validation("parity")),
    _spec("paper_min_sessions", "paper", "min", "min_paper_sessions",
          _paper("n_sessions")),
    _spec("paper_ic_tracking", "paper", "max", "max_paper_ic_gap", _paper_gap),
    _spec("paper_net_pnl", "paper", "min", "min_paper_net_pnl", _paper("net_pnl")),
    _spec("no_kill_events", "paper", "max", "max_kill_events",
          _paper("n_kill_events")),
    _spec("rolling_ic", "live", "min", "watch_ic_gate", _rolling_ic),
)

_SPEC_BY_NAME: Mapping[str, GateSpec] = {s.name: s for s in GATE_SPECS}
if len(_SPEC_BY_NAME) != len(GATE_SPECS):
    raise RuntimeError("duplicate gate name in GATE_SPECS")


class Gate:
    """A :class:`GateSpec` bound to a :class:`PolicyConfig`; satisfies
    ``iap.contracts.protocols.LifecycleGate``."""

    __slots__ = ("_spec", "_config")

    def __init__(self, spec: GateSpec, config: PolicyConfig) -> None:
        self._spec = spec
        self._config = config

    @property
    def name(self) -> str:
        return self._spec.name

    @property
    def spec(self) -> GateSpec:
        return self._spec

    @property
    def threshold(self) -> Optional[float]:
        """The bound threshold (``None`` for a boolean gate)."""
        return self._spec.threshold(self._config)

    def evaluate(self, alpha_id: str, evidence: Any) -> GateResult:
        """Pure: the same evidence always yields the same result.
        ``alpha_id`` is accepted for the protocol; no gate is alpha-specific."""
        if not isinstance(evidence, Evidence):
            raise TypeError(f"gate {self.name}: expected Evidence, got "
                            f"{type(evidence).__name__}")
        metric = self._spec.metric(evidence, self._config)
        threshold = self.threshold
        if self._spec.kind == "bool":
            return GateResult(passed=metric is True, value=None, threshold=None)
        if metric is None:
            return GateResult(passed=False, value=None, threshold=threshold)
        value = float(metric)
        if self._spec.kind == "min":
            passed = value >= threshold
        elif self._spec.kind == "max":
            passed = value <= threshold
        else:
            passed = value > threshold
        return GateResult(passed=passed, value=value, threshold=threshold)

    def __repr__(self) -> str:
        return f"Gate({self.name!r}, {self._spec.kind}, threshold={self.threshold!r})"


def build_gates(config: PolicyConfig) -> Dict[str, Gate]:
    """Every gate of the table bound to ``config``, keyed by name (insertion
    order = table order)."""
    return {spec.name: Gate(spec, config) for spec in GATE_SPECS}
