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
evidence when the data is uniform in time.  It is not: an equity session is
13:30-20:00 UTC, 6.5 h of each 24 h day (and up to v1.3.0 the bundled
equity flow stopped at about 16:05, leaving a lone 20:00 close print), so
equal wall segments put ~100 % of the rows in two of four folds and left
the other two EMPTY — a "4-fold walk-forward" that was a 2-fold.
Boundaries are therefore quantiles of the pooled row index: segment k ends
at the timestamp of pooled row ``round(k * N / (n_folds + 1))``, so every
fold carries (approximately) the same number of rows whatever the calendar
does.  ``mode="wall_span"`` keeps the old behaviour for regression tests.

**Day-aligned folds (v1.9, R2; opt-in).**  Row-mass boundaries fall
wherever the quantile lands — mid-session, with the test segment starting
60 s after the last train row of the same afternoon, so train and test
share the session's regime and its intraday dependence.  Two day-aligned
modes cut only at session-day starts (UTC days; a US equity session never
straddles midnight UTC):

- ``"day_aligned"`` — expanding walk-forward over whole days: with D days
  the last ``min(n_folds, D - 1)`` days are the test days, each trained on
  every earlier day (purge + embargo as above);
- ``"leave_one_day_out"`` — every day is a test day once and the model is
  trained on ALL other days, before and after it; purge and embargo apply
  on both sides of the test day (a train row is dropped when ``[t, t +
  horizon + embargo]`` reaches the test day or ``t`` falls within
  ``embargo`` after it).  This is cross-validation, not a walk-forward: it
  answers "does the relation hold on a day the fit never saw", and is the
  natural design when there are only a handful of sessions.

The default stays ``"row_mass"`` (every published number used it).

A fold whose test set is smaller than ``min_test_pairs`` is **degenerate**:
it is still yielded (so the caller can count and report it) and must be
treated as a FAILED fold by the promotion gates, never silently dropped.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd

#: A fold with fewer usable test pairs than this is degenerate (pinned).
MIN_TEST_PAIRS = 32
#: Minimum number of non-degenerate folds a PROMOTE verdict requires (pinned).
MIN_NONDEGENERATE_FOLDS = 3
#: Fold layouts (module docs): the default first.
SPLIT_MODES = ("row_mass", "wall_span", "day_aligned", "leave_one_day_out")
DEFAULT_SPLIT_MODE = "row_mass"
NS_DAY = 86_400 * 1_000_000_000


@dataclass(frozen=True)
class Fold:
    """One walk-forward fold (event-time boundaries, ns)."""

    index: int
    train_end: int  # exclusive: train rows have t + purge + embargo < test_start
    test_start: int  # inclusive
    test_end: int  # exclusive
    #: leave-one-day-out: train on both sides of the test span
    two_sided: bool = False

    def train_mask(self, ts: np.ndarray, horizon_ns: int, embargo_ns: int) -> np.ndarray:
        ts = np.asarray(ts, dtype=np.int64)
        before = ts + horizon_ns + embargo_ns < self.test_start
        if not self.two_sided:
            return before
        return before | (ts >= self.test_end + embargo_ns)

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
        if mode not in SPLIT_MODES:
            raise ValueError(f"mode must be one of {SPLIT_MODES}")
        self.n_folds = n_folds
        self.embargo_ns = embargo_ns
        self.mode = mode

    def folds(self, t0: int, t1: int) -> list[Fold]:
        """Fold boundaries over the closed event-time span [t0, t1]
        (wall-span mode; see :meth:`folds_by_row_mass` for the default)."""
        if t1 <= t0:
            raise ValueError("empty time span")
        seg = (t1 - t0) // (self.n_folds + 1)
        if seg <= 0:
            raise ValueError("span too short for the requested fold count")
        return self._folds_from_bounds([t0 + seg * k for k in range(1, self.n_folds + 1)], t1)

    def folds_by_row_mass(self, ts_pooled: np.ndarray) -> list[Fold]:
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
                f"too few rows ({n}) for {self.n_folds} folds at {MIN_TEST_PAIRS} test pairs each"
            )
        bounds: list[int] = []
        for k in range(1, self.n_folds + 1):
            idx = int(round(k * n / (self.n_folds + 1)))
            bounds.append(int(ts[min(idx, n - 1)]))
        if any(b <= a for a, b in zip(bounds, bounds[1:], strict=False)) or bounds[0] <= ts[0]:
            raise ValueError(
                "row-mass fold boundaries are not strictly increasing "
                "(too many identical timestamps for this fold count)"
            )
        return self._folds_from_bounds(bounds, int(ts[-1]))

    def folds_by_day(self, ts_pooled: np.ndarray) -> list[Fold]:
        """Day-aligned folds (``day_aligned`` / ``leave_one_day_out``).

        Boundaries are the first row timestamp of each UTC day present in
        ``ts_pooled``; a test span covers exactly one day.  Fewer than two
        days is rejected (fail closed): there is nothing to hold out.
        """
        ts = np.sort(np.asarray(ts_pooled, dtype=np.int64))
        if ts.size == 0:
            raise ValueError("no rows to split")
        days, first = np.unique(ts // NS_DAY, return_index=True)
        if days.size < 2:
            raise ValueError(f"{self.mode} folds need at least 2 session days, got {days.size}")
        starts = [int(ts[i]) for i in first]
        ends = [int(ts[i]) for i in first[1:]] + [int(ts[-1]) + 1]
        if self.mode == "leave_one_day_out":
            picks = range(days.size)
        else:
            k = min(self.n_folds, days.size - 1)
            picks = range(days.size - k, days.size)
        return [
            Fold(
                index=j,
                train_end=starts[d],
                test_start=starts[d],
                test_end=ends[d],
                two_sided=self.mode == "leave_one_day_out",
            )
            for j, d in enumerate(picks, start=1)
        ]

    def _folds_from_bounds(self, starts: list[int], t1: int) -> list[Fold]:
        out: list[Fold] = []
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
    ) -> Iterator[tuple[Fold, dict[int, pd.DataFrame], dict[int, pd.DataFrame]]]:
        """Yield (fold, train_frames, test_frames) with purge+embargo applied."""
        nonempty = [df for df in frames.values() if len(df)]
        if not nonempty:
            raise ValueError("no rows to split")
        if self.mode == "row_mass":
            pooled = np.concatenate([df["exchange_ts"].to_numpy(dtype=np.int64) for df in nonempty])
            folds = self.folds_by_row_mass(pooled)
        elif self.mode in ("day_aligned", "leave_one_day_out"):
            pooled = np.concatenate([df["exchange_ts"].to_numpy(dtype=np.int64) for df in nonempty])
            folds = self.folds_by_day(pooled)
        else:
            t0 = min(int(df["exchange_ts"].iloc[0]) for df in nonempty)
            t1 = max(int(df["exchange_ts"].iloc[-1]) for df in nonempty)
            folds = self.folds(t0, t1)
        for fold in folds:
            train: dict[int, pd.DataFrame] = {}
            test: dict[int, pd.DataFrame] = {}
            for iid, df in frames.items():
                ts = df["exchange_ts"].to_numpy()
                train[iid] = df[fold.train_mask(ts, horizon_ns, self.embargo_ns)].reset_index(
                    drop=True
                )
                test[iid] = df[fold.test_mask(ts)].reset_index(drop=True)
            yield fold, train, test
