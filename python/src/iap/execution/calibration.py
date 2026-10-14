"""Execution-simulator calibration from normalized event data (v1.9 M1).

The simulator's latency, fees and impact are synthetic config values
(``configs/venues/venues.json``, ``configs/execution/execution.json``).
This module estimates the quantities a MAKER backtest depends on from the
platform's own normalized ``MarketEvent`` stream (the ITCH / LOBSTER ingest
output, or the synthetic generator) and writes them as a versioned JSON
document the simulator and the maker backtest can consume:

- ``fill_rates`` — for every displayed ADD that joined (or set) the touch:
  share filled at all, share filled in full, filled / added quantity and
  the time to the first execution, overall and per queue-ahead bucket at
  entry (the displayed size already at the level). Orders still open at the
  end of the stream are right-censored: counted in ``n_open`` and excluded
  from the rates.
- ``queue_depletion`` — touch episodes per side (the best price on one side
  stays the same): hazard ``lambda = depletions / exposure seconds`` where a
  depletion is the episode ending with the touch moving AWAY from the
  spread (bid down / ask up, or the side emptying), per starting
  queue-size bucket, with ``p_deplete_1s = 1 - exp(-lambda)``.
- ``latency`` — per venue, quantiles of ``receive_ts - exchange_ts`` over
  events where it is positive (a FEED latency proxy; ITCH files carry no
  receive stamp, so on them this is absent and the venue config, or an
  explicit :func:`parametric_latency` tail, is what the simulator uses).

  **Feed latency is not order-entry latency.** ``receive_ts - exchange_ts``
  measures how late a MARKET-DATA message reaches us (venue matching engine
  -> feed handler). The simulator's rule-1 venue leg is the ORDER-ENTRY path
  (our gateway -> venue matching engine), a different wire with different
  queueing; the two are only loosely related. A table whose ``source`` is
  ``"feed_receive_minus_exchange"`` is therefore a proxy when used as the
  order leg: :class:`~iap.execution.simulator.ExecutionSimulator` warns
  (:class:`FeedLatencyAsOrderLegWarning`) when handed one. Order-entry
  latency proper comes from the venue config or a ``parametric_tail``.
- ``markouts`` — the passive side of every EXECUTE is a maker fill at the
  resting price; its adverse selection ``s * (m_f - m_h)`` in bps at each
  horizon (``iap.tca.markout`` definitions and undefined-window rules,
  gated timeline).
- ``impact`` — aggressor sweeps (the EXECUTEs of one timestamp and side):
  the mid move in the aggressor's direction ``h`` later in bps, and a
  through-the-origin least-squares slope in bps per % of ADV (ADV proxy =
  executed quantity per session day in the data), which
  :func:`apply_calibration` can hand to the simulator's linear impact rule.
- ``impact.residual`` (v1.16 P0-2, opt-in ``impact_fit=True``; absent
  otherwise so older documents are unchanged) — the RESIDUAL move after the
  depth walk: ``s * (m_h - m_post) / m0`` in bps, ``m_post`` the mid once
  every event of the sweep's timestamp is applied (the walk itself is
  already paid through fill prices), against participation in % of a TAPE
  ADV (displayed executions plus non-displayed TRADE prints that are not the
  print of the EXECUTE right before them). Per horizon: a linear and a
  square-root through-the-origin fit, each with an HC0 standard error, and a
  log-log power fit (exponent + SE) over participation quantile bins.
  :func:`apply_impact_fit` turns it into the simulator's parent rule 6b.

Nothing here changes the simulator's pinned rules or any cross-language
parity golden: :class:`ExecutionSimulator` uses a calibration only when one
is passed explicitly (``ExecutionSimulator(config, calibration=...)``), and
then only for the latency draw (still one SplitMix64 draw per submit or
cancel, mapped through the empirical quantile table instead of the uniform
jitter). With no calibration the synthetic config is the default.

Schema: ``{"schema": "iap.exec_calibration", "version": 1, ...}``; a reader
rejects any other schema/version (fail closed).
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from iap.core.events import EventType, MarketEvent
from iap.execution.config import ExecConfig
from iap.orderbook.book import SYNTHETIC_ID_BASE, ApplyStatus, ConsolidatedBook
from iap.tca.fills import MarketTimeline
from iap.tca.markout import build_gated_timeline, markout_mid

SCHEMA = "iap.exec_calibration"
VERSION = 1

#: Markout / impact horizons of a calibration (event-time ns), report order.
CALIBRATION_HORIZONS_NS: dict[str, int] = {
    "100ms": 100_000_000,
    "1s": 1_000_000_000,
    "10s": 10_000_000_000,
}

#: Upper edges (inclusive) of queue-size buckets, in displayed quantity.
DEFAULT_QUEUE_EDGES: tuple[int, ...] = (0, 100, 500, 2000)

#: Probability grid of a latency quantile table.
LATENCY_PROBS: tuple[float, ...] = (
    0.0,
    0.01,
    0.05,
    0.1,
    0.25,
    0.5,
    0.75,
    0.9,
    0.95,
    0.99,
    0.999,
    1.0,
)

#: Minimum samples before a latency table / a bucket statistic is reported.
MIN_SAMPLES = 20

_NS_PER_DAY = 86_400_000_000_000


#: v1.14 X-P2-9: the unit one maker markout observation stands for
MARKOUT_UNITS = ("message", "touch_order")


def queue_bucket(qty: int, edges: Sequence[int] = DEFAULT_QUEUE_EDGES) -> str:
    """Bucket label of a queue size: ``"0"``, ``"1-100"``, ..., ``">2000"``."""
    lo = 0
    for e in edges:
        if qty <= e:
            return str(e) if e == lo else f"{lo}-{e}"
        lo = e + 1
    return f">{edges[-1]}"


def _quantile(sorted_vals: Sequence[float], p: float) -> float:
    """Linear-interpolation quantile of an ascending sequence."""
    n = len(sorted_vals)
    if n == 1:
        return float(sorted_vals[0])
    pos = p * (n - 1)
    i = int(math.floor(pos))
    if i >= n - 1:
        return float(sorted_vals[-1])
    frac = pos - i
    return float(sorted_vals[i] + (sorted_vals[i + 1] - sorted_vals[i]) * frac)


def _mean_se(values: Sequence[float]) -> tuple[float | None, float | None]:
    n = len(values)
    if n == 0:
        return None, None
    m = math.fsum(values) / n
    if n < 2:
        return m, None
    var = math.fsum((v - m) ** 2 for v in values) / (n - 1)
    return m, math.sqrt(var / n)


def parametric_latency(
    mean_ns: int, jitter_ns: int, tail_prob: float = 0.01, tail_mult: float = 10.0
) -> dict[str, list]:
    """A latency quantile table with an explicit tail, for data without a
    receive stamp: the body is ``mean + U[0, jitter]`` (the venue config
    rule) and the top ``tail_prob`` of the mass stretches linearly up to
    ``tail_mult * (mean + jitter)``. Returns ``{"probs", "quantiles_ns"}``."""
    if mean_ns < 0 or jitter_ns < 0:
        raise ValueError("mean_ns and jitter_ns must be >= 0")
    if not 0.0 <= tail_prob < 1.0 or tail_mult < 1.0:
        raise ValueError("tail_prob must be in [0, 1) and tail_mult >= 1")
    body_hi = mean_ns + jitter_ns
    q: list[int] = []
    for p in LATENCY_PROBS:
        if p <= 1.0 - tail_prob or tail_prob == 0.0:
            body_p = p / (1.0 - tail_prob) if tail_prob < 1.0 else p
            q.append(int(round(mean_ns + min(body_p, 1.0) * jitter_ns)))
        else:
            tp = (p - (1.0 - tail_prob)) / tail_prob
            q.append(int(round(body_hi + tp * (tail_mult - 1.0) * body_hi)))
    return {"probs": list(LATENCY_PROBS), "quantiles_ns": q, "source": "parametric_tail"}


#: ``source`` of a latency table estimated from feed timestamps.
FEED_LATENCY_SOURCE = "feed_receive_minus_exchange"


class FeedLatencyAsOrderLegWarning(UserWarning):
    """A feed-derived latency table is being used as the order-entry leg
    (module docstring: feed latency is not order-entry latency)."""


@dataclass(frozen=True)
class LatencyTable:
    """Empirical latency distribution: inverse CDF by linear interpolation."""

    probs: tuple[float, ...]
    quantiles_ns: tuple[int, ...]

    def __post_init__(self) -> None:
        if len(self.probs) != len(self.quantiles_ns) or len(self.probs) < 2:
            raise ValueError("latency table needs >= 2 matching probs/quantiles")
        if self.probs[0] != 0.0 or self.probs[-1] != 1.0:
            raise ValueError("latency probs must span [0, 1]")
        if any(b <= a for a, b in zip(self.probs, self.probs[1:], strict=False)):
            raise ValueError("latency probs must be strictly ascending")
        if any(b < a for a, b in zip(self.quantiles_ns, self.quantiles_ns[1:], strict=False)):
            raise ValueError("latency quantiles must be non-decreasing")
        if self.quantiles_ns[0] < 0:
            raise ValueError("latency quantiles must be >= 0")

    def sample(self, u: float) -> int:
        """Latency at uniform draw ``u`` in [0, 1)."""
        j = bisect.bisect_right(self.probs, u) - 1
        j = min(max(j, 0), len(self.probs) - 2)
        p0, p1 = self.probs[j], self.probs[j + 1]
        q0, q1 = self.quantiles_ns[j], self.quantiles_ns[j + 1]
        return int(q0 + (q1 - q0) * (u - p0) / (p1 - p0))


@dataclass(frozen=True)
class ExecCalibration:
    """A loaded calibration document (see module docstring)."""

    doc: dict = field(repr=False)
    latency: Mapping[int, LatencyTable] = field(default_factory=dict)
    impact_coeff_bps_per_pct_adv: float | None = None

    @classmethod
    def from_dict(cls, doc: Mapping) -> ExecCalibration:
        if doc.get("schema") != SCHEMA or doc.get("version") != VERSION:
            raise ValueError(
                f"not an {SCHEMA} v{VERSION} document "
                f"(schema={doc.get('schema')!r}, version={doc.get('version')!r})"
            )
        lat: dict[int, LatencyTable] = {}
        for vid, row in (doc.get("latency") or {}).items():
            if row is None:
                continue
            lat[int(vid)] = LatencyTable(
                tuple(float(p) for p in row["probs"]),
                tuple(int(q) for q in row["quantiles_ns"]),
            )
        coeff = (doc.get("impact") or {}).get("slope_bps_per_pct_adv")
        return cls(
            doc=dict(doc),
            latency=dict(sorted(lat.items())),
            impact_coeff_bps_per_pct_adv=None if coeff is None else float(coeff),
        )

    def latency_for(self, venue_id: int) -> LatencyTable | None:
        return self.latency.get(venue_id)

    def feed_derived_latency_venues(self) -> tuple[int, ...]:
        """Venues whose latency table was estimated from FEED timestamps
        (``source == FEED_LATENCY_SOURCE``): a proxy, not order entry."""
        rows = self.doc.get("latency") or {}
        return tuple(
            sorted(
                int(v)
                for v, row in rows.items()
                if row is not None and row.get("source") == FEED_LATENCY_SOURCE
            )
        )

    def adverse_selection_bps(self, horizon: str, instrument_id: int | None = None) -> float | None:
        """Mean maker adverse selection at ``horizon`` (bps, positive = bad);
        per instrument when given and available, else pooled."""
        mk = self.doc.get("markouts") or {}
        if instrument_id is not None:
            cell = (mk.get("by_instrument") or {}).get(str(instrument_id), {}).get(horizon)
            if cell and cell.get("mean_bps") is not None:
                return float(cell["mean_bps"])
        cell = (mk.get("all") or {}).get(horizon)
        if cell and cell.get("mean_bps") is not None:
            return float(cell["mean_bps"])
        return None

    def queue_depletion_hazard(self, side: int, queue_qty: int) -> float | None:
        """Depletions per second of a touch level of ``queue_qty`` on ``side``."""
        cells = (self.doc.get("queue_depletion") or {}).get("bid" if side == 0 else "ask") or {}
        edges = tuple(self.doc.get("queue_edges") or DEFAULT_QUEUE_EDGES)
        cell = cells.get(queue_bucket(queue_qty, edges))
        if not cell or cell.get("hazard_per_s") is None:
            return None
        return float(cell["hazard_per_s"])


def load_calibration(path: str | Path | None) -> ExecCalibration | None:
    """Read a calibration JSON; ``None`` -> ``None`` (synthetic config default)."""
    if path is None:
        return None
    with open(path, encoding="utf-8") as f:
        return ExecCalibration.from_dict(json.load(f))


def write_calibration(doc: Mapping, path: str | Path) -> None:
    """Write a calibration document (sorted keys, stable bytes)."""
    ExecCalibration.from_dict(doc)  # validate before writing
    Path(path).write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def apply_calibration(config: ExecConfig, calibration: ExecCalibration | None) -> ExecConfig:
    """``config`` with the calibrated impact coefficient (when estimated)."""
    if calibration is None or calibration.impact_coeff_bps_per_pct_adv is None:
        return config
    return replace(config, impact_coeff_bps_per_pct_adv=calibration.impact_coeff_bps_per_pct_adv)


# ------------------------------------------------------------- estimation


@dataclass
class _Order:
    side: int
    price: int
    added: int
    remaining: int
    add_ts: int
    ahead: int
    executed: int = 0
    first_exec_ts: int | None = None
    done: bool = False
    end_ts: int | None = None  #: when it left the book (v1.13 TTL cells)
    touch: bool = False  #: joined (or set) the touch (v1.14 X-P2-9)


@dataclass
class _Episode:
    price: int
    start_ts: int
    start_qty: int


def _p_fill_within(orders: Sequence[_Order], ttl_ns: int, last_ts: int) -> dict:
    """v1.13 (X-P2-8) TTL-censored P(any fill within ``ttl_ns``) of touch joins.

    Filled within the TTL -> 1; observed for at least the TTL without a fill
    (left the book later, or still open ``ttl_ns`` before the stream end) ->
    0; left the book unfilled before the TTL -> censored (excluded).
    """
    hit = n = 0
    for o in orders:
        if o.first_exec_ts is not None and o.first_exec_ts - o.add_ts <= ttl_ns:
            hit += 1
            n += 1
            continue
        end = o.end_ts if o.done and o.end_ts is not None else last_ts
        if end - o.add_ts >= ttl_ns:
            n += 1
    return {"n": n, "p": hit / n if n >= MIN_SAMPLES else None}


def _fill_cell(orders: Sequence[_Order]) -> dict:
    closed = [o for o in orders if o.done]
    n_open = len(orders) - len(closed)
    out: dict = {"n": len(closed), "n_open": n_open}
    if len(closed) < MIN_SAMPLES:
        out.update(
            p_any_fill=None, p_full_fill=None, qty_fill_rate=None, median_time_to_fill_ns=None
        )
        return out
    added = sum(o.added for o in closed)
    filled = sum(o.executed for o in closed)
    ttf = sorted(o.first_exec_ts - o.add_ts for o in closed if o.first_exec_ts is not None)
    out.update(
        p_any_fill=sum(1 for o in closed if o.executed > 0) / len(closed),
        p_full_fill=sum(1 for o in closed if o.executed >= o.added) / len(closed),
        qty_fill_rate=filled / added if added else None,
        median_time_to_fill_ns=int(_quantile(ttf, 0.5)) if ttf else None,
    )
    return out


def estimate_calibration(
    events: Sequence[MarketEvent],
    tick_sizes: Mapping[int, float],
    *,
    horizons_ns: Mapping[str, int] | None = None,
    queue_edges: Sequence[int] = DEFAULT_QUEUE_EDGES,
    latency_tail: Mapping[int, dict] | None = None,
    source: Mapping | None = None,
    fill_within_ns: Sequence[int] = (),
    markout_unit: str = "message",
    impact_fit: bool = False,
    impact_fit_bins: int = 10,
) -> dict:
    """Estimate a calibration document from one ordered event stream.

    ``fill_within_ns`` (v1.13 X-P2-8, opt-in; empty = the document is
    unchanged): for each TTL, ``fill_rates.all.p_fill_within[str(ttl)] =
    {"n", "p"}``, the TTL-censored touch fill probability
    (:func:`_p_fill_within`) the maker backtester's passive exit reads.

    ``markout_unit`` (v1.14 X-P2-9, opt-in): ``"message"`` (default, the
    document is unchanged) pools the passive side of every EXECUTE message
    equally, so one resting order hit by ten partial executions counts ten
    times; ``"touch_order"`` takes ONE maker markout per displayed order that
    joined (or set) the touch, at its first execution (the population the
    maker backtester's resting entry belongs to) and records
    ``markouts.unit = "touch_order"``.

    ``tick_sizes``: instrument_id -> tick size (instruments not listed are
    skipped). ``latency_tail``: venue_id -> a :func:`parametric_latency`
    table used where the stream has no receive-stamp latency. ``source`` is
    recorded verbatim (dataset id, sessions, ...).
    """
    if markout_unit not in MARKOUT_UNITS:
        raise ValueError(f"markout_unit must be one of {MARKOUT_UNITS}")
    per_touch_order = markout_unit == "touch_order"
    hz = dict(CALIBRATION_HORIZONS_NS if horizons_ns is None else horizons_ns)
    edges = tuple(queue_edges)
    books: dict[int, ConsolidatedBook] = {}
    orders: dict[tuple[int, int, int], _Order] = {}
    touch_orders: list[tuple[int, _Order]] = []
    episodes: dict[tuple[int, int, int], _Episode] = {}
    exposure: dict[tuple[int, str], list[float]] = {}  # (side, bucket) -> [secs, depletions]
    lat: dict[int, list[int]] = {}
    maker_fills: list[tuple[int, int, int, float]] = []  # (iid, ts, side, price)
    sweeps: dict[tuple[int, int, int], int] = {}  # (iid, ts, aggressor side) -> qty
    days: set[int] = set()
    exec_qty: dict[int, int] = {}
    tape_qty: dict[int, int] = {}  # exec_qty + non-displayed prints (impact_fit)
    prev_exec: dict[int, tuple[int, int]] = {}  # venue -> (ts, trade_id) of last EXECUTE
    n_events = 0

    def close_episode(key, ep: _Episode, t: int, depleted: bool) -> None:
        side = key[2]
        b = queue_bucket(ep.start_qty, edges)
        cell = exposure.setdefault((side, b), [0.0, 0.0])
        cell[0] += max(t - ep.start_ts, 0) / 1e9
        cell[1] += 1.0 if depleted else 0.0

    for ev in events:
        iid = ev.instrument_id
        if iid not in tick_sizes:
            continue
        n_events += 1
        d = ev.receive_ts - ev.exchange_ts
        if d > 0:
            lat.setdefault(ev.venue_id, []).append(d)
        cons = books.get(iid)
        if cons is None:
            cons = books[iid] = ConsolidatedBook(iid)
        pre = cons.books.get(ev.venue_id)
        et = ev.event_type
        okey = (iid, ev.venue_id, ev.order_id)
        joined: _Order | None = None
        if et == EventType.ADD and ev.order_id < SYNTHETIC_ID_BASE:
            best = None
            if pre is not None:
                best = pre.best_bid() if ev.side == 0 else pre.best_ask()
            at_or_better = best is None or (
                ev.price_ticks >= best[0] if ev.side == 0 else ev.price_ticks <= best[0]
            )
            ahead = pre.level_qty(ev.side, ev.price_ticks) if pre is not None else 0
            joined = _Order(ev.side, ev.price_ticks, ev.qty, ev.qty, ev.exchange_ts, ahead)
            if at_or_better:
                joined.touch = True
                touch_orders.append((iid, joined))
        if impact_fit and et == EventType.TRADE:
            last = prev_exec.get(ev.venue_id)
            own_print = (
                last is not None
                and last[0] == ev.exchange_ts
                and (last[1] == 0 or ev.trade_id == 0 or last[1] == ev.trade_id)
            )
            if not own_print:
                tape_qty[iid] = tape_qty.get(iid, 0) + ev.qty
                days.add(ev.exchange_ts // _NS_PER_DAY)
        if et != EventType.EXECUTE:
            prev_exec.pop(ev.venue_id, None)
        status = cons.apply(ev)
        if status != ApplyStatus.APPLIED:
            continue
        t = ev.exchange_ts
        if et == EventType.EXECUTE:
            prev_exec[ev.venue_id] = (t, ev.trade_id)
        if joined is not None:
            orders[okey] = joined
        elif et == EventType.EXECUTE:
            o = orders.get(okey)
            if o is not None and not o.done:
                q = min(ev.qty, o.remaining)
                o.executed += q
                o.remaining -= q
                first = o.first_exec_ts is None
                if first:
                    o.first_exec_ts = t
                if o.remaining <= 0:
                    o.done = True
                    o.end_ts = t
                if not per_touch_order or (first and o.touch):
                    maker_fills.append((iid, t, o.side, o.price * tick_sizes[iid]))
                aggr = 1 - o.side
                sweeps[(iid, t, aggr)] = sweeps.get((iid, t, aggr), 0) + q
                exec_qty[iid] = exec_qty.get(iid, 0) + q
                tape_qty[iid] = tape_qty.get(iid, 0) + q
                days.add(t // _NS_PER_DAY)
        elif et == EventType.CANCEL:
            o = orders.get(okey)
            if o is not None:
                o.done = True
                o.end_ts = t
                o.remaining = 0
        elif et == EventType.MODIFY:
            o = orders.get(okey)
            if o is not None and not o.done:
                if ev.qty <= 0:
                    o.done = True
                    o.end_ts = t
                    o.remaining = 0
                else:
                    o.remaining = ev.qty
        # touch episodes on the venue book after the event
        book = cons.books.get(ev.venue_id)
        for side in (0, 1):
            key = (iid, ev.venue_id, side)
            best = book.best_bid() if side == 0 else book.best_ask()
            ep = episodes.get(key)
            if ep is not None and (best is None or best[0] != ep.price):
                away = best is None or (best[0] < ep.price if side == 0 else best[0] > ep.price)
                close_episode(key, ep, t, away)
                ep = None
                del episodes[key]
            if ep is None and best is not None:
                episodes[key] = _Episode(best[0], t, best[1])

    last_ts = events[-1].exchange_ts if len(events) else 0
    for key, ep in episodes.items():  # right-censored exposure, no depletion
        close_episode(key, ep, last_ts, False)

    # fill rates
    by_bucket: dict[str, list[_Order]] = {}
    for _, o in touch_orders:
        by_bucket.setdefault(queue_bucket(o.ahead, edges), []).append(o)
    labels = [queue_bucket(e, edges) for e in edges] + [f">{edges[-1]}"]
    fill_rates = {
        "all": _fill_cell([o for _, o in touch_orders]),
        "by_queue_ahead": {b: _fill_cell(by_bucket[b]) for b in labels if b in by_bucket},
    }
    if fill_within_ns:
        fill_rates["all"]["p_fill_within"] = {
            str(int(x)): _p_fill_within([o for _, o in touch_orders], int(x), last_ts)
            for x in fill_within_ns
        }

    # queue depletion hazards
    qd: dict[str, dict] = {"bid": {}, "ask": {}}
    for (side, b), (secs, dep) in sorted(exposure.items()):
        qd["bid" if side == 0 else "ask"][b] = {
            "exposure_s": secs,
            "depletions": int(dep),
            "hazard_per_s": dep / secs if secs > 0 and dep >= 1 else (0.0 if secs > 0 else None),
            "p_deplete_1s": (1.0 - math.exp(-dep / secs)) if secs > 0 else None,
        }

    # latency
    latency: dict[str, dict | None] = {}
    for vid, vals in sorted(lat.items()):
        if len(vals) < MIN_SAMPLES:
            continue
        s = sorted(vals)
        latency[str(vid)] = {
            "probs": list(LATENCY_PROBS),
            "quantiles_ns": [int(round(_quantile(s, p))) for p in LATENCY_PROBS],
            "n": len(s),
            "source": FEED_LATENCY_SOURCE,
        }
    for vid, table in sorted((latency_tail or {}).items()):
        if str(vid) not in latency:
            latency[str(vid)] = dict(table)

    # markouts + impact on a gated timeline per instrument
    timelines: dict[int, MarketTimeline] = {
        iid: build_gated_timeline(events, iid, tick_sizes[iid]) for iid in sorted(books)
    }
    pooled: dict[str, list[float]] = {h: [] for h in hz}
    per_ins: dict[int, dict[str, list[float]]] = {}
    for iid, ts, side, price in maker_fills:
        tl = timelines[iid]
        i = tl.prevailing(ts - 1)
        if i is None or any(tl.ts[i] < g <= ts for g in tl.halts):
            continue
        m_f = tl.mid(i)
        s = 1.0 if side == 0 else -1.0
        for h, h_ns in hz.items():
            m_h = markout_mid(tl, ts, h_ns)
            if m_h is None:
                continue
            v = 1e4 * s * (m_f - m_h) / price
            pooled[h].append(v)
            per_ins.setdefault(iid, {k: [] for k in hz})[h].append(v)

    def mk_cell(vals: list[float]) -> dict:
        if len(vals) < MIN_SAMPLES:
            return {"n": len(vals), "mean_bps": None, "se_bps": None}
        m, se = _mean_se(vals)
        return {"n": len(vals), "mean_bps": m, "se_bps": se}

    markouts = {
        "definition": "maker adverse selection s*(m_f - m_h)/p in bps, positive = bad",
        "all": {h: mk_cell(v) for h, v in pooled.items()},
        "by_instrument": {
            str(iid): {h: mk_cell(v) for h, v in cells.items()}
            for iid, cells in sorted(per_ins.items())
        },
    }
    if per_touch_order:
        markouts["unit"] = "touch_order"

    n_days = max(len(days), 1)
    imp_x: list[float] = []
    imp_y: dict[str, list[float]] = {h: [] for h in hz}
    imp_y_xs: dict[str, list[float]] = {h: [] for h in hz}
    for (iid, ts, aggr), q in sorted(sweeps.items()):
        tl = timelines[iid]
        adv = exec_qty.get(iid, 0) / n_days
        if adv <= 0:
            continue
        i = tl.prevailing(ts - 1)
        if i is None or any(tl.ts[i] < g <= ts for g in tl.halts):
            continue
        m0 = tl.mid(i)
        s = 1.0 if aggr == 0 else -1.0
        pct = 100.0 * q / adv
        imp_x.append(pct)
        for h, h_ns in hz.items():
            m_h = markout_mid(tl, ts, h_ns)
            if m_h is None:
                continue
            imp_y[h].append(1e4 * s * (m_h - m0) / m0)
            imp_y_xs[h].append(pct)
    impact: dict = {"n_sweeps": len(imp_x), "adv_proxy_days": n_days, "by_horizon": {}}
    for h in hz:
        ys, xs = imp_y[h], imp_y_xs[h]
        m, se = _mean_se(ys) if len(ys) >= MIN_SAMPLES else (None, None)
        sxx = math.fsum(x * x for x in xs)
        slope = math.fsum(x * y for x, y in zip(xs, ys, strict=True)) / sxx if sxx > 0 else None
        impact["by_horizon"][h] = {
            "n": len(ys),
            "mean_bps": m,
            "se_bps": se,
            "slope_bps_per_pct_adv": slope if len(ys) >= MIN_SAMPLES else None,
        }
    ref_h = "1s" if "1s" in hz else next(iter(hz))
    impact["slope_bps_per_pct_adv"] = impact["by_horizon"][ref_h]["slope_bps_per_pct_adv"]
    impact["slope_horizon"] = ref_h
    if impact_fit:
        impact["residual"] = _residual_impact(
            sweeps, timelines, tape_qty, n_days, hz, ref_h, impact_fit_bins
        )

    return {
        "schema": SCHEMA,
        "version": VERSION,
        "source": dict(source or {}),
        "n_events": n_events,
        "instruments": sorted(int(i) for i in books),
        "horizons_ns": hz,
        "queue_edges": list(edges),
        "fill_rates": fill_rates,
        "queue_depletion": qd,
        "latency": latency,
        "markouts": markouts,
        "impact": impact,
    }


def _through_origin(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float] | None:
    """``y = b * x`` least squares: ``(b, HC0 se)``, or None when degenerate."""
    sxx = math.fsum(x * x for x in xs)
    if sxx <= 0:
        return None
    b = math.fsum(x * y for x, y in zip(xs, ys, strict=True)) / sxx
    meat = math.fsum((x * (y - b * x)) ** 2 for x, y in zip(xs, ys, strict=True))
    return b, math.sqrt(meat) / sxx


def _power_fit(xs: Sequence[float], ys: Sequence[float], bins: int) -> dict:
    """``log(mean y) = log c + k * log(mean x)`` over participation quantile
    bins with a positive mean move (OLS, classical SE of ``k``)."""
    pts = sorted(zip(xs, ys, strict=True))
    n = len(pts)
    pairs: list[tuple[float, float]] = []
    for b in range(bins):
        cell = pts[b * n // bins : (b + 1) * n // bins]
        if not cell:
            continue
        mx = math.fsum(p[0] for p in cell) / len(cell)
        my = math.fsum(p[1] for p in cell) / len(cell)
        if mx > 0 and my > 0:
            pairs.append((math.log(mx), math.log(my)))
    out: dict = {"bins_used": len(pairs), "exponent": None, "exponent_se": None, "coeff": None}
    if len(pairs) < 3:
        return out
    lx = [p[0] for p in pairs]
    ly = [p[1] for p in pairs]
    mx, my = math.fsum(lx) / len(lx), math.fsum(ly) / len(ly)
    sxx = math.fsum((x - mx) ** 2 for x in lx)
    if sxx <= 0:
        return out
    k = math.fsum((x - mx) * (y - my) for x, y in zip(lx, ly, strict=True)) / sxx
    a = my - k * mx
    rss = math.fsum((y - a - k * x) ** 2 for x, y in zip(lx, ly, strict=True))
    out.update(exponent=k, exponent_se=math.sqrt(rss / (len(pairs) - 2) / sxx), coeff=math.exp(a))
    return out


def _residual_impact(
    sweeps: Mapping[tuple[int, int, int], int],
    timelines: Mapping,
    tape_qty: Mapping[int, int],
    n_days: int,
    hz: Mapping[str, int],
    ref_h: str,
    bins: int,
) -> dict:
    """The ``impact.residual`` block (module docstring)."""
    xs: dict[str, list[float]] = {h: [] for h in hz}
    ys: dict[str, list[float]] = {h: [] for h in hz}
    n_sweeps = 0
    for (iid, ts, aggr), q in sorted(sweeps.items()):
        tl = timelines[iid]
        adv = tape_qty.get(iid, 0) / n_days
        i0 = tl.prevailing(ts - 1)
        i1 = tl.prevailing(ts)
        if adv <= 0 or i0 is None or i1 is None:
            continue
        if any(tl.ts[i0] < g <= ts for g in tl.halts):
            continue
        n_sweeps += 1
        m0, m_post = tl.mid(i0), tl.mid(i1)
        s = 1.0 if aggr == 0 else -1.0
        pct = 100.0 * q / adv
        for h, h_ns in hz.items():
            m_h = markout_mid(tl, ts, h_ns)
            if m_h is None:
                continue
            xs[h].append(pct)
            ys[h].append(1e4 * s * (m_h - m_post) / m0)
    by_h: dict[str, dict] = {}
    for h in hz:
        x, y = xs[h], ys[h]
        cell: dict = {"n": len(y), "linear": None, "sqrt": None, "power": None}
        if len(y) >= MIN_SAMPLES:
            lin = _through_origin(x, y)
            sq = _through_origin([math.sqrt(v) for v in x], y)
            if lin is not None:
                cell["linear"] = {"coeff_bps_per_pct_adv": lin[0], "se": lin[1]}
            if sq is not None:
                cell["sqrt"] = {"coeff_bps_per_sqrt_pct_adv": sq[0], "se": sq[1]}
            cell["power"] = _power_fit(x, y, bins)
        by_h[h] = cell
    return {
        "definition": "s*(m_h - m_post)/m0 in bps; m_post = mid after the sweep timestamp",
        "adv": "tape",
        "adv_days": n_days,
        "n_sweeps": n_sweeps,
        "horizon": ref_h,
        "by_horizon": by_h,
    }


#: ``apply_impact_fit`` shapes.
IMPACT_SHAPES = ("linear", "sqrt", "power")


def apply_impact_fit(
    config: ExecConfig,
    calibration: ExecCalibration,
    shape: str = "sqrt",
    horizon: str | None = None,
) -> ExecConfig:
    """``config`` switched to the parent impact rule 6b with the residual fit.

    ``shape``: ``"linear"`` (exponent 1), ``"sqrt"`` (0.5) or ``"power"``
    (the fitted exponent, which must lie in (0, 1]). Raises ValueError when
    the calibration has no residual fit, the chosen cell is empty or the
    fitted coefficient is not positive."""
    if shape not in IMPACT_SHAPES:
        raise ValueError(f"shape must be one of {IMPACT_SHAPES}, got {shape!r}")
    res = (calibration.doc.get("impact") or {}).get("residual")
    if not res:
        raise ValueError("calibration has no impact.residual block (estimate with impact_fit)")
    h = horizon or res["horizon"]
    fit = (res["by_horizon"].get(h) or {}).get(shape)
    if not fit:
        raise ValueError(f"no {shape} impact fit at horizon {h!r}")
    if shape == "linear":
        coeff, k = fit["coeff_bps_per_pct_adv"], 1.0
    elif shape == "sqrt":
        coeff, k = fit["coeff_bps_per_sqrt_pct_adv"], 0.5
    else:
        coeff, k = fit["coeff"], fit["exponent"]
        if coeff is None or k is None:
            raise ValueError("power fit has too few positive bins")
    if coeff <= 0:
        raise ValueError(f"fitted {shape} impact coefficient {coeff!r} is not positive")
    return replace(
        config,
        impact_model="parent",
        impact_coeff_bps_per_pct_adv=float(coeff),
        impact_exponent=float(k),
    )


def _read_events(path: str) -> list[MarketEvent]:
    from iap.core.codec import read_iap1, read_jsonl

    return read_jsonl(path) if path.endswith((".jsonl", ".json")) else read_iap1(path)


def main(argv: Iterable[str] | None = None) -> int:
    """``python -m iap.execution.calibration --events F [F ...] --tick IID=TICK ... --out C``."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--events", nargs="+", required=True, help="IAP1 or JSONL event files")
    ap.add_argument("--tick", nargs="+", required=True, help="instrument_id=tick_size")
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--markout-unit",
        choices=MARKOUT_UNITS,
        default="message",
        help="maker AS observation unit (v1.14 X-P2-9)",
    )
    ap.add_argument(
        "--impact-fit",
        action="store_true",
        help="add the residual-move impact fit (impact.residual, v1.16 P0-2)",
    )
    args = ap.parse_args(list(argv) if argv is not None else None)
    ticks = {int(k): float(v) for k, v in (t.split("=", 1) for t in args.tick)}
    events: list[MarketEvent] = []
    for p in args.events:
        events.extend(_read_events(p))
    doc = estimate_calibration(
        events,
        ticks,
        source={"files": [Path(p).name for p in args.events]},
        markout_unit=args.markout_unit,
        impact_fit=args.impact_fit,
    )
    write_calibration(doc, args.out)
    print(f"wrote {args.out}: {doc['n_events']} events, {len(doc['instruments'])} instruments")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
