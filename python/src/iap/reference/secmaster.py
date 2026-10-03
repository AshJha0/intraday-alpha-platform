"""Point-in-time security master (docs/REAL_DATA.md §5).

One record per ``(symbol, effective_date)``: the attributes of a security
as the venue published them for that session (for ITCH 5.0, the day's
Stock Directory ``R`` message).  Records are only ever read through
:meth:`SecurityMaster.as_of`, which returns the record with the greatest
``effective_date <= date`` and raises :class:`SecurityMasterError` when
there is none — a study can never read an attribute from a later date, and
a symbol that did not exist yet is an error rather than a silent default.

The file form (``<dataset>/reference/security_master.json``) is a sorted
JSON document, byte-deterministic for the same records.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path

SECURITY_MASTER_VERSION = 1


class SecurityMasterError(ValueError):
    """No record is valid for the requested symbol and date."""


@dataclass(frozen=True)
class SecurityRecord:
    """Attributes of one security valid from ``effective_date`` (inclusive)
    until the next record of the same symbol."""

    symbol: str
    effective_date: str
    instrument_id: int
    venue: str
    asset_class: str
    tick_size: float
    round_lot_size: int
    source: str
    locate: int | None = None
    market_category: str | None = None
    financial_status: str | None = None
    round_lots_only: str | None = None
    issue_classification: str | None = None
    issue_subtype: str | None = None
    authenticity: str | None = None
    short_sale_threshold: str | None = None
    ipo_flag: str | None = None
    luld_tier: str | None = None
    etp_flag: str | None = None
    etp_leverage_factor: int | None = None
    inverse_indicator: str | None = None

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("security record: symbol must be non-empty")
        _dt.date.fromisoformat(self.effective_date)
        if type(self.instrument_id) is not int or self.instrument_id < 1:
            raise ValueError(f"security record {self.symbol}: instrument_id must be >= 1")
        if type(self.round_lot_size) is not int or self.round_lot_size < 1:
            raise ValueError(f"security record {self.symbol}: round_lot_size must be >= 1")
        if not self.tick_size > 0:
            raise ValueError(f"security record {self.symbol}: tick_size must be > 0")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, doc: dict) -> SecurityRecord:
        known = {f.name for f in fields(cls)}
        extra = sorted(set(doc) - known)
        if extra:
            raise ValueError(f"security record: unknown fields {extra}")
        return cls(**doc)


class SecurityMaster:
    """Date-keyed instrument reference data with as-of queries."""

    def __init__(self, records: list[SecurityRecord] | None = None) -> None:
        self._by_symbol: dict[str, dict[str, SecurityRecord]] = {}
        for rec in records or []:
            self.upsert(rec)

    def upsert(self, record: SecurityRecord) -> None:
        """Insert the record, replacing one with the same (symbol, date)."""
        self._by_symbol.setdefault(record.symbol, {})[record.effective_date] = record

    def symbols(self) -> list[str]:
        """Every symbol that has at least one record (sorted)."""
        return sorted(self._by_symbol)

    def dates(self, symbol: str) -> list[str]:
        """Effective dates recorded for ``symbol`` (sorted; ``[]`` if unknown)."""
        return sorted(self._by_symbol.get(symbol, {}))

    def as_of(self, symbol: str, date: str) -> SecurityRecord:
        """The record valid on ``date``: the latest one effective on or before it."""
        _dt.date.fromisoformat(date)
        history = self._by_symbol.get(symbol)
        if not history:
            raise SecurityMasterError(f"no security-master record for {symbol!r}")
        eligible = [d for d in history if d <= date]
        if not eligible:
            raise SecurityMasterError(
                f"{symbol!r} has no record on or before {date} "
                f"(earliest is {min(history)}): refusing to read a later date"
            )
        return history[max(eligible)]

    def symbols_on(self, date: str) -> list[str]:
        """Symbols with a record effective exactly on ``date`` (the day's
        directory), sorted."""
        return sorted(s for s, history in self._by_symbol.items() if date in history)

    def records(self) -> list[SecurityRecord]:
        """Every record, sorted by (symbol, effective_date)."""
        return [self._by_symbol[s][d] for s in sorted(self._by_symbol) for d in self.dates(s)]

    # -- persistence ----------------------------------------------------

    def to_document(self) -> dict:
        return {
            "x-version": SECURITY_MASTER_VERSION,
            "description": (
                "Point-in-time security master: one record per (symbol, effective_date); "
                "read through SecurityMaster.as_of (latest record on or before the date)."
            ),
            "records": [r.to_dict() for r in self.records()],
        }

    def save(self, path: str | Path) -> None:
        text = json.dumps(self.to_document(), indent=2, sort_keys=True) + "\n"
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    @classmethod
    def load(cls, path: str | Path) -> SecurityMaster:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        if doc.get("x-version") != SECURITY_MASTER_VERSION:
            raise ValueError(
                f"{path}: security master x-version {doc.get('x-version')!r}, "
                f"this reader understands {SECURITY_MASTER_VERSION}"
            )
        return cls([SecurityRecord.from_dict(r) for r in doc["records"]])
