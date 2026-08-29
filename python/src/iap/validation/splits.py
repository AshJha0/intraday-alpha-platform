"""Walk-forward splitting with purging and embargo (spec §13).

Expanding walk-forward over event time: the joint [t0, t1] span of all
frames is divided into ``n_folds + 1`` equal segments; fold k (k = 1..n)
tests on segment k+1 and trains on everything BEFORE the test segment,
minus:

- **purging**: a train row whose label window ``[t, t + horizon]`` reaches
  into the test segment is dropped (its label is computed from prices the
  test set also sees — overlapping-label contamination);
- **embargo**: an additional ``embargo_ns`` gap before the test start is
  excluded from training, guarding serial correlation that outlives the
  label horizon.

So train rows satisfy ``t + horizon + embargo < test_start`` and test rows
satisfy ``test_start <= t < test_end``.  Splits are pure event-time
interval logic — deterministic, no RNG, never a random shuffle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterator, List, Mapping, Tuple

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Fold:
    """One walk-forward fold (event-time boundaries, ns)."""

    index: int
    train_end: int   # exclusive: train rows have t + purge + embargo < test_start
    test_start: int  # inclusive
    test_end: int    # exclusive

    def train_mask(self, ts: np.ndarray, horizon_ns: int, embargo_ns: int) -> np.ndarray:
        ts = np.asarray(ts, dtype=np.int64)
        return ts + horizon_ns + embargo_ns < self.test_start

    def test_mask(self, ts: np.ndarray) -> np.ndarray:
        ts = np.asarray(ts, dtype=np.int64)
        return (ts >= self.test_start) & (ts < self.test_end)


class WalkForwardSplitter:
    """Expanding, purged, embargoed walk-forward splitter."""

    def __init__(self, n_folds: int = 4, embargo_ns: int = 60_000_000_000) -> None:
        if n_folds < 1:
            raise ValueError("n_folds must be >= 1")
        if embargo_ns < 0:
            raise ValueError("embargo_ns must be >= 0")
        self.n_folds = n_folds
        self.embargo_ns = embargo_ns

    def folds(self, t0: int, t1: int) -> List[Fold]:
        """Fold boundaries over the closed event-time span [t0, t1]."""
        if t1 <= t0:
            raise ValueError("empty time span")
        seg = (t1 - t0) // (self.n_folds + 1)
        if seg <= 0:
            raise ValueError("span too short for the requested fold count")
        out: List[Fold] = []
        for k in range(1, self.n_folds + 1):
            test_start = t0 + seg * k
            test_end = t0 + seg * (k + 1) if k < self.n_folds else t1 + 1
            out.append(
                Fold(
                    index=k,
                    train_end=test_start,
                    test_start=test_start,
                    test_end=test_end,
                )
            )
        return out

    def split_frames(
        self,
        frames: Mapping[int, pd.DataFrame],
        horizon_ns: int,
    ) -> Iterator[Tuple[Fold, Dict[int, pd.DataFrame], Dict[int, pd.DataFrame]]]:
        """Yield (fold, train_frames, test_frames) with purge+embargo applied."""
        t0 = min(int(df["exchange_ts"].iloc[0]) for df in frames.values() if len(df))
        t1 = max(int(df["exchange_ts"].iloc[-1]) for df in frames.values() if len(df))
        for fold in self.folds(t0, t1):
            train: Dict[int, pd.DataFrame] = {}
            test: Dict[int, pd.DataFrame] = {}
            for iid, df in frames.items():
                ts = df["exchange_ts"].to_numpy()
                train[iid] = df[fold.train_mask(ts, horizon_ns, self.embargo_ns)].reset_index(
                    drop=True
                )
                test[iid] = df[fold.test_mask(ts)].reset_index(drop=True)
            yield fold, train, test
