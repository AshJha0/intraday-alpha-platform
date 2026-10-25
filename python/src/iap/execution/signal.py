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
- :class:`AlphaUrgencySource` — the REAL flagship alphas: scores a dataset's
  feature frames with a registered alpha (``configs/strategies/
  alpha_params.json`` by default, or an in-sample refit, labelled as such)
  and standardises the scores to signal units.

Nothing here runs unless a ``ParentOrder`` names a signal opt-in
(``iap.execution.algos``); the replay with the defaults is byte-identical.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from iap import io as iap_io

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


class AlphaUrgencySource(ArraySignalSource):
    """A flagship alpha's scores on a dataset's feature frames, in signal units.

    ``frames`` maps instrument_id to the feature frame the alpha reads
    (``exchange_ts`` + feature columns, as written by ``iap.features``); the
    alpha scores every row, the scores are standardised per instrument to
    ``z`` (mean / std over the finite scored rows of THAT instrument on THIS
    dataset — an in-sample scale, recorded in :meth:`describe`), and each
    row becomes one reading at its ``exchange_ts``.  Instruments outside the
    alpha's universe (EQ10 excludes the ETF) get no readings: every decision
    on them is ``no_signal``.

    ``refit=True`` fits the alpha on the same frames first (in-sample; the
    label columns must be present) instead of using the registered
    parameters — a research variant, labelled ``params="refit:in-sample"``
    and never the gate's arm.
    """

    def __init__(
        self,
        alpha,
        frames: Mapping[int, object],
        *,
        params_label: str = "registered",
    ) -> None:
        import numpy as np

        scored = alpha.score(frames)
        readings: dict[int, list[tuple[int, float, float]]] = {}
        self.scale: dict[int, dict[str, float]] = {}
        for iid, df in scored.items():
            ts = df["exchange_ts"].to_numpy(dtype="int64")
            er = df["expected_return"].to_numpy(dtype=float)
            order = np.argsort(ts, kind="stable")
            ts, er = ts[order], er[order]
            keep = np.ones(len(ts), dtype=bool)
            keep[1:] = ts[1:] > ts[:-1]  # one reading per timestamp: the last row wins
            # (the last of equal timestamps is the latest state): keep the last
            last = np.flatnonzero(keep)
            ends = np.append(last[1:], len(ts)) - 1
            ts, er = ts[ends], er[ends]
            z, mean, std = zscore(er.tolist())
            self.scale[int(iid)] = {"mean": mean, "std": std, "rows": int(len(ts))}
            readings[int(iid)] = list(zip(ts.tolist(), z, er.tolist(), strict=True))
        super().__init__(
            readings,
            horizon_s=horizon_seconds(alpha.horizon),
            label=f"{alpha.alpha_id}:{params_label}",
        )
        self.alpha_id = alpha.alpha_id
        self.horizon = alpha.horizon
        self.params_label = params_label
        self.beta = float(getattr(alpha, "beta", float("nan")))
        self.features_dir: str | None = None

    @classmethod
    def from_dataset(
        cls,
        alpha_id: str,
        features_dir: str | Path,
        *,
        params_path: str | Path | None = None,
        instruments_json: str | Path | None = None,
        refit: bool = False,
        instrument_ids: Sequence[int] | None = None,
    ) -> AlphaUrgencySource:
        """Score ``alpha_id`` over ``features_dir/features_*.parquet``.

        ``params_path`` defaults to the repository's registered
        ``configs/strategies/alpha_params.json`` (its ``feature_version`` must
        match the running registry, as every loader demands).
        ``instruments_json`` re-derives the alpha universe from the dataset's
        instruments (ETF detection); ``refit`` fits on the frames instead.
        """
        import pandas as pd

        from iap.alpha import build, configure_universe, load_params_file

        features_dir = Path(features_dir)
        files = sorted(features_dir.glob("features_*.parquet"))
        if not files:
            raise FileNotFoundError(f"no features_*.parquet under {features_dir}")
        alpha = build(alpha_id)
        if instruments_json is not None:
            configure_universe(instruments_json)
        cols = ["instrument_id", "exchange_ts", *alpha.features]
        if refit:
            cols += [f"label_mid_{alpha.horizon}", f"label_valid_{alpha.horizon}"]
        frames: dict[int, pd.DataFrame] = {}
        for f in files:
            df = iap_io.read_parquet(f, columns=cols, purpose="execution.signal")
            for iid, part in df.groupby("instrument_id", sort=True):
                iid = int(iid)
                if instrument_ids is not None and iid not in instrument_ids:
                    continue
                part = part.drop(columns=["instrument_id"]).reset_index(drop=True)
                frames[iid] = (
                    part if iid not in frames else pd.concat([frames[iid], part], ignore_index=True)
                )
        for iid in frames:
            frames[iid] = (
                frames[iid].sort_values("exchange_ts", kind="stable").reset_index(drop=True)
            )
        if refit:
            alpha.fit(frames)
            label = "refit:in-sample"
        else:
            if params_path is None:
                params_path = Path(__file__).resolve().parents[4] / (
                    "configs/strategies/alpha_params.json"
                )
            models = load_params_file(params_path)
            if alpha_id not in models:
                raise ValueError(f"{params_path}: no parameters for {alpha_id}")
            alpha = models[alpha_id]
            label = f"registered:{Path(params_path).name}"
        src = cls(alpha, frames, params_label=label)
        src.features_dir = str(features_dir)
        return src

    def describe(self) -> dict:
        d = super().describe()
        d.update(
            {
                "alpha_id": self.alpha_id,
                "horizon": self.horizon,
                "params": self.params_label,
                "beta": self.beta,
                "units": "z-score of the alpha's expected_return per instrument on this dataset",
                "scale": {str(k): v for k, v in sorted(self.scale.items())},
                "features_dir": self.features_dir,
            }
        )
        return d
