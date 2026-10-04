"""Typed evidence handed to the lifecycle gates.

One :class:`Evidence` carries at most one block per lifecycle stage; a gate
reads the block of the edge it guards and nothing else:

======================  ============================================
edge                    evidence block
======================  ============================================
RESEARCH -> CANDIDATE   ``research`` (:class:`ExperimentResult`)
CANDIDATE -> VALIDATING ``research`` + ``capacity_usd`` +
                        ``pnl_bootstrap`` (:class:`PnlBootstrapEvidence`) +
                        ``cross_alpha`` (:class:`CrossAlphaEvidence`)
VALIDATING -> PAPER     ``validation`` (:class:`ValidationEvidence`)
PAPER -> ACTIVE         ``paper`` (:class:`PaperEvidence`)
ACTIVE / WATCH          ``live`` (:class:`LiveEvidence`)
======================  ============================================

Every value is a plain JSON scalar (``int`` / ``float`` / ``bool``), finite
(NaN and infinities are rejected on construction — a metric a runner could
not compute is *absent*, never a number), so an evidence document round-trips
through ``to_dict`` / ``from_dict`` byte-exactly and a port can replay the
golden's evidence without float drift.  A missing block means "no evidence
for that stage" and, except for the RESEARCH -> CANDIDATE presence gate, an
absent block moves nothing (silence is not evidence — API_ADAPTIVE.md §6).

**The significance threshold travels with the evidence (pinned, v1.5.0).**
``significance_threshold`` is the PROMOTE t threshold the research result
was judged at — under the default research methods the multiple-testing
ledger's Bonferroni |t| at the run's gate look count
(``iap.validation.ledger``, "Gate look count").  It sits beside
``research`` for the same reason ``capacity_usd`` does: an
``ExperimentResult`` has no field for it.  Under the default lifecycle
policy (``tstat_threshold = "ledger"``) the ``statistical_significance``
gate compares ``research.t_stat`` — the gate's own statistic, the
pooled-slope HAC t under the default methods — with
``max(min_nw_tstat, significance_threshold)``, and FAILS when the evidence
carries none: a result nobody recorded a multiple-testing threshold for is
not significant evidence.  The key is always serialised (``null`` when
absent) and every port reads it.

**The cross-alpha block (pinned, v1.5.0).**  ``cross_alpha`` carries what the
``cross_alpha_correlation`` gate needs and nothing a port would have to
recompute: one :class:`CrossAlphaPeer` per OTHER registered alpha — its id,
its lifecycle state at the moment the evidence was built, and the Pearson
correlation of the two alphas' out-of-sample standardised signals on the
rows both score (``iap.combine.correlation``; pooled across instruments).
The peers are sorted by ``alpha_id``, unique, and never include the alpha the
evidence is for (the gate does not know whose evidence it reads).  The gate
filters the peers by state itself, so the same document can be judged under
another ``cross_alpha_min_state``.  ``null`` means nobody measured the
correlations: the gate FAILS (``iap.lifecycle.gates``).  An empty peer list
is a statement — there is no other alpha — and passes vacuously.  The key is
always serialised and every port reads it.

**The P&L bootstrap block (pinned, v1.5.0).**  ``pnl_bootstrap`` carries the
confidence interval the ``net_pnl_bootstrap_ci`` gate reads, with everything
that pins it, so that no port resamples: ``ci_low`` / ``ci_high`` (USD; both
``null`` when the series had fewer than 8 bars and no interval exists),
``level``, ``n_resamples``, ``seed``, ``mean_block`` (the mean block length
of the stationary bootstrap), ``n_bars`` (the length of the resampled
1-minute bar series) and ``n_trades`` (trades at 1x costs over the same
folds).  It is the ``net_pnl_bootstrap`` field of a validation report
(``iap.validation.diagnostics.stationary_bootstrap_ci``) plus the trade
count.  ``null`` means no interval was computed: the gate FAILS.  The key
is always serialised and every port reads it.

**``live.new_fraction``** (v1.5.0) is the share of a live reading's window
that is new since the last counted reading, in (0, 1] — what the CUSUM
retirement rule weights a reading by (``iap.adaptive.lifecycle``).  The
legacy consecutive rule does not read it; the key is required either way.

**Gate eligibility of the research block (pinned, Python reference).**
``research_gate_eligible`` says whether the ``research`` result may be used
as promotion evidence at all (``iap.research.specs.gate_eligibility``: the
pinned protocol bounds and dataset-derived periods).  It defaults to
``True`` and is serialised ONLY when ``False``, so every existing evidence
document — and the cross-language golden — is unchanged byte-for-byte; a
document that carries ``"research_gate_eligible": false`` makes every
research-block gate fail (``iap.lifecycle.gates``).  The Java / Rust ports
do not read the key: evidence flagged not eligible must not be handed to
them (their strict readers reject the unknown key, which is the safe
failure).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

from iap.contracts.types import ExperimentResult, LifecycleState

__all__ = [
    "CrossAlphaEvidence",
    "CrossAlphaPeer",
    "Evidence",
    "LiveEvidence",
    "PaperEvidence",
    "PnlBootstrapEvidence",
    "ValidationEvidence",
]


def _check_float(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path}: expected a number, got {type(value).__name__}")
    out = float(value)
    if not math.isfinite(out):
        raise ValueError(f"{path}: non-finite number")
    return out


def _check_int(value: Any, path: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{path}: expected an integer, got {type(value).__name__}")
    if value < minimum:
        raise ValueError(f"{path}: {value} < {minimum}")
    return value


def _check_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{path}: expected a bool, got {type(value).__name__}")
    return value


def _from_dict(cls, data: Mapping[str, Any], path: str):
    names = [f.name for f in fields(cls)]
    if not isinstance(data, Mapping):
        raise ValueError(f"{path}: expected an object")
    unknown = sorted(set(data) - set(names))
    missing = [n for n in names if n not in data]
    if unknown:
        raise ValueError(f"{path}: unknown keys {unknown}")
    if missing:
        raise ValueError(f"{path}: missing keys {missing}")
    return cls(**{n: data[n] for n in names})


@dataclass(frozen=True)
class ValidationEvidence:
    """VALIDATING-stage evidence: the alpha replayed on held-out data through
    the production path.

    ``holdout_ic`` is the realized IC of that replay, ``research_ic`` the IC
    the research result claimed (the gate compares the two);
    ``replay_hash_match`` says the replay reproduced the research
    signal stream byte-for-byte (content hash equality);
    ``parity`` says the Java / Rust / C++ ports agree with the Python
    reference on the same replay.
    """

    holdout_ic: float
    research_ic: float
    replay_hash_match: bool
    parity: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "holdout_ic", _check_float(self.holdout_ic, "validation.holdout_ic")
        )
        object.__setattr__(
            self, "research_ic", _check_float(self.research_ic, "validation.research_ic")
        )
        object.__setattr__(
            self,
            "replay_hash_match",
            _check_bool(self.replay_hash_match, "validation.replay_hash_match"),
        )
        object.__setattr__(self, "parity", _check_bool(self.parity, "validation.parity"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "holdout_ic": self.holdout_ic,
            "research_ic": self.research_ic,
            "replay_hash_match": self.replay_hash_match,
            "parity": self.parity,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> ValidationEvidence:
        return _from_dict(ValidationEvidence, data, "validation")


@dataclass(frozen=True)
class PaperEvidence:
    """PAPER-stage evidence: the alpha traded in the paper environment.

    ``n_sessions`` completed paper sessions; ``realized_ic`` the realized IC
    over them versus ``research_ic``; ``net_pnl`` net of modelled costs (money
    unit of the paper book); ``n_kill_events`` hard-risk KILL decisions the
    alpha caused; ``tracking_error`` the std of (paper - backtest) P&L per
    session (diagnostic; no gate reads it yet).
    """

    n_sessions: int
    realized_ic: float
    research_ic: float
    net_pnl: float
    n_kill_events: int
    tracking_error: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "n_sessions", _check_int(self.n_sessions, "paper.n_sessions"))
        object.__setattr__(self, "realized_ic", _check_float(self.realized_ic, "paper.realized_ic"))
        object.__setattr__(self, "research_ic", _check_float(self.research_ic, "paper.research_ic"))
        object.__setattr__(self, "net_pnl", _check_float(self.net_pnl, "paper.net_pnl"))
        object.__setattr__(
            self, "n_kill_events", _check_int(self.n_kill_events, "paper.n_kill_events")
        )
        object.__setattr__(
            self, "tracking_error", _check_float(self.tracking_error, "paper.tracking_error")
        )
        if self.tracking_error < 0.0:
            raise ValueError("paper.tracking_error must be >= 0")

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_sessions": self.n_sessions,
            "realized_ic": self.realized_ic,
            "research_ic": self.research_ic,
            "net_pnl": self.net_pnl,
            "n_kill_events": self.n_kill_events,
            "tracking_error": self.tracking_error,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> PaperEvidence:
        return _from_dict(PaperEvidence, data, "paper")


@dataclass(frozen=True)
class LiveEvidence:
    """One live rolling-IC evaluation (API_ADAPTIVE.md §4/§6).

    ``rolling_ic`` is the mean of the matured live bucket ICs, or ``None``
    when fewer than ``min_ic_buckets`` buckets exist (no evidence);
    ``n_buckets`` the bucket count behind it; ``eval_index`` the adaptive
    block index of the evaluation; ``informative`` is False when the matured
    set gained no new rows since the last counted evaluation (a re-read of a
    frozen window moves nothing — pinned, round-3); ``new_fraction`` the
    share of the reading's window that is new since the last counted one,
    in (0, 1] (module docs).
    """

    rolling_ic: float | None
    n_buckets: int
    eval_index: int
    informative: bool
    new_fraction: float

    def __post_init__(self) -> None:
        if self.rolling_ic is not None:
            object.__setattr__(self, "rolling_ic", _check_float(self.rolling_ic, "live.rolling_ic"))
        object.__setattr__(self, "n_buckets", _check_int(self.n_buckets, "live.n_buckets"))
        object.__setattr__(self, "eval_index", _check_int(self.eval_index, "live.eval_index"))
        object.__setattr__(self, "informative", _check_bool(self.informative, "live.informative"))
        fraction = _check_float(self.new_fraction, "live.new_fraction")
        if not 0.0 < fraction <= 1.0:
            raise ValueError("live.new_fraction must be in (0, 1]")
        object.__setattr__(self, "new_fraction", fraction)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rolling_ic": self.rolling_ic,
            "n_buckets": self.n_buckets,
            "eval_index": self.eval_index,
            "informative": self.informative,
            "new_fraction": self.new_fraction,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> LiveEvidence:
        return _from_dict(LiveEvidence, data, "live")


@dataclass(frozen=True)
class PnlBootstrapEvidence:
    """The bootstrap interval of the pooled net P&L at 1x costs, with what
    pins it (module docs, "The P&L bootstrap block")."""

    ci_low: float | None
    ci_high: float | None
    level: float
    n_resamples: int
    seed: int
    mean_block: float
    n_bars: int
    n_trades: int

    def __post_init__(self) -> None:
        if (self.ci_low is None) != (self.ci_high is None):
            raise ValueError("pnl_bootstrap: ci_low and ci_high are both numbers or both null")
        if self.ci_low is not None:
            low = _check_float(self.ci_low, "pnl_bootstrap.ci_low")
            high = _check_float(self.ci_high, "pnl_bootstrap.ci_high")
            if high < low:
                raise ValueError("pnl_bootstrap: ci_high < ci_low")
            object.__setattr__(self, "ci_low", low)
            object.__setattr__(self, "ci_high", high)
        level = _check_float(self.level, "pnl_bootstrap.level")
        if not 0.0 < level < 1.0:
            raise ValueError("pnl_bootstrap.level must lie in (0, 1)")
        object.__setattr__(self, "level", level)
        object.__setattr__(
            self, "n_resamples", _check_int(self.n_resamples, "pnl_bootstrap.n_resamples", 1)
        )
        object.__setattr__(self, "seed", _check_int(self.seed, "pnl_bootstrap.seed"))
        block = _check_float(self.mean_block, "pnl_bootstrap.mean_block")
        if block < 1.0:
            raise ValueError("pnl_bootstrap.mean_block must be >= 1")
        object.__setattr__(self, "mean_block", block)
        object.__setattr__(self, "n_bars", _check_int(self.n_bars, "pnl_bootstrap.n_bars"))
        object.__setattr__(self, "n_trades", _check_int(self.n_trades, "pnl_bootstrap.n_trades"))

    def gate_value(self, level: float) -> float | None:
        """What the gate compares: ``ci_low`` — or ``None`` (no usable
        interval: the gate fails closed) when the alpha made no trade, when
        the interval was not taken at exactly ``level``, or when it has no
        bounds."""
        if self.n_trades == 0 or self.level != level:
            return None
        return self.ci_low

    def to_dict(self) -> dict[str, Any]:
        return {
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
            "level": self.level,
            "n_resamples": self.n_resamples,
            "seed": self.seed,
            "mean_block": self.mean_block,
            "n_bars": self.n_bars,
            "n_trades": self.n_trades,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> PnlBootstrapEvidence:
        return _from_dict(PnlBootstrapEvidence, data, "pnl_bootstrap")


@dataclass(frozen=True)
class CrossAlphaPeer:
    """One other alpha as the correlation gate sees it: its id, its
    lifecycle state when the evidence was built and the signal correlation
    with the alpha under evaluation, in [-1, 1] (module docs)."""

    alpha_id: str
    state: LifecycleState
    correlation: float

    def __post_init__(self) -> None:
        if not isinstance(self.alpha_id, str) or not self.alpha_id:
            raise ValueError("cross_alpha.peers[].alpha_id: expected a non-empty string")
        state = self.state
        if isinstance(state, str):
            if state not in LifecycleState.__members__:
                raise ValueError(f"cross_alpha.peers[].state: unknown lifecycle state {state!r}")
            state = LifecycleState[state]
        if not isinstance(state, LifecycleState):
            raise ValueError("cross_alpha.peers[].state: expected a lifecycle state name")
        object.__setattr__(self, "state", state)
        rho = _check_float(self.correlation, "cross_alpha.peers[].correlation")
        if not -1.0 <= rho <= 1.0:
            raise ValueError("cross_alpha.peers[].correlation must lie in [-1, 1]")
        object.__setattr__(self, "correlation", rho)

    def to_dict(self) -> dict[str, Any]:
        return {
            "alpha_id": self.alpha_id,
            "state": self.state.name,
            "correlation": self.correlation,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> CrossAlphaPeer:
        return _from_dict(CrossAlphaPeer, data, "cross_alpha.peers[]")


@dataclass(frozen=True)
class CrossAlphaEvidence:
    """The signal correlation of one alpha with every other registered alpha
    (module docs, "The cross-alpha block").

    A peer is *eligible* under a policy when its state is at or beyond the
    policy's ``cross_alpha_min_state`` and it is not RETIRED (a retired
    alpha holds no allocation, so there is nothing to be redundant with).
    """

    peers: tuple[CrossAlphaPeer, ...]

    def __post_init__(self) -> None:
        peers = tuple(self.peers)
        for peer in peers:
            if not isinstance(peer, CrossAlphaPeer):
                raise ValueError("cross_alpha.peers: expected CrossAlphaPeer entries")
        ids = [p.alpha_id for p in peers]
        if any(b <= a for a, b in zip(ids, ids[1:], strict=False)):
            raise ValueError("cross_alpha.peers: must be sorted by alpha_id, without duplicates")
        object.__setattr__(self, "peers", peers)

    def eligible(self, min_state: str) -> tuple[CrossAlphaPeer, ...]:
        """The peers the gate counts under ``min_state`` (class docs)."""
        lowest = int(LifecycleState[min_state])
        retired = int(LifecycleState.RETIRED)
        return tuple(p for p in self.peers if lowest <= int(p.state) < retired)

    def max_abs_correlation(self, min_state: str) -> float:
        """The gate statistic: the largest ``|correlation|`` over the
        eligible peers, ``0.0`` when there is none (the vacuous case)."""
        return max((abs(p.correlation) for p in self.eligible(min_state)), default=0.0)

    def binding_peer(self, min_state: str) -> CrossAlphaPeer | None:
        """The eligible peer with the largest ``|correlation|`` — on a tie
        the one with the smallest ``alpha_id`` — or ``None``.  For reports:
        the gate result itself carries the number only."""
        best: CrossAlphaPeer | None = None
        for peer in self.eligible(min_state):  # ascending alpha_id
            if best is None or abs(peer.correlation) > abs(best.correlation):
                best = peer
        return best

    def to_dict(self) -> dict[str, Any]:
        return {"peers": [p.to_dict() for p in self.peers]}

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> CrossAlphaEvidence:
        if not isinstance(data, Mapping):
            raise ValueError("cross_alpha: expected an object")
        if set(data) != {"peers"}:
            raise ValueError(f"cross_alpha: expected exactly the key 'peers', got {sorted(data)}")
        if not isinstance(data["peers"], (list, tuple)):
            raise ValueError("cross_alpha.peers: expected an array")
        return CrossAlphaEvidence(tuple(CrossAlphaPeer.from_dict(p) for p in data["peers"]))


@dataclass(frozen=True)
class Evidence:
    """Everything the gates may read for one ``advance`` call (see module
    docstring).  ``capacity_usd`` is the alpha's aggregate deployable
    notional proxy (``iap.validation.metrics.capacity_proxy_usd`` summed over
    its universe); it sits beside ``research`` because
    :class:`ExperimentResult` carries no capacity field, and so does
    ``significance_threshold``, the PROMOTE t threshold the research result
    was judged at, ``pnl_bootstrap``, the bootstrap interval of its net
    P&L, and ``cross_alpha``, the signal correlations with the other
    registered alphas (module docs)."""

    research: ExperimentResult | None
    capacity_usd: float | None
    validation: ValidationEvidence | None
    paper: PaperEvidence | None
    live: LiveEvidence | None
    #: False = the research result is recorded but is NOT promotion evidence
    research_gate_eligible: bool = True
    #: the t threshold the research result was judged at (``None``: none)
    significance_threshold: float | None = None
    #: signal correlation with every other registered alpha (``None``: unmeasured)
    cross_alpha: CrossAlphaEvidence | None = None
    #: bootstrap interval of the pooled net P&L at 1x costs (``None``: none)
    pnl_bootstrap: PnlBootstrapEvidence | None = None

    def __post_init__(self) -> None:
        if self.pnl_bootstrap is not None and not isinstance(
            self.pnl_bootstrap, PnlBootstrapEvidence
        ):
            raise ValueError("evidence.pnl_bootstrap must be a PnlBootstrapEvidence or None")
        if self.cross_alpha is not None and not isinstance(self.cross_alpha, CrossAlphaEvidence):
            raise ValueError("evidence.cross_alpha must be a CrossAlphaEvidence or None")
        if self.significance_threshold is not None:
            threshold = _check_float(self.significance_threshold, "evidence.significance_threshold")
            if threshold <= 0.0:
                raise ValueError("evidence.significance_threshold must be > 0")
            object.__setattr__(self, "significance_threshold", threshold)
        object.__setattr__(
            self,
            "research_gate_eligible",
            _check_bool(self.research_gate_eligible, "evidence.research_gate_eligible"),
        )
        if self.research is not None and not isinstance(self.research, ExperimentResult):
            raise ValueError("evidence.research must be an ExperimentResult or None")
        if self.capacity_usd is not None:
            cap = _check_float(self.capacity_usd, "evidence.capacity_usd")
            if cap < 0.0:
                raise ValueError("evidence.capacity_usd must be >= 0")
            object.__setattr__(self, "capacity_usd", cap)
        for name, cls in (
            ("validation", ValidationEvidence),
            ("paper", PaperEvidence),
            ("live", LiveEvidence),
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(value, cls):
                raise ValueError(f"evidence.{name} must be a {cls.__name__} or None")

    @staticmethod
    def empty() -> Evidence:
        """No evidence at all (every block absent)."""
        return Evidence(research=None, capacity_usd=None, validation=None, paper=None, live=None)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready document; absent blocks are ``null``.
        ``research_gate_eligible`` appears only when ``False`` (module docs)."""
        doc: dict[str, Any] = {
            "research": None if self.research is None else self.research.to_dict(),
            "capacity_usd": self.capacity_usd,
            "significance_threshold": self.significance_threshold,
            "pnl_bootstrap": None if self.pnl_bootstrap is None else self.pnl_bootstrap.to_dict(),
            "cross_alpha": None if self.cross_alpha is None else self.cross_alpha.to_dict(),
            "validation": None if self.validation is None else self.validation.to_dict(),
            "paper": None if self.paper is None else self.paper.to_dict(),
            "live": None if self.live is None else self.live.to_dict(),
        }
        if not self.research_gate_eligible:
            doc["research_gate_eligible"] = False
        return doc

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> Evidence:
        """Strict inverse of :meth:`to_dict`."""
        names = (
            "research",
            "capacity_usd",
            "significance_threshold",
            "pnl_bootstrap",
            "cross_alpha",
            "validation",
            "paper",
            "live",
        )
        unknown = sorted(set(data) - set(names) - {"research_gate_eligible"})
        missing = [n for n in names if n not in data]
        if unknown:
            raise ValueError(f"evidence: unknown keys {unknown}")
        if missing:
            raise ValueError(f"evidence: missing keys {missing}")
        research = data["research"]
        validation = data["validation"]
        paper = data["paper"]
        live = data["live"]
        cross = data["cross_alpha"]
        boot = data["pnl_bootstrap"]
        return Evidence(
            research=None if research is None else ExperimentResult.from_dict(research),
            capacity_usd=data["capacity_usd"],
            validation=None if validation is None else ValidationEvidence.from_dict(validation),
            paper=None if paper is None else PaperEvidence.from_dict(paper),
            live=None if live is None else LiveEvidence.from_dict(live),
            research_gate_eligible=data.get("research_gate_eligible", True),
            significance_threshold=data["significance_threshold"],
            cross_alpha=None if cross is None else CrossAlphaEvidence.from_dict(cross),
            pnl_bootstrap=None if boot is None else PnlBootstrapEvidence.from_dict(boot),
        )
