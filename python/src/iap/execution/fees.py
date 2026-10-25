"""Dated fee tables (v2.0 stage 2 D4, defect D-07).

Every fee a simulation charges comes from a schedule with an ``as_of`` date, a
``label`` (``quoted`` when copied from a published price list with its URL,
``indicative`` or ``assumed`` otherwise) and a ``source``.  The tables live in
``research/execution/fees/fee_schedules.json``; :func:`load_fee_schedule`
refuses a schedule without a valid ``as_of`` or ``label``, so an undated fee
cannot reach a number.  The engine's three cost columns
(:func:`iap.execution.sim.simulate_columns`) read their default
:class:`~iap.execution.sim.FeeSchedule` from the ``engine_default`` row.

The regulatory schedule (``configs/reference/regulatory_fee_schedule.json``,
SEC Section 31 and FINRA TAF) is dated per period already and is read by
:mod:`iap.execution.regulatory`; the assumed exchange tiers
(``configs/reference/fee_tiers.json``) and the router's venue model
(``research/execution/venue_model.json``) carry ``as_of`` since D4 and their
loaders refuse a table without one.
"""

from __future__ import annotations

import datetime as _dt
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

__all__ = [
    "FEE_LABELS",
    "FEE_SCHEDULES_PATH",
    "SCHEMA",
    "UndatedFeeError",
    "load_fee_schedule",
    "load_fee_schedules",
    "require_as_of",
]

FEE_SCHEDULES_PATH = (
    Path(__file__).resolve().parents[4] / "research" / "execution" / "fees" / "fee_schedules.json"
)
SCHEMA = "iap.fee_schedules/v1"
FEE_LABELS = ("quoted", "indicative", "assumed")


class UndatedFeeError(ValueError):
    """A fee table or schedule without a usable ``as_of`` date (or label)."""


def require_as_of(doc: dict, where: str) -> str:
    """Return ``doc["as_of"]`` as an ISO date string or raise :class:`UndatedFeeError`."""
    value = doc.get("as_of") if isinstance(doc, dict) else None
    if not isinstance(value, str):
        raise UndatedFeeError(
            f"{where}: no 'as_of' date; an undated fee table cannot be used (D-07)"
        )
    try:
        _dt.date.fromisoformat(value)
    except ValueError as exc:
        raise UndatedFeeError(f"{where}: 'as_of' {value!r} is not an ISO date") from exc
    return value


def _validate_schedule(name: str, row: Any) -> dict[str, Any]:
    where = f"fee schedule {name!r}"
    if not isinstance(row, dict):
        raise UndatedFeeError(f"{where}: not an object")
    as_of = require_as_of(row, where)
    label = row.get("label")
    if label not in FEE_LABELS:
        raise UndatedFeeError(f"{where}: label must be one of {FEE_LABELS}, got {label!r}")
    if not isinstance(row.get("source"), str) or not row["source"].strip():
        raise UndatedFeeError(f"{where}: a non-empty 'source' is required")
    numbers = {
        k: v
        for k, v in row.items()
        if k
        in (
            "per_share",
            "notional_rate",
            "taker_fee_per_share",
            "maker_rebate_per_share",
            "commission_per_share",
        )
    }
    if not numbers:
        raise UndatedFeeError(f"{where}: no fee number")
    for k, v in numbers.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise UndatedFeeError(f"{where}: {k} must be a number >= 0")
    return {**row, "as_of": as_of, "name": name}


@lru_cache(maxsize=8)
def _load(path: str) -> dict[str, dict[str, Any]]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if doc.get("schema") != SCHEMA:
        raise UndatedFeeError(f"{path}: not an {SCHEMA} document")
    schedules = doc.get("schedules")
    if not isinstance(schedules, dict) or not schedules:
        raise UndatedFeeError(f"{path}: no 'schedules'")
    return {name: _validate_schedule(name, row) for name, row in schedules.items()}


def load_fee_schedules(path: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """Every schedule of the table, validated (dated, labelled, sourced)."""
    return dict(_load(str(path or FEE_SCHEDULES_PATH)))


def load_fee_schedule(
    name: str = "engine_default", path: Path | str | None = None
) -> dict[str, Any]:
    """One validated schedule by name."""
    table = _load(str(path or FEE_SCHEDULES_PATH))
    if name not in table:
        raise KeyError(f"fee schedule {name!r} not in {sorted(table)}")
    return dict(table[name])
