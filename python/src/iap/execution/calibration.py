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
- ``markouts`` — the passive side of every EXECUTE is a maker fill at the
  resting price; its adverse selection ``s * (m_f - m_h)`` in bps at each
  horizon (``iap.tca.markout`` definitions and undefined-window rules,
  gated timeline).
- ``impact`` — aggressor sweeps (the EXECUTEs of one timestamp and side):
  the mid move in the aggressor's direction ``h`` later in bps, and a
  through-the-origin least-squares slope in bps per % of ADV (ADV proxy =
  executed quantity per session day in the data), which
  :func:`apply_calibration` can hand to the simulator's linear impact rule.

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


@dataclass
class _Episode:
    price: int
    start_ts: int
    start_qty: int


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
) -> dict:
    """Estimate a calibration document from one ordered event stream.

    ``tick_sizes``: instrument_id -> tick size (instruments not listed are
    skipped). ``latency_tail``: venue_id -> a :func:`parametric_latency`
    table used where the stream has no receive-stamp latency. ``source`` is
    recorded verbatim (dataset id, sessions, ...).
    """
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
                touch_orders.append((iid, joined))
        status = cons.apply(ev)
        if status != ApplyStatus.APPLIED:
            continue
        t = ev.exchange_ts
        if joined is not None:
            orders[okey] = joined
        elif et == EventType.EXECUTE:
            o = orders.get(okey)
            if o is not None and not o.done:
                q = min(ev.qty, o.remaining)
                o.executed += q
                o.remaining -= q
                if o.first_exec_ts is None:
                    o.first_exec_ts = t
                if o.remaining <= 0:
                    o.done = True
                maker_fills.append((iid, t, o.side, o.price * tick_sizes[iid]))
                aggr = 1 - o.side
                sweeps[(iid, t, aggr)] = sweeps.get((iid, t, aggr), 0) + q
                exec_qty[iid] = exec_qty.get(iid, 0) + q
                days.add(t // _NS_PER_DAY)
        elif et == EventType.CANCEL:
            o = orders.get(okey)
            if o is not None:
                o.done = True
                o.remaining = 0
        elif et == EventType.MODIFY:
            o = orders.get(okey)
            if o is not None and not o.done:
                if ev.qty <= 0:
                    o.done = True
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
            "source": "feed_receive_minus_exchange",
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


def _read_events(path: str) -> list[MarketEvent]:
    from iap.core.codec import read_iap1, read_jsonl

    return read_jsonl(path) if path.endswith((".jsonl", ".json")) else read_iap1(path)


def main(argv: Iterable[str] | None = None) -> int:
    """``python -m iap.execution.calibration --events F [F ...] --tick IID=TICK ... --out C``."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--events", nargs="+", required=True, help="IAP1 or JSONL event files")
    ap.add_argument("--tick", nargs="+", required=True, help="instrument_id=tick_size")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(list(argv) if argv is not None else None)
    ticks = {int(k): float(v) for k, v in (t.split("=", 1) for t in args.tick)}
    events: list[MarketEvent] = []
    for p in args.events:
        events.extend(_read_events(p))
    doc = estimate_calibration(events, ticks, source={"files": [Path(p).name for p in args.events]})
    write_calibration(doc, args.out)
    print(f"wrote {args.out}: {doc['n_events']} events, {len(doc['instruments'])} instruments")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
