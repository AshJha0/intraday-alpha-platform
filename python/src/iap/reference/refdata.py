"""Reference-data service: instruments, venues, sessions, calendars.

Reads the JSON configs under ``configs/`` (instruments.json, venues.json).
Prices on contracts are integer ticks; this service owns the tick_size /
lot_size mapping used to convert to/from real prices.

Corporate actions: the API surface (`corporate_actions`) exists so research
code can be written against it, but the synthetic universe defines none —
splits/dividends/mergers are explicitly OUT OF SCOPE for synthetic data
(spec section 8 requires them only for licensed historical data). The method
always returns an empty list here and is the extension point for a real feed.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_NS_PER_SEC = 1_000_000_000


class Instrument:
    """Static reference data for one instrument."""

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
    """Static reference data for one venue."""

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


class ReferenceData:
    """Loads and indexes configs/instruments.json + configs/venues.json."""

    def __init__(self, instruments_cfg: dict, venues_cfg: dict) -> None:
        self._by_symbol: Dict[str, Instrument] = {}
        self._by_id: Dict[int, Instrument] = {}
        for row in instruments_cfg["instruments"]:
            inst = Instrument(row)
            if inst.symbol in self._by_symbol or inst.instrument_id in self._by_id:
                raise ValueError(f"duplicate instrument: {inst.symbol}")
            self._by_symbol[inst.symbol] = inst
            self._by_id[inst.instrument_id] = inst
        self._venue_by_name: Dict[str, Venue] = {}
        self._venue_by_id: Dict[int, Venue] = {}
        for row in venues_cfg["venues"]:
            ven = Venue(row)
            if ven.venue in self._venue_by_name or ven.venue_id in self._venue_by_id:
                raise ValueError(f"duplicate venue: {ven.venue}")
            self._venue_by_name[ven.venue] = ven
            self._venue_by_id[ven.venue_id] = ven
        self.trading_days: List[str] = list(
            instruments_cfg["calendar"]["trading_days"]
        )
        self._sessions: dict = instruments_cfg["sessions"]

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

    def venues(self, asset_class: Optional[str] = None) -> List[Venue]:
        out = [self._venue_by_id[k] for k in sorted(self._venue_by_id)]
        if asset_class is not None:
            out = [v for v in out if v.asset_class == asset_class]
        return out

    # ------------------------------------------------- sessions and calendars

    def is_trading_day(self, date: str) -> bool:
        """True if YYYY-MM-DD is in the synthetic trading calendar."""
        return date in self.trading_days

    def session_bounds_ns(self, asset_class: str, date: str) -> Tuple[int, int]:
        """(open_ns, close_ns) since epoch (UTC) for asset_class on date."""
        if date not in self.trading_days:
            raise ValueError(f"{date} is not a trading day in the synthetic calendar")
        sess = self._sessions.get(asset_class)
        if sess is None:
            raise ValueError(f"no session definition for asset class {asset_class!r}")

        def _ns(timestr: str) -> int:
            day = _dt.datetime.strptime(date, "%Y-%m-%d").replace(
                tzinfo=_dt.timezone.utc
            )
            h, m, s = (int(x) for x in timestr.split(":"))
            return int(
                (day + _dt.timedelta(hours=h, minutes=m, seconds=s)).timestamp()
            ) * _NS_PER_SEC

        return _ns(sess["open"]), _ns(sess["close"])

    # ------------------------------------------------------- corporate actions

    def corporate_actions(self, key, start_date: str = "", end_date: str = "") -> list:
        """Corporate actions for an instrument over [start_date, end_date].

        Synthetic universe: always [] — corporate actions are out of scope for
        synthetic data (see module docstring). Real-data waves replace this
        with a feed-backed implementation keeping this exact signature.
        """
        self.instrument(key)  # validate the key
        return []
