"""Forecast intraday volume curves from data (plan item X3, v1.10, opt-in).

Estimates a per-instrument intraday volume profile from normalized TRADE
events: the session ``[session_start_ns, session_end_ns)`` (offsets from the
start of each UTC day) is cut into ``n_bins`` equal bins; each day's traded
quantity per bin is normalized to sum to 1; days are averaged; the average
is shrunk toward the pinned U-shape ``1 + x^2`` with weight
``k / (days + k)``. The result is written as a versioned JSON document
(``iap.volume_curve`` v1).

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
from pathlib import Path

from iap.core.events import EventType, MarketEvent

SCHEMA = "iap.volume_curve"
VERSION = 1
DAY_NS = 86_400 * 1_000_000_000
#: US equities regular session in UTC (13:30-20:00, no DST adjustment).
DEFAULT_SESSION = (int(13.5 * 3600e9), 20 * 3600 * 1_000_000_000)


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
    session: tuple[int, int] = DEFAULT_SESSION,
    shrinkage: float = 5.0,
    day_ns: int = DAY_NS,
) -> dict:
    """Estimate per-instrument curves; returns the ``iap.volume_curve`` doc."""
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
        day, off = divmod(ev.exchange_ts, day_ns)
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


def curve_slice_weights(curve: Sequence[float], n: int) -> list[float]:
    """Resample a piecewise-constant curve on ``len(curve)`` equal bins to
    ``n`` equal slices (each slice gets the curve mass it overlaps)."""
    m = len(curve)
    if m == 0 or n <= 0:
        raise ValueError("need a non-empty curve and n > 0")
    if any(c < 0 for c in curve) or sum(curve) <= 0:
        raise ValueError("curve must be non-negative with positive mass")
    out = []
    for i in range(n):
        lo, hi = i * m / n, (i + 1) * m / n
        tot = 0.0
        b = int(lo)
        while b < m and b < hi:
            tot += curve[b] * (min(hi, b + 1) - max(lo, b))
            b += 1
        out.append(tot)
    return out


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
    ap.add_argument("--session-start-ns", type=int, default=DEFAULT_SESSION[0])
    ap.add_argument("--session-end-ns", type=int, default=DEFAULT_SESSION[1])
    args = ap.parse_args(list(argv) if argv is not None else None)
    events: list[MarketEvent] = []
    for p in args.events:
        events.extend(_read_events(p))
    doc = estimate_volume_curves(
        events,
        n_bins=args.bins,
        session=(args.session_start_ns, args.session_end_ns),
        shrinkage=args.shrinkage,
    )
    doc["source"] = {"files": [Path(p).name for p in args.events]}
    write_volume_curves(doc, args.out)
    print(f"wrote {args.out}: {len(doc['curves'])} instruments, {args.bins} bins")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
