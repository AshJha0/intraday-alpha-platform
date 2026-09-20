"""Typed evidence handed to the lifecycle gates.

One :class:`Evidence` carries at most one block per lifecycle stage; a gate
reads the block of the edge it guards and nothing else:

======================  ============================================
edge                    evidence block
======================  ============================================
RESEARCH -> CANDIDATE   ``research`` (:class:`ExperimentResult`)
CANDIDATE -> VALIDATING ``research`` + ``capacity_usd``
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
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields
from typing import Any, Dict, Mapping, Optional

from iap.contracts.types import ExperimentResult

__all__ = [
    "Evidence",
    "LiveEvidence",
    "PaperEvidence",
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
        object.__setattr__(self, "holdout_ic", _check_float(self.holdout_ic, "validation.holdout_ic"))
        object.__setattr__(self, "research_ic", _check_float(self.research_ic, "validation.research_ic"))
        object.__setattr__(self, "replay_hash_match", _check_bool(self.replay_hash_match, "validation.replay_hash_match"))
        object.__setattr__(self, "parity", _check_bool(self.parity, "validation.parity"))

    def to_dict(self) -> Dict[str, Any]:
        return {"holdout_ic": self.holdout_ic, "research_ic": self.research_ic,
                "replay_hash_match": self.replay_hash_match, "parity": self.parity}

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "ValidationEvidence":
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
        object.__setattr__(self, "n_kill_events", _check_int(self.n_kill_events, "paper.n_kill_events"))
        object.__setattr__(self, "tracking_error", _check_float(self.tracking_error, "paper.tracking_error"))
        if self.tracking_error < 0.0:
            raise ValueError("paper.tracking_error must be >= 0")

    def to_dict(self) -> Dict[str, Any]:
        return {"n_sessions": self.n_sessions, "realized_ic": self.realized_ic,
                "research_ic": self.research_ic, "net_pnl": self.net_pnl,
                "n_kill_events": self.n_kill_events,
                "tracking_error": self.tracking_error}

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "PaperEvidence":
        return _from_dict(PaperEvidence, data, "paper")


@dataclass(frozen=True)
class LiveEvidence:
    """One live rolling-IC evaluation (API_ADAPTIVE.md §4/§6).

    ``rolling_ic`` is the mean of the matured live bucket ICs, or ``None``
    when fewer than ``min_ic_buckets`` buckets exist (no evidence);
    ``n_buckets`` the bucket count behind it; ``eval_index`` the adaptive
    block index of the evaluation; ``informative`` is False when the matured
    set gained no new rows since the last counted evaluation (a re-read of a
    frozen window moves nothing — pinned, round-3).
    """

    rolling_ic: Optional[float]
    n_buckets: int
    eval_index: int
    informative: bool

    def __post_init__(self) -> None:
        if self.rolling_ic is not None:
            object.__setattr__(self, "rolling_ic", _check_float(self.rolling_ic, "live.rolling_ic"))
        object.__setattr__(self, "n_buckets", _check_int(self.n_buckets, "live.n_buckets"))
        object.__setattr__(self, "eval_index", _check_int(self.eval_index, "live.eval_index"))
        object.__setattr__(self, "informative", _check_bool(self.informative, "live.informative"))

    def to_dict(self) -> Dict[str, Any]:
        return {"rolling_ic": self.rolling_ic, "n_buckets": self.n_buckets,
                "eval_index": self.eval_index, "informative": self.informative}

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "LiveEvidence":
        return _from_dict(LiveEvidence, data, "live")


@dataclass(frozen=True)
class Evidence:
    """Everything the gates may read for one ``advance`` call (see module
    docstring).  ``capacity_usd`` is the alpha's aggregate deployable
    notional proxy (``iap.validation.metrics.capacity_proxy_usd`` summed over
    its universe); it sits beside ``research`` because
    :class:`ExperimentResult` carries no capacity field."""

    research: Optional[ExperimentResult]
    capacity_usd: Optional[float]
    validation: Optional[ValidationEvidence]
    paper: Optional[PaperEvidence]
    live: Optional[LiveEvidence]

    def __post_init__(self) -> None:
        if self.research is not None and not isinstance(self.research, ExperimentResult):
            raise ValueError("evidence.research must be an ExperimentResult or None")
        if self.capacity_usd is not None:
            cap = _check_float(self.capacity_usd, "evidence.capacity_usd")
            if cap < 0.0:
                raise ValueError("evidence.capacity_usd must be >= 0")
            object.__setattr__(self, "capacity_usd", cap)
        for name, cls in (("validation", ValidationEvidence), ("paper", PaperEvidence),
                          ("live", LiveEvidence)):
            value = getattr(self, name)
            if value is not None and not isinstance(value, cls):
                raise ValueError(f"evidence.{name} must be a {cls.__name__} or None")

    @staticmethod
    def empty() -> "Evidence":
        """No evidence at all (every block absent)."""
        return Evidence(research=None, capacity_usd=None, validation=None,
                        paper=None, live=None)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-ready document; absent blocks are ``null``."""
        return {
            "research": None if self.research is None else self.research.to_dict(),
            "capacity_usd": self.capacity_usd,
            "validation": None if self.validation is None else self.validation.to_dict(),
            "paper": None if self.paper is None else self.paper.to_dict(),
            "live": None if self.live is None else self.live.to_dict(),
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "Evidence":
        """Strict inverse of :meth:`to_dict`."""
        names = ("research", "capacity_usd", "validation", "paper", "live")
        unknown = sorted(set(data) - set(names))
        missing = [n for n in names if n not in data]
        if unknown:
            raise ValueError(f"evidence: unknown keys {unknown}")
        if missing:
            raise ValueError(f"evidence: missing keys {missing}")
        research = data["research"]
        validation = data["validation"]
        paper = data["paper"]
        live = data["live"]
        return Evidence(
            research=None if research is None else ExperimentResult.from_dict(research),
            capacity_usd=data["capacity_usd"],
            validation=None if validation is None else ValidationEvidence.from_dict(validation),
            paper=None if paper is None else PaperEvidence.from_dict(paper),
            live=None if live is None else LiveEvidence.from_dict(live),
        )
