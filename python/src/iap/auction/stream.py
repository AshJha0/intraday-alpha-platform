"""Opt-in extraction of the auction-imbalance stream from an ITCH 5.0 file.

The default ITCH ingest length-checks and counts ``I`` (NOII) messages and
drops them, so the normalized IAP1 bytes and the dataset version never
depend on this module.  :func:`extract_auction_stream` makes a *separate*
pass with ``Itch50Reader(noii=True)`` and keeps, for the wanted symbols:

* every NOII snapshot: paired shares, imbalance shares and direction, far /
  near / current reference price, cross type, price-variation indicator;
* every cross print (``Q``): shares, price, cross type.

Prices stay in the feed's integer 1/10000-dollar units (``*_px4``) beside a
float dollar column for research.  ``tod_ns`` is nanoseconds since midnight
America/New_York; ``ts`` is epoch ns when the session date is given (else
equal to ``tod_ns``).  The stream is written as CSV files in its own
directory - never inside a normalized dataset directory.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from iap.marketdata.itch50 import (
    PRICE_SCALE,
    CrossTrade,
    Itch50Reader,
    NetOrderImbalance,
    session_midnight_ns,
)

NOII_FILE = "auction_noii.csv"
CROSS_FILE = "auction_crosses.csv"
META_FILE = "auction_meta.json"
STREAM_FORMAT = "iap.auction_stream"
STREAM_VERSION = 1

NOII_COLUMNS = (
    "date",
    "symbol",
    "ts",
    "tod_ns",
    "cross_type",
    "paired_shares",
    "imbalance_shares",
    "imbalance_direction",
    "far_px4",
    "near_px4",
    "ref_px4",
    "far",
    "near",
    "ref",
    "price_variation",
)
CROSS_COLUMNS = ("date", "symbol", "ts", "tod_ns", "cross_type", "shares", "price_px4", "price")


@dataclass
class AuctionStream:
    """NOII snapshots and cross prints of one or more sessions."""

    noii: pd.DataFrame
    crosses: pd.DataFrame
    meta: dict = field(default_factory=dict)

    @classmethod
    def concat(cls, streams: Iterable[AuctionStream]) -> AuctionStream:
        streams = list(streams)
        if not streams:
            return cls(
                pd.DataFrame(columns=list(NOII_COLUMNS)),
                pd.DataFrame(columns=list(CROSS_COLUMNS)),
                {"sessions": []},
            )
        return cls(
            pd.concat([s.noii for s in streams], ignore_index=True),
            pd.concat([s.crosses for s in streams], ignore_index=True),
            {"sessions": [s.meta for s in streams]},
        )

    def write(self, out_dir: str | Path) -> Path:
        out = Path(out_dir)
        markers = ("dataset.json", "dataset_version.json", "manifest.json", "normalized")
        if any((out / m).exists() for m in markers):
            raise ValueError(f"{out}: looks like a dataset directory; write the stream elsewhere")
        out.mkdir(parents=True, exist_ok=True)
        self.noii.to_csv(out / NOII_FILE, index=False, lineterminator="\n")
        self.crosses.to_csv(out / CROSS_FILE, index=False, lineterminator="\n")
        meta = {"format": STREAM_FORMAT, "version": STREAM_VERSION, **self.meta}
        (out / META_FILE).write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
        return out


def _px(v: int) -> float:
    return v / PRICE_SCALE


def extract_auction_stream(
    path: str | Path,
    symbols: Iterable[str] | None = None,
    date: str | None = None,
    limit_messages: int | None = None,
) -> AuctionStream:
    """One pass over an ITCH 5.0 file keeping only NOII and cross messages."""
    symbols = None if symbols is None else tuple(symbols)
    reader = Itch50Reader(path, symbols=symbols, limit_messages=limit_messages, noii=True)
    base = session_midnight_ns(date) if date else 0
    day = date or ""
    noii_rows: list[tuple] = []
    cross_rows: list[tuple] = []
    for msg in reader:
        if isinstance(msg, NetOrderImbalance):
            noii_rows.append(
                (
                    day,
                    msg.stock,
                    base + msg.ts,
                    msg.ts,
                    msg.cross_type,
                    msg.paired_shares,
                    msg.imbalance_shares,
                    msg.imbalance_direction,
                    msg.far_price,
                    msg.near_price,
                    msg.current_reference_price,
                    _px(msg.far_price),
                    _px(msg.near_price),
                    _px(msg.current_reference_price),
                    msg.price_variation,
                )
            )
        elif isinstance(msg, CrossTrade):
            cross_rows.append(
                (
                    day,
                    msg.stock,
                    base + msg.ts,
                    msg.ts,
                    msg.cross_type,
                    msg.shares,
                    msg.price,
                    _px(msg.price),
                )
            )
    noii = pd.DataFrame(noii_rows, columns=list(NOII_COLUMNS))
    crosses = pd.DataFrame(cross_rows, columns=list(CROSS_COLUMNS))
    meta = {
        "source": Path(path).name,
        "date": date,
        "symbols": None if symbols is None else sorted(symbols),
        "noii_messages": len(noii_rows),
        "cross_messages": len(cross_rows),
        "noii_messages_in_file": reader.counts.get("I", 0),
        "messages_read": reader.messages_read,
    }
    return AuctionStream(noii, crosses, meta)


def read_auction_stream(stream_dir: str | Path) -> AuctionStream:
    d = Path(stream_dir)
    meta_path = d / META_FILE
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    if meta and meta.get("format") != STREAM_FORMAT:
        raise ValueError(f"{meta_path}: not an {STREAM_FORMAT} directory")
    text = {"date": str, "symbol": str, "cross_type": str}
    noii = pd.read_csv(
        d / NOII_FILE,
        dtype={**text, "imbalance_direction": str, "price_variation": str},
        keep_default_na=False,
    )
    crosses = pd.read_csv(d / CROSS_FILE, dtype=text, keep_default_na=False)
    return AuctionStream(noii, crosses, meta)
