"""Combinatorial purged cross-validation and the probability of backtest
overfitting (v1.12, plan item R7; opt-in).

**CPCV** (López de Prado, *Advances in Financial Machine Learning*, 2018,
ch. 12).  The sample is cut into ``N`` contiguous groups; every one of the
``C(N, k)`` ways of choosing ``k`` of them as the test set is a split, and
the model is trained on the other ``N - k`` groups, before AND after the
test groups.  Each group is tested in ``C(N-1, k-1)`` splits, so the
out-of-sample predictions reassemble into

    phi(N, k) = k / N * C(N, k) = C(N - 1, k - 1)

complete backtest **paths**: path ``j`` takes, for every group ``g``, the
prediction of the ``j``-th split (lexicographic order) that tested ``g``.
A walk-forward gives one path; CPCV gives a distribution of them from the
same data, which is the point when there are only a handful of sessions.

Groups (pinned):

- **day-aligned** when the data spans at least ``N`` UTC session days: the
  days are dealt into ``N`` contiguous groups of (as near as possible)
  equal day counts, so no group boundary falls inside a session (the
  :mod:`iap.validation.splits` ``day_aligned`` rationale);
- **row-mass** otherwise: boundaries at quantiles of the pooled row index,
  as the default walk-forward does.

Purge and embargo apply on BOTH sides of every test group, exactly as
``leave_one_day_out`` does: a train row at ``t`` is dropped when its label
window plus the embargo, ``[t, t + horizon + embargo]``, reaches a test
group, or when ``t`` falls within ``embargo`` after a test group ends.  Two
adjacent test groups are one test interval.  Deterministic: no RNG.

**PBO via CSCV** (Bailey, Borwein, López de Prado & Zhu, "The probability
of backtest overfitting", *J. Computational Finance* 20(4), 2017).  Given a
``T x M`` matrix of per-period performance of ``M`` candidate
configurations, the ``T`` rows are cut into ``S`` (even) blocks; for each of
the ``C(S, S/2)`` choices of half the blocks as in-sample, the IS-best
configuration (highest Sharpe) is located in the out-of-sample ranking; its
relative rank ``w`` gives the logit ``lambda = ln(w / (1 - w))`` and

    PBO = share of the splits with lambda <= 0

(the IS winner ranked at or below the OOS median).  Pure noise gives
PBO ~ 0.5; a configuration that is genuinely and robustly best gives
PBO ~ 0.  PBO needs a set of candidates (a sweep), so it is a function of
a performance matrix, not a field of one alpha's validation report.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations

import numpy as np
import pandas as pd

from iap.validation.splits import MIN_TEST_PAIRS, NS_DAY

#: The opt-in defaults: 6 groups, 2 test groups -> 15 splits, 5 paths.
DEFAULT_CPCV_GROUPS = 6
DEFAULT_CPCV_TEST_GROUPS = 2
#: Default number of CSCV blocks for PBO (even).
DEFAULT_PBO_BLOCKS = 16


def n_splits(n_groups: int, k_test: int) -> int:
    """``C(N, k)``: the number of CPCV splits."""
    return math.comb(int(n_groups), int(k_test))


def n_paths(n_groups: int, k_test: int) -> int:
    """``phi(N, k) = k / N * C(N, k) = C(N-1, k-1)`` backtest paths."""
    return math.comb(int(n_groups) - 1, int(k_test) - 1)


@dataclass(frozen=True)
class CPCVSplit:
    """One combinatorial split (event-time boundaries, ns)."""

    index: int
    test_groups: tuple[int, ...]
    #: ``(start, end)`` per test group, ``end`` exclusive, in group order
    test_bounds: tuple[tuple[int, int], ...]

    def intervals(self) -> list[tuple[int, int]]:
        """The test spans with adjacent groups merged."""
        out: list[list[int]] = []
        for s, e in self.test_bounds:
            if out and out[-1][1] >= s:
                out[-1][1] = max(out[-1][1], e)
            else:
                out.append([s, e])
        return [(a, b) for a, b in out]

    def train_mask(self, ts: np.ndarray, horizon_ns: int, embargo_ns: int) -> np.ndarray:
        """Train rows: outside every test interval, purged before it and
        embargoed after it (module docs)."""
        ts = np.asarray(ts, dtype=np.int64)
        keep = np.ones(ts.shape, dtype=bool)
        for s, e in self.intervals():
            keep &= (ts + horizon_ns + embargo_ns < s) | (ts >= e + embargo_ns)
        return keep

    def test_mask(self, ts: np.ndarray) -> np.ndarray:
        ts = np.asarray(ts, dtype=np.int64)
        m = np.zeros(ts.shape, dtype=bool)
        for s, e in self.test_bounds:
            m |= (ts >= s) & (ts < e)
        return m

    def group_mask(self, ts: np.ndarray, group: int) -> np.ndarray:
        """Rows of one of this split's test groups."""
        s, e = self.test_bounds[self.test_groups.index(group)]
        ts = np.asarray(ts, dtype=np.int64)
        return (ts >= s) & (ts < e)


class CombinatorialPurgedSplitter:
    """All ``C(N, k)`` purged, embargoed splits (module docs)."""

    def __init__(
        self,
        n_groups: int = DEFAULT_CPCV_GROUPS,
        k_test: int = DEFAULT_CPCV_TEST_GROUPS,
        embargo_ns: int = 60_000_000_000,
    ) -> None:
        if n_groups < 2:
            raise ValueError("n_groups must be >= 2")
        if not 1 <= k_test < n_groups:
            raise ValueError("k_test must be in [1, n_groups)")
        if embargo_ns < 0:
            raise ValueError("embargo_ns must be >= 0")
        self.n_groups = int(n_groups)
        self.k_test = int(k_test)
        self.embargo_ns = int(embargo_ns)
        #: ``"day_aligned"`` or ``"row_mass"`` once :meth:`groups` has run
        self.grouping: str | None = None

    @property
    def n_splits(self) -> int:
        return n_splits(self.n_groups, self.k_test)

    @property
    def n_paths(self) -> int:
        return n_paths(self.n_groups, self.k_test)

    def groups(self, ts_pooled: np.ndarray) -> list[tuple[int, int]]:
        """``N`` contiguous ``(start, end)`` group spans (end exclusive)
        covering every row: day-aligned when there are >= N days, else
        row-mass.  Fails closed on too little data."""
        ts = np.sort(np.asarray(ts_pooled, dtype=np.int64))
        if ts.size < self.n_groups * MIN_TEST_PAIRS:
            raise ValueError(
                f"too few rows ({ts.size}) for {self.n_groups} CPCV groups "
                f"at {MIN_TEST_PAIRS} rows each"
            )
        end = int(ts[-1]) + 1
        days, first = np.unique(ts // NS_DAY, return_index=True)
        if days.size >= self.n_groups:
            self.grouping = "day_aligned"
            cuts = [int(round(g * days.size / self.n_groups)) for g in range(self.n_groups)]
            starts = [int(ts[first[c]]) for c in cuts]
        else:
            self.grouping = "row_mass"
            starts = [int(ts[0])] + [
                int(ts[int(round(g * ts.size / self.n_groups))]) for g in range(1, self.n_groups)
            ]
            if any(b <= a for a, b in zip(starts, starts[1:], strict=False)):
                raise ValueError(
                    "row-mass CPCV group boundaries are not strictly increasing "
                    "(too many identical timestamps for this group count)"
                )
        ends = starts[1:] + [end]
        return list(zip(starts, ends, strict=True))

    def splits(self, ts_pooled: np.ndarray) -> list[CPCVSplit]:
        bounds = self.groups(ts_pooled)
        return [
            CPCVSplit(
                index=i,
                test_groups=combo,
                test_bounds=tuple(bounds[g] for g in combo),
            )
            for i, combo in enumerate(combinations(range(self.n_groups), self.k_test))
        ]

    def path_assignment(self) -> list[dict[int, int]]:
        """``paths[j][g]`` = index of the split whose prediction path ``j``
        uses for group ``g`` (the ``j``-th split, lexicographically, that
        tests ``g``).  Every split appears in exactly ``k`` (path, group)
        cells and every path covers every group once."""
        combos = list(combinations(range(self.n_groups), self.k_test))
        by_group: dict[int, list[int]] = {g: [] for g in range(self.n_groups)}
        for i, combo in enumerate(combos):
            for g in combo:
                by_group[g].append(i)
        return [{g: by_group[g][j] for g in range(self.n_groups)} for j in range(self.n_paths)]

    def split_frames(
        self, frames: Mapping[int, pd.DataFrame], horizon_ns: int
    ) -> Iterator[tuple[CPCVSplit, dict[int, pd.DataFrame], dict[int, pd.DataFrame]]]:
        """Yield ``(split, train_frames, test_frames)`` with purge + embargo."""
        nonempty = [df for df in frames.values() if len(df)]
        if not nonempty:
            raise ValueError("no rows to split")
        pooled = np.concatenate([df["exchange_ts"].to_numpy(dtype=np.int64) for df in nonempty])
        for split in self.splits(pooled):
            train: dict[int, pd.DataFrame] = {}
            test: dict[int, pd.DataFrame] = {}
            for iid, df in frames.items():
                ts = df["exchange_ts"].to_numpy()
                train[iid] = df[split.train_mask(ts, horizon_ns, self.embargo_ns)].reset_index(
                    drop=True
                )
                test[iid] = df[split.test_mask(ts)].reset_index(drop=True)
            yield split, train, test


# -- probability of backtest overfitting (CSCV) ----------------------------


def _sharpe_cols(m: np.ndarray) -> np.ndarray:
    mu = m.mean(axis=0)
    sd = m.std(axis=0, ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(sd > 0, mu / sd, 0.0)
    return out


def probability_of_backtest_overfitting(
    performance: np.ndarray | pd.DataFrame | Sequence[Sequence[float]],
    n_blocks: int = DEFAULT_PBO_BLOCKS,
) -> dict[str, object]:
    """PBO of a ``T x M`` per-period performance matrix (rows = periods in
    time order, columns = candidate configurations) by CSCV (module docs).

    Returns ``pbo``, the per-split ``logits``, ``n_splits``, ``n_blocks``
    and ``n_candidates``.  The OOS rank of the IS winner is
    ``w = rank / (M + 1)`` with ``rank`` in ``1..M`` (1 = worst), ties
    ranked by average.  Deterministic.
    """
    m = np.asarray(performance, dtype=float)
    if m.ndim != 2 or m.shape[1] < 2:
        raise ValueError("performance must be a T x M matrix with M >= 2 candidates")
    if n_blocks < 2 or n_blocks % 2:
        raise ValueError("n_blocks must be an even integer >= 2")
    t, n_cand = m.shape
    if t < n_blocks * 2:
        raise ValueError(f"too few periods ({t}) for {n_blocks} blocks of >= 2 rows")
    if not np.all(np.isfinite(m)):
        raise ValueError("performance must be finite")
    edges = [int(round(b * t / n_blocks)) for b in range(n_blocks + 1)]
    blocks = [np.arange(edges[b], edges[b + 1]) for b in range(n_blocks)]
    logits: list[float] = []
    for is_blocks in combinations(range(n_blocks), n_blocks // 2):
        is_set = set(is_blocks)
        is_rows = np.concatenate([blocks[b] for b in is_blocks])
        oos_rows = np.concatenate([blocks[b] for b in range(n_blocks) if b not in is_set])
        sr_is = _sharpe_cols(m[is_rows])
        sr_oos = _sharpe_cols(m[oos_rows])
        best = int(np.argmax(sr_is))
        rank = float(pd.Series(sr_oos).rank(method="average").iloc[best])
        w = rank / (n_cand + 1)
        logits.append(math.log(w / (1.0 - w)))
    lam = np.asarray(logits)
    return {
        "pbo": float(np.mean(lam <= 0.0)),
        "logits": [float(v) for v in lam],
        "logit_median": float(np.median(lam)),
        "n_splits": int(lam.size),
        "n_blocks": int(n_blocks),
        "n_candidates": int(n_cand),
        "n_periods": int(t),
    }
