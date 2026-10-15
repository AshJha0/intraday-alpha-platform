"""Parent-order execution algorithms: TWAP / VWAP / POV / IS (spec section 17).

Python reference port of ``cpp/include/iap/execution/algos.hpp`` +
``algos.cpp``. A parent order is worked over an event-time window
``[start_ts, end_ts)`` by scheduling child orders; every schedule is a pure
function of the parent and the replayed stream (deterministic, no wall
clock).

Pinned schedules:

- **Slice decision times** (TWAP/VWAP/IS): slice ``i`` of ``N`` is due at
  ``due_i = start_ts + i * (end_ts - start_ts) // N`` (integer division)
  and is issued while processing the first event with ``exchange_ts >=
  due_i``. Slice weights map to integer child quantities by
  largest-remainder apportionment (floor each target, hand the remaining
  shares to the largest fractional parts, ties to the earlier slice) —
  quantities sum exactly to the parent qty.
- **TWAP**: equal weights (``w_i = 1``).
- **VWAP**: pinned U-shaped session volume curve (``time_of_day_default``):
  ``w_i = 1 + x_i^2``, ``x_i = (2i - (N-1)) / (N-1)`` (``N >= 2``; ``N = 1``
  takes all).
- **IS**: front-loaded exponential decay ``w_i = exp(-risk_aversion * i /
  max(1, N-1))``.
- **POV**: no precomputed slices. Tracks cumulative TRADE volume ``V(t)``
  of the parent's instrument inside the window; after each TRADE ``target =
  floor(participation * V(t))``; whenever the target exceeds the quantity
  COMMITTED (filled + still open/in-flight) a child covers the deficit
  (capped at ``max_child_qty`` and the parent's remainder).

Child sizing (pinned): a slice larger than ``max_child_qty`` is split into
``ceil(slice / max_child_qty)`` children, all decided at the same event.
Every child carries ``expire_ts = end_ts`` (simulator rule 7): no child
outlives its parent's window. TWAP/VWAP children are passive LIMIT orders
joining the same-side best at decision time (MARKET when that side is
empty); POV and IS children are MARKET orders. Venue: ``parent.venue_id``
or SOR-routed when ``venue_id == 0``. The scheduling itself lives in
``iap.execution.replay``.

These child styles are the ``NATIVE`` execution policy, the default.
``ParentOrder.policy`` selects ``AGGRESSIVE`` (every child MARKET) or
``PASSIVE`` (the POST -> REST -> REPRICE / CROSS state machine of
``iap.execution.passive``) per parent; the schedules above do not change.

v1.10 opt-ins (Python only, not part of the parity contract; defaults keep
the pinned schedules): ``is_model=ISModel.ALMGREN_CHRISS`` + ``ac_params``
replaces the IS decay with the closed-form Almgren-Chriss trajectory
(``iap.execution.optimal``); ``volume_curve`` replaces the VWAP ``1 + x^2``
curve with a forecast curve (``iap.execution.volume_curve``).
v1.16 opt-in: ``pov_exact`` (exact participation + catch-up, see the field).
v1.17 WS-B opt-ins (B2 / B3): ``urgency_policy="alpha"`` re-evaluates the
child style from a real alpha signal at every decision
(``iap.execution.urgency.AlphaUrgencyPolicy``); ``pov_signal_pacing``,
``slice_signal_tilt`` and ``ac_signal_drift`` modulate the schedule from the
same signal (``iap.execution.pacing``). All read ``signal_source``
(``iap.execution.signal``) and are off by default.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING

from iap.execution.passive import ExecPolicy, PassiveParams

if TYPE_CHECKING:  # pragma: no cover
    from iap.execution.fading import FadeParams, QueueCancelParams
    from iap.execution.optimal import ACParams
    from iap.execution.pacing import PacingParams
    from iap.execution.signal import SignalSource
    from iap.execution.urgency import AlphaUrgencyPolicy

#: ``ParentOrder.urgency_policy`` values.
URGENCY_POLICIES: tuple[str, ...] = ("fixed", "alpha")


class AlgoType(IntEnum):
    """Parent order algorithm."""

    TWAP = 0
    VWAP = 1
    POV = 2
    IS = 3


class ISModel(IntEnum):
    """IS schedule model. EXP_DECAY is pinned across languages; the
    Almgren-Chriss trajectory (``iap.execution.optimal``) is Python-only and
    opt-in (v1.10)."""

    EXP_DECAY = 0
    ALMGREN_CHRISS = 1


@dataclass(frozen=True, slots=True)
class ParentOrder:
    """A parent order and its algo parameters."""

    parent_id: int = 0
    instrument_id: int = 0
    venue_id: int = 0  #: 0 => SOR-routed
    side: int = 0  #: 0 = buy, 1 = sell
    qty: int = 0
    algo: AlgoType = AlgoType.TWAP
    start_ts: int = 0
    end_ts: int = 0
    slices: int = 8  #: TWAP / VWAP / IS
    participation: float = 0.05  #: POV
    risk_aversion: float = 1.0  #: IS
    max_child_qty: int = 1000
    policy: ExecPolicy = ExecPolicy.NATIVE  #: child style (iap.execution.passive)
    urgency: float = 0.5  #: PASSIVE patience, in [0, 1]; 1 = cross at once
    passive: PassiveParams = PassiveParams()  #: PASSIVE parameters
    #: IS schedule model (v1.10, opt-in): EXP_DECAY is the pinned default.
    is_model: ISModel = ISModel.EXP_DECAY
    #: Almgren-Chriss inputs (``iap.execution.optimal.ACParams``) for
    #: ``is_model=ALMGREN_CHRISS``; its ``risk_aversion`` is the lambda used.
    ac_params: ACParams | None = None
    #: Forecast VWAP volume curve (``iap.execution.volume_curve``, v1.10,
    #: opt-in); ``None`` keeps the pinned ``1 + x^2`` curve.
    volume_curve: tuple[float, ...] | None = None
    #: The part of the curve's session this parent covers, as fractions
    #: (``iap.execution.volume_curve.curve_window``; v1.17, review X-1):
    #: the VWAP slices then follow the curve over the parent window instead
    #: of the whole session.  ``None`` (the v1.10 rule) is the whole span.
    curve_window: tuple[float, float] | None = None
    #: POV exact participation + catch-up (v1.16 P0-5, opt-in; Python only).
    #: Off: the pinned ``floor(participation * V)`` with one child of at most
    #: ``max_child_qty`` per TRADE. On: the target counts our own fills in
    #: the total (``floor(p / (1 - p) * V)``, ``V`` = market TRADE volume,
    #: which never contains our simulated fills), so we trade ``p`` of the
    #: total, and the whole deficit is covered at once in children of at
    #: most ``max_child_qty`` (catch-up after expiries or missed fills).
    pov_exact: bool = False
    # ---- v1.17 WS-B signal-aware opt-ins (Python only; defaults = the plain algo) ----
    #: The signal every opt-in below reads (``iap.execution.signal``); required
    #: by any of them, ignored otherwise. Not part of the parent's identity.
    signal_source: SignalSource | None = None
    #: B2: ``"fixed"`` (the pinned behaviour: ``policy`` / ``urgency`` as set)
    #: or ``"alpha"`` (re-evaluated per child from ``signal_source`` with
    #: ``alpha_urgency``; the base ``policy`` is the neutral child style).
    urgency_policy: str = "fixed"
    alpha_urgency: AlphaUrgencyPolicy | None = None
    #: B3: per-event POV participation scaled by the signal.
    pov_signal_pacing: bool = False
    #: B3: TWAP / VWAP / IS slice quantities tilted by the signal at each slice.
    slice_signal_tilt: bool = False
    #: B3: Almgren-Chriss IS re-solved per slice with the signal as the drift.
    ac_signal_drift: bool = False
    pacing: PacingParams | None = None
    #: B4 quote fading / B5 queue-aware cancels (``iap.execution.fading``,
    #: Python only, opt-in): rules on the children this parent posts through
    #: the PASSIVE state machine (``policy=PASSIVE`` or the B2 *agree*
    #: posts). ``None`` = off (the pinned behaviour).
    fade: FadeParams | None = None
    queue_cancel: QueueCancelParams | None = None

    @property
    def pulls_resting(self) -> bool:
        """True when a B4 / B5 pull rule is on."""
        return self.fade is not None or self.queue_cancel is not None

    @property
    def signal_aware(self) -> bool:
        """True when any v1.17 signal opt-in is on."""
        return (
            self.urgency_policy != "fixed"
            or self.pov_signal_pacing
            or self.slice_signal_tilt
            or self.ac_signal_drift
        )


def slice_weights(parent: ParentOrder) -> list[float]:
    """Slice weights for TWAP/VWAP/IS (raises ValueError for POV or slices <= 0)."""
    if parent.algo == AlgoType.POV:
        raise ValueError("POV has no precomputed slice weights")
    n = parent.slices
    if n <= 0:
        raise ValueError("slices must be > 0")
    if n == 1:
        return [1.0]
    if parent.algo == AlgoType.VWAP and parent.volume_curve is not None:
        from iap.execution.volume_curve import curve_slice_weights

        lo, hi = parent.curve_window or (0.0, 1.0)
        return curve_slice_weights(parent.volume_curve, n, lo, hi)
    if parent.algo == AlgoType.IS and parent.is_model == ISModel.ALMGREN_CHRISS:
        from iap.execution.optimal import ac_trajectory

        if parent.ac_params is None:
            raise ValueError("is_model=ALMGREN_CHRISS needs ac_params")
        return [max(0.0, x) for x in ac_trajectory(1.0, parent.ac_params, n)]
    w: list[float] = []
    for i in range(n):
        if parent.algo == AlgoType.TWAP:
            w.append(1.0)
        elif parent.algo == AlgoType.VWAP:
            x = (2.0 * i - (n - 1)) / (n - 1)
            w.append(1.0 + x * x)
        else:  # IS
            w.append(math.exp(-parent.risk_aversion * i / float(n - 1)))
    return w


def slice_quantities(parent: ParentOrder) -> list[int]:
    """Integer child quantities per slice (largest remainder; sums to ``qty``)."""
    if parent.qty <= 0:
        raise ValueError("parent qty must be > 0")
    w = slice_weights(parent)
    wsum = 0.0
    for x in w:
        wsum += x
    n = len(w)
    q = [0] * n
    frac = []
    assigned = 0
    for i in range(n):
        target = float(parent.qty) * w[i] / wsum
        floor = math.floor(target)
        q[i] = int(floor)
        assigned += q[i]
        # Largest remainder; ties resolved toward the earlier slice.
        frac.append((-(target - floor), i))
    frac.sort()
    left = parent.qty - assigned
    for k in range(n):
        if left <= 0:
            break
        q[frac[k][1]] += 1
        left -= 1
    return q


def slice_times(parent: ParentOrder) -> list[int]:
    """Due time of each slice: ``start_ts + i * span // N``."""
    if parent.end_ts <= parent.start_ts:
        raise ValueError("parent window must have end_ts > start_ts")
    n = parent.slices
    if n <= 0:
        raise ValueError("slices must be > 0")
    span = parent.end_ts - parent.start_ts
    return [parent.start_ts + i * span // n for i in range(n)]
