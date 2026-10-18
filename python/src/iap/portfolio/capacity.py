"""Capacity curves: net performance versus notional and participation (v1.18 REL-17).

For a candidate measured at a base notional ``N0`` (net ``m`` bps per unit
of notional, standard error ``se``, of which ``i0`` bps is market impact at
the base participation ``p0``), scaling the notional by ``k`` moves:

- gross, spread and fees: linear in notional, so unchanged in bps;
- impact: the REL-03/04 square-root rule (:class:`iap.backtest.costs.
  CostModel`, ``impact_bps = c sqrt(p)``; ``exponent`` 0.5) gives
  ``i(k) = i0 k^e`` in bps, with ``p(k) = p0 k``.

So ``net_bps(k) = (m + i0) - i0 k^e`` and ``net_usd(k) = net_bps(k) 1e-4
k N0`` per unit of the study's period.  Reported per grid point: notional,
participation, net bps with a 95 % band, net USD with the same band.
Uncertainty has two sources, both reported: the sampling error of ``m``
(normal band ``m +- z se``; a study's own t-based CI can be passed instead)
and the impact coefficient, swept over the pre-registered sensitivity scales
:data:`iap.backtest.costs.COST_SENSITIVITY_SCALES` (0.5, 1, 1.5).  The
breakeven multiplier ``k* = ((m + i0) / i0)^(1/e)`` and the notional that
maximises net USD, ``k_opt = ((m + i0) / (i0 (1 + e)))^(1/e)``, are given at
the point estimate and over the band (lower edge with the 1.5 impact scale,
upper edge with 0.5).

``i0`` must come from the study: a study that charges no impact at its base
size (``i0 = 0``) gets its impact from the cost model at ``p0``
(``impact_bps_at_base = c sqrt(p0)``, ``c`` the pinned 100 bps per ADV) and
that is stated in the row.  A candidate with neither ``i0`` nor ``p0``
cannot be scaled; its row says so instead of inventing a number.  Passive
(maker) candidates carry ``passive=True``: the square-root rule is a taker
rule, so their curve is labelled "participation only" and impact is not
charged (fills are limited by queue and participation, not priced).

**Scorecards.**  :func:`capacity_section` renders the markdown section
appended to every REL-08 / 08c / 09 / 10 scorecard (``iap.research.
scorecard_capacity`` does the appending without touching the hash-bound
study modules).  With no positive candidate the section is present, says
"not applicable" and why.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from iap.backtest.costs import COST_SENSITIVITY_SCALES, DEFAULT_SQRT_IMPACT_COEFF_BPS

__all__ = [
    "DEFAULT_MULTIPLIERS",
    "CapacityInput",
    "capacity_curve",
    "capacity_section",
]

#: Notional multipliers of the base size reported by default.
DEFAULT_MULTIPLIERS = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0)
_Z95 = 1.959963984540054


@dataclass(frozen=True)
class CapacityInput:
    """One candidate at its base size (module docs)."""

    candidate_id: str
    net_bps: float
    net_bps_se: float | None
    base_notional: float | None
    impact_bps_at_base: float | None = None
    base_participation: float | None = None
    impact_exponent: float = 0.5
    passive: bool = False
    net_bps_ci: tuple[float, float] | None = None
    unit: str = "per period"
    note: str = ""


def _k_star(a: float, i0: float, e: float) -> float | None:
    if i0 <= 0:
        return None if a <= 0 else math.inf
    return (a / i0) ** (1.0 / e) if a > 0 else 0.0


def _k_opt(a: float, i0: float, e: float) -> float | None:
    if i0 <= 0 or a <= 0:
        return None
    return (a / (i0 * (1.0 + e))) ** (1.0 / e)


def capacity_curve(
    c: CapacityInput,
    multipliers: Sequence[float] = DEFAULT_MULTIPLIERS,
    impact_coeff_bps: float = DEFAULT_SQRT_IMPACT_COEFF_BPS,
    impact_scales: Sequence[float] = COST_SENSITIVITY_SCALES,
) -> dict[str, Any]:
    """The capacity curve of one candidate (module docs)."""
    out: dict[str, Any] = {
        "candidate": c.candidate_id,
        "unit": c.unit,
        "passive": c.passive,
        "note": c.note,
    }
    if not math.isfinite(c.net_bps):
        return {**out, "status": "not_computable", "reason": "net bps not finite"}
    e = float(c.impact_exponent)
    if not e > 0:
        raise ValueError("impact_exponent must be > 0")
    if c.passive:
        i0 = 0.0
        source = "passive: impact not charged (participation only)"
    elif c.impact_bps_at_base is not None and c.impact_bps_at_base > 0:
        i0 = float(c.impact_bps_at_base)
        source = "study"
    elif c.base_participation is not None and c.base_participation > 0:
        i0 = impact_coeff_bps * float(c.base_participation) ** e
        source = f"cost model: {impact_coeff_bps:g} bps x participation^{e:g}"
    else:
        return {
            **out,
            "status": "not_computable",
            "reason": "no impact at base and no base participation: cannot scale",
        }
    if c.net_bps_ci is not None:
        lo, hi = (float(v) for v in c.net_bps_ci)
    elif c.net_bps_se is not None and math.isfinite(c.net_bps_se):
        lo, hi = c.net_bps - _Z95 * c.net_bps_se, c.net_bps + _Z95 * c.net_bps_se
    else:
        lo = hi = float("nan")
    s_lo, s_hi = max(impact_scales), min(impact_scales)
    rows = []
    for k in multipliers:
        k = float(k)
        imp = i0 * k**e
        rows.append(
            {
                "multiplier": k,
                "notional": None if c.base_notional is None else c.base_notional * k,
                "participation": None if c.base_participation is None else c.base_participation * k,
                "impact_bps": imp,
                "net_bps": c.net_bps + i0 - imp,
                "net_bps_lo": lo + s_lo * i0 - s_lo * imp,
                "net_bps_hi": hi + s_hi * i0 - s_hi * imp,
                "net_usd": None
                if c.base_notional is None
                else (c.net_bps + i0 - imp) * 1e-4 * k * c.base_notional,
            }
        )
    a = c.net_bps + i0
    return {
        **out,
        "status": "ok",
        "impact_source": source,
        "impact_bps_at_base": i0,
        "band": "95 % sampling band on net bps, combined with impact scale "
        f"{s_lo:g} (lower) / {s_hi:g} (upper)",
        "breakeven_multiplier": _k_star(a, i0, e),
        "breakeven_multiplier_lo": _k_star(lo + s_lo * i0, s_lo * i0, e),
        "breakeven_multiplier_hi": _k_star(hi + s_hi * i0, s_hi * i0, e),
        "best_multiplier": _k_opt(a, i0, e),
        "rows": rows,
    }


def _f(v: Any, nd: int = 2) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        if math.isinf(v):
            return "unbounded"
        if math.isnan(v):
            return "n/a"
        return f"{v:,.{nd}f}"
    return str(v)


def capacity_section(
    study: str, curves: Sequence[dict[str, Any]], why_empty: str = "", heading: str = "##"
) -> str:
    """Markdown capacity section for a scorecard (module docs)."""
    L = [f"{heading} Capacity (REL-17)", ""]
    if not curves:
        L += [
            f"Not applicable: {study} has no positive candidate"
            + (f" ({why_empty})." if why_empty else ".")
            + " A capacity curve is computed only for a candidate the study's own rule "
            "passed; nothing here is a capacity claim.",
            "",
        ]
        return "\n".join(L)
    L += [
        "Net performance versus notional and participation under the REL-03/04 square-root "
        "impact rule (`iap.portfolio.capacity`). Bands combine the 95 % sampling band with "
        "impact scales 0.5 / 1.5. In-sample; not a capacity claim.",
        "",
    ]
    for cv in curves:
        L.append(
            f"**{cv['candidate']}** ({cv['unit']})" + (f": {cv['note']}" if cv["note"] else "")
        )
        L.append("")
        if cv["status"] != "ok":
            L += [f"Not computable: {cv['reason']}.", ""]
            continue
        L.append(
            f"Impact at base {_f(cv['impact_bps_at_base'], 3)} bps ({cv['impact_source']}); "
            f"breakeven at {_f(cv['breakeven_multiplier'])} x base "
            f"(band {_f(cv['breakeven_multiplier_lo'])} to {_f(cv['breakeven_multiplier_hi'])}); "
            f"net USD peaks at {_f(cv['best_multiplier'])} x base."
        )
        L += [
            "",
            "| x base | notional | participation | impact bps | net bps | band | net USD |",
            "|---|---|---|---|---|---|---|",
        ]
        for r in cv["rows"]:
            L.append(
                f"| {r['multiplier']:g} | {_f(r['notional'], 0)} | {_f(r['participation'], 5)} | "
                f"{_f(r['impact_bps'], 3)} | {_f(r['net_bps'], 3)} | "
                f"{_f(r['net_bps_lo'], 3)} to {_f(r['net_bps_hi'], 3)} | {_f(r['net_usd'], 2)} |"
            )
        L.append("")
    return "\n".join(L)
