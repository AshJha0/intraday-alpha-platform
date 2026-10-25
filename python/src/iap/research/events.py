"""Columnar event streams for the study drivers (v1.14, review E-P1-3).

The study drivers used to load a session's events as

    arrays = [t.column(c).to_numpy().tolist() for c in cols]
    events = [MarketEvent(*row) for row in zip(*arrays)]

which holds three copies of the stream at the peak (the Arrow table, twelve
Python-int lists and the ``MarketEvent`` list) and keeps one Python object
per row for the life of the unit.

:class:`EventColumns` keeps the twelve canonical columns as ``int64`` numpy
arrays (about 96 bytes per event) and is a ``Sequence[MarketEvent]``: ``len``,
indexing (``events[-1]``) and iteration behave exactly like the list, so it
can be handed unchanged to anything that replays ``MarketEvent`` objects
(``MakerBacktester.run_instrument``, ``estimate_calibration``,
``maker_labels``).  Iteration is a cursor: rows are decoded ``chunk`` at a
time with ``ndarray.tolist()`` (Python ints, exactly as before), so the
objects a consumer sees are equal field for field and the replay is
byte-identical; only one chunk of objects is alive at a time.

:func:`read_event_columns` reads ``normalized/events.parquet`` row group by
row group with ``pyarrow.compute`` filters.  It never imports
``pyarrow.dataset`` (a Windows Application Control policy can block that
DLL), and returns the same rows in the same file order as
``pq.read_table(path, columns=..., filters=...)``.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import overload

import numpy as np

from iap.core.events import MarketEvent

#: canonical field order (``MarketEvent``; JSONL/IAP1)
EVENT_COLUMNS: tuple[str, ...] = (
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
DEFAULT_CHUNK = 65_536


@dataclass(frozen=True, eq=False)
class EventColumns(Sequence[MarketEvent]):
    """The canonical event columns as equal-length ``int64`` arrays."""

    columns: dict[str, np.ndarray]
    chunk: int = DEFAULT_CHUNK

    def __post_init__(self) -> None:
        missing = [c for c in EVENT_COLUMNS if c not in self.columns]
        if missing:
            raise ValueError(f"missing event columns: {missing}")
        cols = {c: np.ascontiguousarray(self.columns[c], dtype=np.int64) for c in EVENT_COLUMNS}
        n = {len(a) for a in cols.values()}
        if len(n) > 1:
            raise ValueError("event columns must have equal length")
        if self.chunk < 1:
            raise ValueError("chunk must be >= 1")
        object.__setattr__(self, "columns", cols)

    # -- construction ------------------------------------------------------

    @staticmethod
    def from_events(events: Sequence[MarketEvent], chunk: int = DEFAULT_CHUNK) -> EventColumns:
        cols = {
            c: np.fromiter((getattr(e, c) for e in events), dtype=np.int64, count=len(events))
            for c in EVENT_COLUMNS
        }
        return EventColumns(cols, chunk)

    @staticmethod
    def empty() -> EventColumns:
        return EventColumns({c: np.empty(0, dtype=np.int64) for c in EVENT_COLUMNS})

    # -- Sequence ----------------------------------------------------------

    def __len__(self) -> int:
        return int(self.columns["event_id"].shape[0])

    def _row(self, i: int) -> MarketEvent:
        return MarketEvent(*(int(self.columns[c][i]) for c in EVENT_COLUMNS))

    @overload
    def __getitem__(self, i: int) -> MarketEvent: ...

    @overload
    def __getitem__(self, i: slice) -> EventColumns: ...

    def __getitem__(self, i):
        if isinstance(i, slice):
            return EventColumns({c: a[i] for c, a in self.columns.items()}, self.chunk)
        n = len(self)
        j = int(i)
        if j < 0:
            j += n
        if not 0 <= j < n:
            raise IndexError("event index out of range")
        return self._row(j)

    def __iter__(self) -> Iterator[MarketEvent]:
        """The cursor: ``chunk`` rows decoded at a time, Python ints as before."""
        n = len(self)
        cols = [self.columns[c] for c in EVENT_COLUMNS]
        for lo in range(0, n, self.chunk):
            hi = min(lo + self.chunk, n)
            yield from (
                MarketEvent(*row) for row in zip(*(a[lo:hi].tolist() for a in cols), strict=True)
            )

    # -- views -------------------------------------------------------------

    def select(self, mask: np.ndarray) -> EventColumns:
        """The rows where ``mask`` holds (order kept)."""
        mask = np.asarray(mask, dtype=bool)
        if mask.shape != (len(self),):
            raise ValueError("mask must be row-aligned")
        return EventColumns({c: a[mask] for c, a in self.columns.items()}, self.chunk)

    def for_instrument(self, instrument_id: int) -> EventColumns:
        return self.select(self.columns["instrument_id"] == int(instrument_id))

    def venues(self) -> list[int]:
        """Sorted distinct venue ids."""
        return [int(v) for v in np.unique(self.columns["venue_id"])]

    def first_venue(self) -> int | None:
        v = self.columns["venue_id"]
        return int(v[0]) if v.size else None

    @property
    def nbytes(self) -> int:
        return int(sum(a.nbytes for a in self.columns.values()))

    def to_list(self) -> list[MarketEvent]:
        return list(self)


def read_event_columns(
    path: Path | str,
    instrument_id: int,
    end_ns: int | None = None,
    *,
    chunk: int = DEFAULT_CHUNK,
) -> EventColumns:
    """One instrument's events of an events parquet, file order, no ``pyarrow.dataset``."""
    import pyarrow.compute as pc

    from iap import io as iap_io

    pf = iap_io.parquet_file(path, purpose="research.events")
    cols = list(EVENT_COLUMNS)
    parts: dict[str, list[np.ndarray]] = {c: [] for c in cols}
    for rg in range(pf.num_row_groups):
        t = pf.read_row_group(rg, columns=cols)
        mask = pc.equal(t.column("instrument_id"), int(instrument_id))
        if end_ns is not None:
            mask = pc.and_(mask, pc.less_equal(t.column("exchange_ts"), int(end_ns)))
        t = t.filter(mask)
        if t.num_rows:
            for c in cols:
                parts[c].append(t.column(c).to_numpy().astype(np.int64, copy=False))
        del t
    if not parts["event_id"]:
        return EventColumns.empty()
    return EventColumns({c: np.concatenate(v) for c, v in parts.items()}, chunk)


__all__ = ["DEFAULT_CHUNK", "EVENT_COLUMNS", "EventColumns", "read_event_columns"]
