"""Research-backtester cost model (spec §18; configs/execution/execution.json).

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

**Opt-in square-root impact** (``impact_model="sqrt"``; the pinned default
is ``"linear"`` and every committed report uses it).  Linear impact makes
the cost per share proportional to size, which understates the cost of
small orders relative to large ones and makes capacity look unbounded only
quadratically.  The empirical regularity is concave:

    impact_bps  = sqrt_impact_coeff_bps * sqrt(|q| * unit / adv)

with ``sqrt_impact_coeff_bps`` the impact, in basis points, of trading one
full ADV (the familiar ``Y * sigma_daily``; e.g. Y ~ 1 and a 1 % daily
volatility give 100 bps).  Both keys are read from the ``cost_model`` config
block when present (``impact_model``, ``sqrt_impact_coeff_bps``) and default
to the linear model when absent, so the committed config is unchanged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np


#: Pinned impact models (module docs).
IMPACT_MODELS = ("linear", "sqrt")


@dataclass(frozen=True)
class CostModel:
    impact_coeff_bps_per_pct_adv: float
    equity_taker_fee_per_share: float
    fx_commission_per_million: float
    multiplier: float = 1.0
    #: "linear" (pinned default) or "sqrt" (module docs)
    impact_model: str = "linear"
    #: impact in bps of trading one full ADV (``impact_model="sqrt"`` only)
    sqrt_impact_coeff_bps: float = 0.0

    def __post_init__(self) -> None:
        if self.impact_model not in IMPACT_MODELS:
            raise ValueError(
                f"unknown impact_model {self.impact_model!r}; known: {IMPACT_MODELS}")
        if self.sqrt_impact_coeff_bps < 0.0:
            raise ValueError("sqrt_impact_coeff_bps must be >= 0")

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
            impact_model=str(cm.get("impact_model", "linear")),
            sqrt_impact_coeff_bps=float(cm.get("sqrt_impact_coeff_bps", 0.0)),
        )

    def with_multiplier(self, multiplier: float) -> "CostModel":
        return replace(self, multiplier=multiplier)

    def with_sqrt_impact(self, coeff_bps: float) -> "CostModel":
        """This model with square-root impact of ``coeff_bps`` at one ADV."""
        return replace(self, impact_model="sqrt", sqrt_impact_coeff_bps=float(coeff_bps))

    def impact_bps(self, participation: np.ndarray) -> np.ndarray:
        """Impact in bps (before the multiplier) of trading ``participation``
        = size / ADV (a fraction, not a percentage) under the active model."""
        part = np.asarray(participation, dtype=float)
        if self.impact_model == "sqrt":
            return self.sqrt_impact_coeff_bps * np.sqrt(part)
        return self.impact_coeff_bps_per_pct_adv * (part * 100.0)

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
        if self.impact_model == "sqrt":
            impact_bps = self.sqrt_impact_coeff_bps * np.sqrt(aq * unit / adv)
        else:
            impact_bps = self.impact_coeff_bps_per_pct_adv * (aq * unit / adv * 100.0)
        impact_cost = impact_bps * 1e-4 * aq * unit * mid
        m = self.multiplier
        return {"spread": m * spread_cost, "fee": m * fee_cost, "impact": m * impact_cost}

    def round_trip_cost_return(
        self,
        mid: np.ndarray,
        half_spread: np.ndarray,
        asset_class: str,
    ) -> np.ndarray:
        """Spread + fee of opening AND closing one unit, as a return.

        ``multiplier * (2 * half_spread + 2 * fee_per_unit) / mid`` — the
        per-unit fee is ``equity_taker_fee_per_share`` for EQUITY/ETF and
        ``mid * fx_commission_per_million / 1e6`` for FX.  Impact is left
        out: it scales with the order size and this is the size-free hurdle
        an expected return has to clear before trading at all (the engine's
        ``cost_aware`` position policy).  NaN wherever ``mid`` is not a
        positive finite number or the half-spread is invalid.
        """
        mid = np.asarray(mid, dtype=float)
        hs = np.asarray(half_spread, dtype=float)
        if asset_class in ("EQUITY", "ETF"):
            fee = np.full(mid.shape, self.equity_taker_fee_per_share)
        elif asset_class == "FX":
            fee = mid * self.fx_commission_per_million / 1e6
        else:
            raise ValueError(f"unknown asset class {asset_class!r}")
        ok = np.isfinite(mid) & (mid > 0.0) & np.isfinite(hs) & (hs >= 0.0)
        out = np.full(mid.shape, np.nan)
        out[ok] = self.multiplier * (2.0 * hs[ok] + 2.0 * fee[ok]) / mid[ok]
        return out

    def breakeven_size(
        self,
        edge_return: float,
        mid: float,
        half_spread: float,
        asset_class: str,
        adv: float,
        lot_size: int,
    ) -> float:
        """Order size (qty units) at which the edge per trade equals its cost.

        A round trip of ``q`` units earns ``edge_return * q * unit * mid`` and
        costs the spread and fee (per unit, both legs) plus impact on both
        legs, which grows with ``q``.  Per unit of notional:

            edge_return = round_trip_cost_return
                          + 2 * multiplier * impact_bps(q * unit / adv) * 1e-4

        solved for ``q`` under the active impact model.  Returns 0 when the
        edge does not cover spread + fee even at zero size, and ``inf`` when
        the model charges no impact (nothing bounds the size).  This is the
        capacity of the EDGE — the size beyond which the next share loses
        money — where :func:`iap.validation.metrics.capacity_proxy_usd` is
        only a participation cap times ADV and knows nothing about the edge.
        """
        if not (np.isfinite(edge_return) and adv > 0):
            raise ValueError("edge_return must be finite and adv positive")
        fixed = float(self.round_trip_cost_return(
            np.array([mid]), np.array([half_spread]), asset_class)[0])
        if not np.isfinite(fixed):
            raise ValueError("mid / half_spread do not price a round trip")
        room = float(edge_return) - fixed
        if room <= 0.0:
            return 0.0
        unit = float(lot_size) if asset_class == "FX" else 1.0
        per_leg_bps = room / (2.0 * self.multiplier * 1e-4) if self.multiplier > 0 \
            else float("inf")
        if self.impact_model == "sqrt":
            if self.sqrt_impact_coeff_bps <= 0.0 or not np.isfinite(per_leg_bps):
                return float("inf")
            participation = (per_leg_bps / self.sqrt_impact_coeff_bps) ** 2
        else:
            if self.impact_coeff_bps_per_pct_adv <= 0.0 or not np.isfinite(per_leg_bps):
                return float("inf")
            participation = per_leg_bps / (self.impact_coeff_bps_per_pct_adv * 100.0)
        return float(participation * adv / unit)

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
