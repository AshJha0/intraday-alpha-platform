"""Per-(instrument, session) average daily volume (v1.17 A5, review X-9).

An ingested dataset's ``instruments.json`` rows carry ``adv_by_session``
(v1.14, D-P2-8): the ADV AS OF each session (the mean volume of the
dataset's earlier sessions; the first session's own volume for the first),
keyed ``YYYY-MM-DD`` in the session calendar's zone.  Until v1.17 only the
backtester and the capacity proxy read it; the execution simulator sized
impact on the one dataset-level ``adv`` of :class:`InstrumentSpec`.

:class:`SessionAdvTable` is the shared reader: ``adv(instrument_id,
exchange_ts)`` is the value of the session the timestamp falls in, the
instrument's static ADV when that session (or the instrument) is not in
the table, ``None`` when neither is known.  The session date is the
calendar date of ``exchange_ts`` in ``timezone`` (``America/New_York``
unless the table says otherwise: a US equity session never crosses local
midnight).  Lookups are cached per hour of event time, so a fill or a
feature emission costs one dictionary read.

Two file shapes build one:

- an ``instruments.json`` (:meth:`SessionAdvTable.from_instruments_json`):
  the rows' ``adv`` and ``adv_by_session``;
- a plain table (:meth:`SessionAdvTable.from_json`)::

      {"x-version": 1, "timezone": "America/New_York",
       "adv_by_session": {"1": {"2026-05-18": 41234567.0, ...}, ...},
       "adv": {"1": 40000000.0}}          # optional static fallbacks

:meth:`SessionAdvTable.from_path` picks by the file's top-level keys.
A table is immutable once built; ``is_static`` is True when no instrument
has per-session values (every reader then behaves exactly as without a
table, which is how the default stays byte-identical).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

__all__ = ["DEFAULT_SESSION_TIMEZONE", "SessionAdvTable"]

DEFAULT_SESSION_TIMEZONE = "America/New_York"
_NS_PER_HOUR = 3_600 * 1_000_000_000


class SessionAdvTable:
    """Immutable per-(instrument, session) ADV lookup (module docs)."""

    __slots__ = ("_static", "_by_session", "timezone", "_tz", "_date_cache")

    def __init__(
        self,
        by_session: Mapping[int, Mapping[str, float]],
        static: Mapping[int, float] | None = None,
        timezone: str = DEFAULT_SESSION_TIMEZONE,
    ) -> None:
        self._by_session: dict[int, dict[str, float]] = {}
        for iid, days in by_session.items():
            rows = {}
            for day, v in days.items():
                val = float(v)
                if not val > 0.0:
                    raise ValueError(f"adv_by_session[{iid}][{day}] must be > 0, got {v}")
                rows[str(day)] = val
            if rows:
                self._by_session[int(iid)] = rows
        self._static: dict[int, float] = {}
        for iid, v in (static or {}).items():
            val = float(v)
            if not val > 0.0:
                raise ValueError(f"adv[{iid}] must be > 0, got {v}")
            self._static[int(iid)] = val
        self.timezone = timezone
        try:
            self._tz = ZoneInfo(timezone)
        except Exception as exc:  # zoneinfo raises several types
            raise ValueError(f"unknown session timezone {timezone!r}") from exc
        self._date_cache: dict[int, str] = {}

    # ----------------------------------------------------------- queries

    @property
    def is_static(self) -> bool:
        """True when no instrument carries per-session values."""
        return not self._by_session

    @property
    def instruments(self) -> list[int]:
        """Sorted instrument ids with per-session values."""
        return sorted(self._by_session)

    def sessions(self, instrument_id: int) -> list[str]:
        """Sorted session dates of one instrument (empty if none)."""
        return sorted(self._by_session.get(instrument_id, ()))

    def session_date(self, exchange_ts: int) -> str:
        """``YYYY-MM-DD`` of ``exchange_ts`` (epoch ns) in the table's zone."""
        hour = exchange_ts // _NS_PER_HOUR
        day = self._date_cache.get(hour)
        if day is None:
            dt = datetime.fromtimestamp(hour * 3_600, tz=UTC).astimezone(self._tz)
            day = dt.strftime("%Y-%m-%d")
            if len(self._date_cache) > 4096:
                self._date_cache.clear()
            self._date_cache[hour] = day
        return day

    def adv(
        self, instrument_id: int, exchange_ts: int, default: float | None = None
    ) -> float | None:
        """ADV of the session ``exchange_ts`` falls in; the instrument's
        static value (the table's, else ``default``) when that session is
        not in the table; ``default`` (``None``) for an unknown instrument."""
        days = self._by_session.get(instrument_id)
        if days is not None:
            v = days.get(self.session_date(exchange_ts))
            if v is not None:
                return v
        s = self._static.get(instrument_id)
        return s if s is not None else default

    def static(self, instrument_id: int) -> float | None:
        return self._static.get(instrument_id)

    def to_dict(self) -> dict:
        """The plain-table JSON shape (module docs)."""
        return {
            "x-version": 1,
            "timezone": self.timezone,
            "adv": {str(i): self._static[i] for i in sorted(self._static)},
            "adv_by_session": {
                str(i): dict(sorted(self._by_session[i].items())) for i in sorted(self._by_session)
            },
        }

    # ------------------------------------------------------------ builders

    @classmethod
    def from_instruments_json(
        cls, path: str | Path, timezone: str | None = None
    ) -> SessionAdvTable:
        """From ``configs/instruments/instruments.json`` (rows' ``adv`` and,
        when present, ``adv_by_session``).  ``timezone`` defaults to the
        file's ``sessions.<asset_class>.timezone`` of the first instrument
        that has one, else :data:`DEFAULT_SESSION_TIMEZONE`."""
        root = _read(path)
        rows = root.get("instruments")
        if not isinstance(rows, list):
            raise ValueError(f"{path}: missing 'instruments' array")
        by: dict[int, dict[str, float]] = {}
        static: dict[int, float] = {}
        tz = timezone
        sessions = root.get("sessions") if isinstance(root.get("sessions"), dict) else {}
        for row in rows:
            if not isinstance(row, dict) or "instrument_id" not in row:
                raise ValueError(f"{path}: instrument entry must be an object with instrument_id")
            iid = int(row["instrument_id"])
            if isinstance(row.get("adv"), (int, float)) and not isinstance(row.get("adv"), bool):
                static[iid] = float(row["adv"])
            per = row.get("adv_by_session")
            if isinstance(per, dict) and per:
                by[iid] = {str(d): float(v) for d, v in per.items()}
                if tz is None:
                    sess = sessions.get(str(row.get("asset_class")))
                    if isinstance(sess, dict) and isinstance(sess.get("timezone"), str):
                        tz = sess["timezone"]
        return cls(by, static, tz or DEFAULT_SESSION_TIMEZONE)

    @classmethod
    def from_json(cls, path: str | Path) -> SessionAdvTable:
        """From the plain-table shape (module docs)."""
        root = _read(path)
        by = root.get("adv_by_session")
        if not isinstance(by, dict):
            raise ValueError(f"{path}: missing 'adv_by_session' object")
        static = root.get("adv") or {}
        if not isinstance(static, dict):
            raise ValueError(f"{path}: 'adv' must be an object")
        tz = root.get("timezone", DEFAULT_SESSION_TIMEZONE)
        if not isinstance(tz, str):
            raise ValueError(f"{path}: 'timezone' must be a string")
        return cls(
            {int(i): {str(d): float(v) for d, v in days.items()} for i, days in by.items()},
            {int(i): float(v) for i, v in static.items()},
            tz,
        )

    @classmethod
    def from_path(cls, path: str | Path) -> SessionAdvTable:
        """``from_instruments_json`` for a file with an ``instruments``
        array, else ``from_json``."""
        root = _read(path)
        if isinstance(root.get("instruments"), list):
            return cls.from_instruments_json(path)
        return cls.from_json(path)


def _read(path: str | Path) -> dict:
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"missing ADV table {p}")
    with open(p, encoding="utf-8") as f:
        root = json.load(f)
    if not isinstance(root, dict):
        raise ValueError(f"{p}: top-level JSON value must be an object")
    return root
