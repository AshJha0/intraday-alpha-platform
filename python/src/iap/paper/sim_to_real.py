"""Recorded-vs-replayed fill comparison (REL-05, Level 1; Level 2 NOT validated).

Replays the SAME parents of a paper recording on the SAME recorded market
events through :class:`iap.execution.replay.ExecutionReplay` (read-only use)
and compares, child by child, what the recording says was filled with what the
replay says would have been.

What this validates (Level 1): that the record -> replay -> compare chain is
deterministic, internally consistent and traceable signal -> risk -> routing ->
fills -> TCA.  What it does NOT validate (Level 2): the fill model against real
venue fills.  A paper run's "fills" come from the paper environment the caller
drives (typically the same simulator family), so agreement here is agreement of
two models and is NEVER evidence about live execution.  The report states
``"level2": "not validated"`` and the limitations list verbatim.

Matching key: ``(parent_id, decision_ts, k)`` where ``decision_ts`` is the event
time the child was decided at and ``k`` the 0-based occurrence among children of
the same parent sharing that ``decision_ts`` (in send order).  Children present
on one side only are counted, never dropped.

Error definitions (both only where the child filled on BOTH sides)
  * ``price_err_bps`` = side_sign * (recorded vwap - replayed vwap) / decision
    mid * 1e4; positive = the recording paid more / received less than the replay.
  * ``fill_frac_err`` = recorded filled / qty - replayed filled / qty (over ALL
    matched children, so a recorded-only fill shows as a positive error).
Strata: venue, order type and liquidity regime (``tight`` = spread <= 1 tick at
the decision, ``wide`` otherwise, ``unknown`` = no two-sided quote yet).
Standard errors of the mean cluster on the parent (children of one parent are
not independent); a stratum with fewer than ``MIN_N`` children or ``MIN_G``
parents is flagged ``underpowered`` and its tails are not interpretable.
"""

from __future__ import annotations

import bisect
import math
from collections import defaultdict
from collections.abc import Callable, Sequence
from typing import Any

import numpy as np

from iap.contracts.types import ExecStatus
from iap.core.events import EventType, MarketEvent
from iap.execution.config import ExecConfig
from iap.execution.replay import ExecutionReplay
from iap.execution.types import Fill, Liquidity
from iap.orderbook.book import ConsolidatedBook
from iap.paper.recorder import FILL, SEND, Recording, reports_from_recording
from iap.tca.fills import MAKER, TAKER, MarketTimeline, stamp_fill
from iap.tca.fills import ParentOrder as TcaParent
from iap.tca.tca import order_tca

MIN_N = 30
MIN_G = 10

LEVEL2 = "not validated"
LIMITATIONS = (
    "Level 2: not validated. No real venue fills are involved; this compares a paper "
    "recording with a replay of the same tape and is not empirical validation of the live fill model.",
    "Both sides are simulations of the same event stream; agreement shows consistency, not realism.",
    "Per-stratum samples are small and children of one parent are correlated; "
    "strata flagged underpowered carry no tail information.",
    "Clock: recorded receive times are mapped from a monotonic clock once at start; the mapping "
    "uncertainty excludes the host's offset from true UTC.",
    "Fill latency error uses the recorded fill's exchange_ts and is skipped when it is absent.",
)


def _quotes(events: Sequence[MarketEvent], tick: dict[int, float]):
    """Per-instrument (ts list, (bid, ask) list) and a TCA MarketTimeline."""
    books: dict[int, ConsolidatedBook] = {}
    ts: dict[int, list[int]] = defaultdict(list)
    qs: dict[int, list[tuple[int, int]]] = defaultdict(list)
    tls: dict[int, MarketTimeline] = defaultdict(MarketTimeline)
    for ev in events:
        iid = ev.instrument_id
        book = books.setdefault(iid, ConsolidatedBook(iid))
        book.apply(ev)
        tk = tick.get(iid, 1.0)
        if ev.event_type == EventType.TRADE:
            tls[iid].add_trade(ev.exchange_ts, ev.price_ticks * tk, ev.qty)
        bb, ba = book.best_bid(), book.best_ask()
        if bb is None or ba is None or ba[0] < bb[0]:
            continue
        ts[iid].append(ev.exchange_ts)
        qs[iid].append((bb[0], ba[0]))
        tls[iid].append_state_pinned(ev.exchange_ts, bb[0] * tk, ba[0] * tk, bb[1], ba[1])
    return ts, qs, tls


def _at(ts: dict[int, list[int]], qs: dict, iid: int, t: int):
    i = bisect.bisect_right(ts.get(iid, []), t) - 1
    return None if i < 0 else qs[iid][i]


def _stats(errs: Sequence[float], clusters: Sequence[int]) -> dict[str, Any]:
    n = len(errs)
    g = len(set(clusters))
    out: dict[str, Any] = {"n": n, "n_parents": g, "underpowered": n < MIN_N or g < MIN_G}
    if n == 0:
        return out
    a = np.asarray(errs, dtype=float)
    m = float(a.mean())
    out.update(
        {
            "bias": m,
            "sd": float(a.std(ddof=1)) if n > 1 else None,
            "median": float(np.median(a)),
            "p05": float(np.quantile(a, 0.05)),
            "p95": float(np.quantile(a, 0.95)),
            "abs_p95": float(np.quantile(np.abs(a), 0.95)),
            "abs_p99": float(np.quantile(np.abs(a), 0.99)),
            "abs_max": float(np.abs(a).max()),
        }
    )
    if g >= 2:
        sums: dict[int, float] = defaultdict(float)
        for e, c in zip(a, clusters, strict=True):
            sums[c] += e - m
        se = math.sqrt(g / (g - 1) * sum(v * v for v in sums.values())) / n
        out["se_cluster_parent"] = se
    else:
        out["se_cluster_parent"] = None
    return out


def _vwap(fs: Sequence[Fill | dict]) -> float:
    q = sum(f["qty"] if isinstance(f, dict) else f.qty for f in fs)
    p = sum(
        (f["price_ticks"] if isinstance(f, dict) else f.price_ticks)
        * (f["qty"] if isinstance(f, dict) else f.qty)
        for f in fs
    )
    return p / q


def compare(
    rec: Recording,
    config: ExecConfig,
    *,
    regime_of: Callable[[float], str] | None = None,
) -> dict[str, Any]:
    """Replay ``rec`` and compare per child; see the module docstring.

    ``regime_of`` maps the spread in ticks at the decision to a regime label
    (default ``tight`` for <= 1 tick, else ``wide``).
    """
    regime_of = regime_of or (lambda s: "tight" if s <= 1 else "wide")
    tick = {i: s.tick_size for i, s in config.instruments.items()}
    parents = list(rec.parents.values())
    result = ExecutionReplay(config, parents).run(rec.events)
    ts_q, qs_q, timelines = _quotes(rec.events, tick)

    # recorded side
    sends = [o for o in rec.orders if o["kind"] == SEND]
    fills_rec: dict[int, list[dict]] = defaultdict(list)
    for o in rec.orders:
        if o["kind"] == FILL:
            fills_rec[o["child_id"]].append(o)
    rec_by_key: dict[tuple, dict] = {}
    seen: dict[tuple, int] = defaultdict(int)
    for o in sends:
        k0 = (o["parent_id"], o["decision_ts"])
        rec_by_key[(*k0, seen[k0])] = o
        seen[k0] += 1
    # replay side
    fills_rep: dict[int, list[Fill]] = defaultdict(list)
    for f in result.fills:
        fills_rep[f.order_id].append(f)
    rep_by_key: dict[tuple, Any] = {}
    seen = defaultdict(int)
    for pid in sorted(result.children):
        for c in result.children[pid]:
            k0 = (pid, c.decision_ts)
            rep_by_key[(*k0, seen[k0])] = c
            seen[k0] += 1

    rows: list[dict[str, Any]] = []
    outcome = {"both": 0, "recorded_only": 0, "replay_only": 0, "neither": 0}
    for key in sorted(set(rec_by_key) & set(rep_by_key)):
        o, c = rec_by_key[key], rep_by_key[key]
        fr, fp = fills_rec.get(o["child_id"], []), fills_rep.get(c.order_id, [])
        qr, qp = sum(f["qty"] for f in fr), sum(f.qty for f in fp)
        kind = (
            "both" if qr and qp else "recorded_only" if qr else "replay_only" if qp else "neither"
        )
        outcome[kind] += 1
        quote = _at(ts_q, qs_q, o["instrument_id"], o["decision_ts"])
        regime = "unknown" if quote is None else regime_of(quote[1] - quote[0])
        row: dict[str, Any] = {
            "parent_id": key[0],
            "decision_ts": key[1],
            "k": key[2],
            "venue_id": o["venue_id"],
            "order_type": o["order_type"],
            "regime": regime,
            "qty": o["qty"],
            "recorded_qty": qr,
            "replayed_qty": qp,
            "fill_frac_err": (qr - qp) / o["qty"],
            "price_err_bps": None,
            "first_fill_latency_err_ns": None,
        }
        if qr and qp and quote is not None:
            sign = 1 if o["side"] == 0 else -1
            mid = (quote[0] + quote[1]) / 2.0
            row["price_err_bps"] = 1e4 * sign * (_vwap(fr) - _vwap(fp)) / mid
            ex = [f["exchange_ts"] for f in fr if f["exchange_ts"] > 0]
            if ex:
                row["first_fill_latency_err_ns"] = (min(ex) - o["decision_ts"]) - (
                    min(f.ts for f in fp) - c.decision_ts
                )
        rows.append(row)

    def strat(field: str, value_key: str) -> dict[str, Any]:
        groups: dict[Any, list[dict]] = defaultdict(list)
        for r in rows:
            groups[r[field]].append(r)
        out = {}
        for gk in sorted(groups, key=str):
            rs = groups[gk]
            pe = [
                (r["price_err_bps"], r["parent_id"]) for r in rs if r["price_err_bps"] is not None
            ]
            out[str(gk)] = {
                "price_err_bps": _stats([e for e, _ in pe], [p for _, p in pe]),
                "fill_frac_err": _stats(
                    [r["fill_frac_err"] for r in rs], [r["parent_id"] for r in rs]
                ),
            }
        return out

    pe_all = [(r["price_err_bps"], r["parent_id"]) for r in rows if r["price_err_bps"] is not None]
    report: dict[str, Any] = {
        "schema": "iap.sim_to_real",
        "version": 1,
        "level1": "deterministic replay validation (see checks)",
        "level2": LEVEL2,
        "limitations": list(LIMITATIONS),
        "clock": rec.manifest["clock"],
        "n_children_recorded": len(rec_by_key),
        "n_children_replayed": len(rep_by_key),
        "unmatched_recorded": len(set(rec_by_key) - set(rep_by_key)),
        "unmatched_replayed": len(set(rep_by_key) - set(rec_by_key)),
        "outcome": outcome,
        "overall": {
            "price_err_bps": _stats([e for e, _ in pe_all], [p for _, p in pe_all]),
            "fill_frac_err": _stats(
                [r["fill_frac_err"] for r in rows], [r["parent_id"] for r in rows]
            ),
        },
        "by_venue": strat("venue_id", "venue"),
        "by_order_type": strat("order_type", "type"),
        "by_regime": strat("regime", "regime"),
        "children": rows,
        "trace": _trace(rec, result, timelines, tick),
    }
    return report


def _trace(rec: Recording, result, timelines, tick) -> list[dict[str, Any]]:
    """signal -> risk -> routing -> fills -> TCA, one row per parent."""
    reports = reports_from_recording(rec)
    child_parent = {o["child_id"]: o["parent_id"] for o in rec.orders if o["kind"] == SEND}
    out = []
    for pid in sorted(rec.parents):
        p = rec.parents[pid]
        tr = rec.manifest["parents"][str(pid)]["trace"]
        mine = [r for r in reports if child_parent[r.order_id] == pid]
        row: dict[str, Any] = {
            "parent_id": pid,
            **tr,
            "children": sum(1 for v in child_parent.values() if v == pid),
            "recorded_filled": sum(
                r.filled_qty for r in mine if r.status in (ExecStatus.PARTIAL, ExecStatus.FILLED)
            ),
            "replayed_filled": result.parents[pid].filled_qty,
            "tca_recorded_is_bps": None,
            "tca_replayed_is_bps": None,
            "tca_note": "",
        }
        tl = timelines.get(p.instrument_id)
        tk = tick.get(p.instrument_id, 1.0)
        try:
            if tl is None:
                raise ValueError("no market states for the instrument")
            mine_fills = [
                o for o in rec.orders if o["kind"] == FILL and child_parent[o["child_id"]] == pid
            ]
            if any(o["exchange_ts"] <= 0 for o in mine_fills):
                raise ValueError("a recorded fill has no exchange_ts (event time)")
            tp = _tca_parent(p, pid)
            for o in sorted(mine_fills, key=lambda o: (o["exchange_ts"], o["seq"])):
                liq = MAKER if o["liquidity"] == "MAKER" else TAKER
                tp.fills.append(
                    stamp_fill(tl, o["exchange_ts"], o["price_ticks"] * tk, o["qty"], p.side, liq)
                )
            row["tca_recorded_is_bps"] = order_tca(tp, tl)["perold"]["total_is_bps"]  # type: ignore[index]
            rp = _tca_parent(p, pid)
            for f in sorted(
                (f for f in result.fills if f.parent_id == pid), key=lambda f: (f.ts, f.fill_id)
            ):
                liq = MAKER if f.liquidity == Liquidity.MAKER else TAKER
                rp.fills.append(stamp_fill(tl, f.ts, f.price_ticks * tk, f.qty, p.side, liq))
            row["tca_replayed_is_bps"] = order_tca(rp, tl)["perold"]["total_is_bps"]  # type: ignore[index]
        except ValueError as exc:
            row["tca_note"] = f"TCA not computed: {exc}"
        out.append(row)
    return out


def _tca_parent(p, pid: int) -> TcaParent:
    return TcaParent(
        order_id=pid,
        instrument_id=p.instrument_id,
        side=p.side,
        qty_target=p.qty,
        decision_ts=p.start_ts,
        arrival_ts=p.start_ts,
        end_ts=p.end_ts,
    )
