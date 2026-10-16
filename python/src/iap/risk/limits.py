"""Hard-risk limits (``configs/risk/risk.json``, x-version 4).

Mirrors ``rust/risk/src/limits.rs`` (normative): the complete pinned limit
set plus the currency block. Parsing is STRICT and checks the fields in the
same order as the Rust struct initialiser, so the first error message is
identical; any missing or invalid limit raises ``ValueError`` with the Rust
message text (``"risk.json: ..."``), and the engine built from a failed
parse is fail-closed (rejects every order with ``CONFIG_MISSING``).

v1.17 (F8) adds, still optional and parsed last: ``pre_trade.venue_types``
(the lit / dark / midpoint type of each venue id), ``pre_trade.
allowed_venue_types`` (check 31), ``pre_trade.max_dark_participation_bps``
+ ``dark_participation_floor_qty`` (check 32) and
``pre_trade.max_dark_fill_notional`` (check 33).

Type strictness follows ``serde_json``'s accessors: a number is read by
``as_f64`` (int or float, finite), an integer by ``as_i64`` / ``as_u64``
(a JSON float such as ``50000.0`` is NOT an integer), a bool by ``as_bool``
(never coerced) and a string by ``as_str``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from iap.risk.serialize import rust_display_f64

__all__ = ["FxConversion", "RiskLimits", "RISK_CONFIG_VERSION", "VENUE_TYPES"]

#: The ``x-version`` of ``configs/risk/risk.json`` this parser reads.
RISK_CONFIG_VERSION = 4

#: The venue types the pre-trade controls know (v1.17 F8; the Rust
#: ``VenueType`` enum), in the pinned order type names appear in reasons.
#: Every venue is ``"lit"`` unless ``pre_trade.venue_types`` says otherwise;
#: ``"dark"`` and ``"midpoint"`` are the non-displayed types checks 32-33 count.
VENUE_TYPES = ("lit", "dark", "midpoint")

_U32_MAX = (1 << 32) - 1
_I64_MAX = (1 << 63) - 1
_I64_MIN = -(1 << 63)
_U64_MAX = (1 << 64) - 1


@dataclass(frozen=True)
class FxConversion:
    """How one quote currency converts into the reporting currency: the
    last consolidated mid of ``instrument_id`` (an FX pair), inverted when
    the pair is quoted ``REPORTING/CCY`` (e.g. USD/JPY for JPY -> USD)."""

    #: The FX pair whose mid is the conversion rate (u32, > 0).
    instrument_id: int
    #: ``True``: rate = 1 / mid (pair quoted REPORTING/CCY); ``False``: rate = mid.
    invert: bool


@dataclass(frozen=True)
class RiskLimits:
    """The complete pinned limit set (field order = Rust struct order)."""

    #: Start with the global kill switch engaged.
    kill_switch_engaged: bool
    #: Firm-wide gross notional cap (reporting currency).
    max_gross_notional: float
    #: Firm-wide |net| notional cap (reporting currency).
    max_net_notional: float
    #: Firm-wide daily loss limit (positive number).
    max_daily_loss: float
    #: Token-bucket refill rate per strategy (orders/second).
    max_order_rate_per_sec: float
    #: Token-bucket capacity per strategy.
    order_rate_burst: float
    #: Fat-finger quantity cap per order.
    max_order_qty: int
    #: Fat-finger notional cap per order (reporting currency).
    max_order_notional: float
    #: Price band around the last mid (bps).
    price_band_bps: float
    #: Reject orders when the reference price is stale.
    stale_book_reject: bool
    #: Duplicate-order-id window (ns); 0 = the entire session.
    duplicate_order_window_ns: int
    #: Per-instrument absolute position cap.
    max_position_qty: int
    #: Per-instrument absolute marked-notional cap (reporting currency).
    max_instrument_notional: float
    #: Per-strategy daily loss limit (positive number).
    strategy_max_daily_loss: float
    #: Sequence gaps tolerated before the feed gate closes (u64).
    max_sequence_gap_before_halt: int
    #: Reference-price staleness timeout (ns).
    stale_feed_timeout_ns: int
    #: Currency every limit and P&L figure is expressed in.
    reporting_ccy: str
    #: Quote currency -> conversion source (``currency.conversion``); a quote
    #: currency equal to ``reporting_ccy`` needs no entry. Sorted mapping.
    fx_conversion: Mapping[str, FxConversion]
    # ---- x-version 4: every key below is OPTIONAL (absent or null =
    # ---- that control is off); a present key is parsed strictly.
    #: ``market_data.max_mid_jump_ratio`` (> 1): a two-sided update whose
    #: ``bid + ask`` is more than this factor above / below the stored
    #: two-sided one is dropped (G-P2-6). Crossed books are always dropped.
    max_mid_jump_ratio: float | None = None
    #: ``pre_trade.restricted_instruments`` (check 23).
    restricted_instruments: frozenset[int] = frozenset()
    #: ``pre_trade.max_open_orders`` per strategy (check 24).
    max_open_orders: int | None = None
    #: ``pre_trade.max_open_notional`` per strategy (check 25).
    max_open_notional: float | None = None
    #: ``pre_trade.max_strategy_position_qty`` per (strategy, instrument) (check 26).
    max_strategy_position_qty: int | None = None
    #: ``pre_trade.max_participation_bps`` of session market volume (check 27).
    max_participation_bps: float | None = None
    #: ``pre_trade.participation_floor_qty`` (required with the bps).
    participation_floor_qty: int | None = None
    #: ``pre_trade.max_cancels_per_sec`` per strategy (check 28).
    max_cancels_per_sec: float | None = None
    #: ``pre_trade.cancel_burst`` (required with the rate).
    cancel_burst: float | None = None
    #: ``pre_trade.max_venue_msgs_per_sec`` per venue (check 29).
    max_venue_msgs_per_sec: float | None = None
    #: ``pre_trade.venue_msg_burst`` (required with the rate).
    venue_msg_burst: float | None = None
    #: ``pre_trade.max_tracked_keys``: bound of every per-key state map (G-P2-7).
    max_tracked_keys: int | None = None
    #: ``overrides.max_loss_override_multiple`` x the configured loss limit
    #: caps an override (G-P2-3).
    max_loss_override_multiple: float | None = None
    #: ``overrides.min_approvers``: distinct comma-separated approvers an
    #: override needs (G-P2-3).
    min_approvers: int | None = None
    #: ``pre_trade.venue_types`` (v1.17 F8): venue id -> type; an unmapped
    #: venue is ``"lit"``. Absent / null = empty. Sorted mapping.
    venue_types: Mapping[int, str] = field(default_factory=dict)
    #: ``pre_trade.allowed_venue_types`` (check 31); ``None`` = off.
    allowed_venue_types: frozenset[str] | None = None
    #: ``pre_trade.max_dark_participation_bps`` (check 32).
    max_dark_participation_bps: float | None = None
    #: ``pre_trade.dark_participation_floor_qty`` (required with the bps).
    dark_participation_floor_qty: int | None = None
    #: ``pre_trade.max_dark_fill_notional`` (check 33).
    max_dark_fill_notional: float | None = None

    def venue_type(self, venue_id: int) -> str:
        """The type of ``venue_id`` as the pre-trade controls see it: the
        ``pre_trade.venue_types`` entry, else ``"lit"``."""
        return self.venue_types.get(venue_id, "lit")

    @classmethod
    def from_json(cls, doc: Any) -> RiskLimits:
        """Strict parse of a ``configs/risk/risk.json`` document (a parsed
        JSON value); ``ValueError`` names the first offending key."""
        dup = _as_i64(_lookup(doc, "per_order", "duplicate_order_window_ns"))
        if dup is None:
            raise ValueError("risk.json: missing per_order.duplicate_order_window_ns")
        if dup < 0:
            raise ValueError("risk.json: duplicate_order_window_ns must be >= 0")
        kill = _need_bool(doc, "global", "kill_switch_engaged")
        max_gross = _need_pos_f64(doc, "global", "max_gross_notional")
        max_net = _need_pos_f64(doc, "global", "max_net_notional")
        max_daily_loss = _need_pos_f64(doc, "global", "max_daily_loss")
        rate = _need_pos_f64(doc, "global", "max_order_rate_per_sec")
        burst = _need_pos_f64(doc, "global", "order_rate_burst")
        max_qty = _need_pos_i64(doc, "per_order", "max_order_qty")
        max_notional = _need_pos_f64(doc, "per_order", "max_order_notional")
        band = _need_pos_f64(doc, "per_order", "price_band_bps")
        stale_reject = _need_bool(doc, "per_order", "stale_book_reject")
        max_pos = _need_pos_i64(doc, "per_instrument", "max_position_qty")
        max_ins_notional = _need_pos_f64(doc, "per_instrument", "max_instrument_notional")
        strat_loss = _need_pos_f64(doc, "per_strategy", "max_daily_loss")
        gaps = _as_u64(_lookup(doc, "market_data", "max_sequence_gap_before_halt"))
        if gaps is None:
            raise ValueError("risk.json: missing market_data.max_sequence_gap_before_halt")
        stale_timeout = _need_pos_i64(doc, "market_data", "stale_feed_timeout_ns")
        ccy = _lookup(doc, "currency", "reporting_ccy")
        if not isinstance(ccy, str) or not ccy:
            raise ValueError("risk.json: missing/empty currency.reporting_ccy")
        conversion = _parse_conversion(doc)
        jump = _opt(doc, "market_data", "max_mid_jump_ratio", _need_pos_f64)
        if jump is not None and not jump > 1.0:
            raise ValueError(
                f"risk.json: market_data.max_mid_jump_ratio must be > 1, got "
                f"{rust_display_f64(jump)}"
            )
        restricted = _parse_restricted(doc)
        max_open_orders = _opt(doc, "pre_trade", "max_open_orders", _need_pos_i64)
        max_open_notional = _opt(doc, "pre_trade", "max_open_notional", _need_pos_f64)
        max_strat_pos = _opt(doc, "pre_trade", "max_strategy_position_qty", _need_pos_i64)
        part_bps = _opt(doc, "pre_trade", "max_participation_bps", _need_pos_f64)
        part_floor = (
            _need_pos_i64(doc, "pre_trade", "participation_floor_qty")
            if part_bps is not None
            else None
        )
        cancel_rate = _opt(doc, "pre_trade", "max_cancels_per_sec", _need_pos_f64)
        cancel_burst = (
            _need_pos_f64(doc, "pre_trade", "cancel_burst") if cancel_rate is not None else None
        )
        venue_rate = _opt(doc, "pre_trade", "max_venue_msgs_per_sec", _need_pos_f64)
        venue_burst = (
            _need_pos_f64(doc, "pre_trade", "venue_msg_burst") if venue_rate is not None else None
        )
        max_keys = _opt(doc, "pre_trade", "max_tracked_keys", _need_pos_i64)
        override_mult = _opt(doc, "overrides", "max_loss_override_multiple", _need_pos_f64)
        min_approvers = _opt(doc, "overrides", "min_approvers", _need_pos_i64)
        # v1.17 F8 keys, parsed after every x-version 4 key, in this order
        venue_types = _parse_venue_types(doc)
        allowed_venue_types = _parse_allowed_venue_types(doc)
        dark_bps = _opt(doc, "pre_trade", "max_dark_participation_bps", _need_pos_f64)
        dark_floor = (
            _need_pos_i64(doc, "pre_trade", "dark_participation_floor_qty")
            if dark_bps is not None
            else None
        )
        max_dark_notional = _opt(doc, "pre_trade", "max_dark_fill_notional", _need_pos_f64)
        return cls(
            kill_switch_engaged=kill,
            max_gross_notional=max_gross,
            max_net_notional=max_net,
            max_daily_loss=max_daily_loss,
            max_order_rate_per_sec=rate,
            order_rate_burst=burst,
            max_order_qty=max_qty,
            max_order_notional=max_notional,
            price_band_bps=band,
            stale_book_reject=stale_reject,
            duplicate_order_window_ns=dup,
            max_position_qty=max_pos,
            max_instrument_notional=max_ins_notional,
            strategy_max_daily_loss=strat_loss,
            max_sequence_gap_before_halt=gaps,
            stale_feed_timeout_ns=stale_timeout,
            reporting_ccy=ccy,
            fx_conversion=conversion,
            max_mid_jump_ratio=jump,
            restricted_instruments=restricted,
            max_open_orders=max_open_orders,
            max_open_notional=max_open_notional,
            max_strategy_position_qty=max_strat_pos,
            max_participation_bps=part_bps,
            participation_floor_qty=part_floor,
            max_cancels_per_sec=cancel_rate,
            cancel_burst=cancel_burst,
            max_venue_msgs_per_sec=venue_rate,
            venue_msg_burst=venue_burst,
            max_tracked_keys=max_keys,
            max_loss_override_multiple=override_mult,
            min_approvers=min_approvers,
            venue_types=venue_types,
            allowed_venue_types=allowed_venue_types,
            max_dark_participation_bps=dark_bps,
            dark_participation_floor_qty=dark_floor,
            max_dark_fill_notional=max_dark_notional,
        )

    @classmethod
    def load(cls, path: str | Path) -> RiskLimits:
        """Load and strictly parse a risk.json file (``ValueError`` on an
        unreadable file, malformed JSON or an invalid limit set)."""
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as e:
            raise ValueError(f"io error: {e}") from None
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise ValueError(f"codec error: risk.json: {e}") from None
        return cls.from_json(doc)


def _lookup(doc: Any, section: str, key: str) -> Any:
    """``doc[section][key]`` with serde_json's indexing semantics: a
    missing key or a non-object container yields ``None`` (Null)."""
    if not isinstance(doc, dict):
        return None
    sec = doc.get(section)
    if not isinstance(sec, dict):
        return None
    return sec.get(key)


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _as_f64(v: Any) -> float | None:
    return float(v) if _is_number(v) else None


def _as_i64(v: Any) -> int | None:
    if isinstance(v, int) and not isinstance(v, bool) and _I64_MIN <= v <= _I64_MAX:
        return v
    return None


def _as_u64(v: Any) -> int | None:
    if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= _U64_MAX:
        return v
    return None


def _need_f64(doc: Any, section: str, key: str) -> float:
    v = _as_f64(_lookup(doc, section, key))
    if v is None:
        raise ValueError(f"risk.json: missing/non-numeric {section}.{key}")
    if not math.isfinite(v):
        raise ValueError(f"risk.json: non-finite {section}.{key}")
    return v


def _need_pos_f64(doc: Any, section: str, key: str) -> float:
    v = _need_f64(doc, section, key)
    if v <= 0.0:
        raise ValueError(f"risk.json: {section}.{key} must be > 0, got {rust_display_f64(v)}")
    return v


def _need_pos_i64(doc: Any, section: str, key: str) -> int:
    v = _as_i64(_lookup(doc, section, key))
    if v is None:
        raise ValueError(f"risk.json: missing/non-integer {section}.{key}")
    if v <= 0:
        raise ValueError(f"risk.json: {section}.{key} must be > 0, got {v}")
    return v


def _need_bool(doc: Any, section: str, key: str) -> bool:
    v = _lookup(doc, section, key)
    if not isinstance(v, bool):
        raise ValueError(f"risk.json: missing/non-bool {section}.{key}")
    return v


def _opt(doc: Any, section: str, key: str, need: Any) -> Any:
    """An optional key: ``None`` when absent or null, else ``need``'s strict parse."""
    if _lookup(doc, section, key) is None:
        return None
    return need(doc, section, key)


def _parse_restricted(doc: Any) -> frozenset[int]:
    """``pre_trade.restricted_instruments``: absent / null = empty; else an
    array of u32 instrument ids."""
    v = _lookup(doc, "pre_trade", "restricted_instruments")
    if v is None:
        return frozenset()
    if not isinstance(v, list):
        raise ValueError("risk.json: pre_trade.restricted_instruments must be an array")
    out = set()
    for item in v:
        iid = _as_u64(item)
        if iid is None or iid > _U32_MAX:
            raise ValueError(
                "risk.json: pre_trade.restricted_instruments entries must be u32 instrument ids"
            )
        out.add(iid)
    return frozenset(out)


def _parse_venue_types(doc: Any) -> dict[int, str]:
    """``pre_trade.venue_types``: absent / null = empty; else an object whose
    keys are decimal u16 venue ids and whose values are ``lit`` / ``dark`` /
    ``midpoint`` (sorted by venue id, as the Rust ``BTreeMap``)."""
    v = _lookup(doc, "pre_trade", "venue_types")
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise ValueError("risk.json: pre_trade.venue_types must be an object")
    out: dict[int, str] = {}
    for k, t in v.items():
        vid = _parse_u16_key(k)
        if vid is None:
            raise ValueError(f"risk.json: pre_trade.venue_types key {k} is not a u16 venue id")
        if not isinstance(t, str) or t not in VENUE_TYPES:
            raise ValueError(
                f"risk.json: pre_trade.venue_types.{k} must be one of lit, dark, midpoint"
            )
        out[vid] = t
    return dict(sorted(out.items()))


def _parse_u16_key(k: Any) -> int | None:
    """Rust ``str::parse::<u16>``: an optional ``+``, decimal digits, in range."""
    if not isinstance(k, str) or not k:
        return None
    digits = k[1:] if k[0] == "+" else k
    if not digits or not digits.isascii() or not digits.isdigit():
        return None
    value = int(digits)
    return value if value <= 0xFFFF else None


def _parse_allowed_venue_types(doc: Any) -> frozenset[str] | None:
    """``pre_trade.allowed_venue_types``: absent / null = off; else a
    non-empty array of ``lit`` / ``dark`` / ``midpoint``."""
    v = _lookup(doc, "pre_trade", "allowed_venue_types")
    if v is None:
        return None
    msg = (
        "risk.json: pre_trade.allowed_venue_types must be a non-empty array of lit, dark, midpoint"
    )
    if not isinstance(v, list) or not v:
        raise ValueError(msg)
    out = set()
    for item in v:
        if not isinstance(item, str) or item not in VENUE_TYPES:
            raise ValueError(msg)
        out.add(item)
    return frozenset(out)


def _parse_conversion(doc: Any) -> dict[str, FxConversion]:
    table = _lookup(doc, "currency", "conversion")
    if not isinstance(table, dict):
        raise ValueError("risk.json: missing currency.conversion object")
    out: dict[str, FxConversion] = {}
    for ccy in sorted(table):
        spec = table[ccy]
        iid = _as_u64(spec.get("instrument_id")) if isinstance(spec, dict) else None
        if iid is None:
            raise ValueError(f"risk.json: currency.conversion.{ccy}.instrument_id missing/invalid")
        if iid == 0 or iid > _U32_MAX:
            raise ValueError(f"risk.json: currency.conversion.{ccy}.instrument_id out of u32 range")
        invert = spec.get("invert")
        if not isinstance(invert, bool):
            raise ValueError(f"risk.json: currency.conversion.{ccy}.invert missing/non-bool")
        out[ccy] = FxConversion(instrument_id=iid, invert=invert)
    return out
