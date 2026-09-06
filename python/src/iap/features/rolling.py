"""Rolling event-time state primitives for the feature engine.

All windows are half-open intervals ``(t - w, t]`` in **event time**
(``exchange_ts``): a sample stamped exactly ``t - w`` has left the window,
a sample stamped ``t`` is inside it.  Every structure is O(1) amortized per
event and fully deterministic (no wall clock, no unordered iteration).

Integer inputs keep integer running sums, so incremental window sums are
*exact* (bit-identical to a brute-force recomputation); float inputs drift
only by normal IEEE accumulation, well inside the 1e-9 golden tolerance.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import deque
from typing import Deque, Dict, List, Optional, Sequence, Tuple


class RollingSum:
    """Rolling sums of an n-tuple of values over an event-time window.

    ``add(ts, vals)`` appends a sample; ``trim(now)`` evicts samples with
    ``ts <= now - window``.  ``sums[i]`` is the running sum of component i,
    ``count`` the number of live samples.
    """

    __slots__ = ("window", "nvals", "buf", "sums", "count")

    def __init__(self, window_ns: int, nvals: int) -> None:
        if window_ns <= 0 or nvals <= 0:
            raise ValueError("window_ns and nvals must be positive")
        self.window = window_ns
        self.nvals = nvals
        self.buf: Deque[Tuple] = deque()
        self.sums: List = [0] * nvals
        self.count = 0

    def add(self, ts: int, vals: Sequence) -> None:
        """Append one sample at event time ``ts`` and evict expired samples."""
        self.buf.append((ts, vals))
        sums = self.sums
        for i, v in enumerate(vals):
            sums[i] += v
        self.count += 1
        self.trim(ts)

    def trim(self, now: int) -> None:
        """Evict samples with ts <= now - window."""
        cutoff = now - self.window
        buf = self.buf
        sums = self.sums
        while buf and buf[0][0] <= cutoff:
            _, vals = buf.popleft()
            for i, v in enumerate(vals):
                sums[i] -= v
            self.count -= 1


class RollingExtrema:
    """Rolling max and min of a scalar over an event-time window.

    Two monotonic deques; O(1) amortized. ``max()``/``min()`` return None
    when the window is empty.
    """

    __slots__ = ("window", "_maxq", "_minq")

    def __init__(self, window_ns: int) -> None:
        self.window = window_ns
        self._maxq: Deque[Tuple[int, float]] = deque()
        self._minq: Deque[Tuple[int, float]] = deque()

    def add(self, ts: int, val) -> None:
        maxq, minq = self._maxq, self._minq
        while maxq and maxq[-1][1] <= val:
            maxq.pop()
        maxq.append((ts, val))
        while minq and minq[-1][1] >= val:
            minq.pop()
        minq.append((ts, val))
        self.trim(ts)

    def trim(self, now: int) -> None:
        cutoff = now - self.window
        while self._maxq and self._maxq[0][0] <= cutoff:
            self._maxq.popleft()
        while self._minq and self._minq[0][0] <= cutoff:
            self._minq.popleft()

    def max(self):
        return self._maxq[0][1] if self._maxq else None

    def min(self):
        return self._minq[0][1] if self._minq else None


class TimeSeries:
    """Append-only (ts, value) series with at-or-before lookup and trimming.

    ``at_or_before(t)`` returns the latest value with sample ts <= t (None if
    none).  ``trim(min_ts)`` forgets samples older than min_ts (amortized via
    periodic compaction so lookups stay O(log n) on the retained span).
    """

    __slots__ = ("_ts", "_vals", "_start")

    _COMPACT_AT = 4096

    def __init__(self) -> None:
        self._ts: List[int] = []
        self._vals: List = []
        self._start = 0

    def append(self, ts: int, val) -> None:
        if self._ts and ts < self._ts[-1]:
            raise ValueError("TimeSeries requires non-decreasing timestamps")
        self._ts.append(ts)
        self._vals.append(val)

    def at_or_before(self, t: int):
        """Latest value with ts <= t, or None."""
        i = bisect_right(self._ts, t, self._start)
        return self._vals[i - 1] if i > self._start else None

    def first_ts(self) -> Optional[int]:
        return self._ts[self._start] if self._start < len(self._ts) else None

    def last(self):
        return self._vals[-1] if self._vals else None

    def __len__(self) -> int:
        return len(self._ts) - self._start

    def trim(self, min_ts: int) -> None:
        """Forget samples with ts < min_ts (keeps the newest at-or-before)."""
        i = bisect_right(self._ts, min_ts)
        if i > 0:
            self._start = max(self._start, i - 1)
        if self._start >= self._COMPACT_AT:
            del self._ts[: self._start]
            del self._vals[: self._start]
            self._start = 0


class RollingKeyCount:
    """Rolling per-key event counts over an event-time window (venue shares)."""

    __slots__ = ("window", "buf", "counts", "total")

    def __init__(self, window_ns: int) -> None:
        self.window = window_ns
        self.buf: Deque[Tuple[int, int]] = deque()
        self.counts: Dict[int, int] = {}
        self.total = 0

    def add(self, ts: int, key: int) -> None:
        self.buf.append((ts, key))
        self.counts[key] = self.counts.get(key, 0) + 1
        self.total += 1
        self.trim(ts)

    def trim(self, now: int) -> None:
        cutoff = now - self.window
        while self.buf and self.buf[0][0] <= cutoff:
            _, key = self.buf.popleft()
            c = self.counts[key] - 1
            if c:
                self.counts[key] = c
            else:
                del self.counts[key]
            self.total -= 1

    def shares(self) -> List[float]:
        """Per-key count shares, iterated in sorted key order."""
        if not self.total:
            return []
        tot = float(self.total)
        return [self.counts[k] / tot for k in sorted(self.counts)]


class SessionProfile:
    """Expanding minute-of-day session profile built from the data itself.

    **Bound / regime caveat (documented, round-3)**: the profile is
    EXPANDING and never forgets.  Over weeks the normalizer converges to a
    long-run mean and stops tracking the current regime, so
    ``norm_*_m5_v1`` measures "vs the whole history", not "vs recent
    comparable sessions".  Memory is bounded (288 buckets x metrics), so
    this is a modelling choice, not a leak: a decayed variant is available
    via ``decay`` (an exponential-forgetting factor applied to the running
    count and sum at each update; ``decay = 1.0``, the default, is the
    pinned expanding behaviour and the only one the goldens cover).

    Buckets are 5 minutes of the SESSION-LOCAL day (0..287).  For each
    (metric, bucket) the
    profile keeps an expanding count and sum.  ``prior(metric, bucket)``
    returns the (count, mean) accumulated *before* the current observation —
    readers must call ``prior`` before ``update`` so normalization never uses
    the value being normalized (no lookahead).
    """

    BUCKETS = 288  # 5-minute buckets per session-local day
    MIN_OBS = 10  # observations required before a normalization is valid

    __slots__ = ("_count", "_sum", "decay")

    def __init__(self, metrics: Sequence[str], decay: float = 1.0) -> None:
        if not 0.0 < decay <= 1.0:
            raise ValueError("decay must be in (0, 1]")
        self.decay = float(decay)
        self._count: Dict[str, List[float]] = {
            m: [0.0] * self.BUCKETS for m in metrics
        }
        self._sum: Dict[str, List[float]] = {
            m: [0.0] * self.BUCKETS for m in metrics
        }

    @staticmethod
    def bucket_of(ts_ns: int, utc_offset_s: int = 0) -> int:
        """5-minute-of-day bucket for an event timestamp.

        ``utc_offset_s`` is the venue's UTC offset at that instant
        (``InstrumentContext.clock``): buckets are keyed in SESSION-LOCAL
        time so a DST shift moves the whole profile with the venue
        (API_FEATURES §3).  The default 0 keeps pure-UTC callers exact.
        """
        sec_of_day = (ts_ns // 1_000_000_000 + utc_offset_s) % 86_400
        return int(sec_of_day // 300)

    def prior(self, metric: str, bucket: int) -> Tuple[float, float]:
        """(count, mean) accumulated so far for (metric, bucket); mean=0 if empty."""
        c = self._count[metric][bucket]
        s = self._sum[metric][bucket]
        return c, (s / c if c else 0.0)

    def update(self, metric: str, bucket: int, value: float) -> None:
        """Fold one observation into the profile (call after ``prior``).

        With ``decay < 1`` the running count and sum are scaled first, so
        the effective memory is ``1 / (1 - decay)`` observations per bucket.
        """
        d = self.decay
        if d != 1.0:
            self._count[metric][bucket] *= d
            self._sum[metric][bucket] *= d
        self._count[metric][bucket] += 1.0
        self._sum[metric][bucket] += value
