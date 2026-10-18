"""15 m / 30 m / 1 h labels on a session-local calendar grid (v1.18 REL-07).

:func:`iap.labels.labels_v2.compute_labels_v2` is the label contract (the
session-close truncation rule, the ``truncated`` flag and the effective
horizon); this module only chooses its ANCHORS and writes the result as a
frame a cross-sectional study can join across instruments.

**Grid.**  For the session-local day of one instrument's input file, the
anchors are ``open + k * step`` for ``k >= 0`` while ``< close`` (session
bounds from :class:`iap.features.context.InstrumentContext`, converted with
its IANA clock, so a DST day is handled like any other).  Every instrument
of a dataset gets the SAME instants on the same day, which is what a
cross-sectional rank at a decision time needs; the row clock of the feature
store (``--cadence-ms`` or ``--sampling``) plays no part.

**Causality.**  ``anchor_ts`` is the decision time.  The anchor mid is the
last book-refresh sample with ``ts <= anchor_ts`` (an event AT the anchor
instant is known at it, as for the v1 row labels); the forward mid is the
last sample at or before ``min(anchor_ts + h, session_close)``.  No sample
after that forward instant is read (tested by truncation and by a
shift-by-one canary).  An anchor whose forward instant was pulled back to the
close carries ``label_truncated_<h>_v2 = True`` and
``label_horizon_ns_<h>_v2 = close - anchor_ts``: filter on it for full-horizon
labels.

**Mid series.**  The series is the one the store's v1 labels use (one sample
per book refresh, non-tradable refreshes recorded as such), so the v2 grid
labels and the v1 row labels are computed from the same prices.

Output (one file per instrument, ``labels_v2_<instrument_id>.parquet``, one
row group per input file): ``instrument_id``, ``anchor_ts``,
``session_close_ts`` and the six :func:`label_columns_v2` columns per horizon.
"""

from __future__ import annotations

import numpy as np
import pyarrow as pa

from iap.features.context import InstrumentContext
from iap.labels.labels import LABEL_MAX_AGE_FLOOR_NS, MidSeries, max_sample_age
from iap.labels.labels_v2 import (
    HORIZON_V2_ORDER,
    compute_labels_v2,
    label_columns_v2,
    session_close_ts,
)

__all__ = [
    "GRID_V2_VERSION",
    "grid_labels_v2",
    "grid_schema_v2",
    "parse_step",
    "session_grid",
]

_NS_S = 1_000_000_000
#: Contract version of the grid files.
GRID_V2_VERSION = 1


def parse_step(text: str) -> int:
    """``"60s"`` / ``"1m"`` / ``"5m"`` -> nanoseconds (positive)."""
    t = text.strip().lower()
    for suffix, mult in (("ms", 1_000_000), ("s", _NS_S), ("m", 60 * _NS_S)):
        if t.endswith(suffix) and t[: -len(suffix)].isdigit():
            n = int(t[: -len(suffix)]) * mult
            if n > 0:
                return n
    raise ValueError(f"grid step must be like 30s / 1m / 5m (got {text!r})")


def session_grid(ts_ns: int, ctx: InstrumentContext, step_ns: int) -> tuple[list[int], int]:
    """(anchors, close) for the session-local day holding ``ts_ns`` (module docs)."""
    if step_ns <= 0:
        raise ValueError("step_ns must be positive")
    off = ctx.clock.offset_seconds(int(ts_ns))
    open_ts = session_close_ts(ts_ns, ctx.session_open_min * 60, off)
    close = session_close_ts(ts_ns, ctx.session_close_min * 60, off)
    return list(range(open_ts, close, step_ns)), close


def grid_schema_v2(horizons=HORIZON_V2_ORDER) -> pa.Schema:
    fields = [
        pa.field("instrument_id", pa.uint32()),
        pa.field("anchor_ts", pa.int64()),
        pa.field("session_close_ts", pa.int64()),
    ]
    for h in horizons:
        c = label_columns_v2(h)
        fields += [
            pa.field(c["mid"], pa.float64()),
            pa.field(c["cost"], pa.float64()),
            pa.field(c["valid"], pa.bool_()),
            pa.field(c["reason"], pa.uint8()),
            pa.field(c["truncated"], pa.bool_()),
            pa.field(c["horizon_ns"], pa.int64()),
        ]
    return pa.schema(fields)


def grid_labels_v2(
    instrument_id: int,
    series: MidSeries,
    last_event_ts: int,
    ctx: InstrumentContext,
    step_ns: int,
    freshness: str = "trailing",
    horizons=HORIZON_V2_ORDER,
) -> pa.Table | None:
    """One instrument-day of grid labels; ``None`` when the series is empty."""
    if len(series.ts) == 0:
        return None
    anchors, close = session_grid(int(last_event_ts), ctx, step_ns)
    if not anchors:
        return None
    if freshness == "trailing":
        labels = compute_labels_v2(
            anchors, series, last_event_ts, close, horizons, freshness="trailing"
        )
    else:
        age = max(LABEL_MAX_AGE_FLOOR_NS, max_sample_age(series))
        labels = compute_labels_v2(anchors, series, last_event_ts, close, horizons, max_age_ns=age)
    n = len(anchors)
    cols: dict[str, pa.Array] = {
        "instrument_id": pa.array(np.full(n, instrument_id, dtype=np.uint32), type=pa.uint32()),
        "anchor_ts": pa.array(anchors, type=pa.int64()),
        "session_close_ts": pa.array([close] * n, type=pa.int64()),
    }
    for h in horizons:
        lab = labels[h]
        c = label_columns_v2(h)
        cols[c["mid"]] = pa.array(lab.mid, type=pa.float64())
        cols[c["cost"]] = pa.array(lab.cost, type=pa.float64())
        cols[c["valid"]] = pa.array(lab.valid, type=pa.bool_())
        cols[c["reason"]] = pa.array(lab.reason, type=pa.uint8())
        cols[c["truncated"]] = pa.array(lab.truncated, type=pa.bool_())
        cols[c["horizon_ns"]] = pa.array(lab.effective_horizon_ns, type=pa.int64())
    return pa.table(cols, schema=grid_schema_v2(horizons))
