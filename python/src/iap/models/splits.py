"""Time-aware walk-forward splits: expanding train, embargoed test (spec §13).

Semantics (mirrors the platform's alpha-validation walk-forward, pinned):

- The time axis is partitioned into ``n_folds + 1`` contiguous equal-width
  segments by timestamp.  Fold k (k = 0..n_folds-1) trains on segments
  [0..k] (EXPANDING) and tests on segment k+1.
- An **embargo** of ``embargo_ns`` is skipped at the start of every test
  segment: no test sample within ``embargo_ns`` of the train boundary.
- **Purging**: train samples whose forward label window crosses the train
  boundary (``anchor_ts + label_horizon_ns > train_end``) are dropped, so no
  train label overlaps the test period.

Splits are computed from timestamps only — random shuffling never occurs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

import numpy as np


@dataclass(frozen=True)
class Fold:
    """One walk-forward fold: index arrays into the caller's row order."""

    fold: int
    train_idx: np.ndarray
    test_idx: np.ndarray
    train_window: Tuple[int, int]  # [start_ts, end_ts) actually used by train
    test_window: Tuple[int, int]   # [start_ts, end_ts) actually used by test


class WalkForwardSplitter:
    """Expanding-train / embargoed-test walk-forward splitter.

    Segments are quantiles of the ROW INDEX (pinned, round-3), so every fold
    carries about the same number of rows whatever the calendar does; folds
    that still come out empty are recorded in :attr:`degenerate_folds`
    instead of being silently skipped.
    """

    def __init__(
        self,
        n_folds: int = 4,
        embargo_ns: int = 60_000_000_000,
        label_horizon_ns: int = 5_000_000_000,
    ) -> None:
        if n_folds < 1:
            raise ValueError(f"n_folds must be >= 1, got {n_folds}")
        if embargo_ns < 0 or label_horizon_ns < 0:
            raise ValueError("embargo_ns / label_horizon_ns must be >= 0")
        self.n_folds = n_folds
        self.embargo_ns = embargo_ns
        self.label_horizon_ns = label_horizon_ns

    def split(self, ts: np.ndarray) -> List[Fold]:
        """Build folds from a (not necessarily sorted) int64 ns timestamp array.

        Returned index arrays index into ``ts`` as given.  Every fold
        satisfies: max(train ts) + label_horizon <= boundary and
        min(test ts) >= boundary + embargo.
        """
        ts = np.asarray(ts, dtype=np.int64)
        if ts.size == 0:
            raise ValueError("cannot split an empty timestamp array")
        t0 = int(ts.min())
        t1 = int(ts.max())
        span = t1 - t0
        if span <= 0:
            raise ValueError("timestamp span must be positive")
        n_seg = self.n_folds + 1
        # Segment boundaries at quantiles of the ROW INDEX, not of the wall
        # span (pinned, round-3 — mirrors iap.validation.splits): this data
        # occupies 2.6 h of each 24 h day, so equal wall segments put ~100 %
        # of the rows in two folds and left the others empty.
        order = np.sort(ts)
        n = order.size
        bounds = [t0]
        for i in range(1, n_seg):
            bounds.append(int(order[min(int(round(i * n / n_seg)), n - 1)]))
        bounds.append(t1 + 1)

        folds: List[Fold] = []
        self.degenerate_folds: List[int] = []
        for k in range(self.n_folds):
            train_end = bounds[k + 1]         # exclusive train boundary
            test_start = train_end + self.embargo_ns
            test_end = bounds[k + 2]
            # purge: train anchors whose label window crosses the boundary
            train_mask = (ts >= t0) & (ts + self.label_horizon_ns <= train_end)
            test_mask = (ts >= test_start) & (ts < test_end)
            train_idx = np.flatnonzero(train_mask)
            test_idx = np.flatnonzero(test_mask)
            if train_idx.size == 0 or test_idx.size == 0:
                # Reported, never silently dropped: a fold with no usable
                # rows is a failure of the split, not a missing datum.
                self.degenerate_folds.append(k)
                continue
            folds.append(
                Fold(
                    fold=k,
                    train_idx=train_idx,
                    test_idx=test_idx,
                    train_window=(t0, int(ts[train_idx].max())),
                    test_window=(int(ts[test_idx].min()),
                                 int(ts[test_idx].max())),
                )
            )
        if not folds:
            raise ValueError("walk-forward produced no usable folds")
        return folds

    @property
    def n_degenerate(self) -> int:
        """Folds the last :meth:`split` could not populate (reported)."""
        return len(getattr(self, "degenerate_folds", []))
