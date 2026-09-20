"""Hard-risk limits (``configs/risk/risk.json``, x-version 3).

Mirrors ``rust/risk/src/limits.rs`` (normative): the complete pinned limit
set plus the currency block. Parsing is STRICT and checks the fields in the
same order as the Rust struct initialiser, so the first error message is
identical; any missing or invalid limit raises ``ValueError`` with the Rust
message text (``"risk.json: ..."``), and the engine built from a failed
parse is fail-closed (rejects every order with ``CONFIG_MISSING``).

Type strictness follows ``serde_json``'s accessors: a number is read by
``as_f64`` (int or float, finite), an integer by ``as_i64`` / ``as_u64``
(a JSON float such as ``50000.0`` is NOT an integer), a bool by ``as_bool``
(never coerced) and a string by ``as_str``.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping

from iap.risk.serialize import rust_display_f64

__all__ = ["FxConversion", "RiskLimits", "RISK_CONFIG_VERSION"]

#: The ``x-version`` of ``configs/risk/risk.json`` this parser reads.
RISK_CONFIG_VERSION = 3

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

    @classmethod
    def from_json(cls, doc: Any) -> "RiskLimits":
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
        )

    @classmethod
    def load(cls, path: "str | Path") -> "RiskLimits":
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


def _as_f64(v: Any) -> "float | None":
    return float(v) if _is_number(v) else None


def _as_i64(v: Any) -> "int | None":
    if isinstance(v, int) and not isinstance(v, bool) and _I64_MIN <= v <= _I64_MAX:
        return v
    return None


def _as_u64(v: Any) -> "int | None":
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
        raise ValueError(
            f"risk.json: {section}.{key} must be > 0, got {rust_display_f64(v)}"
        )
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


def _parse_conversion(doc: Any) -> Dict[str, FxConversion]:
    table = _lookup(doc, "currency", "conversion")
    if not isinstance(table, dict):
        raise ValueError("risk.json: missing currency.conversion object")
    out: Dict[str, FxConversion] = {}
    for ccy in sorted(table):
        spec = table[ccy]
        iid = _as_u64(spec.get("instrument_id")) if isinstance(spec, dict) else None
        if iid is None:
            raise ValueError(
                f"risk.json: currency.conversion.{ccy}.instrument_id missing/invalid"
            )
        if iid == 0 or iid > _U32_MAX:
            raise ValueError(
                f"risk.json: currency.conversion.{ccy}.instrument_id out of u32 range"
            )
        invert = spec.get("invert")
        if not isinstance(invert, bool):
            raise ValueError(
                f"risk.json: currency.conversion.{ccy}.invert missing/non-bool"
            )
        out[ccy] = FxConversion(instrument_id=iid, invert=invert)
    return out
