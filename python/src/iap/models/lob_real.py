"""Real-data adapter for the deep LOB models (v1.16, AI4 rebuild).

Fixes the three gaps that deferred AI4-DEEPLOB-1 (research/r7/AI4_DEFERRED.md):

1. **A real-data path.** :func:`session_snapshots` replays one session's
   normalised ITCH events of one instrument through the platform order book
   and records, after every applied event, the top ``levels`` of both sides
   in the DeepLOB layout ``[ask_px_1, ask_sz_1, bid_px_1, bid_sz_1, ...]``.
2. **Per-session construction.** Snapshots, labels and windows are built
   per (session, instrument): no window or smoothed label crosses an
   overnight gap, an auction or a halt (only events while the book is
   TRADING, not stale and two-sided are recorded, and each session is its
   own series). :class:`WindowIndex` addresses windows lazily across many
   sessions without materialising ``(N, W, F)``.
3. **Stationary inputs.** Prices are MID-RELATIVE, in ticks
   (``(px - mid) / tick``), so a static normaliser does not drift with the
   price level; sizes are ``log1p(qty)``. An EMPTY level (fewer than
   ``levels`` price levels on a side) gets size 0 and a price one tick
   beyond the previous level (monotone ladder), and a per-row count of
   empty levels is kept for audit.

Cache: :func:`build_cache` writes one ``<session>_<symbol>.npz`` per unit
(``X`` float16, ``mid`` float64, ``ts`` int64) and refuses the R7 holdout
sessions and units that would leave less than ``min_free_gb`` of disk.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from iap.models.deeplob import smoothed_labels

__all__ = [
    "HOLDOUT_DATES",
    "WindowIndex",
    "build_cache",
    "fit_column_normalizer",
    "load_unit",
    "session_snapshots",
]

LEVELS = 10
#: R7 holdout sessions: never read for AI4 development (AI4_DEFERRED.md).
HOLDOUT_DATES = (
    "20251128",
    "20251208",
    "20251209",
    "20251210",
    "20251211",
    "20251212",
    "20260612",
)
HOLDOUT_DIRS = ("r7_holdout", "holdout2026")


def _refuse_holdout(name: str) -> None:
    if any(d in name for d in HOLDOUT_DATES) or any(h in name for h in HOLDOUT_DIRS):
        raise ValueError(f"{name}: R7 holdout session, refused for AI4 development")


def session_snapshots(
    events: Iterable, tick: float, levels: int = LEVELS
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """``(ts, mid, X, n_empty)`` for one session of one instrument.

    ``X`` is ``(T, 4 * levels)`` float32 in the layout above; rows only for
    applied events after which the venue book is open and two-sided.
    """
    from iap.execution.simulator import ExecutionSimulator
    from iap.orderbook.book import ApplyStatus, ConsolidatedBook

    cons: ConsolidatedBook | None = None
    ts_out: list[int] = []
    mid_out: list[float] = []
    rows: list[np.ndarray] = []
    empty_out: list[int] = []
    width = 4 * levels
    for ev in events:
        if cons is None:
            cons = ConsolidatedBook(ev.instrument_id)
        if cons.apply(ev) != ApplyStatus.APPLIED:
            continue
        vb = cons.books.get(ev.venue_id)
        if not ExecutionSimulator.venue_open(vb):
            continue
        asks = vb.depth(1, levels)
        bids = vb.depth(0, levels)
        if not asks or not bids or asks[0][0] <= bids[0][0]:
            continue
        mid_t = 0.5 * (asks[0][0] + bids[0][0])  # in ticks
        row = np.empty(width, dtype=np.float32)
        empty = 0
        for k in range(levels):
            if k < len(asks):
                ap, aq = asks[k]
            else:
                ap, aq = (row[4 * (k - 1)] + mid_t + 1 if k else mid_t + 1), 0
                empty += 1
            if k < len(bids):
                bp, bq = bids[k]
            else:
                bp, bq = (row[4 * (k - 1) + 2] + mid_t - 1 if k else mid_t - 1), 0
                empty += 1
            row[4 * k] = ap - mid_t
            row[4 * k + 1] = np.log1p(aq)
            row[4 * k + 2] = bp - mid_t
            row[4 * k + 3] = np.log1p(bq)
        ts_out.append(int(ev.exchange_ts))
        mid_out.append(mid_t * tick)
        rows.append(row)
        empty_out.append(empty)
    if not rows:
        return (np.empty(0, np.int64), np.empty(0), np.empty((0, width), np.float32), np.empty(0, np.int16))
    return (
        np.asarray(ts_out, dtype=np.int64),
        np.asarray(mid_out, dtype=np.float64),
        np.vstack(rows),
        np.asarray(empty_out, dtype=np.int16),
    )


@dataclass(frozen=True)
class ColumnNormalizer:
    mean: np.ndarray
    std: np.ndarray

    def transform(self, x: np.ndarray) -> np.ndarray:
        return ((np.asarray(x, dtype=np.float32) - self.mean) / self.std).astype(np.float32)

    def to_dict(self) -> dict:
        return {"mean": self.mean.tolist(), "std": self.std.tolist()}


def fit_column_normalizer(units: Sequence[np.ndarray]) -> ColumnNormalizer:
    """Per-column mean / std over the TRAINING units' rows (streamed, float64)."""
    n = 0
    s = None
    ss = None
    for x in units:
        x = np.asarray(x, dtype=np.float64)
        s = x.sum(axis=0) if s is None else s + x.sum(axis=0)
        ss = (x * x).sum(axis=0) if ss is None else ss + (x * x).sum(axis=0)
        n += x.shape[0]
    if not n:
        raise ValueError("no training rows")
    mean = s / n
    std = np.sqrt(np.maximum(ss / n - mean * mean, 0.0))
    return ColumnNormalizer(mean.astype(np.float32), np.where(std > 0, std, 1.0).astype(np.float32))


class WindowIndex:
    """Lazy windows over many per-session units (no window crosses a unit).

    ``units`` are ``(X, mid)`` pairs; labels are the smoothed labels of each
    unit separately. ``__getitem__(i)`` returns ``(window (W, F) float32,
    label)``; ``stride`` > 1 keeps every stride-th window end (to bound the
    epoch size; the label horizons of kept windows may still overlap).
    """

    def __init__(
        self,
        units: Sequence[tuple[np.ndarray, np.ndarray]],
        *,
        window: int,
        horizon: int,
        threshold: float,
        normalizer: ColumnNormalizer | None = None,
        stride: int = 1,
    ) -> None:
        if window < 1 or horizon < 1 or stride < 1:
            raise ValueError("window, horizon and stride must be >= 1")
        self.window = window
        self.units: list[np.ndarray] = []
        self.labels: list[np.ndarray] = []
        ends_u: list[np.ndarray] = []
        ends_t: list[np.ndarray] = []
        for u, (x, mid) in enumerate(units):
            lab = smoothed_labels(mid, horizon, threshold)
            ends = np.arange(window - 1, len(lab))
            ends = ends[lab[ends] >= 0][::stride]
            self.units.append(normalizer.transform(x) if normalizer else np.asarray(x, np.float32))
            self.labels.append(lab)
            ends_u.append(np.full(len(ends), u, dtype=np.int32))
            ends_t.append(ends.astype(np.int64))
        self._u = np.concatenate(ends_u) if ends_u else np.empty(0, np.int32)
        self._t = np.concatenate(ends_t) if ends_t else np.empty(0, np.int64)

    def __len__(self) -> int:
        return int(self._u.size)

    def __getitem__(self, i: int) -> tuple[np.ndarray, int]:
        u, t = int(self._u[i]), int(self._t[i])
        return self.units[u][t - self.window + 1 : t + 1], int(self.labels[u][t])

    def label_array(self) -> np.ndarray:
        return np.asarray([self.labels[u][t] for u, t in zip(self._u, self._t, strict=True)])

    def batch(self, idx: Sequence[int]) -> tuple[np.ndarray, np.ndarray]:
        xs, ys = zip(*(self[int(i)] for i in idx), strict=True)
        return np.stack(xs), np.asarray(ys, dtype=np.int64)


def load_unit(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(ts, mid, X float32)`` of a cached unit."""
    with np.load(path) as z:
        return z["ts"], z["mid"], z["X"].astype(np.float32)


def build_cache(
    data_root: Path,
    sessions: Sequence[str],
    symbols: dict[str, int],
    out: Path,
    *,
    levels: int = LEVELS,
    min_free_gb: float = 8.0,
) -> list[dict]:
    """One ``<session>_<symbol>.npz`` per unit (skips units already cached)."""
    from iap.research.maker_real import load_events

    out.mkdir(parents=True, exist_ok=True)
    done = []
    for sess in sessions:
        _refuse_holdout(sess)
        sdir = data_root / sess
        instruments = json.loads(
            (sdir / "configs" / "instruments" / "instruments.json").read_text(encoding="utf-8")
        )
        rows = instruments.get("instruments", instruments)
        ticks = {int(r["instrument_id"]): float(r["tick_size"]) for r in rows}
        for sym, iid in symbols.items():
            path = out / f"{sess}_{sym}.npz"
            if path.is_file():
                continue
            free = shutil.disk_usage(out).free / 1e9
            if free < min_free_gb:
                raise RuntimeError(f"only {free:.1f} GB free (< {min_free_gb}); stopping before {path.name}")
            ev = load_events(sdir, iid)
            ts, mid, x, empty = session_snapshots(ev, ticks[iid], levels)
            del ev
            tmp = path.with_suffix(".partial.npz")
            np.savez(tmp, ts=ts, mid=mid, X=x.astype(np.float16), n_empty=empty)
            tmp.replace(path)
            info = {"unit": path.name, "rows": int(len(ts)), "empty_level_frac": float(empty.mean() / (2 * levels)) if len(empty) else None}
            done.append(info)
            print(json.dumps(info), flush=True)
    return done


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="build the AI4 real-data LOB cache")
    ap.add_argument("--data-root", required=True, type=Path)
    ap.add_argument("--sessions", nargs="+", required=True)
    ap.add_argument("--symbols", nargs="+", default=["AAPL=1", "MSFT=2", "QQQ=3"])
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--levels", type=int, default=LEVELS)
    a = ap.parse_args(argv)
    syms = {s.split("=")[0]: int(s.split("=")[1]) for s in a.symbols}
    build_cache(a.data_root, a.sessions, syms, a.out, levels=a.levels)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
