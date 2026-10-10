"""Trade-matched maker labels (v1.9 M3).

The pinned research label (:mod:`iap.labels.labels`) is the forward MID
return: it says where the price went, not whether a passive order would
have been filled or what that fill was worth. A maker strategy needs three
other targets, all measured on the replayed book with the FIFO execution
simulator (:class:`iap.execution.ExecutionSimulator`, queue position from
the displayed size ahead of us, rules 1-9):

- ``{side}_filled`` — a ``qty``-lot LIMIT posted at the touch of ``side``
  (``bid`` = buy at the best bid, ``ask`` = sell at the best ask) at the
  decision time, travelling the simulator's latency path and expiring
  ``ttl_ns`` after the decision, received at least one MAKER fill. (An
  order the moved book made marketable on arrival fills as a TAKER: it is
  reported in ``{side}_taker`` and is NOT a maker fill.)
- ``{side}_markout_{h}_bps`` — the markout of that first maker fill,
  ``1e4 * s * (m_h - p) / p`` (``iap.tca.markout`` definition: positive =
  good for us; ``m_h`` the mid at or before ``fill_ts + h`` on the gated
  timeline; NaN when the fill is absent or the markout undefined — a
  session end, halt, or no-quote gap inside the window).
- ``{side}_not_run_over`` — 1.0 when the order filled and its markout at
  ``run_over_horizon`` is ``>= 0`` (the mid did not move through our price
  against us: the half-spread was kept at least in part), 0.0 when it
  filled and was run over, or did not fill, NaN when it filled and the
  markout is undefined.

Also reported: ``{side}_post_ticks`` (the touch at decision time),
``{side}_queue_ahead`` (displayed quantity ahead at rest, the simulator's
``entry_ahead_qty``), ``{side}_fill_ts``.

Every decision posts both sides independently. All label orders of one call
share ONE simulator (one pass over the events): two label orders resting
at the same price queue behind each other (rule 4), which is why the
default ``qty`` is a single lot — the interaction is then at most a lot per
earlier open label order. The labels are causal in what they condition on
(the touch at decision time) and forward-looking in what they measure, like
every label; they must only be used as targets, never as features.

This module is new in v1.9 and leaves :mod:`iap.labels.labels` untouched.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
import pandas as pd

from iap.core.events import MarketEvent
from iap.execution.calibration import CALIBRATION_HORIZONS_NS, ExecCalibration
from iap.execution.config import ExecConfig
from iap.execution.simulator import ExecutionSimulator
from iap.execution.types import ChildOrder, Liquidity, OrderType
from iap.tca.markout import build_gated_timeline, markout_mid

SIDES = ("bid", "ask")


def maker_label_columns(horizons: Sequence[str] = tuple(CALIBRATION_HORIZONS_NS)) -> list[str]:
    """The column names :func:`maker_labels` returns, in order."""
    cols = ["exchange_ts"]
    for s in SIDES:
        cols += [
            f"{s}_post_ticks",
            f"{s}_queue_ahead",
            f"{s}_filled",
            f"{s}_taker",
            f"{s}_fill_ts",
        ]
        cols += [f"{s}_markout_{h}_bps" for h in horizons]
        cols.append(f"{s}_not_run_over")
    return cols


def maker_labels(
    events: Sequence[MarketEvent],
    decision_ts: Sequence[int] | np.ndarray,
    *,
    instrument_id: int,
    exec_config: ExecConfig,
    venue_id: int | None = None,
    ttl_ns: int = 1_000_000_000,
    qty: int = 1,
    horizons_ns: Mapping[str, int] | None = None,
    run_over_horizon: str = "1s",
    calibration: ExecCalibration | None = None,
) -> pd.DataFrame:
    """Maker labels for each decision timestamp (module docstring).

    ``events`` is one ordered event stream (needed twice: the simulator pass
    and the markout timeline). ``venue_id`` defaults to the venue of the
    instrument's first event. A decision at ``t`` sees every event with
    ``exchange_ts <= t``. Rows are returned in ``decision_ts`` order.
    """
    hz = dict(CALIBRATION_HORIZONS_NS if horizons_ns is None else horizons_ns)
    if run_over_horizon not in hz:
        raise ValueError(f"run_over_horizon {run_over_horizon!r} not in horizons {list(hz)}")
    if ttl_ns <= 0 or qty <= 0:
        raise ValueError("ttl_ns and qty must be positive")
    dts = np.asarray(decision_ts, dtype=np.int64)
    if dts.size and np.any(np.diff(dts) < 0):
        raise ValueError("decision_ts must be non-decreasing")
    ins = exec_config.instrument(instrument_id)
    if venue_id is None:
        venue_id = next((e.venue_id for e in events if e.instrument_id == instrument_id), None)
        if venue_id is None:
            raise ValueError(f"no event for instrument {instrument_id}")

    n = int(dts.size)
    post = np.zeros((2, n), dtype=np.int64)
    order_of: dict[int, tuple[int, int]] = {}  # order_id -> (side, row)
    sim = ExecutionSimulator(exec_config, calibration)
    r = 0

    def decide_until(t_next: int | None) -> None:
        nonlocal r
        while r < n and (t_next is None or dts[r] < t_next):
            book = sim.venue_book(instrument_id, venue_id)
            open_ = sim.venue_open(book)
            for side in (0, 1):
                best = None
                if open_:
                    best = book.best_bid() if side == 0 else book.best_ask()
                if best is None:
                    continue
                oid = sim.submit(
                    ChildOrder(
                        instrument_id=instrument_id,
                        venue_id=venue_id,
                        side=side,
                        type=OrderType.LIMIT,
                        qty=qty,
                        limit_ticks=best[0],
                        decision_ts=int(dts[r]),
                        expire_ts=int(dts[r]) + ttl_ns,
                    )
                )
                post[side, r] = best[0]
                order_of[oid] = (side, r)
            r += 1

    for ev in events:
        if ev.instrument_id != instrument_id:
            continue
        decide_until(ev.exchange_ts)
        sim.on_event(ev)
    decide_until(None)
    sim.cancel_all()

    filled = np.zeros((2, n), dtype=bool)
    taker = np.zeros((2, n), dtype=bool)
    fill_ts = np.full((2, n), -1, dtype=np.int64)
    fill_px = np.full((2, n), np.nan)
    for f in sim.fills:
        key = order_of.get(f.order_id)
        if key is None:
            continue
        side, row = key
        if f.liquidity == Liquidity.TAKER:
            taker[side, row] = True
            continue
        if not filled[side, row]:
            filled[side, row] = True
            fill_ts[side, row] = f.ts
            fill_px[side, row] = f.price_ticks * ins.tick_size
    ahead = np.zeros((2, n), dtype=np.int64)
    for oid, (side, row) in order_of.items():
        ahead[side, row] = sim.orders[oid].entry_ahead_qty

    tl = build_gated_timeline(events, instrument_id, ins.tick_size)
    out: dict[str, np.ndarray] = {"exchange_ts": dts}
    for side, name in enumerate(SIDES):
        s = 1.0 if side == 0 else -1.0
        marks: dict[str, np.ndarray] = {h: np.full(n, np.nan) for h in hz}
        for row in np.flatnonzero(filled[side]):
            p = fill_px[side, row]
            for h, h_ns in hz.items():
                m_h = markout_mid(tl, int(fill_ts[side, row]), h_ns)
                if m_h is not None:
                    marks[h][row] = 1e4 * s * (m_h - p) / p
        ro = marks[run_over_horizon]
        nro = np.where(
            filled[side], np.where(np.isfinite(ro), (ro >= 0).astype(float), np.nan), 0.0
        )
        out[f"{name}_post_ticks"] = post[side]
        out[f"{name}_queue_ahead"] = ahead[side]
        out[f"{name}_filled"] = filled[side]
        out[f"{name}_taker"] = taker[side]
        out[f"{name}_fill_ts"] = fill_ts[side]
        for h in hz:
            out[f"{name}_markout_{h}_bps"] = marks[h]
        out[f"{name}_not_run_over"] = nro
    return pd.DataFrame(out, columns=maker_label_columns(tuple(hz)))
