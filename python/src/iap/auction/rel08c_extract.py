"""REL-08c: one-pass NOII extractor for a full-market ITCH 5.0 file.

One streaming pass over the file (``Itch50Reader(symbols=universe,
noii=True)``: messages of other symbols are skipped after three bytes), kept
for the breadth universe only (``research/breadth/universe.json``, 100
names):

* ``noii.csv`` - every NOII snapshot of the wanted symbols with the per-symbol
  imbalance stream derived causally, snapshot by snapshot, in the pass:
  ``level`` (signed imbalance / (paired + imbalance), in [-1, 1]),
  ``change`` (level minus the previous snapshot's level of the same symbol
  and cross type, NaN for the first), ``persistence`` (consecutive snapshots,
  this one included, with the same non-zero direction; 0 when no imbalance),
  ``distance_to_close_s`` (seconds to the scheduled 16:00 close; to the 09:30
  open for opening snapshots) and the reference / far / near prices;
* ``crosses.csv`` - every cross print (``Q``) of the wanted symbols;
* ``bbo_grid.csv`` - the continuous-session best bid / ask (price and
  displayed size) of every wanted symbol on a 1 s grid from
  ``grid_start`` to ``grid_end`` (default 15:30:00-16:00:00 ET).  The value
  at grid time ``t`` is the book after every message with ``ts <= t`` (ITCH
  timestamps are non-decreasing, so a grid point is emitted as soon as the
  first message with ``ts > t`` arrives): nothing after ``t`` is used;
* ``meta.json`` - provenance: input file name, size and sha256 (hashed in
  its own streaming read, or the caller's pre-verified value), the reader's
  per-type message counts, filtered and total messages, NOII / cross /
  grid row counts, wanted symbols missing from the file's directory,
  peak live orders tracked, wall time and the code version.

Memory is bounded by the live orders of the wanted symbols (a dict
``order_ref -> [locate, side, price, shares]``) plus one
:class:`~iap.marketdata.itch50.LevelBook` per symbol; the output rows are
appended to open CSV writers, never held in memory.  Prices stay in the
feed's 1/10000-dollar integers (``*_px4``).

This module never reads a real file by itself: the run is the detached job
``tools/run_rel08c_extract.cmd``, started by the coordinator only after the
REL-08c pre-declaration is signed.
"""

from __future__ import annotations

import csv
import hashlib
import json
import time
from collections.abc import Iterable, Sequence
from pathlib import Path

from iap.marketdata.itch50 import (
    PRICE_SCALE,
    AddOrder,
    CrossTrade,
    Itch50Reader,
    LevelBook,
    NetOrderImbalance,
    OrderCancel,
    OrderDelete,
    OrderExecuted,
    OrderExecutedWithPrice,
    OrderReplace,
    StockDirectory,
    SystemEvent,
)

EXTRACT_FORMAT = "iap.rel08c_extract"
EXTRACT_VERSION = 1
NS_S = 1_000_000_000
NOII_FILE = "noii.csv"
CROSS_FILE = "crosses.csv"
GRID_FILE = "bbo_grid.csv"
META_FILE = "meta.json"
#: Scheduled cross, ns since midnight America/New_York.
SCHEDULED_TOD_NS = {"O": (9 * 3600 + 30 * 60) * NS_S, "C": 16 * 3600 * NS_S}
DEFAULT_GRID_START_NS = (15 * 3600 + 30 * 60) * NS_S
DEFAULT_GRID_END_NS = 16 * 3600 * NS_S
GRID_STEP_NS = NS_S
DIRECTION_SIGN = {"B": 1, "S": -1}

NOII_COLUMNS = (
    "symbol",
    "tod_ns",
    "cross_type",
    "paired_shares",
    "imbalance_shares",
    "imbalance_direction",
    "level",
    "change",
    "persistence",
    "distance_to_close_s",
    "ref_px4",
    "far_px4",
    "near_px4",
    "price_variation",
)
CROSS_COLUMNS = ("symbol", "tod_ns", "cross_type", "shares", "price_px4")
GRID_COLUMNS = ("symbol", "tod_ns", "bid_px4", "bid_size", "ask_px4", "ask_size")


def load_universe(path: str | Path) -> list[str]:
    """The ``universe`` symbols of ``research/breadth/universe.json``."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = doc["universe"]
    symbols = [r["symbol"] if isinstance(r, dict) else str(r) for r in rows]
    if len(set(symbols)) != len(symbols):
        raise ValueError(f"{path}: duplicate symbols in the universe")
    return symbols


def file_sha256(path: str | Path, chunk: int = 1 << 22) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


class _SymbolStream:
    """Causal per-(symbol, cross type) imbalance state."""

    __slots__ = ("prev_level", "run_dir", "run_len")

    def __init__(self) -> None:
        self.prev_level: float | None = None
        self.run_dir = 0
        self.run_len = 0

    def update(self, msg: NetOrderImbalance) -> tuple[float, float, int]:
        sign = DIRECTION_SIGN.get(msg.imbalance_direction, 0)
        denom = msg.paired_shares + msg.imbalance_shares
        level = sign * msg.imbalance_shares / denom if denom > 0 and sign else 0.0
        change = float("nan") if self.prev_level is None else level - self.prev_level
        self.prev_level = level
        if sign == 0:
            self.run_dir, self.run_len = 0, 0
        elif sign == self.run_dir:
            self.run_len += 1
        else:
            self.run_dir, self.run_len = sign, 1
        return level, change, self.run_len


def _fmt(x: float) -> str:
    return "" if x != x else repr(round(x, 9))


def extract_rel08c(
    itch_path: str | Path,
    symbols: Sequence[str],
    out_dir: str | Path,
    *,
    date: str | None = None,
    expected_sha256: str | None = None,
    hash_input: bool = True,
    grid_start_ns: int = DEFAULT_GRID_START_NS,
    grid_end_ns: int = DEFAULT_GRID_END_NS,
    limit_messages: int | None = None,
    chunk_size: int = 1 << 22,
) -> dict:
    """Run the one-pass extraction; returns (and writes) the meta dict."""
    t0 = time.monotonic()
    path = Path(itch_path)
    out = Path(out_dir)
    if any((out / m).exists() for m in ("dataset.json", "manifest.json", "normalized")):
        raise ValueError(f"{out}: looks like a dataset directory; write elsewhere")
    if grid_end_ns < grid_start_ns:
        raise ValueError("grid_end_ns < grid_start_ns")
    sha = None
    if hash_input:
        sha = file_sha256(path)
        if expected_sha256 is not None and sha != expected_sha256.lower():
            raise ValueError(f"{path.name}: sha256 {sha} != expected {expected_sha256}")
    elif expected_sha256 is not None:
        sha = expected_sha256.lower()
    out.mkdir(parents=True, exist_ok=True)

    wanted = tuple(symbols)
    reader = Itch50Reader(
        path, symbols=wanted, noii=True, chunk_size=chunk_size, limit_messages=limit_messages
    )
    loc_sym: dict[int, str] = {}
    books: dict[int, LevelBook] = {}
    orders: dict[int, list[int]] = {}
    streams: dict[tuple[int, str], _SymbolStream] = {}
    peak_orders = 0
    unknown_refs = 0
    n_noii = n_cross = n_grid = 0
    next_grid = grid_start_ns

    with (
        open(out / NOII_FILE, "w", newline="", encoding="utf-8") as fn,
        open(out / CROSS_FILE, "w", newline="", encoding="utf-8") as fc,
        open(out / GRID_FILE, "w", newline="", encoding="utf-8") as fg,
    ):
        wn, wc, wg = (csv.writer(f, lineterminator="\n") for f in (fn, fc, fg))
        wn.writerow(NOII_COLUMNS)
        wc.writerow(CROSS_COLUMNS)
        wg.writerow(GRID_COLUMNS)

        def emit_grid(upto_ts: int) -> None:
            """Emit every grid point strictly before ``upto_ts``."""
            nonlocal next_grid, n_grid
            while next_grid <= grid_end_ns and next_grid < upto_ts:
                for lc in sorted(books, key=loc_sym.__getitem__):
                    b = books[lc]
                    bid, ask = b.best(0), b.best(1)
                    wg.writerow(
                        (
                            loc_sym[lc],
                            next_grid,
                            "" if bid is None else bid,
                            "" if bid is None else b.totals[0][bid],
                            "" if ask is None else ask,
                            "" if ask is None else b.totals[1][ask],
                        )
                    )
                    n_grid += 1
                next_grid += GRID_STEP_NS

        for msg in reader:
            if isinstance(msg, StockDirectory):
                loc_sym[msg.locate] = msg.stock
                books.setdefault(msg.locate, LevelBook())
                continue
            ts = msg.ts  # type: ignore[attr-defined]  # every decoded message has ts
            if ts > next_grid and next_grid <= grid_end_ns:
                emit_grid(ts)
            if isinstance(msg, SystemEvent):
                continue
            if isinstance(msg, AddOrder):
                side = 0 if msg.side == "B" else 1
                orders[msg.order_ref] = [msg.locate, side, msg.price, msg.shares]
                books[msg.locate].add(side, msg.price, msg.shares)
                if len(orders) > peak_orders:
                    peak_orders = len(orders)
            elif isinstance(msg, (OrderExecuted, OrderExecutedWithPrice, OrderCancel)):
                o = orders.get(msg.order_ref)
                if o is None:
                    unknown_refs += 1
                    continue
                q = min(msg.shares, o[3])
                books[o[0]].remove(o[1], o[2], q)
                o[3] -= q
                if o[3] <= 0:
                    del orders[msg.order_ref]
            elif isinstance(msg, OrderDelete):
                o = orders.pop(msg.order_ref, None)
                if o is None:
                    unknown_refs += 1
                    continue
                books[o[0]].remove(o[1], o[2], o[3])
            elif isinstance(msg, OrderReplace):
                o = orders.pop(msg.orig_ref, None)
                if o is None:
                    unknown_refs += 1
                    continue
                books[o[0]].remove(o[1], o[2], o[3])
                orders[msg.new_ref] = [o[0], o[1], msg.price, msg.shares]
                books[o[0]].add(o[1], msg.price, msg.shares)
            elif isinstance(msg, NetOrderImbalance):
                key = (msg.locate, msg.cross_type)
                st = streams.get(key)
                if st is None:
                    st = streams[key] = _SymbolStream()
                level, change, persist = st.update(msg)
                sched = SCHEDULED_TOD_NS.get(msg.cross_type)
                dist = (sched - msg.ts) / NS_S if sched is not None else float("nan")
                wn.writerow(
                    (
                        msg.stock,
                        msg.ts,
                        msg.cross_type,
                        msg.paired_shares,
                        msg.imbalance_shares,
                        msg.imbalance_direction,
                        _fmt(level),
                        _fmt(change),
                        persist,
                        _fmt(dist),
                        msg.current_reference_price,
                        msg.far_price,
                        msg.near_price,
                        msg.price_variation,
                    )
                )
                n_noii += 1
            elif isinstance(msg, CrossTrade):
                wc.writerow((msg.stock, msg.ts, msg.cross_type, msg.shares, msg.price))
                n_cross += 1
        emit_grid(grid_end_ns + 1)

    meta = {
        "format": EXTRACT_FORMAT,
        "version": EXTRACT_VERSION,
        "date": date,
        "input": {
            "name": path.name,
            "bytes": path.stat().st_size,
            "sha256": sha,
            "sha256_source": "hashed" if hash_input else ("caller" if sha else None),
        },
        "symbols_wanted": len(wanted),
        "symbols_found": sorted(loc_sym.values()),
        "symbols_missing": sorted(set(wanted) - set(loc_sym.values())),
        "messages_read": reader.messages_read,
        "messages_filtered": reader.filtered,
        "message_counts": {k: v for k, v in sorted(reader.counts.items()) if v},
        "skipped_unknown": dict(sorted(reader.skipped_unknown.items())),
        "limit_reached": reader.limit_reached,
        "rows": {"noii": n_noii, "crosses": n_cross, "bbo_grid": n_grid},
        "grid": {"start_tod_ns": grid_start_ns, "end_tod_ns": grid_end_ns, "step_ns": GRID_STEP_NS},
        "peak_live_orders": peak_orders,
        "unknown_order_refs": unknown_refs,
        "price_scale": PRICE_SCALE,
        "wall_s": round(time.monotonic() - t0, 3),
    }
    (out / META_FILE).write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return meta


def main(argv: Iterable[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="python -m iap.auction.rel08c_extract")
    p.add_argument("--itch", required=True)
    p.add_argument("--universe", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--date")
    p.add_argument("--expected-sha256")
    p.add_argument(
        "--no-hash",
        action="store_true",
        help="skip hashing (meta records the caller's --expected-sha256)",
    )
    p.add_argument("--limit-messages", type=int)
    a = p.parse_args(None if argv is None else list(argv))
    meta = extract_rel08c(
        a.itch,
        load_universe(a.universe),
        a.out,
        date=a.date,
        expected_sha256=a.expected_sha256,
        hash_input=not a.no_hash,
        limit_messages=a.limit_messages,
    )
    print(
        json.dumps(
            {
                k: meta[k]
                for k in ("rows", "messages_read", "wall_s", "peak_live_orders", "symbols_missing")
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
