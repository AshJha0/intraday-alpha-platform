"""The alpha promotion state machine (``iap.contracts.protocols.AlphaLifecycle``).

States are the ordered ``LifecycleState`` integers 0..6::

    RESEARCH(0) -> CANDIDATE(1) -> VALIDATING(2) -> PAPER(3) -> ACTIVE(4)
                                                      ACTIVE(4) <-> WATCH(5) -> RETIRED(6)

The transition table :data:`ALLOWED_TRANSITIONS` is data: one :class:`Edge`
per allowed move with its kind, the actor that may take it and the gates it
evaluates, in pinned order.  ``advance`` looks up the SYSTEM edge leaving the
current state, evaluates its gates against the :class:`Evidence` and applies
the outcome:

* **promotion** (RESEARCH..PAPER): every gate passes ⇒ move one state up,
  reset the failure counter;
* **demotion**: at CANDIDATE a failed ``leakage_clean`` demotes to RESEARCH
  at once (a leaking alpha is not a candidate); at VALIDATING / PAPER each
  failed evaluation increments ``consecutive_failures`` and the
  ``max_consecutive_failures``-th one demotes to CANDIDATE (counter reset);
  a passing evaluation resets the counter;
* **silence**: an evaluation whose evidence block for the edge is absent
  (``research`` at CANDIDATE, ``validation`` at VALIDATING, ``paper`` at
  PAPER, ``live`` / a null or uninformative rolling IC at ACTIVE / WATCH)
  evaluates nothing and moves nothing — not even the failure counter
  (silence is not evidence, API_ADAPTIVE.md §6).  RESEARCH is the exception
  by construction: its ``ledger_entry_exists`` gate IS the presence check;
* **live** (ACTIVE / WATCH): delegated unchanged to
  :class:`iap.adaptive.lifecycle.LifecycleTracker` with the pinned
  ``adaptive.lifecycle`` gates and hysteresis; its ``Transition`` is wrapped
  into a ``LifecycleTransition`` with ``gates = {"rolling_ic": ...}`` and the
  tracker's policy name;
* **RETIRED is terminal for SYSTEM**: the adaptive study lets a retired
  alpha recover to WATCH because shadow scoring continues inside one
  backtest; on the platform a retirement is final and re-entry is the HUMAN
  ``reset_to_research``, which re-runs the whole evidence chain (spec §20).
  A SYSTEM ``advance`` on a RETIRED alpha records a ``TERMINAL`` evaluation
  and returns ``None``.

Manual edges: ``retire`` (any non-retired state → RETIRED) and
``reset_to_research`` (RETIRED → RESEARCH) require ``Actor.HUMAN`` and a
non-empty reason; a SYSTEM actor is rejected with ``ValueError``.

Every call is a pure function of the registry state and its arguments —
no wall clock, no RNG, sorted iteration only — so replaying the transition
log reproduces the registry.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Mapping, Optional, Tuple

from iap.adaptive.lifecycle import LifecycleTracker
from iap.adaptive.lifecycle import Transition as TrackerTransition
from iap.contracts.types import (
    Actor,
    GateResult,
    LifecycleState,
    LifecycleTransition,
)
from iap.lifecycle.config import PolicyConfig
from iap.lifecycle.evidence import Evidence
from iap.lifecycle.gates import Gate, build_gates
from iap.lifecycle.registry import (
    AlphaRecord,
    AlphaRegistry,
    GateEvaluation,
    LifecycleTransitionLog,
    Outcome,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "AlphaLifecycle",
    "Edge",
    "EdgeKind",
    "PROMOTION_EDGES",
    "STATE_COUNT",
    "edge_for",
    "transition_table",
]

S = LifecycleState

#: Number of lifecycle states (integer ids ``0 .. STATE_COUNT - 1``).
STATE_COUNT = len(LifecycleState)


class EdgeKind(str, Enum):
    """Why an edge exists."""

    PROMOTION = "PROMOTION"
    DEMOTION = "DEMOTION"
    LIVE = "LIVE"
    MANUAL = "MANUAL"


@dataclass(frozen=True)
class Edge:
    """One allowed transition: who may take it and which gates it evaluates
    (in this order)."""

    from_state: LifecycleState
    to_state: LifecycleState
    kind: EdgeKind
    actor: Actor
    gates: Tuple[str, ...]

    def __post_init__(self) -> None:
        if self.from_state is self.to_state:
            raise ValueError("Edge: from_state == to_state")

    def to_dict(self) -> Dict[str, object]:
        return {"from_state": self.from_state.name, "to_state": self.to_state.name,
                "kind": self.kind.value, "actor": self.actor.value,
                "gates": list(self.gates)}


_LIVE_GATES: Tuple[str, ...] = ("rolling_ic",)

#: The transition table (pinned order).  SYSTEM promotion edges carry the
#: gate list evaluated by ``advance``; the two SYSTEM demotion edges out of
#: VALIDATING / PAPER are taken on the ``max_consecutive_failures``-th failed
#: promotion evaluation; CANDIDATE -> RESEARCH on a failed ``leakage_clean``.
ALLOWED_TRANSITIONS: Tuple[Edge, ...] = (
    Edge(S.RESEARCH, S.CANDIDATE, EdgeKind.PROMOTION, Actor.SYSTEM,
         ("ledger_entry_exists", "leakage_clean")),
    Edge(S.CANDIDATE, S.VALIDATING, EdgeKind.PROMOTION, Actor.SYSTEM,
         ("leakage_clean", "oos_ic", "statistical_significance", "fold_consistency",
          "fold_count", "hypothesis_sign", "net_pnl_after_costs", "capacity",
          "stability")),
    Edge(S.CANDIDATE, S.RESEARCH, EdgeKind.DEMOTION, Actor.SYSTEM, ("leakage_clean",)),
    Edge(S.VALIDATING, S.PAPER, EdgeKind.PROMOTION, Actor.SYSTEM,
         ("holdout_ic_tracks_research", "replay_reproducible", "cross_language_parity")),
    Edge(S.VALIDATING, S.CANDIDATE, EdgeKind.DEMOTION, Actor.SYSTEM, ()),
    Edge(S.PAPER, S.ACTIVE, EdgeKind.PROMOTION, Actor.SYSTEM,
         ("paper_min_sessions", "paper_ic_tracking", "paper_net_pnl", "no_kill_events")),
    Edge(S.PAPER, S.CANDIDATE, EdgeKind.DEMOTION, Actor.SYSTEM, ()),
    Edge(S.ACTIVE, S.WATCH, EdgeKind.LIVE, Actor.SYSTEM, _LIVE_GATES),
    Edge(S.WATCH, S.ACTIVE, EdgeKind.LIVE, Actor.SYSTEM, _LIVE_GATES),
    Edge(S.WATCH, S.RETIRED, EdgeKind.LIVE, Actor.SYSTEM, _LIVE_GATES),
    Edge(S.RESEARCH, S.RETIRED, EdgeKind.MANUAL, Actor.HUMAN, ()),
    Edge(S.CANDIDATE, S.RETIRED, EdgeKind.MANUAL, Actor.HUMAN, ()),
    Edge(S.VALIDATING, S.RETIRED, EdgeKind.MANUAL, Actor.HUMAN, ()),
    Edge(S.PAPER, S.RETIRED, EdgeKind.MANUAL, Actor.HUMAN, ()),
    Edge(S.ACTIVE, S.RETIRED, EdgeKind.MANUAL, Actor.HUMAN, ()),
    Edge(S.WATCH, S.RETIRED, EdgeKind.MANUAL, Actor.HUMAN, ()),
    Edge(S.RETIRED, S.RESEARCH, EdgeKind.MANUAL, Actor.HUMAN, ()),
)

_EDGE_INDEX: Mapping[Tuple[LifecycleState, LifecycleState, EdgeKind], Edge] = {
    (e.from_state, e.to_state, e.kind): e for e in ALLOWED_TRANSITIONS
}
if len(_EDGE_INDEX) != len(ALLOWED_TRANSITIONS):
    raise RuntimeError("duplicate edge in ALLOWED_TRANSITIONS")

#: The SYSTEM promotion edge leaving each pre-live state.
PROMOTION_EDGES: Mapping[LifecycleState, Edge] = {
    e.from_state: e for e in ALLOWED_TRANSITIONS if e.kind is EdgeKind.PROMOTION
}

#: The evidence block a promotion edge needs before its gates are evaluated
#: (RESEARCH has none: its presence gate does the checking).
_REQUIRED_BLOCK: Mapping[LifecycleState, Optional[str]] = {
    S.RESEARCH: None,
    S.CANDIDATE: "research",
    S.VALIDATING: "validation",
    S.PAPER: "paper",
}


def edge_for(from_state: LifecycleState, to_state: LifecycleState,
             kind: EdgeKind) -> Edge:
    """The table entry for ``(from, to, kind)`` or ``KeyError``."""
    try:
        return _EDGE_INDEX[(from_state, to_state, kind)]
    except KeyError:
        raise KeyError(f"no {kind.value} edge {from_state.name} -> {to_state.name}") from None


def transition_table() -> List[Dict[str, object]]:
    """JSON-ready copy of :data:`ALLOWED_TRANSITIONS` (embedded in the golden)."""
    return [e.to_dict() for e in ALLOWED_TRANSITIONS]


def _live_gate_result(tr: TrackerTransition, config: PolicyConfig) -> GateResult:
    """The ``rolling_ic`` gate as it decided a tracker transition: a breach
    transition (-> WATCH, -> RETIRED) failed the watch gate; a re-activation
    (-> ACTIVE) passed the reactivate gate."""
    if tr.to_state == "ACTIVE":
        return GateResult(passed=True, value=tr.rolling_ic,
                          threshold=config.live.reactivate_ic_gate)
    return GateResult(passed=False, value=tr.rolling_ic,
                      threshold=config.live.watch_ic_gate)


class AlphaLifecycle:
    """The machine over an :class:`AlphaRegistry` (see module docstring).

    ``transition_log`` (optional) receives every transition as it happens;
    ``evaluations`` and ``transitions`` keep the in-memory history of this
    instance in call order.
    """

    def __init__(self, config: PolicyConfig, registry: AlphaRegistry,
                 transition_log: Optional[LifecycleTransitionLog] = None) -> None:
        if registry.policy != config.policy:
            raise ValueError(f"registry policy {registry.policy!r} != config policy "
                             f"{config.policy!r}")
        self.config = config
        self.registry = registry
        self.log = transition_log
        self.gates: Dict[str, Gate] = build_gates(config)
        self.evaluations: List[GateEvaluation] = []
        self.transitions: List[LifecycleTransition] = []
        self._trackers: Dict[str, LifecycleTracker] = {}
        for edge in ALLOWED_TRANSITIONS:
            for name in edge.gates:
                if name not in self.gates:
                    raise RuntimeError(f"edge {edge} names unknown gate {name!r}")

    # -- registration / queries --------------------------------------------

    def register(self, alpha_id: str, event_ts: int, *,
                 experiment_id: Optional[str] = None,
                 data_version: Optional[str] = None,
                 feature_version: Optional[str] = None,
                 model_version: Optional[str] = None) -> AlphaRecord:
        """Enter ``alpha_id`` at RESEARCH as of ``event_ts``."""
        return self.registry.add(AlphaRecord.new(
            alpha_id, event_ts, experiment_id=experiment_id, data_version=data_version,
            feature_version=feature_version, model_version=model_version))

    def state(self, alpha_id: str) -> LifecycleState:
        return self.registry.get(alpha_id).state

    def record(self, alpha_id: str) -> AlphaRecord:
        return self.registry.get(alpha_id)

    # -- transitions ---------------------------------------------------------

    def _apply(self, rec: AlphaRecord, transition: LifecycleTransition) -> LifecycleTransition:
        if self.log is not None:
            self.log.append(transition)
        self.transitions.append(transition)
        rec.state = transition.to_state
        rec.since_ts = transition.event_ts
        rec.last_transition = transition
        rec.consecutive_failures = 0
        rec.breach_count = 0
        rec.recovery_count = 0
        self._trackers.pop(rec.alpha_id, None)
        return transition

    def _record_evaluation(self, rec: AlphaRecord, evaluation: GateEvaluation) -> None:
        self.evaluations.append(evaluation)
        rec.last_evaluation = evaluation

    def _transition(self, rec: AlphaRecord, edge: Edge, event_ts: int, reason: str,
                    gates: Mapping[str, GateResult], actor: Actor,
                    policy: Optional[str] = None) -> LifecycleTransition:
        return LifecycleTransition(
            alpha_id=rec.alpha_id, from_state=edge.from_state, to_state=edge.to_state,
            event_ts=event_ts, reason=reason, gates=dict(gates),
            policy=self.config.policy if policy is None else policy, actor=actor)

    def advance(self, alpha_id: str, event_ts: int,
                evidence: Evidence) -> Optional[LifecycleTransition]:
        """Evaluate the SYSTEM edge leaving the alpha's current state.

        Returns the transition made, or ``None`` (a :class:`GateEvaluation`
        is recorded either way).
        """
        if not isinstance(evidence, Evidence):
            raise TypeError(f"advance: expected Evidence, got {type(evidence).__name__}")
        rec = self.registry.get(alpha_id)
        state = rec.state
        if state is S.RETIRED:
            self._record_evaluation(rec, GateEvaluation(
                alpha_id, event_ts, state, Outcome.TERMINAL, {},
                rec.consecutive_failures, None))
            return None
        if state in (S.ACTIVE, S.WATCH):
            return self._advance_live(rec, event_ts, evidence)
        return self._advance_promotion(rec, event_ts, evidence)

    def _advance_promotion(self, rec: AlphaRecord, event_ts: int,
                           evidence: Evidence) -> Optional[LifecycleTransition]:
        state = rec.state
        edge = PROMOTION_EDGES[state]
        block = _REQUIRED_BLOCK[state]
        if block is not None and getattr(evidence, block) is None:
            self._record_evaluation(rec, GateEvaluation(
                rec.alpha_id, event_ts, state, Outcome.NO_EVIDENCE, {},
                rec.consecutive_failures, None))
            return None

        results: Dict[str, GateResult] = {
            name: self.gates[name].evaluate(rec.alpha_id, evidence) for name in edge.gates
        }
        failed = [name for name in edge.gates if not results[name].passed]

        if not failed:
            transition = self._transition(
                rec, edge, event_ts,
                f"all {len(edge.gates)} gates passed: {state.name} -> {edge.to_state.name}",
                results, Actor.SYSTEM)
            self._apply(rec, transition)
            self._record_evaluation(rec, GateEvaluation(
                rec.alpha_id, event_ts, state, Outcome.TRANSITION, results, 0, transition))
            return transition

        if state is S.CANDIDATE and "leakage_clean" in failed:
            demote = edge_for(S.CANDIDATE, S.RESEARCH, EdgeKind.DEMOTION)
            transition = self._transition(
                rec, demote, event_ts,
                "leakage_clean failed: a leaking alpha is not a candidate",
                results, Actor.SYSTEM)
            self._apply(rec, transition)
            self._record_evaluation(rec, GateEvaluation(
                rec.alpha_id, event_ts, state, Outcome.TRANSITION, results, 0, transition))
            return transition

        if state in (S.VALIDATING, S.PAPER):
            rec.consecutive_failures += 1
            if rec.consecutive_failures >= self.config.max_consecutive_failures:
                demote = edge_for(state, S.CANDIDATE, EdgeKind.DEMOTION)
                transition = self._transition(
                    rec, demote, event_ts,
                    f"{rec.consecutive_failures} consecutive failed evaluations "
                    f"(max {self.config.max_consecutive_failures}); failed gates: "
                    f"{', '.join(failed)}",
                    results, Actor.SYSTEM)
                self._apply(rec, transition)
                self._record_evaluation(rec, GateEvaluation(
                    rec.alpha_id, event_ts, state, Outcome.TRANSITION, results, 0,
                    transition))
                return transition

        self._record_evaluation(rec, GateEvaluation(
            rec.alpha_id, event_ts, state, Outcome.HOLD, results,
            rec.consecutive_failures, None))
        return None

    def _tracker(self, rec: AlphaRecord) -> LifecycleTracker:
        tracker = self._trackers.get(rec.alpha_id)
        if tracker is None:
            tracker = LifecycleTracker(
                alpha_id=rec.alpha_id, config=self.config.live, policy=self.config.policy,
                log=None, state=rec.state.name, breach_count=rec.breach_count,
                recovery_count=rec.recovery_count)
            self._trackers[rec.alpha_id] = tracker
        return tracker

    def _advance_live(self, rec: AlphaRecord, event_ts: int,
                      evidence: Evidence) -> Optional[LifecycleTransition]:
        state = rec.state
        live = evidence.live
        if live is None or live.rolling_ic is None or not live.informative:
            self._record_evaluation(rec, GateEvaluation(
                rec.alpha_id, event_ts, state, Outcome.NO_EVIDENCE, {},
                rec.consecutive_failures, None))
            return None
        tracker = self._tracker(rec)
        n_before = len(tracker.transitions)
        tracker.update(event_ts, live.rolling_ic, live.informative)
        gate = {"rolling_ic": self.gates["rolling_ic"].evaluate(rec.alpha_id, evidence)}
        if len(tracker.transitions) == n_before:
            rec.breach_count = tracker.breach_count
            rec.recovery_count = tracker.recovery_count
            self._record_evaluation(rec, GateEvaluation(
                rec.alpha_id, event_ts, state, Outcome.HOLD, gate,
                rec.consecutive_failures, None))
            return None
        tr = tracker.transitions[-1]
        to_state = LifecycleState[tr.to_state]
        edge = edge_for(state, to_state, EdgeKind.LIVE)
        transition = self._transition(
            rec, edge, event_ts, tr.reason,
            {"rolling_ic": _live_gate_result(tr, self.config)}, Actor.SYSTEM,
            policy=tracker.policy)
        self._apply(rec, transition)
        # The tracker keeps counting across its own transition (the breach
        # that enters WATCH counts as breach #1 — pinned): keep it, and mirror
        # its counters on the record so a reload resumes exactly.
        self._trackers[rec.alpha_id] = tracker
        rec.breach_count = tracker.breach_count
        rec.recovery_count = tracker.recovery_count
        self._record_evaluation(rec, GateEvaluation(
            rec.alpha_id, event_ts, state, Outcome.TRANSITION, gate, 0, transition))
        return transition

    # -- manual edges --------------------------------------------------------

    @staticmethod
    def _check_manual(actor: Actor, reason: str, what: str) -> None:
        if not isinstance(actor, Actor):
            raise TypeError(f"{what}: actor must be an Actor")
        if actor is not Actor.HUMAN:
            raise ValueError(f"{what}: requires Actor.HUMAN, got {actor.value}")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"{what}: a non-empty reason is required")

    def retire(self, alpha_id: str, event_ts: int, reason: str,
               actor: Actor = Actor.HUMAN) -> LifecycleTransition:
        """Manual retirement from any non-retired state (HUMAN only)."""
        self._check_manual(actor, reason, "retire")
        rec = self.registry.get(alpha_id)
        if rec.state is S.RETIRED:
            raise ValueError(f"retire: {alpha_id} is already RETIRED")
        edge = edge_for(rec.state, S.RETIRED, EdgeKind.MANUAL)
        return self._apply(rec, self._transition(rec, edge, event_ts, reason, {}, actor))

    def reset_to_research(self, alpha_id: str, event_ts: int, reason: str,
                          actor: Actor = Actor.HUMAN) -> LifecycleTransition:
        """Manual re-research: RETIRED -> RESEARCH (HUMAN only)."""
        self._check_manual(actor, reason, "reset_to_research")
        rec = self.registry.get(alpha_id)
        if rec.state is not S.RETIRED:
            raise ValueError(f"reset_to_research: {alpha_id} is {rec.state.name}, "
                             "not RETIRED")
        edge = edge_for(S.RETIRED, S.RESEARCH, EdgeKind.MANUAL)
        return self._apply(rec, self._transition(rec, edge, event_ts, reason, {}, actor))
