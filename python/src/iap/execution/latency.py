"""Declared feed latency and the latency sensitivity every execution number carries (v2.0 stage 2 D4).

Defect D-06: the execution replay's market-data delay (``md_delay_ns``) defaulted
to zero, so the replayed decision saw the event the instant the exchange
stamped it, and ``receive_ts == exchange_ts`` on every ingested real row
(the feeds carry exchange time only).  Nothing here measures a latency: the
real feeds do not allow one (``docs/REAL_DATA.md`` section 11, the IEX capture
offset of ``docs/EXECUTION_QUALITY.md`` section 10).  What this module does is
make the assumption **declared** instead of silent:

* :func:`declared_feed_delay` reads the one table,
  ``configs/reference/venue_latency.json`` (``feed_delay``): the default delay
  in nanoseconds, where it comes from, and the ``receive_ts`` relationship per
  route (``itch50``, ``iex-deep-plus`` / ``iex-deep``, ``iex-capture``,
  ``synthetic``).  :class:`~iap.execution.replay.ExecutionReplay` uses it when
  the caller passes ``md_delay_ns=None`` (the default) and records which it
  used; passing ``0`` is still allowed, and recorded as explicit.
* :data:`SENSITIVITY_STEPS_NS` are the four decision-to-arrival latencies of
  plan section 5.9 (0.5 s, 1 s, 2 s, 5 s): :func:`latency_sensitivity`
  evaluates any number-producing callable at each step beside its base value,
  and :func:`replay_sensitivity` does it for a replay (one fresh
  ``ExecutionReplay`` per step, same events, same seed), reporting per parent
  the filled quantity, average price and total cost at each step.

The table's ``engine_decision_to_arrival_ns`` is the engine's one fill-price
convention (E0 section 9, C8: 500 ms); :mod:`iap.execution.sim` asserts its
constant equals it, so the two documents cannot drift apart.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "SENSITIVITY_STEPS_NS",
    "VENUE_LATENCY_PATH",
    "FeedDelay",
    "declared_feed_delay",
    "latency_sensitivity",
    "replay_sensitivity",
    "step_label",
]

VENUE_LATENCY_PATH = (
    Path(__file__).resolve().parents[4] / "configs" / "reference" / "venue_latency.json"
)

#: plan section 5.9: every execution number is reported at these decision-to-arrival latencies
SENSITIVITY_STEPS_NS: tuple[int, ...] = (
    500_000_000,
    1_000_000_000,
    2_000_000_000,
    5_000_000_000,
)


def step_label(ns: int) -> str:
    """``500000000 -> "0.5s"``, ``2000000000 -> "2s"``."""
    s = ns / 1e9
    return f"{s:g}s"


@dataclass(frozen=True, slots=True)
class FeedDelay:
    """The declared market-data delay of one route and its ``receive_ts`` statement."""

    ns: int
    route: str
    source: str  #: where the number comes from (the table, or "explicit" when a caller passed one)
    receive_ts: str  #: how ``receive_ts`` relates to ``exchange_ts`` on this route
    label: str = "assumed"  #: never "measured" for a real feed

    def as_dict(self) -> dict[str, Any]:
        return {
            "md_delay_ns": self.ns,
            "route": self.route,
            "source": self.source,
            "receive_ts": self.receive_ts,
            "label": self.label,
        }


def _table(path: Path | None = None) -> dict:
    doc = json.loads(Path(path or VENUE_LATENCY_PATH).read_text(encoding="utf-8"))
    fd = doc.get("feed_delay")
    if not isinstance(fd, dict):
        raise ValueError(f"{path or VENUE_LATENCY_PATH}: no 'feed_delay' block")
    default = fd.get("default_ns")
    if not isinstance(default, int) or isinstance(default, bool) or default <= 0:
        raise ValueError("feed_delay.default_ns must be a positive integer: a silent zero is D-06")
    if not isinstance(fd.get("routes"), dict) or not fd["routes"]:
        raise ValueError("feed_delay.routes must name every ingest route")
    for name, row in fd["routes"].items():
        if not isinstance(row, dict) or "receive_ts" not in row:
            raise ValueError(f"feed_delay.routes[{name!r}] must state its receive_ts relationship")
    return doc


def declared_feed_delay(route: str | None = None, path: Path | None = None) -> FeedDelay:
    """The declared delay for ``route`` (default: the table's ``default`` route).

    A route may carry its own ``default_ns``; otherwise the table default applies.
    Unknown routes raise: a new ingest format must declare its relationship first.
    """
    doc = _table(path)
    fd = doc["feed_delay"]
    name = route or fd.get("default_route") or "default"
    routes = fd["routes"]
    if name not in routes:
        raise KeyError(f"feed_delay has no route {name!r}; declared: {sorted(routes)}")
    row = routes[name]
    ns = int(row.get("default_ns", fd["default_ns"]))
    if ns <= 0:
        raise ValueError(f"feed_delay.routes[{name!r}].default_ns must be positive")
    return FeedDelay(
        ns=ns,
        route=name,
        source=f"declared: configs/reference/venue_latency.json feed_delay (route {name})",
        receive_ts=str(row["receive_ts"]),
        label=str(row.get("label", fd.get("label", "assumed"))),
    )


def latency_sensitivity(
    number: Callable[[int], Any],
    base_ns: int,
    steps: Sequence[int] = SENSITIVITY_STEPS_NS,
) -> dict[str, Any]:
    """``number(latency_ns)`` at the base latency and at every step of plan 5.9.

    Returns ``{"base_ns", "base", "steps": {"0.5s": value, ...}, "steps_ns": {...}}``;
    ``number`` may return a float, a dict or any record: the caller decides what
    an execution number is, this function only guarantees the four steps are there.
    """
    if base_ns < 0:
        raise ValueError("base_ns >= 0")
    return {
        "base_ns": int(base_ns),
        "base": number(int(base_ns)),
        "steps_ns": {step_label(s): int(s) for s in steps},
        "steps": {step_label(s): number(int(s)) for s in steps},
    }


def replay_sensitivity(
    build: Callable[[int], Any],
    events: Callable[[], Iterable[Any]] | Sequence[Any],
    base_ns: int | None = None,
    steps: Sequence[int] = SENSITIVITY_STEPS_NS,
) -> dict[str, Any]:
    """The replay's execution numbers at the declared delay and at the four steps.

    ``build(md_delay_ns)`` returns a fresh, un-run :class:`~iap.execution.replay.ExecutionReplay`
    (one-shot objects: one per step); ``events`` is a sequence replayed at every
    step or a zero-argument callable returning a fresh iterable.  Per parent:
    ``filled_qty``, ``avg_price``, ``total_cost``, ``fees``, ``rebates``,
    ``impact``, ``unfilled_qty``, plus the fill count.
    """
    if base_ns is None:
        base_ns = declared_feed_delay().ns

    def _events():
        return events() if callable(events) else list(events)

    def number(ns: int) -> dict[str, Any]:
        res = build(ns).run(_events())
        return {
            "md_delay_ns": ns,
            "fills": len(res.fills),
            "parents": {
                str(pid): {
                    "filled_qty": r.filled_qty,
                    "unfilled_qty": r.unfilled_qty,
                    "avg_price": r.avg_price,
                    "fees": r.fees,
                    "rebates": r.rebates,
                    "impact": r.impact,
                    "total_cost": r.total_cost,
                }
                for pid, r in sorted(res.parents.items())
            },
        }

    out = latency_sensitivity(number, base_ns, steps)
    out["statement"] = (
        "decision-to-arrival latency stepped at the plan 5.9 grid; the base is the "
        "declared feed delay (assumed, not measured); fills are re-simulated, not scaled"
    )
    return out


def sensitivity_table(
    sens: Mapping[str, Any], key: str = "total_cost"
) -> dict[str, dict[str, Any]]:
    """``{parent_id: {"base": x, "0.5s": x, ...}}`` for one per-parent field of a :func:`replay_sensitivity` result."""
    out: dict[str, dict[str, Any]] = {}
    for pid, row in sens["base"]["parents"].items():
        out[pid] = {"base": row[key]}
        for label, rec in sens["steps"].items():
            out[pid][label] = rec["parents"][pid][key]
    return out
