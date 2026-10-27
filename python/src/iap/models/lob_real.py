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

Cache: :func:`build_cache` writes per unit ``<session>_<symbol>.X.npy``
(float16, memory-mappable) and ``<session>_<symbol>.meta.npz`` (``ts``,
``mid``, ``n_empty``, ``tick``; written last, so it marks a complete unit),
and refuses the R7 holdout
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

from iap import io as iap_io
from iap.models.deeplob import smoothed_labels

__all__ = [
    "WindowIndex",
    "build_cache",
    "iter_events",
    "fit_column_normalizer",
    "load_unit",
    "unit_paths",
    "session_snapshots",
]

LEVELS = 10
# R7 holdout sessions are refused through iap.governance.holdout_registry (v1.18 REL-02b).


EVENT_COLS = (
    "event_id",
    "instrument_id",
    "venue_id",
    "exchange_ts",
    "receive_ts",
    "sequence",
    "event_type",
    "side",
    "price_ticks",
    "qty",
    "order_id",
    "trade_id",
)


def iter_events(session_dir: Path, iid: int, end_ns: int | None = None) -> Iterable:
    """``MarketEvent``s of one instrument, streamed row group by row group
    (never the whole session in memory; file order = event order)."""
    import pyarrow.compute as pc

    from iap import io as iap_io
    from iap.core.events import MarketEvent

    pf = iap_io.parquet_file(
        Path(session_dir) / "normalized" / "events.parquet", purpose="lob_real"
    )
    for rg in range(pf.num_row_groups):
        t = pf.read_row_group(rg, columns=list(EVENT_COLS))
        mask = pc.equal(t.column("instrument_id"), iid)
        if end_ns is not None:
            mask = pc.and_(mask, pc.less_equal(t.column("exchange_ts"), end_ns))
        t = t.filter(mask)
        if not t.num_rows:
            continue
        cols = [t.column(c).to_numpy() for c in EVENT_COLS]
        del t
        for k in range(len(cols[0])):
            yield MarketEvent(*(int(c[k]) for c in cols))
        del cols


def _refuse_holdout(name: str) -> None:
    """v1.18 REL-02b: refuse by the holdout registry (session date / dataset label),
    not by a hard-coded list; an unregistered date is refused too."""
    from iap.governance.holdout_registry import HoldoutRefused, refuse_unit_name

    try:
        refuse_unit_name(name)
    except HoldoutRefused as exc:
        raise ValueError(f"{name}: holdout session, refused for AI4 development ({exc})") from exc


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
    width = 4 * levels
    chunk = 200_000
    xs: list[np.ndarray] = []
    tss: list[np.ndarray] = []
    mids: list[np.ndarray] = []
    emps: list[np.ndarray] = []
    buf = np.empty((chunk, width), dtype=np.float32)
    bts = np.empty(chunk, dtype=np.int64)
    bmid = np.empty(chunk, dtype=np.float64)
    bemp = np.empty(chunk, dtype=np.int16)
    n = 0
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
        row = buf[n]
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
        bts[n] = int(ev.exchange_ts)
        bmid[n] = mid_t * tick
        bemp[n] = empty
        n += 1
        if n == chunk:
            xs.append(buf.copy())
            tss.append(bts.copy())
            mids.append(bmid.copy())
            emps.append(bemp.copy())
            n = 0
    if n:
        xs.append(buf[:n].copy())
        tss.append(bts[:n].copy())
        mids.append(bmid[:n].copy())
        emps.append(bemp[:n].copy())
    if not xs:
        return (
            np.empty(0, np.int64),
            np.empty(0),
            np.empty((0, width), np.float32),
            np.empty(0, np.int16),
        )
    return np.concatenate(tss), np.concatenate(mids), np.concatenate(xs), np.concatenate(emps)


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
    Units are kept as given (a float16 memory map is fine): each window is
    cast to float32 and normalised when it is read, so memory is O(batch).
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
        self.normalizer = normalizer
        self.units: list[np.ndarray] = []
        self.labels: list[np.ndarray] = []
        ends_u: list[np.ndarray] = []
        ends_t: list[np.ndarray] = []
        for u, (x, mid) in enumerate(units):
            lab = smoothed_labels(mid, horizon, threshold).astype(np.int8)
            ends = np.arange(window - 1, len(lab))
            ends = ends[lab[ends] >= 0][::stride]
            self.units.append(x)  # raw; may be a read-only memory map
            self.labels.append(lab)
            ends_u.append(np.full(len(ends), u, dtype=np.int32))
            ends_t.append(ends.astype(np.int64))
        self._u = np.concatenate(ends_u) if ends_u else np.empty(0, np.int32)
        self._t = np.concatenate(ends_t) if ends_t else np.empty(0, np.int64)

    def __len__(self) -> int:
        return int(self._u.size)

    def __getitem__(self, i: int) -> tuple[np.ndarray, int]:
        u, t = int(self._u[i]), int(self._t[i])
        w = np.asarray(self.units[u][t - self.window + 1 : t + 1], dtype=np.float32)
        if self.normalizer is not None:
            w = self.normalizer.transform(w)
        return w, int(self.labels[u][t])

    def ends(self) -> tuple[np.ndarray, np.ndarray]:
        """``(unit, end_row)`` of every window, in index order."""
        return self._u, self._t

    def label_array(self) -> np.ndarray:
        return np.asarray([self.labels[u][t] for u, t in zip(self._u, self._t, strict=True)])

    def batch(self, idx: Sequence[int]) -> tuple[np.ndarray, np.ndarray]:
        xs, ys = zip(*(self[int(i)] for i in idx), strict=True)
        return np.stack(xs), np.asarray(ys, dtype=np.int64)


def load_unit(path: Path, mmap: bool = True) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(ts, mid, X)`` of a cached unit (``<unit>.meta.npz`` + ``<unit>.X.npy``);
    ``X`` is a read-only float16 memory map unless ``mmap`` is False."""
    path = Path(path)
    stem = path.name.removesuffix(".meta.npz").removesuffix(".X.npy")
    with iap_io.np_load(path.parent / f"{stem}.meta.npz", purpose="lob unit") as z:
        ts, mid = z["ts"], z["mid"]
    x = iap_io.np_load(
        path.parent / f"{stem}.X.npy", mmap_mode="r" if mmap else None, purpose="lob unit"
    )
    return ts, mid, x


def unit_paths(cache: Path) -> list[Path]:
    """Cached units, sorted (one ``.meta.npz`` per unit)."""
    return sorted(Path(cache).glob("*.meta.npz"))


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
    out.mkdir(parents=True, exist_ok=True)
    done = []
    for sess in sessions:
        _refuse_holdout(sess)
        sdir = data_root / sess
        from iap.governance.holdout_registry import HoldoutRefused, guard

        try:
            guard(sdir, purpose="ai4_build_cache")
        except HoldoutRefused as exc:
            raise ValueError(f"{sess}: refused for AI4 development ({exc})") from exc
        instruments = json.loads(
            (sdir / "configs" / "instruments" / "instruments.json").read_text(encoding="utf-8")
        )
        rows = instruments.get("instruments", instruments)
        ticks = {int(r["instrument_id"]): float(r["tick_size"]) for r in rows}
        for sym, iid in symbols.items():
            path = out / f"{sess}_{sym}.meta.npz"
            if path.is_file():
                continue
            free = shutil.disk_usage(out).free / 1e9
            if free < min_free_gb:
                raise RuntimeError(
                    f"only {free:.1f} GB free (< {min_free_gb}); stopping before {path.name}"
                )
            ts, mid, x, empty = session_snapshots(iter_events(sdir, iid), ticks[iid], levels)
            xpath = out / f"{sess}_{sym}.X.npy"
            np.save(xpath, x.astype(np.float16))
            del x
            tmp = out / f"{sess}_{sym}.partial.npz"
            np.savez(tmp, ts=ts, mid=mid, n_empty=empty, tick=np.float64(ticks[iid]))
            tmp.replace(path)  # the meta file last: its presence marks a complete unit
            info = {
                "unit": path.name,
                "rows": int(len(ts)),
                "empty_level_frac": float(empty.mean() / (2 * levels)) if len(empty) else None,
            }
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
