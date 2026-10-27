"""Signal sources for signal-aware execution (v1.17 WS-B items B2 / B3, opt-in).

A *signal source* answers one question at any decision time ``t`` of a
parent order: what is the instrument's expected return right now, in
**signal units**?  Signal units are the z-score of the alpha's own score
distribution: ``z = (expected_return - mean) / std`` over the rows the
alpha scored on the dataset (per instrument), so a threshold of ``1`` means
"one standard deviation of this alpha's scores", whatever the alpha's
``beta`` happens to be.  The sign is the alpha's sign (positive = price
expected to rise).

Every reading is **causal**: :meth:`SignalSource.at` returns the last row
with ``ts <= t - latency_ns`` and never a later one; a reading older than
``max_age_ns`` is reported stale and the caller treats it as no signal.

Two sources:

- :class:`ArraySignalSource` — explicit ``(ts, z, expected_return)`` arrays
  per instrument (tests, planted signals, hand-built studies);
- :class:`iap.alpha.urgency_source.AlphaUrgencySource` — the REAL flagship
  alphas: scores a dataset's feature frames with a registered alpha
  (``configs/strategies/alpha_params.json`` by default, or an in-sample
  refit, labelled as such) and standardises the scores to signal units.
  It lived here until v2.1 Track 3; it builds alphas, and ``iap.alpha``
  sits above ``iap.execution``, so it moved to the alpha package.

Nothing here runs unless a ``ParentOrder`` names a signal opt-in
(``iap.execution.algos``); the replay with the defaults is byte-identical.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

#: Why a child was given the urgency it got (``ChildOrder.urgency_regime``).
URGENCY_REGIMES: tuple[str, ...] = (
    "agree",  #: the signal favours waiting: posted passively
    "adverse",  #: the price is expected to run away: crossed
    "neutral",  #: |z| inside the threshold: the parent's plain child style
    "no_signal",  #: the source has no reading for this instrument / time
    "stale",  #: the last reading is older than ``max_age_ns``
)


@dataclass(frozen=True, slots=True)
class SignalReading:
    """One causal reading of a signal source."""

    ts: int  #: exchange_ts of the row the reading comes from
    z: float  #: signal units (z-score of the alpha's own score distribution)
    expected_return: float  #: the alpha's expected return (dimensionless)
    horizon_s: float  #: the alpha's prediction horizon, seconds


@runtime_checkable
class SignalSource(Protocol):
    """The replay's view of a signal: a causal reading per instrument and time."""

    def at(self, instrument_id: int, t: int) -> SignalReading | None:
        """The last reading with ``ts <= t`` or ``None`` when there is none."""
        ...


def zscore(values: Sequence[float]) -> tuple[list[float], float, float]:
    """``(z, mean, std)`` of ``values`` ignoring NaN; a zero std gives all-zero z."""
    finite = [v for v in values if math.isfinite(v)]
    if not finite:
        return [0.0 for _ in values], 0.0, 0.0
    mean = sum(finite) / len(finite)
    var = sum((v - mean) ** 2 for v in finite) / len(finite)
    std = math.sqrt(var)
    if std <= 0.0:
        return [0.0 for _ in values], mean, 0.0
    return [((v - mean) / std if math.isfinite(v) else 0.0) for v in values], mean, std


class ArraySignalSource:
    """Explicit per-instrument readings, ``ts`` strictly increasing per instrument."""

    def __init__(
        self,
        readings: Mapping[int, Sequence[tuple[int, float, float]]],
        horizon_s: float = 1.0,
        label: str = "array",
    ) -> None:
        self._ts: dict[int, list[int]] = {}
        self._rows: dict[int, list[SignalReading]] = {}
        self.horizon_s = float(horizon_s)
        self.label = label
        for iid, rows in readings.items():
            ts = [int(r[0]) for r in rows]
            if any(b <= a for a, b in zip(ts, ts[1:], strict=False)):
                raise ValueError(f"signal readings of instrument {iid} must be strictly increasing")
            self._ts[iid] = ts
            self._rows[iid] = [
                SignalReading(int(t), float(z), float(er), self.horizon_s) for t, z, er in rows
            ]

    def at(self, instrument_id: int, t: int) -> SignalReading | None:
        ts = self._ts.get(instrument_id)
        if not ts:
            return None
        i = bisect.bisect_right(ts, t) - 1
        return None if i < 0 else self._rows[instrument_id][i]

    def describe(self) -> dict:
        return {"source": self.label, "horizon_s": self.horizon_s, "instruments": sorted(self._ts)}


#: seconds per pinned label horizon (``iap.alpha.base.VALID_HORIZONS``)
_HORIZON_S: dict[str, float] = {
    "10ms": 0.01,
    "50ms": 0.05,
    "100ms": 0.1,
    "500ms": 0.5,
    "1s": 1.0,
    "5s": 5.0,
    "10s": 10.0,
    "30s": 30.0,
    "1m": 60.0,
    "5m": 300.0,
    "15m": 900.0,
}


def horizon_seconds(horizon: str) -> float:
    if horizon not in _HORIZON_S:
        raise ValueError(f"unknown horizon {horizon!r}")
    return _HORIZON_S[horizon]
