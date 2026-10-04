"""Child-order execution policies and the passive posting rules.

Python port of the policy section of ``cpp/include/iap/execution/algos.hpp``
(the C++ port is the reference; ``tests/golden/expected_replay_fills_passive.json``
pins all three languages).

A parent order names one of three policies (``ParentOrder.policy``):

- ``NATIVE`` (``"native"``, the default and the only behaviour before
  v1.5.0): TWAP / VWAP children are LIMIT orders joining the same-side best
  and waiting there until they fill or expire with the window; POV / IS
  children are MARKET orders.
- ``AGGRESSIVE`` (``"aggressive"``): every child is a MARKET order, whatever
  the algo.
- ``PASSIVE`` (``"passive"``): every schedule step is worked by the pinned
  state machine POST -> REST -> REPRICE / CROSS below.

PASSIVE state machine (per child; the scheduler evaluates it after every
market event, against the post-event books):

- **POST.** The step's quantity (split at ``max_child_qty``) is posted as a
  LIMIT at ``post_price``: the same-side best of the routed venue, improved
  by ONE tick when the venue's spread is at least
  ``improve_min_spread_ticks`` ticks (0 disables the improvement), and then
  clamped so that it never reaches the opposite touch (a buy posts at most
  at ``best_ask - 1``, a sell at least at ``best_bid + 1``). With no
  same-side quote, with ``patience_ns == 0``, or at or after ``end_ts -
  end_margin_ns``, the child is sent as a MARKET order instead (CROSS).
  The rule is evaluated on the book at decision time: the simulator has no
  post-only order type, so a limit the market moved through while the order
  was in flight executes as a taker up to its limit (simulator rule 3).
- **REST.** The child rests until ``deadline = min(decision_ts +
  patience_ns, end_ts - end_margin_ns)`` or until the parent's schedule is
  BEHIND, whichever comes first.
  ``patience_ns = floor(max_rest_ns * (1 - urgency) * k)`` with ``urgency``
  clamped to [0, 1] and ``k = exp(-risk_aversion)`` for IS, 1 otherwise.
  ``backlog(t) = scheduled(t) - filled(t) - q_cur``: ``scheduled`` is the
  sum of the slice quantities that have come due (TWAP / VWAP / IS, with
  ``q_cur`` the most recent of them — a slice is by construction a whole
  slice behind the instant it is due) or ``min(floor(participation *
  V(t)), qty)`` (POV, ``q_cur = 0``). The schedule is BEHIND when
  ``backlog(t) > floor(max_behind_fraction * qty)``.
- **REPRICE.** At the deadline, while ``reprices < max_reprices`` and the
  schedule is not behind: if ``post_price`` still equals the child's limit
  the child keeps its queue position and gets a fresh deadline (counted as
  ``rest_extensions``); otherwise it is cancelled and, once the cancel has
  taken effect, its unfilled remainder is posted again at the new
  ``post_price`` (counted as ``reprices``). Both use up one reprice.
- **CROSS.** At the deadline with no reprice left, or as soon as the
  schedule is behind, the child is cancelled and, once the cancel has taken
  effect, its unfilled remainder is sent as a MARKET order
  (``crosses_timeout`` / ``crosses_behind``).

A cancel travels the simulator's latency path (rule 7), so a child can
still fill while its cancel is in flight; only the quantity that was
actually cancelled is re-sent — quantity is never duplicated. Nothing is
re-sent at or after ``end_ts`` (the remainder is opportunity cost).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import IntEnum


class ExecPolicy(IntEnum):
    """How a parent order's children are sent (module docstring)."""

    NATIVE = 0
    PASSIVE = 1
    AGGRESSIVE = 2


#: Config names of the policies (``configs/execution/passive_policy.json``).
POLICY_NAMES: dict[str, ExecPolicy] = {
    "native": ExecPolicy.NATIVE,
    "passive": ExecPolicy.PASSIVE,
    "aggressive": ExecPolicy.AGGRESSIVE,
}


@dataclass(frozen=True, slots=True)
class PassiveParams:
    """Parameters of the PASSIVE policy (defaults = the pinned ones)."""

    max_rest_ns: int = 30_000_000_000  #: rest time at urgency 0
    max_reprices: int = 1  #: reprices / rest extensions before crossing
    max_behind_fraction: float = 0.1  #: of the parent qty
    improve_min_spread_ticks: int = 3  #: 0 = never post inside the spread
    end_margin_ns: int = 1_000_000_000  #: no resting inside this margin of end_ts

    def __post_init__(self) -> None:
        if self.max_rest_ns < 0 or self.end_margin_ns < 0:
            raise ValueError("max_rest_ns and end_margin_ns must be >= 0")
        if self.max_reprices < 0:
            raise ValueError("max_reprices must be >= 0")
        if not 0.0 <= self.max_behind_fraction <= 1.0:
            raise ValueError("max_behind_fraction must be in [0, 1]")
        if self.improve_min_spread_ticks < 0:
            raise ValueError("improve_min_spread_ticks must be >= 0")


def patience_ns(params: PassiveParams, urgency: float, is_algo: bool, risk_aversion: float) -> int:
    """Rest time of one posted child (module docstring, REST)."""
    u = min(max(urgency, 0.0), 1.0)
    x = float(params.max_rest_ns) * (1.0 - u)
    if is_algo:
        x *= math.exp(-risk_aversion)
    return int(math.floor(x))


def post_price(
    side: int,
    best_bid: tuple[int, int] | None,
    best_ask: tuple[int, int] | None,
    improve_min_spread_ticks: int,
) -> int | None:
    """Limit price of a posted child, or None when it cannot be posted.

    ``best_bid`` / ``best_ask`` are the routed venue's ``(price_ticks, qty)``
    touches at decision time. Never returns a price at or through the
    opposite touch.
    """
    near = best_bid if side == 0 else best_ask
    if near is None:
        return None
    opp = best_ask if side == 0 else best_bid
    price = near[0]
    if opp is not None:
        spread = opp[0] - near[0] if side == 0 else near[0] - opp[0]
        if improve_min_spread_ticks > 0 and spread >= improve_min_spread_ticks:
            price += 1 if side == 0 else -1
        if side == 0 and price >= opp[0]:
            price = opp[0] - 1
        elif side == 1 and price <= opp[0]:
            price = opp[0] + 1
    return price if price > 0 else None


def max_behind_qty(params: PassiveParams, parent_qty: int) -> int:
    """``floor(max_behind_fraction * qty)`` — the BEHIND tolerance."""
    return int(math.floor(params.max_behind_fraction * float(parent_qty)))


@dataclass(slots=True)
class PassiveStats:
    """Per-parent transition counters of the PASSIVE state machine."""

    posts: int = 0  #: LIMIT children posted (first posts and reprices)
    reprices: int = 0  #: cancel + re-post at a new price
    rest_extensions: int = 0  #: deadline renewed at an unchanged price
    crosses_timeout: int = 0  #: MARKET for a remainder after the last reprice
    crosses_behind: int = 0  #: MARKET for a remainder, schedule behind
    crosses_immediate: int = 0  #: step sent as MARKET without posting

    def to_dict(self) -> dict[str, int]:
        """Row in the key order of ``expected_replay_fills_passive.json``."""
        return {
            "posts": self.posts,
            "reprices": self.reprices,
            "rest_extensions": self.rest_extensions,
            "crosses_timeout": self.crosses_timeout,
            "crosses_behind": self.crosses_behind,
            "crosses_immediate": self.crosses_immediate,
        }
