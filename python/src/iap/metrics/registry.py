"""Prometheus text exposition written by hand (no dependency): counters,
gauges and fixed log2 histograms, in the exact format the retired Java
``com.iap.monitoring.MetricsRegistry`` rendered and ``rust/telemetry`` pins
(PLATFORM_CONVENTIONS.md §12.4-§12.6):

- metrics sorted by name, one ``# TYPE`` line per family;
- a labelled series is ``name{label="value"}`` with the value escaped
  (backslash, double quote, newline);
- a histogram has 65 buckets: bucket 0 holds the value 0, bucket ``i >= 1``
  holds ``[2^(i-1), 2^i)``, so the exposed ``le`` bounds are ``0, 1, 3, 7,
  ..., 2^i - 1``; empty buckets are skipped, ``+Inf`` is always present and
  equals ``_count``, and ``_sum`` follows.

The registry is rebuilt by :class:`iap.metrics.exporter.PlatformExporter` on
every refresh, so it needs no locking: a render reads one immutable snapshot.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

__all__ = ["Counter", "Gauge", "Histogram", "MetricsRegistry", "escape_label", "series_key"]

BUCKETS = 65


def escape_label(value: str) -> str:
    """Escape a label value per the text exposition format."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def series_key(name: str, labels: Mapping[str, str] | None) -> str:
    """``name`` or ``name{a="x",b="y"}`` with labels in sorted order."""
    if not labels:
        return name
    inner = ",".join(f'{k}="{escape_label(str(v))}"' for k, v in sorted(labels.items()))
    return f"{name}{{{inner}}}"


def _num(value: float) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(float(value))


@dataclass
class Counter:
    value: int = 0

    def inc(self, by: int = 1) -> None:
        if by < 0:
            raise ValueError("a counter never decreases")
        self.value += by

    def set(self, value: int) -> None:
        """Set an absolute value (the exporter re-reads totals from files)."""
        if value < 0:
            raise ValueError("a counter is never negative")
        self.value = int(value)


@dataclass
class Gauge:
    value: float = 0.0

    def set(self, value: float) -> None:
        self.value = value


@dataclass
class Histogram:
    counts: list[int] = field(default_factory=lambda: [0] * BUCKETS)
    total: int = 0
    sum: float = 0.0

    @staticmethod
    def bucket_of(value: int) -> int:
        if value < 0:
            raise ValueError(f"histogram samples must be >= 0: {value}")
        return 0 if value == 0 else value.bit_length()

    @staticmethod
    def bucket_upper(index: int) -> int:
        """Inclusive upper bound of a bucket (``2^i - 1``; 0 for bucket 0)."""
        if index < 0 or index >= BUCKETS:
            raise ValueError(f"bucket index out of range: {index}")
        if index == 0:
            return 0
        if index == BUCKETS - 1:
            return (1 << 63) - 1
        return (1 << index) - 1

    def record(self, value: int) -> None:
        self.counts[self.bucket_of(int(value))] += 1
        self.total += 1
        self.sum += float(value)

    def quantile(self, q: float) -> int:
        """Inclusive upper bound of the bucket holding the rank-``ceil(q*n)``
        sample (conservative: within one power of two above the exact value)."""
        if not 0.0 < q <= 1.0:
            raise ValueError("q must be in (0, 1]")
        if self.total == 0:
            return 0
        rank = max(1, math.ceil(q * self.total))
        seen = 0
        for i, c in enumerate(self.counts):
            seen += c
            if seen >= rank:
                return self.bucket_upper(i)
        return self.bucket_upper(BUCKETS - 1)


def _base(key: str) -> str:
    brace = key.find("{")
    return key if brace < 0 else key[:brace]


def _labels(key: str) -> str | None:
    brace = key.find("{")
    if brace < 0 or not key.endswith("}"):
        return None
    inner = key[brace + 1 : -1]
    return inner or None


def _child(base: str, labels: str | None, suffix: str, le: str | None) -> str:
    out = base + suffix
    if labels is None and le is None:
        return out
    parts = []
    if labels is not None:
        parts.append(labels)
    if le is not None:
        parts.append(f'le="{le}"')
    return out + "{" + ",".join(parts) + "}"


class MetricsRegistry:
    """Counters, gauges and histograms keyed by their series key."""

    def __init__(self) -> None:
        self.counters: dict[str, Counter] = {}
        self.gauges: dict[str, Gauge] = {}
        self.histograms: dict[str, Histogram] = {}

    def counter(self, name: str, **labels: str) -> Counter:
        return self.counters.setdefault(series_key(name, labels), Counter())

    def gauge(self, name: str, **labels: str) -> Gauge:
        return self.gauges.setdefault(series_key(name, labels), Gauge())

    def histogram(self, name: str, **labels: str) -> Histogram:
        return self.histograms.setdefault(series_key(name, labels), Histogram())

    def names(self) -> set[str]:
        """The bare metric names of every family (no label suffixes)."""
        keys: Iterable[str] = (*self.counters, *self.gauges, *self.histograms)
        return {_base(k) for k in keys}

    def render(self) -> str:
        out: list[str] = []
        typed: str | None = None
        for key in sorted(self.counters):
            base = _base(key)
            if base != typed:
                out.append(f"# TYPE {base} counter")
                typed = base
            out.append(f"{key} {self.counters[key].value}")
        typed = None
        for key in sorted(self.gauges):
            base = _base(key)
            if base != typed:
                out.append(f"# TYPE {base} gauge")
                typed = base
            out.append(f"{key} {_num(self.gauges[key].value)}")
        typed = None
        for key in sorted(self.histograms):
            base = _base(key)
            labels = _labels(key)
            if base != typed:
                out.append(f"# TYPE {base} histogram")
                typed = base
            h = self.histograms[key]
            cumulative = 0
            for i, c in enumerate(h.counts):
                if c == 0:
                    continue
                cumulative += c
                out.append(
                    f"{_child(base, labels, '_bucket', str(h.bucket_upper(i)))} {cumulative}"
                )
            out.append(f"{_child(base, labels, '_bucket', '+Inf')} {cumulative}")
            out.append(f"{_child(base, labels, '_sum', None)} {_num(h.sum)}")
            out.append(f"{_child(base, labels, '_count', None)} {cumulative}")
        return "\n".join(out) + ("\n" if out else "")
