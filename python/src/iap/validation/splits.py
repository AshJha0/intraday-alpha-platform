"""Walk-forward splitting with purging and embargo (spec §13).

Expanding walk-forward: the sample is divided into ``n_folds + 1`` segments;
fold k (k = 1..n) tests on segment k+1 and trains on everything BEFORE the
test segment, minus:

- **purging**: a train row whose label window ``[t, t + horizon]`` reaches
  into the test segment is dropped (its label is computed from prices the
  test set also sees — overlapping-label contamination);
- **embargo**: an additional ``embargo_ns`` gap before the test start is
  excluded from training, guarding serial correlation that outlives the
  label horizon.

So train rows satisfy ``t + horizon + embargo < test_start`` and test rows
satisfy ``test_start <= t < test_end``.  Splits are pure event-time
interval logic — deterministic, no RNG, never a random shuffle.

**Segmenting by ROW MASS, not wall span (pinned, round-3).**  Dividing the
wall-clock span ``[t0, t1]`` into equal segments is only equivalent to equal
evidence when the data is uniform in time.  It is not: this platform's
equity frames occupy 13:30-16:05 UTC of each day plus a lone 20:00 close
print, so equal wall segments put ~100 % of the rows in two of four folds
and left the other two EMPTY — a "4-fold walk-forward" that was a 2-fold.
Boundaries are therefore quantiles of the pooled row index: segment k ends
at the timestamp of pooled row ``round(k * N / (n_folds + 1))``, so every
fold carries (approximately) the same number of rows whatever the calendar
does.  ``mode="wall_span"`` keeps the old behaviour for regression tests.

A fold whose test set is smaller than ``min_test_pairs`` is **degenerate**:
it is still yielded (so the caller can count and report it) and must be
treated as a FAILED fold by the promotion gates, never silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterator, List, Mapping, Tuple

import numpy as np
import pandas as pd

#: A fold with fewer usable test pairs than this is degenerate (pinned).
MIN_TEST_PAIRS = 32
#: Minimum number of non-degenerate folds a PROMOTE verdict requires (pinned).
MIN_NONDEGENERATE_FOLDS = 3


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

    def __init__(
        self,
        n_folds: int = 4,
        embargo_ns: int = 60_000_000_000,
        mode: str = "row_mass",
    ) -> None:
        if n_folds < 1:
            raise ValueError("n_folds must be >= 1")
        if embargo_ns < 0:
            raise ValueError("embargo_ns must be >= 0")
        if mode not in ("row_mass", "wall_span"):
            raise ValueError("mode must be 'row_mass' or 'wall_span'")
        self.n_folds = n_folds
        self.embargo_ns = embargo_ns
        self.mode = mode

    def folds(self, t0: int, t1: int) -> List[Fold]:
        """Fold boundaries over the closed event-time span [t0, t1]
        (wall-span mode; see :meth:`folds_by_row_mass` for the default)."""
        if t1 <= t0:
            raise ValueError("empty time span")
        seg = (t1 - t0) // (self.n_folds + 1)
        if seg <= 0:
            raise ValueError("span too short for the requested fold count")
        return self._folds_from_bounds(
            [t0 + seg * k for k in range(1, self.n_folds + 1)], t1
        )

    def folds_by_row_mass(self, ts_pooled: np.ndarray) -> List[Fold]:
        """Fold boundaries at quantiles of the pooled row index (pinned).

        ``ts_pooled`` is every row timestamp of every frame in the split
        (unsorted is fine).  Boundaries are strictly increasing; if the data
        is so degenerate that two quantiles land on the same timestamp the
        split is rejected (fail closed) rather than silently producing an
        empty fold.
        """
        ts = np.sort(np.asarray(ts_pooled, dtype=np.int64))
        n = ts.size
        if n < (self.n_folds + 1) * MIN_TEST_PAIRS:
            raise ValueError(
                f"too few rows ({n}) for {self.n_folds} folds at "
                f"{MIN_TEST_PAIRS} test pairs each"
            )
        bounds: List[int] = []
        for k in range(1, self.n_folds + 1):
            idx = int(round(k * n / (self.n_folds + 1)))
            bounds.append(int(ts[min(idx, n - 1)]))
        if any(b <= a for a, b in zip(bounds, bounds[1:])) or bounds[0] <= ts[0]:
            raise ValueError(
                "row-mass fold boundaries are not strictly increasing "
                "(too many identical timestamps for this fold count)"
            )
        return self._folds_from_bounds(bounds, int(ts[-1]))

    def _folds_from_bounds(self, starts: List[int], t1: int) -> List[Fold]:
        out: List[Fold] = []
        for k, test_start in enumerate(starts, start=1):
            test_end = starts[k] if k < len(starts) else t1 + 1
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
        nonempty = [df for df in frames.values() if len(df)]
        if not nonempty:
            raise ValueError("no rows to split")
        if self.mode == "row_mass":
            pooled = np.concatenate(
                [df["exchange_ts"].to_numpy(dtype=np.int64) for df in nonempty]
            )
            folds = self.folds_by_row_mass(pooled)
        else:
            t0 = min(int(df["exchange_ts"].iloc[0]) for df in nonempty)
            t1 = max(int(df["exchange_ts"].iloc[-1]) for df in nonempty)
            folds = self.folds(t0, t1)
        for fold in folds:
            train: Dict[int, pd.DataFrame] = {}
            test: Dict[int, pd.DataFrame] = {}
            for iid, df in frames.items():
                ts = df["exchange_ts"].to_numpy()
                train[iid] = df[fold.train_mask(ts, horizon_ns, self.embargo_ns)].reset_index(
                    drop=True
                )
                test[iid] = df[fold.test_mask(ts)].reset_index(drop=True)
            yield fold, train, test
