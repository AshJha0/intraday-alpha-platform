"""Per-event latency of the Python MVP loop on a real slice (v1.17, F4).

Times every :meth:`MvpEngine.on_event` call of one symbol of an ingested
dataset (normally the committed slice ``benchmarks/slices/itch50_20190130_AAPL``)
with ``time.perf_counter_ns`` and reports the distribution.  This is the
cost of the whole pinned loop per event — simulator, risk wiring, TCA
timeline, feature engine, decision, children, traces — the Python
REFERENCE path (docs/POLYGLOT.md), not a hot path: the C++ tick-to-trade
benchmark is the latency figure of the platform, this is the budget the
reference loop must stay inside so that a real day stays runnable.

The guard around it is ``tests/harness/check_mvp_latency_budget.py``
(budget file ``benchmarks/latency_budget.json``: a measured CI p99 with its
run id, times a margin).  Timings on a shared CI runner move with neighbour
load, so the guard is coarse by design and compares medians and p99s at a
generous factor, never the tail.

Nothing here changes the loop: the engine is driven exactly as
``run_session`` drives it (same config, same feed, a JSONL trace sink),
only the clock reads are added around ``on_event``.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from pathlib import Path

from iap.mvp.engine import MvpEngine
from iap.mvp.real import PreparedSymbol
from iap.trace.sinks import JsonlTraceSink

PERCENTILES = (0.50, 0.90, 0.99, 0.999)


@dataclass(frozen=True)
class LoopLatency:
    """Per-event ``on_event`` wall time in nanoseconds."""

    events: int
    mean_ns: float
    quantiles_ns: dict[str, int]  #: ``"p50"`` .. ``"p99.9"`` plus ``"max"``
    total_s: float
    decisions: int
    children_submitted: int
    fills: int

    def to_dict(self) -> dict:
        return {
            "events": self.events,
            "mean_ns": round(self.mean_ns, 1),
            "quantiles_ns": dict(self.quantiles_ns),
            "total_s": round(self.total_s, 3),
            "decisions": self.decisions,
            "children_submitted": self.children_submitted,
            "fills": self.fills,
        }


def _quantiles(samples: list[int]) -> dict[str, int]:
    """Nearest-rank percentiles (exact order statistics; max exact)."""
    s = sorted(samples)
    n = len(s)
    out: dict[str, int] = {}
    for q in PERCENTILES:
        rank = max(1, min(n, math.ceil(q * n)))
        label = f"p{q * 100:g}"
        out[label] = s[rank - 1]
    out["max"] = s[-1]
    return out


def measure_loop_latency(
    prep: PreparedSymbol, work_dir: str | Path, *, limit: int | None = None
) -> LoopLatency:
    """Drive the engine over the prepared symbol's feed, timing each event.

    ``limit`` stops after that many events (the whole slice when ``None``).
    Traces go to ``work_dir/traces.jsonl`` (the engine needs a sink; the
    file is a by-product).  The first event is included: there is no
    warm-up pass, because the loop is stateful (a second pass would see
    a different book) — the p50 is unaffected and the p99 of 65,000 events
    is not moved by one cold call.
    """
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    samples: list[int] = []
    t_start = time.perf_counter()
    with JsonlTraceSink(work / "traces.jsonl") as sink:
        engine = MvpEngine(prep.cfg, prep.feed, sink, realism=prep.realism)
        clock = time.perf_counter_ns
        on_event = engine.on_event
        for ev in prep.feed.events:
            t0 = clock()
            on_event(ev)
            samples.append(clock() - t0)
            if limit is not None and len(samples) >= limit:
                break
        engine.finish()
    total = time.perf_counter() - t_start
    if not samples:
        raise ValueError("no events in the feed")
    counters = engine.counters
    return LoopLatency(
        events=len(samples),
        mean_ns=sum(samples) / len(samples),
        quantiles_ns=_quantiles(samples),
        total_s=total,
        decisions=int(counters.decisions),
        children_submitted=int(counters.child_orders_submitted),
        fills=int(counters.fills),
    )
