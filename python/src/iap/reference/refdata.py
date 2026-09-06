"""Reference-data service: instruments, venues, sessions, calendars.

Reads the JSON configs under ``configs/`` (instruments.json, venues.json).
Prices on contracts are integer ticks; this service owns the tick_size /
lot_size mapping used to convert to/from real prices.

Validation is fail-fast (pinned, API_CORE.md section 7): every instrument
must carry a positive finite ``tick_size``, an integer ``lot_size >= 1``, a
positive ``ref_price``, a non-negative ``adv`` and at least one venue that
exists in ``venues.json`` with the same asset class; venue ids must be in
1..65535 (0 is the synthetic "any venue" book id) and unique; unknown
instrument/venue keys raise ``ValueError``.

Sessions (pinned): each asset class defines ``timezone`` (an IANA name, e.g.
``America/New_York``; ``UTC`` for the synthetic universe) and local ``open`` /
``close`` wall-clock times. ``session_bounds_ns`` converts them to UTC
nanoseconds through ``zoneinfo`` for the requested trading day, so DST
transitions are honoured. FX additionally defines the trading week
(``fx_week``: Sunday 17:00 -> Friday 17:00 America/New_York by default);
``is_open(asset_class, ts_ns)`` answers "is the market open at this instant".

Corporate actions: the API surface (`corporate_actions`) exists so research
code can be written against it, but the synthetic universe defines none —
splits/dividends/mergers are explicitly OUT OF SCOPE for synthetic data
(spec section 8 requires them only for licensed historical data). The method
always returns an empty list here and is the extension point for a real feed.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_NS_PER_SEC = 1_000_000_000

#: Default FX trading week (pinned): Sunday 17:00 New York -> Friday 17:00
#: New York (22:00 UTC in winter, 21:00 UTC in summer).
DEFAULT_FX_WEEK = {
    "timezone": "America/New_York",
    "open_weekday": 6,
    "open": "17:00:00",
    "close_weekday": 4,
    "close": "17:00:00",
}


def _parse_hms(timestr: str, what: str) -> Tuple[int, int, int]:
    parts = timestr.split(":")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"{what}: time must be HH:MM:SS, got {timestr!r}")
    h, m, s = (int(p) for p in parts)
    if not (0 <= h <= 23 and 0 <= m <= 59 and 0 <= s <= 59):
        raise ValueError(f"{what}: time out of range: {timestr!r}")
    return h, m, s


def _zone(name: str, what: str) -> ZoneInfo:
    if not isinstance(name, str) or not name:
        raise ValueError(f"{what}: timezone must be an IANA name, got {name!r}")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        raise ValueError(f"{what}: unknown IANA timezone {name!r}") from None


def _local_ns(date: _dt.date, hms: Tuple[int, int, int], tz: ZoneInfo) -> int:
    local = _dt.datetime(date.year, date.month, date.day, *hms, tzinfo=tz)
    return int(local.timestamp()) * _NS_PER_SEC


class Instrument:
    """Static reference data for one instrument (validated on construction)."""

    __slots__ = (
        "symbol",
        "instrument_id",
        "asset_class",
        "tick_size",
        "lot_size",
        "ref_price",
        "adv",
        "venues",
        "pip",
    )

    def __init__(self, row: dict) -> None:
        self.symbol: str = row["symbol"]
        self.instrument_id: int = row["instrument_id"]
        self.asset_class: str = row["asset_class"]
        self.tick_size: float = row["tick_size"]
        self.lot_size: int = row["lot_size"]
        self.ref_price: float = row["ref_price"]
        self.adv: int = row["adv"]
        self.venues: List[str] = list(row["venues"])
        self.pip: Optional[float] = row.get("pip")
        what = f"instrument {self.symbol!r}"
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ValueError(f"{what}: symbol must be a non-empty string")
        if type(self.instrument_id) is not int or not (1 <= self.instrument_id <= 0xFFFFFFFF):
            raise ValueError(f"{what}: instrument_id must be an integer in 1..2^32-1")
        if not isinstance(self.tick_size, (int, float)) or isinstance(self.tick_size, bool):
            raise ValueError(f"{what}: tick_size must be a number")
        if not (math.isfinite(self.tick_size) and self.tick_size > 0):
            raise ValueError(f"{what}: tick_size must be > 0, got {self.tick_size!r}")
        if type(self.lot_size) is not int or self.lot_size < 1:
            raise ValueError(f"{what}: lot_size must be an integer >= 1, got {self.lot_size!r}")
        if not (isinstance(self.ref_price, (int, float)) and math.isfinite(self.ref_price)
                and self.ref_price > 0):
            raise ValueError(f"{what}: ref_price must be > 0, got {self.ref_price!r}")
        if type(self.adv) is not int or self.adv < 0:
            raise ValueError(f"{what}: adv must be an integer >= 0, got {self.adv!r}")
        if not self.venues:
            raise ValueError(f"{what}: needs at least one venue")
        if self.pip is not None and not (math.isfinite(self.pip) and self.pip > 0):
            raise ValueError(f"{what}: pip must be > 0 when present")

    def price_to_ticks(self, price: float) -> int:
        """Convert a real price to integer ticks (round half away from zero)."""
        scaled = price / self.tick_size
        return int(scaled + 0.5) if scaled >= 0 else -int(-scaled + 0.5)

    def ticks_to_price(self, ticks: int) -> float:
        """Convert integer ticks to a real price (research double)."""
        return ticks * self.tick_size

    @property
    def ref_price_ticks(self) -> int:
        return self.price_to_ticks(self.ref_price)


class Venue:
    """Static reference data for one venue (validated on construction)."""

    __slots__ = ("venue", "venue_id", "asset_class", "fees", "latency_mean_ns",
                 "latency_jitter_ns", "supports")

    def __init__(self, row: dict) -> None:
        self.venue: str = row["venue"]
        self.venue_id: int = row["venue_id"]
        self.asset_class: str = row["asset_class"]
        self.fees: dict = {
            k: v
            for k, v in row.items()
            if k in ("taker_fee_per_share", "maker_rebate_per_share",
                     "commission_per_million")
        }
        self.latency_mean_ns: int = row["latency"]["mean_ns"]
        self.latency_jitter_ns: int = row["latency"]["jitter_ns"]
        self.supports: List[str] = list(row["supports"])
        what = f"venue {self.venue!r}"
        if not isinstance(self.venue, str) or not self.venue:
            raise ValueError(f"{what}: venue must be a non-empty string")
        if type(self.venue_id) is not int or not (1 <= self.venue_id <= 0xFFFF):
            raise ValueError(f"{what}: venue_id must be an integer in 1..65535 (0 is reserved)")
        if type(self.latency_mean_ns) is not int or self.latency_mean_ns < 0:
            raise ValueError(f"{what}: latency.mean_ns must be an integer >= 0")
        if type(self.latency_jitter_ns) is not int or self.latency_jitter_ns < 0:
            raise ValueError(f"{what}: latency.jitter_ns must be an integer >= 0")
        for k, v in self.fees.items():
            if not (isinstance(v, (int, float)) and math.isfinite(v) and v >= 0):
                raise ValueError(f"{what}: fee {k} must be a finite number >= 0")


class _Session:
    __slots__ = ("tz", "open", "close")

    def __init__(self, asset_class: str, cfg: dict, default_tz: str) -> None:
        what = f"session {asset_class!r}"
        self.tz = _zone(cfg.get("timezone", default_tz), what)
        self.open = _parse_hms(cfg["open"], what)
        self.close = _parse_hms(cfg["close"], what)
        if self.open >= self.close:
            raise ValueError(f"{what}: open must be before close")


class _FxWeek:
    __slots__ = ("tz", "open_weekday", "open", "close_weekday", "close")

    def __init__(self, cfg: dict) -> None:
        what = "fx_week"
        self.tz = _zone(cfg["timezone"], what)
        self.open_weekday = int(cfg["open_weekday"])
        self.close_weekday = int(cfg["close_weekday"])
        if not (0 <= self.open_weekday <= 6 and 0 <= self.close_weekday <= 6):
            raise ValueError(f"{what}: weekdays must be 0 (Mon) .. 6 (Sun)")
        self.open = _parse_hms(cfg["open"], what)
        self.close = _parse_hms(cfg["close"], what)

    def is_open(self, ts_ns: int) -> bool:
        """True iff ``ts_ns`` (UTC ns) falls inside the weekly FX window."""
        local = _dt.datetime.fromtimestamp(ts_ns // _NS_PER_SEC, tz=self.tz)
        # Locate the most recent weekly open (on or before `local`).
        days_back = (local.weekday() - self.open_weekday) % 7
        open_day = local.date() - _dt.timedelta(days=days_back)
        open_ns = _local_ns(open_day, self.open, self.tz)
        if open_ns > ts_ns:
            open_day -= _dt.timedelta(days=7)
            open_ns = _local_ns(open_day, self.open, self.tz)
        days_fwd = (self.close_weekday - self.open_weekday) % 7
        close_day = open_day + _dt.timedelta(days=days_fwd)
        close_ns = _local_ns(close_day, self.close, self.tz)
        return open_ns <= ts_ns < close_ns


class ReferenceData:
    """Loads, validates and indexes configs/instruments.json + configs/venues.json."""

    def __init__(self, instruments_cfg: dict, venues_cfg: dict) -> None:
        self._venue_by_name: Dict[str, Venue] = {}
        self._venue_by_id: Dict[int, Venue] = {}
        for row in venues_cfg["venues"]:
            ven = Venue(row)
            if ven.venue in self._venue_by_name:
                raise ValueError(f"duplicate venue name: {ven.venue}")
            if ven.venue_id in self._venue_by_id:
                raise ValueError(f"duplicate venue id: {ven.venue_id} ({ven.venue})")
            self._venue_by_name[ven.venue] = ven
            self._venue_by_id[ven.venue_id] = ven
        self._by_symbol: Dict[str, Instrument] = {}
        self._by_id: Dict[int, Instrument] = {}
        for row in instruments_cfg["instruments"]:
            inst = Instrument(row)
            if inst.symbol in self._by_symbol or inst.instrument_id in self._by_id:
                raise ValueError(f"duplicate instrument: {inst.symbol}")
            for vname in inst.venues:
                ven = self._venue_by_name.get(vname)
                if ven is None:
                    raise ValueError(
                        f"instrument {inst.symbol!r}: venue {vname!r} not in venues.json"
                    )
                if ven.asset_class != self._venue_class(inst.asset_class):
                    raise ValueError(
                        f"instrument {inst.symbol!r}: venue {vname!r} is a "
                        f"{ven.asset_class} venue, instrument is {inst.asset_class}"
                    )
            self._by_symbol[inst.symbol] = inst
            self._by_id[inst.instrument_id] = inst
        calendar = instruments_cfg["calendar"]
        self.trading_days: List[str] = list(calendar["trading_days"])
        for d in self.trading_days:
            _dt.date.fromisoformat(d)  # ValueError on malformed dates
        if self.trading_days != sorted(set(self.trading_days)):
            raise ValueError("calendar.trading_days must be sorted and unique")
        default_tz = calendar.get("timezone", "UTC")
        self._sessions: Dict[str, _Session] = {
            ac: _Session(ac, cfg, default_tz)
            for ac, cfg in instruments_cfg["sessions"].items()
        }
        self.fx_week = _FxWeek(instruments_cfg.get("fx_week", DEFAULT_FX_WEEK))

    @staticmethod
    def _venue_class(asset_class: str) -> str:
        return "EQUITY" if asset_class in ("EQUITY", "ETF") else asset_class

    @classmethod
    def load(cls, config_dir) -> "ReferenceData":
        """Load from a configs/ directory (expects instruments.json, venues.json)."""
        config_dir = Path(config_dir)
        with open(config_dir / "instruments.json") as f:
            instruments_cfg = json.load(f)
        with open(config_dir / "venues.json") as f:
            venues_cfg = json.load(f)
        return cls(instruments_cfg, venues_cfg)

    # ------------------------------------------------------------- instruments

    def instrument(self, key) -> Instrument:
        """Look up an instrument by symbol (str) or instrument_id (int)."""
        table = self._by_id if isinstance(key, int) else self._by_symbol
        inst = table.get(key)
        if inst is None:
            raise ValueError(f"unknown instrument: {key!r}")
        return inst

    def has_instrument(self, instrument_id: int) -> bool:
        return instrument_id in self._by_id

    def instruments(self, asset_class: Optional[str] = None) -> List[Instrument]:
        """All instruments (sorted by id), optionally filtered by asset class."""
        out = [self._by_id[k] for k in sorted(self._by_id)]
        if asset_class is not None:
            classes = (
                ("EQUITY", "ETF") if asset_class == "EQUITY" else (asset_class,)
            )
            out = [i for i in out if i.asset_class in classes]
        return out

    def tick_size(self, key) -> float:
        return self.instrument(key).tick_size

    def lot_size(self, key) -> int:
        return self.instrument(key).lot_size

    # ------------------------------------------------------------------ venues

    def venue(self, key) -> Venue:
        """Look up a venue by name (str) or venue_id (int)."""
        table = self._venue_by_id if isinstance(key, int) else self._venue_by_name
        ven = table.get(key)
        if ven is None:
            raise ValueError(f"unknown venue: {key!r}")
        return ven

    def has_venue(self, venue_id: int) -> bool:
        return venue_id in self._venue_by_id

    def venues(self, asset_class: Optional[str] = None) -> List[Venue]:
        out = [self._venue_by_id[k] for k in sorted(self._venue_by_id)]
        if asset_class is not None:
            out = [v for v in out if v.asset_class == asset_class]
        return out

    def venue_ids_for(self, instrument_id: int) -> List[int]:
        """Sorted venue ids on which the instrument trades."""
        inst = self.instrument(instrument_id)
        return sorted(self._venue_by_name[v].venue_id for v in inst.venues)

    # ------------------------------------------------- sessions and calendars

    def is_trading_day(self, date: str) -> bool:
        """True if YYYY-MM-DD is in the trading calendar."""
        return date in self.trading_days

    def session_timezone(self, asset_class: str) -> str:
        """IANA timezone name the asset class's session is defined in."""
        return self._session(asset_class).tz.key

    def _session(self, asset_class: str) -> _Session:
        sess = self._sessions.get(asset_class)
        if sess is None:
            raise ValueError(f"no session definition for asset class {asset_class!r}")
        return sess

    def session_bounds_ns(self, asset_class: str, date: str) -> Tuple[int, int]:
        """(open_ns, close_ns) UTC ns since epoch for asset_class on a trading day.

        Local session times are converted through the session's IANA zone, so
        the UTC bounds shift with DST.
        """
        if date not in self.trading_days:
            raise ValueError(f"{date} is not a trading day in the calendar")
        sess = self._session(asset_class)
        day = _dt.date.fromisoformat(date)
        return _local_ns(day, sess.open, sess.tz), _local_ns(day, sess.close, sess.tz)

    def is_open(self, asset_class: str, ts_ns: int) -> bool:
        """True iff the market for ``asset_class`` is open at UTC ``ts_ns``.

        FX: inside the weekly window (``fx_week``). Others: inside the
        session of a calendar trading day.
        """
        if asset_class == "FX":
            return self.fx_week.is_open(ts_ns)
        sess = self._session(asset_class)
        local = _dt.datetime.fromtimestamp(ts_ns // _NS_PER_SEC, tz=sess.tz)
        date = local.date().isoformat()
        if date not in self.trading_days:
            return False
        open_ns, close_ns = self.session_bounds_ns(asset_class, date)
        return open_ns <= ts_ns < close_ns

    # ------------------------------------------------------- corporate actions

    def corporate_actions(self, key, start_date: str = "", end_date: str = "") -> list:
        """Corporate actions for an instrument over [start_date, end_date].

        Synthetic universe: always [] — corporate actions are out of scope for
        synthetic data (see module docstring). Real-data waves replace this
        with a feed-backed implementation keeping this exact signature.
        """
        self.instrument(key)  # validate the key
        return []
