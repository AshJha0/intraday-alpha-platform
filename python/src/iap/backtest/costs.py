"""Research-backtester cost model (spec §18; configs/execution.json).

Pinned per-execution cost of trading ``q`` units at a row with mid ``m``
and half-spread ``hs`` (all in price units of the instrument):

    unit        = lot_size for FX (1 qty unit = lot_size base ccy,
                  conventions §1), 1 for EQUITY/ETF (qty already in shares)
    spread_cost = |q| * unit * hs               (taker crosses the spread)
    fee_cost    = |q| * fee_per_share             (EQUITY/ETF)
                = notional * commission_per_million / 1e6   (FX)
    impact_cost = impact_bps * 1e-4 * |q| * unit * m
    impact_bps  = impact_coeff_bps_per_pct_adv * (|q| * unit / adv * 100)

with notional = |q| * unit * m, so every cost is in real quote-currency
terms, matching the engine's P&L accounting (also scaled by unit).  The
impact term is linear in child size as a fraction of ADV — a deliberately
simple, auditable research model; the production simulator owns queue-level
realism.  A ``multiplier`` scales the TOTAL cost (stress grid
{0.5, 1, 2} pinned in the config).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class CostModel:
    impact_coeff_bps_per_pct_adv: float
    equity_taker_fee_per_share: float
    fx_commission_per_million: float
    multiplier: float = 1.0

    @classmethod
    def load(cls, execution_config_path, multiplier: float = 1.0) -> "CostModel":
        blob = json.loads(Path(execution_config_path).read_text())
        cm = blob.get("cost_model")
        if cm is None:
            raise ValueError(
                f"{execution_config_path}: missing 'cost_model' section"
            )
        return cls(
            impact_coeff_bps_per_pct_adv=float(cm["impact_coeff_bps_per_pct_adv"]),
            equity_taker_fee_per_share=float(cm["equity_taker_fee_per_share"]),
            fx_commission_per_million=float(cm["fx_commission_per_million"]),
            multiplier=multiplier,
        )

    def with_multiplier(self, multiplier: float) -> "CostModel":
        return CostModel(
            self.impact_coeff_bps_per_pct_adv,
            self.equity_taker_fee_per_share,
            self.fx_commission_per_million,
            multiplier,
        )

    def cost_components(
        self,
        qty: np.ndarray,
        mid: np.ndarray,
        half_spread: np.ndarray,
        asset_class: str,
        adv: float,
        lot_size: int,
    ) -> dict:
        """{'spread', 'fee', 'impact'} arrays per execution row (multiplier
        applied to each; qty signed units)."""
        aq = np.abs(np.asarray(qty, dtype=float))
        mid = np.asarray(mid, dtype=float)
        hs = np.asarray(half_spread, dtype=float)
        if asset_class in ("EQUITY", "ETF"):
            unit = 1.0
            fee_cost = aq * self.equity_taker_fee_per_share
        elif asset_class == "FX":
            unit = float(lot_size)
            notional = aq * unit * mid
            fee_cost = notional * self.fx_commission_per_million / 1e6
        else:
            raise ValueError(f"unknown asset class {asset_class!r}")
        if adv <= 0:
            raise ValueError("adv must be positive")
        spread_cost = aq * unit * hs
        impact_bps = self.impact_coeff_bps_per_pct_adv * (aq * unit / adv * 100.0)
        impact_cost = impact_bps * 1e-4 * aq * unit * mid
        m = self.multiplier
        return {"spread": m * spread_cost, "fee": m * fee_cost, "impact": m * impact_cost}

    def execution_costs(
        self,
        qty: np.ndarray,
        mid: np.ndarray,
        half_spread: np.ndarray,
        asset_class: str,
        adv: float,
        lot_size: int,
    ) -> np.ndarray:
        """Total cost per execution row (arrays aligned; qty signed units)."""
        c = self.cost_components(qty, mid, half_spread, asset_class, adv, lot_size)
        return c["spread"] + c["fee"] + c["impact"]
