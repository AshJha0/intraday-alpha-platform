"""Alpha-aware urgency (plan item X2, v1.10, Python only, opt-in).

``signal`` is the instrument's expected return (positive = price expected
to rise). ``alignment = -signal`` for a buy and ``+signal`` for a sell, so
``alignment > 0`` means waiting is expected to get a better price: the
signal *agrees* with working the order patiently and the parent posts
passively. ``alignment < 0`` means the price is expected to run away from
the order: the signal is *adverse*, so the parent crosses and front-loads.

Nothing here runs unless a caller applies it; ``ParentOrder`` defaults are
unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from iap.execution.algos import ParentOrder
from iap.execution.passive import ExecPolicy


@dataclass(frozen=True, slots=True)
class AlphaUrgencyParams:
    """Thresholds and actions of the alpha-aware urgency rule."""

    threshold: float = 0.0  #: |alignment| at or below this -> unchanged
    passive_urgency: float = 0.1  #: PASSIVE patience when the signal agrees
    adverse_urgency: float = 1.0  #: urgency when the signal is adverse
    adverse_policy: ExecPolicy = ExecPolicy.AGGRESSIVE  #: cross when adverse
    accelerate: float = 2.0  #: risk_aversion multiplier when adverse (IS)

    def __post_init__(self) -> None:
        if self.threshold < 0 or self.accelerate < 1.0:
            raise ValueError("need threshold >= 0 and accelerate >= 1")
        for u in (self.passive_urgency, self.adverse_urgency):
            if not 0.0 <= u <= 1.0:
                raise ValueError("urgencies must be in [0, 1]")


def alignment(side: int, signal: float) -> float:
    """``> 0`` when waiting is expected to pay for this side (0 = buy)."""
    return -signal if side == 0 else signal


def urgency_regime(side: int, signal: float, params: AlphaUrgencyParams) -> str:
    """``"agree"``, ``"adverse"`` or ``"neutral"``."""
    a = alignment(side, signal)
    if a > params.threshold:
        return "agree"
    if a < -params.threshold:
        return "adverse"
    return "neutral"


def apply_alpha_urgency(
    parent: ParentOrder, signal: float, params: AlphaUrgencyParams | None = None
) -> ParentOrder:
    """``parent`` with policy, urgency and risk aversion set from ``signal``.

    agree: ``PASSIVE`` at ``passive_urgency``. adverse: ``adverse_policy`` at
    ``adverse_urgency`` with ``risk_aversion * accelerate``. neutral:
    ``parent`` unchanged.
    """
    p = params or AlphaUrgencyParams()
    regime = urgency_regime(parent.side, signal, p)
    if regime == "agree":
        return replace(parent, policy=ExecPolicy.PASSIVE, urgency=p.passive_urgency)
    if regime == "adverse":
        return replace(
            parent,
            policy=p.adverse_policy,
            urgency=p.adverse_urgency,
            risk_aversion=parent.risk_aversion * p.accelerate,
        )
    return parent
