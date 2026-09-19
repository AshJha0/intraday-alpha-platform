"""Alpha promotion lifecycle — RESEARCH -> CANDIDATE -> VALIDATING -> PAPER ->
ACTIVE -> WATCH -> RETIRED with a gate at every edge.

Extends (never alters) the ACTIVE / WATCH / RETIRED machinery of
``iap.adaptive.lifecycle``: the live edges delegate to ``LifecycleTracker``
with the pinned ``adaptive.lifecycle`` gates; the promotion edges evaluate
the spec §20 gates of ``iap.validation.validate`` from
``configs/strategies/lifecycle.json``.  Every transition is an
``iap.contracts.types.LifecycleTransition``.

Modules: ``config`` (policy), ``evidence`` (typed inputs), ``gates`` (the gate
table), ``machine`` (the state machine), ``registry`` (persistence),
``bootstrap`` (the research artefacts -> registry), ``golden`` (the scripted
scenarios behind ``tests/golden/expected_lifecycle.json``).
"""

from iap.lifecycle.config import (
    DEFAULT_LIFECYCLE_PATH,
    DEFAULT_STRATEGIES_PATH,
    LIFECYCLE_CONFIG_VERSION,
    GateThresholds,
    PolicyConfig,
    load_policy_config,
)
from iap.lifecycle.evidence import (
    Evidence,
    LiveEvidence,
    PaperEvidence,
    ValidationEvidence,
)
from iap.lifecycle.gates import GATE_SPECS, Gate, GateSpec, build_gates, ic_rank_gap
from iap.lifecycle.machine import (
    ALLOWED_TRANSITIONS,
    PROMOTION_EDGES,
    STATE_COUNT,
    AlphaLifecycle,
    Edge,
    EdgeKind,
    edge_for,
    transition_table,
)
from iap.lifecycle.registry import (
    REGISTRY_VERSION,
    AlphaRecord,
    AlphaRegistry,
    GateEvaluation,
    LifecycleTransitionLog,
    Outcome,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "DEFAULT_LIFECYCLE_PATH",
    "DEFAULT_STRATEGIES_PATH",
    "GATE_SPECS",
    "LIFECYCLE_CONFIG_VERSION",
    "PROMOTION_EDGES",
    "REGISTRY_VERSION",
    "STATE_COUNT",
    "AlphaLifecycle",
    "AlphaRecord",
    "AlphaRegistry",
    "Edge",
    "EdgeKind",
    "Evidence",
    "Gate",
    "GateEvaluation",
    "GateSpec",
    "GateThresholds",
    "LifecycleTransitionLog",
    "LiveEvidence",
    "Outcome",
    "PaperEvidence",
    "PolicyConfig",
    "ValidationEvidence",
    "build_gates",
    "edge_for",
    "ic_rank_gap",
    "load_policy_config",
    "transition_table",
]
