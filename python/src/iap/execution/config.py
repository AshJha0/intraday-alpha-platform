"""Execution configuration: ``ExecConfig`` and its loaders.

Resolution mirrors the C++ reference (``load_venues`` in ``execution.cpp``)
and the Java ``ConfigService`` / ``PaperTrading`` wiring that builds an
``ExecConfig`` from the config tree:

- venues from ``configs/venues/venues.json`` (fees, commission, latency
  profile; ``asset_class == "FX"`` marks a commission venue);
- instruments from ``configs/instruments/instruments.json`` (``qty_unit`` =
  ``lot_size`` for FX and ``1.0`` for EQUITY/ETF, quote currency from
  ``quote_currency`` / ``currency``; ``tick_size``, ``lot_size`` and ``adv``
  must be > 0 — fail-fast, conventions section 12.2);
- ``seed`` from ``configs/execution/execution.json`` ``defaults.seed``,
  ``impact_coeff_bps_per_pct_adv`` from its ``cost_model`` block and the
  SOR options from its ``sor`` block.

Every validation failure is a ``ValueError`` naming the file and the key.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Mapping, Union

from iap.execution.types import InstrumentSpec, LatencyConfig, VenueSpec

PathLike = Union[str, Path]

#: Config file paths relative to the config directory (conventions section 0).
VENUES_FILE = "venues/venues.json"
INSTRUMENTS_FILE = "instruments/instruments.json"
EXECUTION_FILE = "execution/execution.json"


@dataclass(frozen=True, slots=True)
class SorOptions:
    """``configs/execution/execution.json`` ``sor`` block (see ``sor.py``)."""

    prefer_rebate: bool = True
    max_venue_latency_ns: int = (1 << 63) - 1


@dataclass(frozen=True)
class ExecConfig:
    """Simulator configuration; treated as immutable once handed to a simulator."""

    latency: LatencyConfig = LatencyConfig()
    seed: int = 20260829
    impact_coeff_bps_per_pct_adv: float = 2.0
    instruments: Mapping[int, InstrumentSpec] = field(default_factory=dict)
    venues: Mapping[int, VenueSpec] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.seed, int) or self.seed < 0:
            raise ValueError(f"seed must be a non-negative int, got {self.seed!r}")
        # Freeze the maps in ascending key order (deterministic iteration).
        object.__setattr__(
            self, "instruments", dict(sorted(self.instruments.items()))
        )
        object.__setattr__(self, "venues", dict(sorted(self.venues.items())))

    def venue(self, venue_id: int) -> VenueSpec:
        """Venue profile; raises ValueError on an unknown venue_id."""
        spec = self.venues.get(venue_id)
        if spec is None:
            raise ValueError(f"unknown venue_id {venue_id}")
        return spec

    def instrument(self, instrument_id: int) -> InstrumentSpec:
        """Instrument reference data; raises ValueError on an unknown instrument_id."""
        spec = self.instruments.get(instrument_id)
        if spec is None:
            raise ValueError(f"unknown instrument_id {instrument_id}")
        return spec


def _read_json(path: PathLike) -> dict:
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"missing config file {p}")
    with open(p, encoding="utf-8") as f:
        root = json.load(f)
    if not isinstance(root, dict):
        raise ValueError(f"{p}: top-level JSON value must be an object")
    return root


def _number(row: dict, key: str, where: str) -> float:
    v = row.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"{where}: missing/non-numeric {key}")
    return float(v)


def _integer(row: dict, key: str, where: str) -> int:
    v = row.get(key)
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError(f"{where}: missing/non-integer {key}")
    return v


def load_venues(path: PathLike) -> Dict[int, VenueSpec]:
    """Load every venue from ``configs/venues/venues.json`` keyed by venue_id."""
    root = _read_json(path)
    rows = root.get("venues")
    if not isinstance(rows, list):
        raise ValueError(f"{path}: missing 'venues' array")
    out: Dict[int, VenueSpec] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"{path}: venue entry must be an object")
        vid = _integer(row, "venue_id", f"{path} venue")
        name = row.get("venue")
        if not isinstance(name, str) or not name:
            raise ValueError(f"{path}: venue {vid} missing 'venue' name")
        lat = row.get("latency")
        if not isinstance(lat, dict):
            raise ValueError(f"{path}: venue {name} missing 'latency' block")
        where = f"{path} venue {name}"
        spec = VenueSpec(
            venue_id=vid,
            name=name,
            is_fx=row.get("asset_class") == "FX",
            taker_fee_per_share=(
                _number(row, "taker_fee_per_share", where)
                if "taker_fee_per_share" in row else 0.0
            ),
            maker_rebate_per_share=(
                _number(row, "maker_rebate_per_share", where)
                if "maker_rebate_per_share" in row else 0.0
            ),
            commission_per_million=(
                _number(row, "commission_per_million", where)
                if "commission_per_million" in row else 0.0
            ),
            latency_mean_ns=_integer(lat, "mean_ns", f"{where} latency"),
            latency_jitter_ns=_integer(lat, "jitter_ns", f"{where} latency"),
        )
        if vid in out:
            raise ValueError(f"{path}: duplicate venue_id {vid}")
        out[vid] = spec
    if not out:
        raise ValueError(f"no venues in {path}")
    return dict(sorted(out.items()))


def load_instruments(path: PathLike) -> Dict[int, InstrumentSpec]:
    """Load instrument reference data from ``configs/instruments/instruments.json``."""
    root = _read_json(path)
    rows = root.get("instruments")
    if not isinstance(rows, list):
        raise ValueError(f"{path}: missing 'instruments' array")
    out: Dict[int, InstrumentSpec] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"{path}: instrument entry must be an object")
        for key in ("instrument_id", "tick_size", "lot_size", "adv", "asset_class"):
            if key not in row:
                raise ValueError(f"{path}: instrument missing {key}")
        iid = _integer(row, "instrument_id", f"{path} instrument")
        where = f"{path} instrument {iid}"
        tick = _number(row, "tick_size", where)
        if iid <= 0 or not tick > 0.0:
            raise ValueError(f"{path}: bad instrument_id/tick_size for {iid}")
        lot = _number(row, "lot_size", where)
        adv = _number(row, "adv", where)
        if not lot > 0.0:
            raise ValueError(f"{path}: lot_size must be > 0 for instrument {iid}, got {lot}")
        if not adv > 0.0:
            raise ValueError(f"{path}: adv must be > 0 for instrument {iid}, got {adv}")
        asset_class = str(row["asset_class"])
        if asset_class == "FX":
            unit = lot
            ccy = row.get("quote_currency")
        elif asset_class in ("EQUITY", "ETF"):
            unit = 1.0
            ccy = row.get("currency")
        else:
            raise ValueError(f"{path}: unknown asset_class {asset_class} for {iid}")
        if not isinstance(ccy, str) or not ccy:
            raise ValueError(f"{path}: missing currency for {iid}")
        if iid in out:
            raise ValueError(f"{path}: duplicate instrument_id {iid}")
        out[iid] = InstrumentSpec(iid, tick, unit, adv, ccy)
    if not out:
        raise ValueError(f"{path}: empty universe")
    return dict(sorted(out.items()))


def load_sor_options(path: PathLike) -> SorOptions:
    """``execution.json`` ``sor.{prefer_rebate, max_venue_latency_ns}`` (strict)."""
    root = _read_json(path)
    sor = root.get("sor")
    if not isinstance(sor, dict):
        raise ValueError(f"{path}: missing sor block")
    pr = sor.get("prefer_rebate")
    if not isinstance(pr, bool):
        raise ValueError(f"{path}: missing/non-bool sor.prefer_rebate")
    return SorOptions(pr, int(_number(sor, "max_venue_latency_ns", f"{path} sor")))


def load_exec_config(
    config_dir: PathLike, latency: LatencyConfig = LatencyConfig()
) -> ExecConfig:
    """Build the ``ExecConfig`` the platform runs with from a config directory.

    Mirrors the Java ``PaperTrading`` wiring: ``LatencyConfig`` defaults (or
    the one given), ``defaults.seed`` and ``cost_model.impact_coeff_bps_per_pct_adv``
    from ``execution/execution.json``, every instrument and every venue.
    """
    base = Path(config_dir)
    exec_path = base / EXECUTION_FILE
    root = _read_json(exec_path)
    defaults = root.get("defaults")
    if not isinstance(defaults, dict):
        raise ValueError(f"{exec_path}: missing defaults block")
    cost_model = root.get("cost_model")
    if not isinstance(cost_model, dict):
        raise ValueError(f"{exec_path}: missing cost_model block")
    return ExecConfig(
        latency=latency,
        seed=_integer(defaults, "seed", f"{exec_path} defaults"),
        impact_coeff_bps_per_pct_adv=_number(
            cost_model, "impact_coeff_bps_per_pct_adv", f"{exec_path} cost_model"
        ),
        instruments=load_instruments(base / INSTRUMENTS_FILE),
        venues=load_venues(base / VENUES_FILE),
    )
