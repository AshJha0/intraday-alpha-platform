"""Claim taxonomy: what a research result is allowed to say (v1.18 review finding 7).

Every scorecard, screen and lifecycle summary labels its result with ONE
:class:`ClaimStatus`.  Five evidence statuses form a ladder; two are off it:

=====================================  ==============================================
``feature_correct``                    the feature / label computes what it claims
                                       (parity, leakage and unit tests); says nothing
                                       about predictive value
``exploratory_signal``                 a positive IC, a ranking, one session, or any
                                       result not covered by a signed registration
``passes_registered_net_screen``       a signed, pre-registered net-of-cost screen
                                       passed on >= 2 sessions (clusters)
``confirmed_untouched_oos``            the same registered hypothesis confirmed on
                                       out-of-sample data never touched before
``strategy_validated_execution_capacity``  confirmed, AND execution (fills, queue,
                                       latency) and capacity validated against real or
                                       independently calibrated fills
``inconclusive``                       underpowered, insufficient or missing evidence
``rejected``                           a registered test ran with adequate power and
                                       failed
=====================================  ==============================================

Rules (pinned by ``python/tests/test_claims.py``):

* A positive IC alone, a single session, or a top rank NEVER classifies above
  ``exploratory_signal``.  :attr:`ClaimFacts.ranked_top` is accepted and
  deliberately ignored, so a caller cannot buy a higher status with a ranking.
* Anything above ``exploratory_signal`` needs a signed registration made before
  the data were seen and at least :data:`MIN_SESSIONS_ABOVE_EXPLORATORY` sessions.
* Promotion (a lifecycle move into PAPER or beyond, or any "promote" verdict)
  requires ``confirmed_untouched_oos`` (:func:`may_promote`,
  :func:`require_promotable`); ACTIVE requires
  ``strategy_validated_execution_capacity`` (:data:`LIFECYCLE_REQUIRED_CLAIM`).
* Missing or insufficient evidence is ``inconclusive``, never a pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
    "ClaimFacts",
    "ClaimStatus",
    "LADDER",
    "LIFECYCLE_CLAIM_CEILING",
    "LIFECYCLE_REQUIRED_CLAIM",
    "MIN_SESSIONS_ABOVE_EXPLORATORY",
    "PromotionBlocked",
    "classify",
    "from_screen_verdict",
    "may_promote",
    "require_promotable",
]


class ClaimStatus(str, Enum):
    FEATURE_CORRECT = "feature_correct"
    EXPLORATORY_SIGNAL = "exploratory_signal"
    PASSES_REGISTERED_NET_SCREEN = "passes_registered_net_screen"
    CONFIRMED_UNTOUCHED_OOS = "confirmed_untouched_oos"
    STRATEGY_VALIDATED_EXECUTION_CAPACITY = "strategy_validated_execution_capacity"
    INCONCLUSIVE = "inconclusive"
    REJECTED = "rejected"


#: the five evidence statuses, weakest first (``inconclusive`` / ``rejected`` are off the ladder)
LADDER: tuple[ClaimStatus, ...] = (
    ClaimStatus.FEATURE_CORRECT,
    ClaimStatus.EXPLORATORY_SIGNAL,
    ClaimStatus.PASSES_REGISTERED_NET_SCREEN,
    ClaimStatus.CONFIRMED_UNTOUCHED_OOS,
    ClaimStatus.STRATEGY_VALIDATED_EXECUTION_CAPACITY,
)

#: one session is one cluster: it supports a screen, never a confirmation
MIN_SESSIONS_ABOVE_EXPLORATORY = 2

#: lifecycle state name -> the claim an alpha must hold to ENTER it
LIFECYCLE_REQUIRED_CLAIM: dict[str, ClaimStatus] = {
    "PAPER": ClaimStatus.CONFIRMED_UNTOUCHED_OOS,
    "ACTIVE": ClaimStatus.STRATEGY_VALIDATED_EXECUTION_CAPACITY,
}

#: lifecycle state name -> the strongest claim that state may be described with
LIFECYCLE_CLAIM_CEILING: dict[str, ClaimStatus] = {
    "RESEARCH": ClaimStatus.EXPLORATORY_SIGNAL,
    "CANDIDATE": ClaimStatus.EXPLORATORY_SIGNAL,
    "VALIDATING": ClaimStatus.PASSES_REGISTERED_NET_SCREEN,
    "PAPER": ClaimStatus.CONFIRMED_UNTOUCHED_OOS,
    "ACTIVE": ClaimStatus.STRATEGY_VALIDATED_EXECUTION_CAPACITY,
    "WATCH": ClaimStatus.STRATEGY_VALIDATED_EXECUTION_CAPACITY,
    "RETIRED": ClaimStatus.REJECTED,
}


class PromotionBlocked(ValueError):
    """A promotion was attempted on a claim weaker than ``confirmed_untouched_oos``."""


@dataclass(frozen=True)
class ClaimFacts:
    """The facts a classifier may use.  Every field defaults to "not shown"."""

    feature_verified: bool = False
    positive_ic: bool = False
    sessions: int = 0
    #: accepted and ignored: a ranking is never evidence of a higher claim
    ranked_top: bool = False
    #: a signed pre-registration made before the data were seen
    registered: bool = False
    net_screen_passed: bool = False
    untouched_oos_confirmed: bool = False
    execution_capacity_validated: bool = False
    #: a registered test ran with adequate power and failed
    rejected: bool = False
    #: the test could not decide (power, data sufficiency, missing evidence)
    underpowered: bool = False


def classify(f: ClaimFacts) -> ClaimStatus:
    """The single status of a result.  See the module docstring for the rules."""
    if f.rejected:
        return ClaimStatus.REJECTED
    if f.underpowered:
        return ClaimStatus.INCONCLUSIVE
    enough = f.registered and f.sessions >= MIN_SESSIONS_ABOVE_EXPLORATORY
    if enough and f.net_screen_passed and f.untouched_oos_confirmed:
        if f.execution_capacity_validated:
            return ClaimStatus.STRATEGY_VALIDATED_EXECUTION_CAPACITY
        return ClaimStatus.CONFIRMED_UNTOUCHED_OOS
    if enough and f.net_screen_passed:
        return ClaimStatus.PASSES_REGISTERED_NET_SCREEN
    if f.positive_ic or f.net_screen_passed:
        return ClaimStatus.EXPLORATORY_SIGNAL
    if f.feature_verified:
        return ClaimStatus.FEATURE_CORRECT
    return ClaimStatus.INCONCLUSIVE


def from_screen_verdict(
    verdict: str,
    *,
    sessions: int,
    registered: bool,
    positive_ic: bool = False,
) -> ClaimStatus:
    """Map a screen verdict (REL-08 ``continue`` / ``passed_not_expanded`` / ``stop`` /
    ``underpowered``; REL-08c / REL-09 / REL-10 ``continue`` / ``reject`` /
    ``underpowered`` / ``iterate``) to a status.  A screen never confirms: its best
    status is ``passes_registered_net_screen``.  An unknown verdict is ``inconclusive``."""
    v = verdict.lower()
    if v in ("stop", "reject", "rejected"):
        return classify(ClaimFacts(rejected=True))
    if v in ("continue", "passed_not_expanded", "pass", "passed"):
        return classify(
            ClaimFacts(
                sessions=sessions,
                registered=registered,
                net_screen_passed=True,
                positive_ic=positive_ic,
            )
        )
    if v == "iterate":
        return classify(ClaimFacts(positive_ic=positive_ic))
    return ClaimStatus.INCONCLUSIVE


def may_promote(status: ClaimStatus) -> bool:
    """Promotion requires ``confirmed_untouched_oos`` or stronger; never IC, one
    session or a ranking (all of which classify at most ``exploratory_signal``)."""
    return status in (
        ClaimStatus.CONFIRMED_UNTOUCHED_OOS,
        ClaimStatus.STRATEGY_VALIDATED_EXECUTION_CAPACITY,
    )


def require_promotable(status: ClaimStatus, target_state: str = "PAPER") -> None:
    """Raise :class:`PromotionBlocked` unless ``status`` may enter ``target_state``."""
    need = LIFECYCLE_REQUIRED_CLAIM.get(target_state.upper(), ClaimStatus.CONFIRMED_UNTOUCHED_OOS)
    if not may_promote(status) or LADDER.index(status) < LADDER.index(need):
        raise PromotionBlocked(
            f"promotion to {target_state} needs {need.value}; the evidence is {status.value}"
        )
