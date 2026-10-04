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

**The significance threshold (pinned, v1.5.0).**  ``statistical_significance``
is the one gate whose threshold is not a config constant.  Under the default
policy (``PolicyConfig.tstat_threshold = "ledger"``) it is

    max(gates.min_nw_tstat, evidence.significance_threshold)

— the multiple-testing threshold the research result was judged at, never
below the configured floor — and when the evidence carries no
``significance_threshold`` the gate FAILS with ``threshold = null`` (the
value is still reported): a missing threshold never passes either.  Under
``"fixed"`` — the rule up to v1.4.0 — the threshold is ``min_nw_tstat`` and
the evidence field is not read.  The metric is ``research.t_stat``, which
under the default research methods is the pooled-slope HAC t the PROMOTE
gate read; the gate table itself (names, blocks, comparison kinds, edges)
is unchanged, and the Java and Rust ports implement both policies.

**Non-eligible research evidence is refused (pinned).**  When the evidence
carries ``research_gate_eligible = False`` (``iap.lifecycle.evidence``; the
result came from a configuration outside the pinned protocol bounds or from
caller-chosen periods — ``iap.research.specs.gate_eligibility``), EVERY gate
that reads the ``research`` block fails with ``value = null``, exactly as if
the number were missing: a result that may not be used as evidence is not a
number a gate can pass on.  This is a property of the evidence, not a new
row of the gate table, so the table, the edges and the golden are unchanged.

**The net P&L bootstrap gate (pinned, v1.5.0).**  ``net_pnl_bootstrap_ci``
sits on the CANDIDATE -> VALIDATING edge directly after
``net_pnl_after_costs`` and requires

    lower bound of the bootstrap interval of net P&L  >  min_net_pnl_ci_low  (0.0)

* *The interval.*  ``Evidence.pnl_bootstrap``: the percentile interval of the
  SUM of the 1-minute bar net P&L by the stationary bootstrap
  (``iap.validation.diagnostics.stationary_bootstrap_ci``; Politis & Romano
  1994) — 1 000 resamples, mean block ``max(1, round(n ** (1/3)))`` bars,
  SplitMix64 from the recorded seed, lower empirical quantiles — taken at
  the level ``gates.net_pnl_ci_level`` (0.95).  Level, resamples, seed,
  block and series length travel in the evidence; a gate never resamples.
* *The series.*  The same backtest the ``net_pnl_after_costs`` gate reads
  — the research backtester at 1x costs under the method bundle in force —
  but over ALL walk-forward test folds pooled (``net_pnl_1x_pooled``),
  where ``net_pnl_after_costs`` reads the point estimate of the LAST fold.
* *No trade.*  An alpha that made no trade in any fold (``n_trades = 0``)
  has no P&L to resample: its interval is the degenerate [0, 0] and is not
  evidence.  The gate FAILS with ``value = null`` — it does not pass
  vacuously, and the evidence's ``n_trades = 0`` is the stated reason
  (:func:`bootstrap_gate_reason`).  The same holds for an interval without
  bounds (fewer than 8 bars), one taken at another level than the
  configured one, and a missing block.
* *Strict.*  ``gt``: a lower bound of exactly 0.0 fails.
* *Not redundant with its neighbours.*  ``net_pnl_after_costs`` asks whether
  the last fold's net P&L is positive — a point estimate on one fifth of
  the sample; this gate asks whether the pooled P&L of every fold is
  positive beyond its own sampling noise.  An alpha can pass either one
  alone.  ``stability`` reads the Pearson / rank IC gap and nothing of the
  P&L or of the folds.  The per-fold diagnostics (cost survival, decay and
  regime per fold) stay report-only: how consistently the SIGNAL holds
  across folds is already gated by ``fold_consistency``, and the P&L of
  every fold enters this gate's pooled series, so a per-fold P&L count
  would gate the same evidence a third time at four observations.
* *Legacy.*  Under ``net_pnl_ci_gate = "absent"`` — the legacy policy — the
  gate is not evaluated and is not part of any result: the rules up to
  v1.4.0 had no such gate.

**The cross-alpha correlation gate (pinned, v1.5.0; backlog AF03).**  A new
alpha must be additive: ``cross_alpha_correlation`` sits on the CANDIDATE ->
VALIDATING edge and requires

    max over eligible peers of |signal correlation|  <=  max_cross_alpha_correlation

* *The series.*  The Pearson correlation of the two alphas' out-of-sample
  standardised signals ``z`` on the rows BOTH score — same instrument, same
  feature row, both confidences > 0 — pooled across instruments and
  walk-forward test folds (``iap.combine.correlation``).  Two alphas that
  never score a common row (an equity and an FX alpha) have correlation 0.0
  by definition: they cannot be the same bet in the sense of this gate.
  The SIGNAL is gated rather than the realised P&L because P&L exists only
  where the alpha trades, and under the cost-aware policy most alphas here
  make no trade — a P&L correlation would be undefined exactly where the
  gate is needed.  The P&L correlation is reported beside it
  (``research/combination``), not gated.
* *The peers.*  Every other registered alpha whose lifecycle state is at or
  beyond ``cross_alpha_min_state`` (config; VALIDATING by default) and is
  not RETIRED.  The absolute value is taken: an alpha that is the mirror
  image of an allocated one adds nothing either.
* *No eligible peer.*  The statistic is 0.0 and the gate passes — vacuously,
  and the result says so by its value.  The first alpha through the gate is
  never blocked by it.
* *Ties.*  ``max`` is inclusive: a correlation exactly at the threshold
  passes.  When several peers share the largest ``|correlation|`` the
  binding peer a report names is the one with the smallest ``alpha_id``
  (``CrossAlphaEvidence.binding_peer``); the gate result is the same.
* *Order.*  The gate compares a candidate with what is already through, so
  the order candidates are evaluated in decides which of two correlated
  candidates passes.  The bootstrap evaluates alphas in ascending
  ``alpha_id`` order at one instant: the smaller id is evaluated first and
  the larger one then meets it as a peer.
* *Missing evidence.*  ``Evidence.cross_alpha`` absent — nobody measured the
  correlations — FAILS the gate with ``value = null``: fail closed.
* *Ports.*  The evidence carries the peers with their states and
  correlations, so Java and Rust evaluate the gate from the document alone.

Gate inventory (config key in ``configs/strategies/lifecycle.json``):

==========================  ==========  =========================================  ==========================
gate                        block       metric                                     threshold key
==========================  ==========  =========================================  ==========================
ledger_entry_exists         research    n_experiments_in_ledger (min)              min_experiments_in_ledger
leakage_clean               research    leakage_passed (bool)                      —
oos_ic                      research    ic (min)                                   min_oos_ic
statistical_significance    research    t_stat (min)                               min_nw_tstat (floor; see above)
fold_consistency            research    fold_consistency (min)                     min_fold_sign_consistency
fold_count                  research    n_folds (min)                              min_folds
hypothesis_sign             research    hypothesis_sign_confirmed is True (bool)   —
net_pnl_after_costs         research    net_return_bps (gt)                        min_net_return_bps
net_pnl_bootstrap_ci        pnl_bootstrap ci_low of a traded, right-level interval (gt) min_net_pnl_ci_low
capacity                    capacity    capacity_usd (min)                         min_capacity_usd
stability                   research    |ic - rank_ic| / max(|ic|, eps) (max)      max_ic_rank_gap
cross_alpha_correlation     cross_alpha max eligible-peer |correlation| (max)      max_cross_alpha_correlation
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

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from iap.contracts.types import GateResult
from iap.lifecycle.config import PolicyConfig
from iap.lifecycle.evidence import Evidence

__all__ = [
    "GATE_SPECS",
    "Gate",
    "GateSpec",
    "BOOTSTRAP_GATE",
    "SIGNIFICANCE_GATE",
    "bootstrap_gate_reason",
    "build_gates",
    "ic_rank_gap",
]

Metric = float | int | bool | None

#: The gate whose threshold comes from the evidence under the ledger policy.
SIGNIFICANCE_GATE = "statistical_significance"
#: The gate the legacy policy leaves out (``net_pnl_ci_gate = "absent"``).
BOOTSTRAP_GATE = "net_pnl_bootstrap_ci"


def bootstrap_gate_reason(evidence: Evidence, config: PolicyConfig) -> str:
    """Why ``net_pnl_bootstrap_ci`` decides as it does on ``evidence`` —
    the explicit reason a report prints beside the gate result."""
    boot = evidence.pnl_bootstrap
    level = config.gates.net_pnl_ci_level
    if boot is None:
        return "no bootstrap interval in the evidence"
    if boot.n_trades == 0:
        return "no trade at 1x costs in any fold: no P&L to resample, no interval"
    if boot.level != level:
        return f"interval taken at level {boot.level}, the policy requires {level}"
    if boot.ci_low is None:
        return f"only {boot.n_bars} bars: too few for an interval"
    bound = config.gates.min_net_pnl_ci_low
    relation = ">" if boot.ci_low > bound else "<="
    return f"lower bound {boot.ci_low:.2f} {relation} {bound} at level {level}"


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


def _pnl_bootstrap(ev: Evidence, cfg: PolicyConfig) -> Metric:
    if ev.pnl_bootstrap is None:
        return None
    return ev.pnl_bootstrap.gate_value(cfg.gates.net_pnl_ci_level)


def _cross_alpha(ev: Evidence, cfg: PolicyConfig) -> Metric:
    if ev.cross_alpha is None:
        return None
    return ev.cross_alpha.max_abs_correlation(cfg.cross_alpha_min_state)


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


def _threshold_from_gates(key: str) -> Callable[[PolicyConfig], float | None]:
    def read(cfg: PolicyConfig) -> float | None:
        return float(getattr(cfg.gates, key))

    return read


def _threshold_none(cfg: PolicyConfig) -> float | None:
    return None


def _threshold_watch_gate(cfg: PolicyConfig) -> float | None:
    return cfg.live.watch_ic_gate


@dataclass(frozen=True)
class GateSpec:
    """One row of the gate table (see module docstring)."""

    name: str
    block: str
    kind: str
    threshold_key: str | None
    metric: Callable[[Evidence, PolicyConfig], Metric]
    threshold: Callable[[PolicyConfig], float | None]

    def __post_init__(self) -> None:
        if self.kind not in ("min", "max", "gt", "bool"):
            raise ValueError(f"gate {self.name}: unknown comparison {self.kind!r}")
        if (self.kind == "bool") != (self.threshold_key is None):
            raise ValueError(f"gate {self.name}: bool gates have no threshold key")


def _spec(
    name: str,
    block: str,
    kind: str,
    key: str | None,
    metric: Callable[[Evidence, PolicyConfig], Metric],
) -> GateSpec:
    if key is None:
        threshold = _threshold_none
    elif key == "watch_ic_gate":
        threshold = _threshold_watch_gate
    else:
        threshold = _threshold_from_gates(key)
    return GateSpec(
        name=name, block=block, kind=kind, threshold_key=key, metric=metric, threshold=threshold
    )


#: The complete gate table, in a fixed order (the machine evaluates the
#: subset of an edge in the edge's own pinned order).
GATE_SPECS: tuple[GateSpec, ...] = (
    _spec(
        "ledger_entry_exists",
        "research",
        "min",
        "min_experiments_in_ledger",
        _research("n_experiments_in_ledger"),
    ),
    _spec("leakage_clean", "research", "bool", None, _research("leakage_passed")),
    _spec("oos_ic", "research", "min", "min_oos_ic", _research("ic")),
    _spec("statistical_significance", "research", "min", "min_nw_tstat", _research("t_stat")),
    _spec(
        "fold_consistency",
        "research",
        "min",
        "min_fold_sign_consistency",
        _research("fold_consistency"),
    ),
    _spec("fold_count", "research", "min", "min_folds", _research("n_folds")),
    _spec("hypothesis_sign", "research", "bool", None, _hypothesis),
    _spec(
        "net_pnl_after_costs", "research", "gt", "min_net_return_bps", _research("net_return_bps")
    ),
    _spec("net_pnl_bootstrap_ci", "pnl_bootstrap", "gt", "min_net_pnl_ci_low", _pnl_bootstrap),
    _spec("capacity", "capacity", "min", "min_capacity_usd", _capacity),
    _spec("stability", "research", "max", "max_ic_rank_gap", _stability),
    _spec(
        "cross_alpha_correlation",
        "cross_alpha",
        "max",
        "max_cross_alpha_correlation",
        _cross_alpha,
    ),
    _spec("holdout_ic_tracks_research", "validation", "max", "max_holdout_ic_gap", _holdout_gap),
    _spec("replay_reproducible", "validation", "bool", None, _validation("replay_hash_match")),
    _spec("cross_language_parity", "validation", "bool", None, _validation("parity")),
    _spec("paper_min_sessions", "paper", "min", "min_paper_sessions", _paper("n_sessions")),
    _spec("paper_ic_tracking", "paper", "max", "max_paper_ic_gap", _paper_gap),
    _spec("paper_net_pnl", "paper", "min", "min_paper_net_pnl", _paper("net_pnl")),
    _spec("no_kill_events", "paper", "max", "max_kill_events", _paper("n_kill_events")),
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
    def threshold(self) -> float | None:
        """The configured threshold (``None`` for a boolean gate).  For
        ``statistical_significance`` this is the floor ``min_nw_tstat``;
        :meth:`threshold_for` gives the threshold applied to an evidence."""
        return self._spec.threshold(self._config)

    def threshold_for(self, evidence: Evidence) -> float | None:
        """The threshold this gate applies to ``evidence`` (module docs,
        "The significance threshold")."""
        configured = self.threshold
        if self._spec.name != SIGNIFICANCE_GATE or self._config.tstat_threshold == "fixed":
            return configured
        if evidence.significance_threshold is None:
            return None
        return max(float(configured), float(evidence.significance_threshold))

    def evaluate(self, alpha_id: str, evidence: Any) -> GateResult:
        """Pure: the same evidence always yields the same result.
        ``alpha_id`` is accepted for the protocol; no gate is alpha-specific."""
        if not isinstance(evidence, Evidence):
            raise TypeError(f"gate {self.name}: expected Evidence, got {type(evidence).__name__}")
        metric = self._spec.metric(evidence, self._config)
        threshold = self.threshold_for(evidence)
        if self._spec.block == "research" and not evidence.research_gate_eligible:
            # Recorded, ledgered, but not promotion evidence (module docs).
            return GateResult(
                passed=False, value=None, threshold=None if self._spec.kind == "bool" else threshold
            )
        if self._spec.kind == "bool":
            return GateResult(passed=metric is True, value=None, threshold=None)
        if metric is None:
            return GateResult(passed=False, value=None, threshold=threshold)
        value = float(metric)
        if threshold is None:  # the ledger policy with no threshold in the evidence
            return GateResult(passed=False, value=value, threshold=None)
        if self._spec.kind == "min":
            passed = value >= threshold
        elif self._spec.kind == "max":
            passed = value <= threshold
        else:
            passed = value > threshold
        return GateResult(passed=passed, value=value, threshold=threshold)

    def __repr__(self) -> str:
        return f"Gate({self.name!r}, {self._spec.kind}, threshold={self.threshold!r})"


def build_gates(config: PolicyConfig) -> dict[str, Gate]:
    """Every gate of the table bound to ``config``, keyed by name (insertion
    order = table order)."""
    return {spec.name: Gate(spec, config) for spec in GATE_SPECS}
