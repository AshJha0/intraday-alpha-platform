"""Risk-engine reference data: ``InstrumentRef`` per instrument.

Mirrors ``risk::InstrumentRef`` (``rust/risk/src/engine.rs``):
``{tick_size, qty_unit, quote_ccy}`` where ``qty_unit`` is the real base
units per qty unit — ``lot_size`` for FX (1 qty unit = 1,000 base ccy),
``1.0`` for EQUITY/ETF because equity ``qty`` is already in shares
(PLATFORM_CONVENTIONS.md §1, §12.1) — and ``quote_ccy`` the currency of
prices and P&L.

Two builders reproduce how the reference and its ports obtain the map:

- :func:`instrument_refs_from_golden` — the explicit
  ``{instrument_id: {tick_size, qty_unit, quote_ccy}}`` table the golden
  harness passes (``rust/risk/tests/golden_risk.rs::instruments``);
- :func:`instrument_refs_from_config` / :func:`load_instrument_refs` — the
  ``configs/instruments/instruments.json`` document, derived exactly like
  the Java ``ConfigService.instruments()`` (FX: ``qty_unit = lot_size``,
  ``quote_ccy = quote_currency``; EQUITY/ETF: ``1.0`` and ``currency``;
  missing or invalid fields fail closed with ``ValueError``), and
  :func:`instrument_refs_from_reference_data` for an already loaded
  :class:`iap.reference.refdata.ReferenceData`.

Rust's ``InstrumentRef::new`` accepts any values; this port validates on
construction like the Java record (``tick_size > 0``, ``qty_unit > 0``,
non-empty ``quote_ccy``) — reference data is configuration, and a
malformed entry is a startup error, never a per-order decision.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping

from iap.reference.refdata import ReferenceData

__all__ = [
    "InstrumentRef",
    "equity_refs",
    "instrument_refs_from_golden",
    "instrument_refs_from_config",
    "instrument_refs_from_reference_data",
    "load_instrument_refs",
]

_U32_MAX = (1 << 32) - 1


@dataclass(frozen=True)
class InstrumentRef:
    """Per-instrument reference data the engine needs."""

    #: Real price per tick.
    tick_size: float
    #: Real base units per qty unit (lot_size for FX, 1 for EQUITY/ETF).
    qty_unit: float
    #: Currency of prices / P&L of this instrument.
    quote_ccy: str

    def __post_init__(self) -> None:
        for name in ("tick_size", "qty_unit"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"{name} must be a number, got {v!r}")
            if not (math.isfinite(v) and v > 0.0):
                raise ValueError(f"{name} must be > 0, got {v!r}")
            object.__setattr__(self, name, float(v))
        if not isinstance(self.quote_ccy, str) or not self.quote_ccy:
            raise ValueError("quote_ccy must be non-empty")

    @classmethod
    def equity(cls, tick_size: float) -> "InstrumentRef":
        """A USD equity: qty in shares, prices in USD."""
        return cls(tick_size, 1.0, "USD")


def equity_refs(ticks: Mapping[int, float]) -> Dict[int, InstrumentRef]:
    """``{instrument_id: tick_size}`` -> USD-equity references
    (``RiskEngine::with_ticks`` / ``from_config_ticks`` convenience)."""
    return {int(iid): InstrumentRef.equity(t) for iid, t in sorted(ticks.items())}


def _check_iid(iid: Any) -> int:
    if isinstance(iid, str):
        if not iid.isdigit():
            raise ValueError(f"instrument_id must be a u32, got {iid!r}")
        iid = int(iid)
    if isinstance(iid, bool) or not isinstance(iid, int) or not (0 <= iid <= _U32_MAX):
        raise ValueError(f"instrument_id must be a u32, got {iid!r}")
    return iid


def instrument_refs_from_golden(table: Mapping[Any, Any]) -> Dict[int, InstrumentRef]:
    """Build from the golden vector's explicit ``instruments`` table
    (keys are decimal instrument ids, values carry ``tick_size``,
    ``qty_unit`` and ``quote_ccy``)."""
    out: Dict[int, InstrumentRef] = {}
    for key in sorted(table, key=lambda k: _check_iid(k)):
        spec = table[key]
        if not isinstance(spec, dict):
            raise ValueError(f"instrument {key!r}: reference must be an object")
        out[_check_iid(key)] = InstrumentRef(
            spec["tick_size"], spec["qty_unit"], spec["quote_ccy"]
        )
    return out


def _ref_from_row(row: Mapping[str, Any]) -> tuple[int, InstrumentRef]:
    for key in ("instrument_id", "tick_size", "lot_size", "adv", "asset_class"):
        if key not in row:
            raise ValueError(f"instruments.json: instrument missing {key}")
    iid = row["instrument_id"]
    if isinstance(iid, bool) or not isinstance(iid, int) or iid <= 0 or iid > _U32_MAX:
        raise ValueError(f"instruments.json: bad instrument_id/tick_size for {iid!r}")
    tick = row["tick_size"]
    if isinstance(tick, bool) or not isinstance(tick, (int, float)) or not tick > 0.0:
        raise ValueError(f"instruments.json: bad instrument_id/tick_size for {iid}")
    lot = row["lot_size"]
    adv = row["adv"]
    for name, v in (("lot_size", lot), ("adv", adv)):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not v > 0.0:
            raise ValueError(
                f"instruments.json: {name} must be > 0 for instrument {iid}, got {v!r}"
            )
    asset_class = str(row["asset_class"])
    if asset_class == "FX":
        unit = float(lot)
        ccy = row.get("quote_currency")
    elif asset_class in ("EQUITY", "ETF"):
        unit = 1.0
        ccy = row.get("currency")
    else:
        raise ValueError(
            f"instruments.json: unknown asset_class {asset_class} for {iid}"
        )
    if not isinstance(ccy, str) or not ccy:
        raise ValueError(f"instruments.json: missing currency for {iid}")
    return iid, InstrumentRef(float(tick), unit, ccy)


def instrument_refs_from_config(doc: Any) -> Dict[int, InstrumentRef]:
    """Build from a parsed ``configs/instruments/instruments.json`` document
    (fail closed: ``ValueError`` on a missing/invalid field or an empty or
    duplicated universe)."""
    if not isinstance(doc, dict) or not isinstance(doc.get("instruments"), list):
        raise ValueError("instruments.json: missing instruments[]")
    out: Dict[int, InstrumentRef] = {}
    for row in doc["instruments"]:
        if not isinstance(row, dict):
            raise ValueError("instruments.json: instrument must be an object")
        iid, ref = _ref_from_row(row)
        if iid in out:
            raise ValueError(f"instruments.json: duplicate instrument_id {iid}")
        out[iid] = ref
    if not out:
        raise ValueError("instruments.json: empty universe")
    return {iid: out[iid] for iid in sorted(out)}


def load_instrument_refs(config_dir: "str | Path") -> Dict[int, InstrumentRef]:
    """Load ``<config_dir>/instruments/instruments.json`` (conventions §0
    layout) into the engine's reference map."""
    path = Path(config_dir) / "instruments" / "instruments.json"
    with open(path, encoding="utf-8") as f:
        return instrument_refs_from_config(json.load(f))


def instrument_refs_from_reference_data(refdata: ReferenceData) -> Dict[int, InstrumentRef]:
    """Build from a loaded :class:`ReferenceData` (same derivation as the
    config builder; an instrument without the currency the engine needs
    fails closed)."""
    out: Dict[int, InstrumentRef] = {}
    for inst in refdata.instruments():
        row = {
            "instrument_id": inst.instrument_id,
            "tick_size": inst.tick_size,
            "lot_size": inst.lot_size,
            "adv": inst.adv,
            "asset_class": inst.asset_class,
            "currency": inst.currency,
            "quote_currency": inst.quote_currency,
        }
        iid, ref = _ref_from_row(row)
        out[iid] = ref
    if not out:
        raise ValueError("instruments.json: empty universe")
    return out
