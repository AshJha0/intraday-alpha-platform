"""Two-venue SOR on real books: merged replay + per-venue calibration (v1.17 B8).

    python -m iap.research.two_venue calibrate \\
        --nasdaq-dir data/real/r7_20251208 --nasdaq-dir data/real/r7_20251209 \\
        --iex-dir data/real/iex/iex_20251208 --iex-dir data/real/iex/iex_20251209 \\
        [--iex-clock exchange] [--symbols AAPL,MSFT,QQQ] [--out research/execution/b8]

The paired router-vs-pinned evaluation runs through the B1 benchmark
(:mod:`iap.research.execution_benchmark`) with ``--second-data-dir`` and
the ``sor`` / ``pin_nasdaq`` / ``pin_iex`` policies (docs/API_TRADING.md
section 2.18, docs/EXECUTION_QUALITY.md section 8).

**Merged stream.** One Nasdaq ITCH dataset and the IEX dataset of the same
day become ONE consolidated event stream (:func:`merge_events`). Venue ids
come from the events themselves (XNAS 101, IEXG 102); the IEX instruments
are re-keyed onto the Nasdaq dataset's instrument ids by symbol (an IEX
symbol absent from the Nasdaq universe is dropped and counted). The order
is ``(exchange_ts, venue_id)`` with each stream's own order kept inside a
tie (a stable two-way merge; no stream is ever re-sorted), and the merged
events are renumbered ``event_id = 1..N``.

**Clock alignment rule.** Both clocks must be nanoseconds since the
session's New York midnight on the exchange's UTC-disciplined clock.
Nasdaq ITCH stamps already are. The IEX HIST capture host stamps packets in
TAI, :data:`iap.execution.fill_hazard.IEX_TAI_OFFSET_S` = 37 s ahead of
UTC; the IEX reader (:mod:`iap.marketdata.iex`) takes ``exchange_ts`` from
the messages' own UTC send times, so an ingested IEX dataset is ALREADY on
the exchange clock and ``iex_clock="exchange"`` (the default) applies no
shift. ``iex_clock="capture"`` is for a stream stamped by the capture clock:
``exchange_ts`` and ``receive_ts`` are shifted by ``-37 s`` before the
merge. Nothing else is adjusted (no latency, no skew estimate).

**Feed latency and cross-venue skew (v1.18 REL-04, X-D).** The IEX reader keeps the
capture host's packet timestamp as ``IexReader.receive_ts`` and summarises
``receive_ts - send_ts`` in ``IexReader.feed_latency``; that quantity contains an
unidentified clock offset between the capture host and IEX (and says nothing about
the Nasdaq stamps of another host), so any latency or skew figure derived from it
must carry :data:`SKEW_QUALIFICATION` and is never a trading advantage or an
order-entry latency.  The replay does not use it: venue legs stay as configured.

**Per-venue calibration** (:func:`calibrate_day`, :func:`combine_days`):

- *fill probability*: the queue-depletion hazard of each venue's own book
  (:func:`iap.execution.calibration.estimate_calibration` on that venue's
  events, ``depletion_split=True``) with the X-4 split: the EXECUTION rate
  is the one that fills a resting order (``exec_hazard_per_s``), the cancel
  rate is reported beside it. Days pool by summing exposure seconds and
  depletions per (side, queue bucket). ``p_fill(h) = 1 - exp(-rate * h)``.
- *toxicity*: every displayed execution on the venue is a maker fill of the
  resting order; its markout at each horizon against the CONSOLIDATED mid
  (:func:`iap.tca.markout.fill_measures`, gated timeline of the merged
  stream) is one row, clustered by day (:func:`cluster_mean`, CR1).
  ``toxicity_bps[h] = -mean markout`` (positive = the maker loses).

One document per venue, schema ``iap.venue_calibration/v1``
(``venue_calibration_<VENUE>.json``); :func:`router_from_calibrations`
turns them into the ``sor_v2`` router the benchmark's ``sor`` policy uses.

Python reference, written line-for-line portable (plain loops, no RNG);
the C++ port is a later F7 item (PLATFORM_CONVENTIONS section 11).
"""

from __future__ import annotations

import argparse
import heapq
import json
import math
import sys
from collections.abc import Container, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from iap.core.codec import iter_iap1, iter_jsonl, read_iap1, read_jsonl
from iap.core.events import EventType, MarketEvent
from iap.execution.calibration import DEFAULT_QUEUE_EDGES, estimate_calibration, queue_bucket
from iap.execution.fill_hazard import IEX_TAI_OFFSET_S
from iap.execution.sor_v2 import CostAwareRouter, FillProb, RouterOptions, hazard_fill_prob
from iap.execution.types import VenueSpec
from iap.execution.venues_model import VenueModel
from iap.tca.markout import (
    DEFAULT_HORIZONS_NS,
    MAKER,
    MarkoutFill,
    build_gated_timeline,
    fill_measures,
)
from iap.tca.markout_decomposition import cluster_mean

SEC = 1_000_000_000
SCHEMA = "iap.venue_calibration/v1"
NASDAQ_VENUE = "XNAS"
IEX_VENUE = "IEXG"
IEX_CLOCKS = ("exchange", "capture")
DEFAULT_OUT = Path("research/execution/b8")
DEFAULT_TOX_HORIZONS: tuple[str, ...] = ("1s", "5s", "30s")
DEFAULT_FILL_HORIZON_S = 1.0
#: v1.18 REL-04 (X-D): the qualification every feed-latency / cross-venue skew figure carries.
SKEW_QUALIFICATION = (
    "feed latency = capture-host packet time minus IEX send time: an unidentified clock offset "
    "is included, only within-capture differences are interpretable, cross-venue skew against "
    "Nasdaq stamps from another host is not identified, and none of it is a trading advantage "
    "or an order-entry latency (iap.marketdata.iex.CAPTURE_TS_PROVENANCE)"
)

ALIGNMENT_RULE = (
    "both streams on ns since the session's New York midnight, exchange (UTC) clock; "
    "Nasdaq ITCH as stamped; IEX: exchange_ts from the messages' own UTC send times "
    "(iex_clock='exchange', no shift) or, for a capture-clock (TAI) stream, "
    f"exchange_ts/receive_ts - {IEX_TAI_OFFSET_S} s (iex_clock='capture'); merge order "
    "(exchange_ts, venue_id), each stream's own order kept inside a tie"
)
#: benchmark policy -> venue name it is pinned to (``None`` = the sor_v2 router)
ROUTE_POLICIES: dict[str, str | None] = {
    "sor": None,
    #: v1.17.1: the same router, marketable children swept across both books'
    #: displayed depth (``plan_sweep``, all-in rank) instead of one venue each
    "sor_sweep": None,
    "pin_nasdaq": NASDAQ_VENUE,
    "pin_iex": IEX_VENUE,
}


# ------------------------------------------------------------- alignment --


def clock_shift_ns(iex_clock: str) -> int:
    """Nanoseconds ADDED to every IEX timestamp before the merge."""
    if iex_clock not in IEX_CLOCKS:
        raise ValueError(f"iex_clock must be one of {IEX_CLOCKS}, got {iex_clock!r}")
    return -IEX_TAI_OFFSET_S * SEC if iex_clock == "capture" else 0


def instrument_map(primary: Mapping[str, int], second: Mapping[str, int]) -> dict[int, int]:
    """``second instrument_id -> primary instrument_id`` by symbol."""
    out: dict[int, int] = {}
    for sym in sorted(second):
        if sym in primary:
            out[int(second[sym])] = int(primary[sym])
    return out


def symbols_of(dataset_dir: Path) -> dict[str, int]:
    """``symbol -> instrument_id`` of a dataset's ``configs/instruments/instruments.json``."""
    path = Path(dataset_dir) / "configs" / "instruments" / "instruments.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(r.get("symbol", r["instrument_id"])): int(r["instrument_id"])
        for r in doc["instruments"]
    }


def merge_events(
    primary: Iterable[MarketEvent],
    second: Iterable[MarketEvent],
    *,
    id_map: Mapping[int, int] | None = None,
    iex_clock: str = "exchange",
    keep: Container[int] | None = None,
    counters: dict[str, int] | None = None,
) -> list[MarketEvent]:
    """One consolidated stream from a Nasdaq (``primary``) and an IEX
    (``second``) stream of the same day (module docstring: the rule).

    ``id_map`` re-keys the second stream's instrument ids (None = keep);
    unmapped second-stream events are dropped and counted in
    ``counters["second_unmapped"]``. ``keep`` (primary instrument ids)
    drops every other instrument of both streams before the merge.

    The input events are CONSUMED: they are updated in place (``event_id``
    renumbered, the IEX ``instrument_id`` re-keyed and its clock shifted)
    and returned in the merged list, so a real day (about 23 M Nasdaq
    events) is held once, not twice."""
    shift = clock_shift_ns(iex_clock)
    cnt = counters if counters is not None else {}
    for k in ("primary", "second", "second_unmapped", "dropped_by_keep"):
        cnt.setdefault(k, 0)

    def first() -> Iterator[MarketEvent]:
        for ev in primary:
            if keep is not None and ev.instrument_id not in keep:
                cnt["dropped_by_keep"] += 1
                continue
            cnt["primary"] += 1
            yield ev

    def other() -> Iterator[MarketEvent]:
        for ev in second:
            iid = ev.instrument_id
            if id_map is not None:
                if iid not in id_map:
                    cnt["second_unmapped"] += 1
                    continue
                iid = id_map[iid]
            if keep is not None and iid not in keep:
                cnt["dropped_by_keep"] += 1
                continue
            cnt["second"] += 1
            ev.instrument_id = iid
            ev.exchange_ts += shift
            ev.receive_ts += shift
            yield ev

    out: list[MarketEvent] = []
    n = 0
    for ev in heapq.merge(first(), other(), key=lambda e: (e.exchange_ts, e.venue_id)):
        n += 1
        ev.event_id = n
        out.append(ev)
    return out


def remap_instruments(
    events: Iterable[MarketEvent], id_map: Mapping[int, int]
) -> list[MarketEvent]:
    """``events`` with instrument ids re-keyed by ``id_map``; unmapped ids are dropped."""
    out: list[MarketEvent] = []
    for ev in events:
        if ev.instrument_id in id_map:
            d = ev.to_dict()
            d["instrument_id"] = id_map[ev.instrument_id]
            out.append(MarketEvent(**d))
    return out


def session_file(dataset_dir: Path, label: str | None = None) -> Path:
    """The (single, or ``label``-matching) ``eq_*.normalized.{iap1,jsonl}`` of a dataset."""
    files: list[Path] = []
    for sub in (Path(dataset_dir) / "normalized", Path(dataset_dir)):
        files = sorted(sub.glob("eq_*.normalized.iap1")) or sorted(
            sub.glob("eq_*.normalized.jsonl")
        )
        if files:
            break
    if label is not None:
        files = [p for p in files if label in p.name]
    if not files:
        raise SystemExit(
            f"no eq_*.normalized.{{iap1,jsonl}} under {dataset_dir} (or its normalized/)"
            + ("" if label is None else f" for session {label}")
            + "; the B8 replay needs the normalized events of BOTH venues"
        )
    return files[0]


def read_events(path: Path, instruments: Container[int] | None = None) -> list[MarketEvent]:
    """The session's events; with ``instruments`` the file is STREAMED and
    only those instrument ids are kept (a real day never exists in full)."""
    if instruments is None:
        return read_iap1(path) if path.suffix == ".iap1" else read_jsonl(path)
    it = iter_iap1(path) if path.suffix == ".iap1" else iter_jsonl(path)
    return [ev for ev in it if ev.instrument_id in instruments]


def load_merged_session(
    nasdaq_dir: Path,
    iex_dir: Path,
    label: str | None = None,
    *,
    iex_clock: str = "exchange",
    max_events: int = 0,
    symbols: Iterable[str] = (),
    counters: dict[str, int] | None = None,
) -> list[MarketEvent]:
    """The merged stream of one overlap day (``max_events`` caps EACH input;
    ``symbols`` keeps only those instruments)."""
    from iap.research.holdout_registry import guard_session_label

    for _d in (nasdaq_dir, iex_dir):  # v1.18 REL-02b: content-hash registry
        guard_session_label(_d, label, "two_venue")
    prim = symbols_of(nasdaq_dir)
    id_map = instrument_map(prim, symbols_of(iex_dir))
    want = set(symbols)
    keep = {iid for sym, iid in prim.items() if sym in want} if want else None
    keep_iex = None if keep is None else {k for k, v in id_map.items() if v in keep}
    a = read_events(session_file(nasdaq_dir, label), keep)
    b = read_events(session_file(iex_dir, label), keep_iex)
    if max_events > 0:
        a, b = a[:max_events], b[:max_events]
    return merge_events(
        a,
        b,
        id_map=id_map,
        iex_clock=iex_clock,
        keep=keep,
        counters=counters,
    )


# ----------------------------------------------------------- calibration --


def maker_markout_rows(
    events: Sequence[MarketEvent],
    tick_sizes: Mapping[int, float],
    *,
    horizons: Sequence[str] = DEFAULT_TOX_HORIZONS,
    cluster: str,
) -> list[dict[str, object]]:
    """One row per displayed execution: ``venue_id``, ``instrument_id``,
    ``cluster``, ``qty`` and ``markout_bps[h]`` of the resting (maker) order
    against the consolidated mid (``None`` where undefined)."""
    hz = {h: DEFAULT_HORIZONS_NS[h] for h in horizons}
    resting: dict[tuple[int, int, int], tuple[int, int]] = {}  # (venue, iid, oid) -> side, px
    per_iid: dict[int, list[MarketEvent]] = {}
    execs: list[tuple[int, int, int, int, int]] = []  # iid, venue, ts, side, px, ...
    qtys: list[int] = []
    for ev in events:
        if ev.instrument_id not in tick_sizes:
            continue
        per_iid.setdefault(ev.instrument_id, []).append(ev)
        key = (ev.venue_id, ev.instrument_id, ev.order_id)
        et = ev.event_type
        if et == EventType.ADD:
            resting[key] = (ev.side, ev.price_ticks)
        elif et == EventType.MODIFY:
            if key in resting:
                resting[key] = (resting[key][0], ev.price_ticks or resting[key][1])
        elif et == EventType.EXECUTE:
            r = resting.get(key)
            side = r[0] if r is not None else ev.side
            px = ev.price_ticks if ev.price_ticks > 0 else (r[1] if r is not None else 0)
            if px > 0:
                execs.append((ev.instrument_id, ev.venue_id, ev.exchange_ts, side, px))
                qtys.append(ev.qty)
    timelines = {
        iid: build_gated_timeline(evs, iid, tick_sizes[iid]) for iid, evs in sorted(per_iid.items())
    }
    rows: list[dict[str, object]] = []
    for (iid, vid, ts, side, px), qty in zip(execs, qtys, strict=True):
        price = px * tick_sizes[iid]
        f = MarkoutFill(ts=ts, price=price, qty=qty, side=side, liquidity=MAKER, venue_id=vid)
        mk: dict[str, float | None] = {}
        for name, h in hz.items():
            x = fill_measures(f, timelines[iid], h)
            mk[name] = None if x is None else 1e4 * x["markout"] / price
        rows.append(
            {
                "venue_id": vid,
                "instrument_id": iid,
                "cluster": cluster,
                "qty": qty,
                "markout_bps": mk,
            }
        )
    return rows


def calibrate_day(
    events: Sequence[MarketEvent],
    tick_sizes: Mapping[int, float],
    *,
    label: str,
    horizons: Sequence[str] = DEFAULT_TOX_HORIZONS,
) -> dict[str, object]:
    """One day of the merged stream -> ``{"venues": {vid: calibration doc},
    "markouts": rows}`` (the inputs :func:`combine_days` pools)."""
    by_venue: dict[int, list[MarketEvent]] = {}
    for ev in events:
        by_venue.setdefault(ev.venue_id, []).append(ev)
    docs = {
        vid: estimate_calibration(
            evs, tick_sizes, depletion_split=True, source={"session": label, "venue_id": vid}
        )
        for vid, evs in sorted(by_venue.items())
    }
    return {
        "label": label,
        "venues": docs,
        "markouts": maker_markout_rows(events, tick_sizes, horizons=horizons, cluster=label),
    }


def _pool_depletion(docs: Sequence[Mapping]) -> dict[str, dict[str, dict[str, Any]]]:
    acc: dict[tuple[str, str], list[float]] = {}  # (side, bucket) -> secs, dep, exec, cancel
    for doc in docs:
        qd = doc.get("queue_depletion") or {}
        for side in ("bid", "ask"):
            for b, cell in sorted((qd.get(side) or {}).items()):
                if not cell:
                    continue
                a = acc.setdefault((side, b), [0.0, 0.0, 0.0, 0.0])
                a[0] += float(cell.get("exposure_s") or 0.0)
                a[1] += float(cell.get("depletions") or 0)
                a[2] += float(cell.get("exec_depletions") or 0)
                a[3] += float(cell.get("cancel_depletions") or 0)
    out: dict[str, dict[str, dict[str, Any]]] = {"bid": {}, "ask": {}}
    for (side, b), (secs, dep, dx, dc) in sorted(acc.items()):
        out[side][b] = {
            "exposure_s": secs,
            "depletions": int(dep),
            "exec_depletions": int(dx),
            "cancel_depletions": int(dc),
            "hazard_per_s": dep / secs if secs > 0 else None,
            "exec_hazard_per_s": dx / secs if secs > 0 else None,
            "cancel_hazard_per_s": dc / secs if secs > 0 else None,
        }
    return out


def combine_days(
    days: Sequence[Mapping[str, Any]],
    venues: Mapping[int, VenueSpec],
    *,
    horizons: Sequence[str] = DEFAULT_TOX_HORIZONS,
    fill_horizon_s: float = DEFAULT_FILL_HORIZON_S,
    queue_edges: Sequence[int] = DEFAULT_QUEUE_EDGES,
    source: Mapping[str, object] | None = None,
) -> dict[int, dict[str, object]]:
    """Per venue: the ``iap.venue_calibration/v1`` document (module docstring)."""
    vids = sorted({int(v) for d in days for v in d["venues"]})
    labels = [str(d["label"]) for d in days]
    out: dict[int, dict[str, object]] = {}
    for vid in vids:
        qd = _pool_depletion([d["venues"][vid] for d in days if vid in d["venues"]])
        secs = 0.0
        dx = 0.0
        for side in ("bid", "ask"):
            for cell in qd[side].values():
                secs += float(cell["exposure_s"])
                dx += float(cell["exec_depletions"])
        rate = dx / secs if secs > 0 else None
        p_touch = None if rate is None else 1.0 - math.exp(-rate * fill_horizon_s)
        rows = [r for d in days for r in d["markouts"] if r["venue_id"] == vid]
        tox: dict[str, object] = {}
        tox_bps: dict[str, float] = {}
        for h in horizons:
            vals: list[float] = []
            cl: list[str] = []
            for r in rows:
                v = r["markout_bps"][h]
                if v is not None:
                    vals.append(float(v))
                    cl.append(str(r["cluster"]))
            stats: dict[str, Any] = cluster_mean(vals, cl)
            tox[h] = stats
            if stats["mean"] is not None:
                tox_bps[h] = -float(stats["mean"])
        spec = venues.get(vid)
        out[vid] = {
            "schema": SCHEMA,
            "item": "v1.17 B8",
            "venue_id": vid,
            "venue": spec.name if spec is not None else str(vid),
            "sessions": labels,
            "source": dict(source or {}),
            "alignment": ALIGNMENT_RULE,
            "fill_probability": {
                "model": "queue-depletion hazard, X-4 execution split (exec_hazard_per_s)",
                "horizon_s": fill_horizon_s,
                "queue_edges": list(queue_edges),
                "exec_hazard_per_s": rate,
                "p_fill_touch": p_touch,
                "queue_depletion": qd,
            },
            "toxicity": {
                "definition": "-(maker markout bps vs the consolidated mid), day-clustered CR1",
                "n_fills": len(rows),
                "markout_bps": tox,
                "toxicity_bps": tox_bps,
            },
        }
    return out


def save_calibrations(docs: Mapping[int, Mapping[str, object]], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for _vid, doc in sorted(docs.items()):
        p = out_dir / f"venue_calibration_{doc['venue']}.json"
        p.write_text(
            json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
        )
        paths.append(p)
    return paths


def load_calibrations(path: Path) -> dict[int, dict]:
    """``venue_id -> document`` from a directory of ``venue_calibration_*.json``."""
    out: dict[int, dict] = {}
    for p in sorted(Path(path).glob("venue_calibration_*.json")):
        doc = json.loads(p.read_text(encoding="utf-8"))
        if doc.get("schema") != SCHEMA:
            raise SystemExit(f"{p}: not an {SCHEMA} document")
        out[int(doc["venue_id"])] = doc
    if not out:
        raise SystemExit(f"no venue_calibration_*.json under {path}")
    return out


def router_from_calibrations(
    venues: Mapping[int, VenueSpec],
    docs: Mapping[int, Mapping] | None,
    *,
    tox_horizon: str = "1s",
    fill_horizon_s: float = DEFAULT_FILL_HORIZON_S,
    sor_options=None,
    sweep_rank: str = "price",
    sweep_levels: int = 10,
) -> tuple[CostAwareRouter, FillProb | None]:
    """The ``sor`` policy's router: one :class:`VenueModel` per lit venue
    (``p_fill_touch`` and ``toxicity_bps`` from its calibration; the
    VenueModel defaults without one) and, when calibrated, the queue-aware
    ``fill_prob`` = :func:`hazard_fill_prob` over each venue's EXECUTION
    depletion hazard (X-4). ``sweep_rank`` / ``sweep_levels`` set the
    router's :meth:`~CostAwareRouter.plan_sweep` (``sor_sweep`` passes
    ``"all_in"`` and the walk depth); the defaults leave ``sor`` unchanged."""
    models: dict[int, VenueModel] = {}
    for vid, spec in sorted(venues.items()):
        if not spec.is_lit:
            continue
        doc = (docs or {}).get(vid)
        if doc is None:
            models[vid] = VenueModel(spec=spec)
            continue
        p = doc["fill_probability"]["p_fill_touch"]
        models[vid] = VenueModel(
            spec=spec,
            p_fill_touch=0.5 if p is None else float(p),
            toxicity_bps={k: float(v) for k, v in sorted(doc["toxicity"]["toxicity_bps"].items())},
        )
    router = CostAwareRouter(
        models,
        RouterOptions(tox_horizon=tox_horizon, sweep_rank=sweep_rank, sweep_levels=sweep_levels),
        sor_options,
    )
    if not docs:
        return router, None
    edges = {
        vid: tuple(doc["fill_probability"]["queue_edges"]) for vid, doc in sorted(docs.items())
    }

    def exec_hazard(vid: int, side: int, q: int) -> float | None:
        doc = docs.get(vid)
        if doc is None:
            return None
        cells = doc["fill_probability"]["queue_depletion"]["bid" if side == 0 else "ask"]
        cell = cells.get(queue_bucket(q, edges[vid]))
        if not cell or cell.get("exec_hazard_per_s") is None:
            return None
        return float(cell["exec_hazard_per_s"])

    return router, hazard_fill_prob(exec_hazard, fill_horizon_s)


# ------------------------------------------------------------------ CLI --


def _tick_sizes(dataset_dir: Path) -> dict[int, float]:
    path = Path(dataset_dir) / "configs" / "instruments" / "instruments.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    return {int(r["instrument_id"]): float(r["tick_size"]) for r in doc["instruments"]}


def _label(dataset_dir: Path) -> str:
    return session_file(dataset_dir).name.split(".")[0].removeprefix("eq_")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m iap.research.two_venue", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("calibrate", help="per-venue fill probability + toxicity documents")
    c.add_argument("--nasdaq-dir", type=Path, action="append", required=True)
    c.add_argument("--iex-dir", type=Path, action="append", required=True)
    c.add_argument("--iex-clock", choices=IEX_CLOCKS, default="exchange")
    c.add_argument("--symbols", default="", help="comma list (default: every shared symbol)")
    c.add_argument("--horizons", default=",".join(DEFAULT_TOX_HORIZONS))
    c.add_argument("--fill-horizon-s", type=float, default=DEFAULT_FILL_HORIZON_S)
    c.add_argument("--venues", type=Path, default=None, help="venues.json naming both venues")
    c.add_argument("--max-events", type=int, default=0, help="per input stream (smoke runs)")
    c.add_argument("--out", type=Path, default=DEFAULT_OUT)
    a = ap.parse_args(argv)
    from iap.execution.config import load_venues

    if len(a.nasdaq_dir) != len(a.iex_dir):
        raise SystemExit("give one --iex-dir per --nasdaq-dir (same day, same order)")
    horizons = tuple(h for h in a.horizons.split(",") if h)
    want = {s for s in a.symbols.split(",") if s}
    venues: dict[int, VenueSpec] = {}
    if a.venues is not None:
        venues = dict(load_venues(a.venues))
    days = []
    for nd, xd in zip(a.nasdaq_dir, a.iex_dir, strict=True):
        label = _label(nd)
        if _label(xd) != label:
            raise SystemExit(f"{nd} is {label} but {xd} is {_label(xd)}: not the same day")
        if a.venues is None:
            for d in (nd, xd):
                venues.update(load_venues(Path(d) / "configs" / "venues" / "venues.json"))
        ticks = _tick_sizes(nd)
        if want:
            syms = symbols_of(nd)
            ticks = {iid: t for iid, t in ticks.items() if any(syms.get(s) == iid for s in want)}
        cnt: dict[str, int] = {}
        evs = load_merged_session(
            nd,
            xd,
            label,
            iex_clock=a.iex_clock,
            max_events=a.max_events,
            symbols=sorted(want),
            counters=cnt,
        )
        print(f"{label}: {len(evs):,} merged events {cnt}", file=sys.stderr)
        days.append(calibrate_day(evs, ticks, label=label, horizons=horizons))
    docs = combine_days(
        days,
        venues,
        horizons=horizons,
        fill_horizon_s=a.fill_horizon_s,
        source={
            "nasdaq_dirs": [str(p) for p in a.nasdaq_dir],
            "iex_dirs": [str(p) for p in a.iex_dir],
            "iex_clock": a.iex_clock,
            "max_events": a.max_events,
        },
    )
    for p in save_calibrations(docs, a.out):
        print(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
