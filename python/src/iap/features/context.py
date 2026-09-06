"""Per-instrument static context for the feature engine.

Built from ``configs/instruments.json`` (tick size, asset class, session
bounds) — independent of any live data.  The cross-asset reference instrument
is pinned per asset class:

- EQUITY  -> the ETF (asset_class == "ETF"; SYN.ETF.IDX, instrument_id 11)
- ETF     -> itself
- FX      -> EUR/USD (instrument_id 101); EUR/USD -> itself

For the reference instrument itself, cross-asset features degenerate
naturally (beta = 1, residual = 0, correlation = 1).

**Session time zones (pinned, API_FEATURES §3 "Time of day")**: every session
block in ``configs/instruments.json`` MUST carry an IANA ``timezone``; its
``open``/``close`` are wall-clock times *in that zone*, converted per event
with :mod:`zoneinfo`.  Every time-of-day feature (``minute_of_day``,
``session_frac``, ``is_open_phase``, ``is_close_phase``) and every
5-minute-of-day session-profile bucket is therefore keyed in SESSION-LOCAL
time, so a DST shift moves the whole session with the venue instead of
silently sliding the profile by an hour.  A missing or unknown ``timezone``
is a start-up error (fail fast) — never a silent fallback to UTC.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_FX_REF_SYMBOL = "EUR/USD"
_NS_S = 1_000_000_000
_NS_H = 3600 * _NS_S


class SessionClock:
    """UTC -> session-local conversion for one IANA time zone (cached).

    ``offset_seconds(ts_ns)`` is the venue's UTC offset at that instant.
    Offsets are cached per UTC hour: IANA transitions always land on a
    minute boundary within an hour, so an hourly cache is exact while
    keeping the hot path allocation-free after the first event of an hour.
    """

    __slots__ = ("name", "_zone", "_cache")

    def __init__(self, name: str) -> None:
        self.name = str(name)
        try:
            self._zone = ZoneInfo(self.name)
        except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
            raise ValueError(f"unknown session timezone {name!r}") from exc
        self._cache: Dict[int, int] = {}

    @property
    def is_utc(self) -> bool:
        """True when the zone never deviates from UTC (ports may assume UTC)."""
        return self.name in ("UTC", "Etc/UTC", "Universal", "Zulu")

    def offset_seconds(self, ts_ns: int) -> int:
        """Venue UTC offset in seconds at event time ``ts_ns``."""
        hour = ts_ns // _NS_H
        off = self._cache.get(hour)
        if off is None:
            dt = datetime.fromtimestamp(hour * 3600, tz=timezone.utc).astimezone(
                self._zone
            )
            off = int(dt.utcoffset().total_seconds())
            self._cache[hour] = off
        return off

    def local_second_of_day(self, ts_ns: int) -> int:
        """Seconds since session-local midnight (0..86399)."""
        return int((ts_ns // _NS_S + self.offset_seconds(ts_ns)) % 86_400)


@dataclass(frozen=True)
class InstrumentContext:
    """Static per-instrument facts the feature engine needs."""

    instrument_id: int
    symbol: str
    asset_class: str
    tick_size: float
    session_open_min: int  # minute of the SESSION-LOCAL day the session opens
    session_close_min: int  # minute of the SESSION-LOCAL day it closes
    ref_instrument_id: int  # cross-asset reference instrument
    session_timezone: str = "UTC"
    clock: SessionClock = field(
        default_factory=lambda: SessionClock("UTC"), compare=False, repr=False
    )


def _minute_of_day(hhmmss: str) -> int:
    h, m, s = (int(x) for x in hhmmss.split(":"))
    return h * 60 + m + (1 if s >= 30 else 0)


def build_contexts(config_dir: Union[str, Path]) -> Dict[int, InstrumentContext]:
    """Build {instrument_id: InstrumentContext} from configs/instruments.json."""
    config_dir = Path(config_dir)
    with open(config_dir / "instruments.json") as f:
        cfg = json.load(f)

    sessions = cfg["sessions"]
    rows = cfg["instruments"]

    etf_id = None
    fx_ref_id = None
    for row in rows:
        if row["asset_class"] == "ETF" and etf_id is None:
            etf_id = row["instrument_id"]
        if row["symbol"] == _FX_REF_SYMBOL:
            fx_ref_id = row["instrument_id"]

    clocks: Dict[str, SessionClock] = {}
    out: Dict[int, InstrumentContext] = {}
    for row in rows:
        ac = row["asset_class"]
        sess = sessions.get(ac)
        if sess is None:
            raise ValueError(f"no session definition for asset class {ac!r}")
        tzname = sess.get("timezone")
        if not tzname:
            raise ValueError(
                f"session {ac!r} has no 'timezone': sessions must declare an "
                "IANA zone (API_FEATURES §3) — UTC is never assumed"
            )
        clock = clocks.get(tzname)
        if clock is None:
            clock = clocks[tzname] = SessionClock(tzname)
        tick = float(row["tick_size"])
        if not (tick > 0.0) or tick != tick or tick == float("inf"):
            raise ValueError(
                f"instrument {row['instrument_id']}: tick_size must be finite "
                f"and > 0 (got {tick!r})"
            )
        if ac == "FX":
            ref = fx_ref_id if fx_ref_id is not None else row["instrument_id"]
        elif ac == "ETF":
            ref = row["instrument_id"]
        else:  # EQUITY
            ref = etf_id if etf_id is not None else row["instrument_id"]
        out[row["instrument_id"]] = InstrumentContext(
            instrument_id=row["instrument_id"],
            symbol=row["symbol"],
            asset_class=ac,
            tick_size=tick,
            session_open_min=_minute_of_day(sess["open"]),
            session_close_min=_minute_of_day(sess["close"]),
            ref_instrument_id=ref,
            session_timezone=tzname,
            clock=clock,
        )
    return out
