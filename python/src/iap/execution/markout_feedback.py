"""Markout feedback into the passive posting policy (v1.12 X4).

Before v1.12 the realised TCA (``iap.tca.markout``) was a report: a maker
policy kept posting at the same size and price however badly its fills were
being run over. :class:`MarkoutFeedback` closes that loop, deterministically:

- every passive (MAKER) fill is registered with :meth:`on_fill` at its fill
  time and price; its markout ``1e4 * s * (m_h - p) / p`` (the
  ``iap.tca.markout`` sign: positive = good for us) becomes KNOWN only once
  the clock passes ``fill_ts + horizon_ns`` — :meth:`advance` resolves it
  with the mid in force at that time (the caller passes the pre-event mid
  of the first event at or after it, i.e. the mid at ``fill_ts + h``), so
  the policy never peeks at a markout it could not yet have measured;
- resolved markouts update an EWMA (``halflife`` in fills, first value
  seeds it);
- the regime is TOXIC once at least ``min_fills`` markouts are known and
  ``ewma < -threshold_bps``. :meth:`action` then returns the configured
  response: ``"stand_down"`` (post nothing for ``cooldown_ns``; the EWMA is
  then reset and posting resumes), ``"widen"`` (post ``widen_ticks`` behind
  the price the policy wanted) or ``"reduce"`` (post ``size_mult`` of the
  size, at least one lot).

No RNG, no wall clock: the same fills and mids give the same actions.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

FEEDBACK_MODES = ("stand_down", "widen", "reduce")


@dataclass(frozen=True)
class FeedbackConfig:
    """Parameters of :class:`MarkoutFeedback` (module docstring)."""

    mode: str = "stand_down"
    horizon_ns: int = 1_000_000_000
    halflife: float = 5.0
    threshold_bps: float = 0.0
    min_fills: int = 3
    cooldown_ns: int = 30_000_000_000
    widen_ticks: int = 1
    size_mult: float = 0.5

    def __post_init__(self) -> None:
        if self.mode not in FEEDBACK_MODES:
            raise ValueError(f"mode must be one of {FEEDBACK_MODES}")
        if self.horizon_ns <= 0 or self.halflife <= 0 or self.cooldown_ns < 0:
            raise ValueError("horizon_ns, halflife must be positive and cooldown_ns >= 0")
        if self.min_fills < 1 or self.widen_ticks < 0 or not 0.0 < self.size_mult <= 1.0:
            raise ValueError("min_fills >= 1, widen_ticks >= 0, size_mult in (0, 1]")


@dataclass(frozen=True)
class FeedbackAction:
    """What the policy should do with its next post."""

    stand_down: bool = False
    widen_ticks: int = 0
    size_mult: float = 1.0

    @property
    def neutral(self) -> bool:
        return not self.stand_down and self.widen_ticks == 0 and self.size_mult == 1.0

    def size(self, qty: int) -> int:
        return 0 if self.stand_down else max(1, int(math.floor(qty * self.size_mult)))


NEUTRAL = FeedbackAction()


class MarkoutFeedback:
    """EWMA of realised markouts driving the posting policy (module docstring)."""

    def __init__(self, config: FeedbackConfig | None = None) -> None:
        self.config = config or FeedbackConfig()
        self._alpha = 1.0 - 0.5 ** (1.0 / self.config.halflife)
        self._pending: deque[tuple[int, int, float]] = deque()  # (due_ts, sign, px)
        self.ewma: float | None = None
        self.n_known = 0
        self.stand_until: int | None = None
        self.markouts: list[float] = []
        self.counters = {"feedback_triggers": 0, "feedback_resets": 0}

    def on_fill(self, ts: int, side: int, price: float) -> None:
        """Register a passive fill (``side`` of OUR order, 0 buy / 1 sell)."""
        self._pending.append((ts + self.config.horizon_ns, 1 if side == 0 else -1, float(price)))

    def advance(self, t: int, mid: float | None) -> None:
        """Resolve every markout due at or before ``t`` with ``mid``."""
        if mid is None:
            return
        while self._pending and self._pending[0][0] <= t:
            _, s, px = self._pending.popleft()
            m = 1e4 * s * (mid - px) / px
            self.markouts.append(m)
            self.ewma = m if self.ewma is None else self.ewma + self._alpha * (m - self.ewma)
            self.n_known += 1

    @property
    def toxic(self) -> bool:
        c = self.config
        return (
            self.ewma is not None and self.n_known >= c.min_fills and self.ewma < -c.threshold_bps
        )

    def action(self, t: int) -> FeedbackAction:
        """The response at decision time ``t`` (call :meth:`advance` first)."""
        c = self.config
        if self.stand_until is not None:
            if t < self.stand_until:
                return FeedbackAction(stand_down=True)
            self.stand_until = None
            self.ewma = None
            self.n_known = 0
            self.counters["feedback_resets"] += 1
        if not self.toxic:
            return NEUTRAL
        if c.mode == "stand_down":
            self.stand_until = t + c.cooldown_ns
            self.counters["feedback_triggers"] += 1
            return FeedbackAction(stand_down=True)
        self.counters["feedback_triggers"] += 1
        if c.mode == "widen":
            return FeedbackAction(widen_ticks=c.widen_ticks)
        return FeedbackAction(size_mult=c.size_mult)
