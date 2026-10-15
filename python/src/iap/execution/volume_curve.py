"""Forecast intraday volume curves from data (plan item X3, v1.10, opt-in).

Estimates a per-instrument intraday volume profile from normalized TRADE
events: the session ``[session_start_ns, session_end_ns)`` (offsets from the
start of each UTC day) is cut into ``n_bins`` equal bins; each day's traded
quantity per bin is normalized to sum to 1; days are averaged; the average
is shrunk toward the pinned U-shape ``1 + x^2`` with weight
``k / (days + k)``. The result is written as a versioned JSON document
(``iap.volume_curve`` v1).

**Exchange-local sessions (v1.13, X-P2-13, opt-in).** The default session
is fixed in UTC (13:30-20:00), which is the New York session only during
EDT; in EST (November-March) the open is 14:30 UTC and every bin is
shifted by an hour. ``tz="America/New_York"`` interprets
``session`` as offsets from LOCAL midnight in that zone (``zoneinfo``, so
the UTC offset follows DST per event) and groups days by local date; with
``tz`` set and no ``session``, the session is :data:`NY_SESSION`
(09:30-16:00 local). ``tz=None`` (the default) is the v1.10 rule.

A ``ParentOrder`` with ``volume_curve`` set uses it for VWAP slice weights
(see ``curve_slice_weights``); with the default ``None`` the pinned
``1 + x^2`` curve (cross-language contract) is unchanged.

CLI: ``python -m iap.execution.volume_curve --events F [F ...] --out C``.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from iap.core.events import EventType, MarketEvent

SCHEMA = "iap.volume_curve"
VERSION = 1
DAY_NS = 86_400 * 1_000_000_000
#: US equities regular session in UTC (13:30-20:00, no DST adjustment).
DEFAULT_SESSION = (int(13.5 * 3600e9), 20 * 3600 * 1_000_000_000)
#: US equities regular session in exchange-local time (09:30-16:00), v1.13.
NY_SESSION = (int(9.5 * 3600e9), 16 * 3600 * 1_000_000_000)
NY_TZ = "America/New_York"
_HOUR_NS = 3600 * 1_000_000_000


def _local_offset_ns(zone: ZoneInfo, ts_ns: int, cache: dict[int, int]) -> int:
    """UTC offset of ``zone`` at ``ts_ns`` (cached per UTC hour; DST changes
    happen on the hour)."""
    h = ts_ns // _HOUR_NS
    off = cache.get(h)
    if off is None:
        d = datetime.fromtimestamp(h * 3600, tz=UTC).astimezone(zone).utcoffset()
        off = cache[h] = int(d.total_seconds()) * 1_000_000_000 if d is not None else 0
    return off


def u_shape(n_bins: int) -> list[float]:
    """The pinned ``1 + x^2`` curve on ``n_bins`` bins, normalized to sum 1."""
    if n_bins <= 0:
        raise ValueError("n_bins must be > 0")
    if n_bins == 1:
        return [1.0]
    w = [1.0 + ((2.0 * i - (n_bins - 1)) / (n_bins - 1)) ** 2 for i in range(n_bins)]
    s = sum(w)
    return [x / s for x in w]


def estimate_volume_curves(
    events: Iterable[MarketEvent],
    *,
    n_bins: int = 26,
    session: tuple[int, int] | None = None,
    shrinkage: float = 5.0,
    day_ns: int = DAY_NS,
    tz: str | None = None,
) -> dict:
    """Estimate per-instrument curves; returns the ``iap.volume_curve`` doc.

    ``session`` defaults to :data:`DEFAULT_SESSION` (UTC) or, with ``tz``,
    :data:`NY_SESSION` in local time (module docstring).
    """
    if session is None:
        session = DEFAULT_SESSION if tz is None else NY_SESSION
    zone = ZoneInfo(tz) if tz is not None else None
    off_cache: dict[int, int] = {}
    start, end = session
    if not 0 <= start < end <= day_ns:
        raise ValueError("session must satisfy 0 <= start < end <= day_ns")
    if shrinkage < 0:
        raise ValueError("shrinkage must be >= 0")
    width = (end - start) / n_bins
    per: dict[int, dict[int, list[float]]] = defaultdict(dict)
    for ev in events:
        if ev.event_type != EventType.TRADE or ev.qty <= 0:
            continue
        t = ev.exchange_ts
        if zone is not None:
            t += _local_offset_ns(zone, t, off_cache)
        day, off = divmod(t, day_ns)
        if not start <= off < end:
            continue
        b = min(int((off - start) // width), n_bins - 1)
        bins = per[ev.instrument_id].setdefault(day, [0.0] * n_bins)
        bins[b] += ev.qty
    prior = u_shape(n_bins)
    curves: dict[str, dict] = {}
    for iid in sorted(per):
        days = [d for d in per[iid].values() if sum(d) > 0]
        nd = len(days)
        emp = [0.0] * n_bins
        for d in days:
            s = sum(d)
            for i in range(n_bins):
                emp[i] += d[i] / s / nd
        w = nd / (nd + shrinkage) if (nd + shrinkage) > 0 else 1.0
        curve = [w * emp[i] + (1.0 - w) * prior[i] for i in range(n_bins)]
        s = sum(curve)
        curves[str(iid)] = {"days": nd, "weight_empirical": w, "curve": [c / s for c in curve]}
    return {
        "schema": SCHEMA,
        "version": VERSION,
        "n_bins": n_bins,
        "session_ns": [start, end],
        **({"tz": tz} if tz is not None else {}),
        "shrinkage": shrinkage,
        "prior": "u_shape_1_plus_x2",
        "curves": curves,
    }


def write_volume_curves(doc: Mapping, path: str | Path) -> None:
    """Write the document (sorted keys, stable bytes)."""
    if doc.get("schema") != SCHEMA or doc.get("version") != VERSION:
        raise ValueError("not an iap.volume_curve v1 document")
    Path(path).write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_volume_curve(path: str | Path, instrument_id: int) -> tuple[float, ...]:
    """The curve of ``instrument_id`` (falls back to the U-shape prior)."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if doc.get("schema") != SCHEMA or doc.get("version") != VERSION:
        raise ValueError("not an iap.volume_curve v1 document")
    cell = doc["curves"].get(str(instrument_id))
    return tuple(cell["curve"] if cell else u_shape(int(doc["n_bins"])))


def curve_slice_weights(
    curve: Sequence[float], n: int, start_frac: float = 0.0, end_frac: float = 1.0
) -> list[float]:
    """Resample a piecewise-constant curve on ``len(curve)`` equal bins to
    ``n`` equal slices (each slice gets the curve mass it overlaps).

    ``start_frac`` / ``end_frac`` (v1.17, review X-1) are the part of the
    curve's span the parent window covers, as fractions of the session the
    curve was estimated on (:func:`curve_window`): the ``n`` slices divide
    ``[start_frac, end_frac]``, not the whole session, so a parent worked
    from 10:00 to 11:00 follows the curve's 10-11 shape instead of the
    whole day's U.  The defaults (the whole span) are the v1.10 rule, so the
    pinned ``1 + x^2`` resampling is unchanged.  A window with no curve
    mass raises."""
    m = len(curve)
    if m == 0 or n <= 0:
        raise ValueError("need a non-empty curve and n > 0")
    if any(c < 0 for c in curve) or sum(curve) <= 0:
        raise ValueError("curve must be non-negative with positive mass")
    if not (0.0 <= start_frac < end_frac <= 1.0):
        raise ValueError("need 0 <= start_frac < end_frac <= 1")
    span = (end_frac - start_frac) * m
    out = []
    for i in range(n):
        lo = start_frac * m + i * span / n
        hi = start_frac * m + (i + 1) * span / n
        tot = 0.0
        b = int(lo)
        while b < m and b < hi:
            tot += curve[b] * (min(hi, b + 1) - max(lo, b))
            b += 1
        out.append(tot)
    if sum(out) <= 0:
        raise ValueError("the parent window covers no mass of the volume curve")
    return out


def curve_window(
    start_ts: int, end_ts: int, session_ns: Sequence[int] = DEFAULT_SESSION
) -> tuple[float, float]:
    """The fraction of the curve's session ``[start, end)`` (offsets from
    the start of the UTC day, as the curve document's ``session_ns``) that
    the parent window ``[start_ts, end_ts)`` covers, clamped to ``[0, 1]``
    (v1.17, X-1).  The window must lie within one UTC day and overlap the
    session.  Pass the result to :func:`curve_slice_weights` or set it as
    ``ParentOrder.curve_window``."""
    if end_ts <= start_ts:
        raise ValueError("parent window must have end_ts > start_ts")
    s0, s1 = int(session_ns[0]), int(session_ns[1])
    if s1 <= s0:
        raise ValueError("session_ns must have end > start")
    day = (int(start_ts) // DAY_NS) * DAY_NS
    if (int(end_ts) - 1) // DAY_NS != int(start_ts) // DAY_NS:
        raise ValueError("parent window must lie within one UTC day")
    a = (int(start_ts) - day - s0) / (s1 - s0)
    b = (int(end_ts) - day - s0) / (s1 - s0)
    a, b = max(0.0, min(1.0, a)), max(0.0, min(1.0, b))
    if not a < b:
        raise ValueError("parent window does not overlap the volume curve's session")
    return a, b


def _read_events(path: str) -> list[MarketEvent]:
    from iap.core.codec import read_iap1, read_jsonl

    return read_jsonl(path) if path.endswith((".jsonl", ".json")) else read_iap1(path)


def main(argv: Iterable[str] | None = None) -> int:
    """Estimate volume curves from event files and write the JSON."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--events", nargs="+", required=True, help="IAP1 or JSONL event files")
    ap.add_argument("--out", required=True)
    ap.add_argument("--bins", type=int, default=26)
    ap.add_argument("--shrinkage", type=float, default=5.0)
    ap.add_argument("--session-start-ns", type=int, default=None)
    ap.add_argument("--session-end-ns", type=int, default=None)
    ap.add_argument(
        "--tz",
        default=None,
        help=f"exchange time zone for the session bins (e.g. {NY_TZ}); default: UTC (v1.10)",
    )
    args = ap.parse_args(list(argv) if argv is not None else None)
    events: list[MarketEvent] = []
    for p in args.events:
        events.extend(_read_events(p))
    doc = estimate_volume_curves(
        events,
        n_bins=args.bins,
        session=(
            None
            if args.session_start_ns is None and args.session_end_ns is None
            else (
                args.session_start_ns
                if args.session_start_ns is not None
                else (DEFAULT_SESSION if args.tz is None else NY_SESSION)[0],
                args.session_end_ns
                if args.session_end_ns is not None
                else (DEFAULT_SESSION if args.tz is None else NY_SESSION)[1],
            )
        ),
        shrinkage=args.shrinkage,
        tz=args.tz,
    )
    doc["source"] = {"files": [Path(p).name for p in args.events]}
    write_volume_curves(doc, args.out)
    print(f"wrote {args.out}: {len(doc['curves'])} instruments, {args.bins} bins")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
