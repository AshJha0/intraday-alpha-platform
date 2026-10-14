"""Live-vs-backtest TCA: the day's simulated fills against the research cost model.

For every finalised parent order of the paper run, the *simulated* cost
components come from the MVP's per-parent :class:`~iap.contracts.types.TCAResult`
(``iap.tca`` definitions: implementation shortfall, spread, impact, fees, in
bps).  The *expected* components are what the research backtester's
:class:`~iap.backtest.costs.CostModel` (``configs/execution/execution.json``,
plus the dated regulatory fees when the prereg opts in) charges for the SAME
decision: the parent's filled quantity, at the decision-time mid and half
spread, against the instrument ADV.  Expected bps are per filled notional at
the decision mid; ``expected_is_bps = spread + fee + impact``.

The comparison is per parent and aggregated (filled-quantity weighted).
Parents that filled nothing have no cost to compare and are only counted.
The MVP simulator charges the LINEAR rule-6 impact while the research model
is square-root by default, so an impact gap is expected and is the point of
the report.  Everything here is a pure function of its inputs (deterministic).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from iap.backtest.costs import CostModel

COMPONENTS = ("is", "spread", "fee", "impact")


@dataclass(frozen=True)
class ParentCostRow:
    """One parent: the decision context and the simulated TCA (bps)."""

    parent_order_id: int
    algo: str
    side: int  # +1 buy, -1 sell
    qty: int
    filled_qty: int
    decision_mid: float
    half_spread: float
    decision_ts: int
    sim_is_bps: float
    sim_spread_bps: float
    sim_fee_bps: float
    sim_impact_bps: float


def rows_from_engine(engine: Any) -> list[ParentCostRow]:
    """:class:`ParentCostRow` for every parent of an :class:`~iap.mvp.engine.MvpEngine`
    that has a TCA record (``engine.outcomes``)."""
    tl = engine.timeline
    rows = []
    for o in engine.outcomes:
        if o.tca is None:
            continue
        p = o.parent
        i = tl.prevailing(p.decision_ts)
        mid = float("nan") if i is None else tl.mid(i)
        hs = float("nan") if i is None else tl.half_spread(i)
        rows.append(
            ParentCostRow(
                parent_order_id=int(p.parent_order_id),
                algo=str(getattr(p.algo, "name", p.algo)),
                side=1 if str(getattr(p.side, "name", p.side)).upper() in ("BID", "BUY") else -1,
                qty=int(p.qty),
                filled_qty=int(o.tca.filled_qty),
                decision_mid=float(mid),
                half_spread=float(hs),
                decision_ts=int(p.decision_ts),
                sim_is_bps=float(o.tca.implementation_shortfall_bps),
                sim_spread_bps=float(o.tca.spread_cost_bps),
                sim_fee_bps=float(o.tca.fees_bps),
                sim_impact_bps=float(o.tca.impact_bps),
            )
        )
    return rows


def expected_bps(
    row: ParentCostRow, model: CostModel, asset_class: str, adv: float, lot_size: int
) -> dict[str, float]:
    """The research cost model's components for the row's filled quantity, bps."""
    q = np.array([row.side * row.filled_qty], dtype=float)
    comp = model.cost_components(
        q, np.array([row.decision_mid]), np.array([row.half_spread]), asset_class, adv, lot_size
    )
    unit = float(lot_size) if asset_class == "FX" else 1.0
    notional = row.filled_qty * unit * row.decision_mid
    out = {k: float(comp[k][0]) / notional * 1e4 for k in ("spread", "fee", "impact")}
    out["is"] = out["spread"] + out["fee"] + out["impact"]
    return out


def _wmean(values: Sequence[float], weights: Sequence[float]) -> float | None:
    w = float(sum(weights))
    if w <= 0:
        return None
    return float(sum(v * x for v, x in zip(values, weights, strict=True)) / w)


def compare(
    rows: Iterable[ParentCostRow],
    model: CostModel,
    *,
    asset_class: str,
    adv: float,
    lot_size: int,
    is_gap_alert_bps: float,
    model_for_row: Any = None,
) -> dict[str, Any]:
    """Per-parent and aggregated simulated-vs-expected costs (module docstring).

    ``model_for_row(row) -> CostModel`` overrides ``model`` per row (dated
    regulatory fees); ``None`` uses ``model`` for every row."""
    per_parent = []
    skipped = {"no_fill": 0, "no_market": 0}
    for r in rows:
        if r.filled_qty <= 0:
            skipped["no_fill"] += 1
            continue
        if not (math.isfinite(r.decision_mid) and r.decision_mid > 0) or not math.isfinite(
            r.half_spread
        ):
            skipped["no_market"] += 1
            continue
        m = model_for_row(r) if model_for_row is not None else model
        exp = expected_bps(r, m, asset_class, adv, lot_size)
        sim = {
            "is": r.sim_is_bps,
            "spread": r.sim_spread_bps,
            "fee": r.sim_fee_bps,
            "impact": r.sim_impact_bps,
        }
        per_parent.append(
            {
                **asdict(r),
                "expected_bps": exp,
                "simulated_bps": sim,
                "gap_bps": {k: sim[k] - exp[k] for k in COMPONENTS},
            }
        )
    w = [float(p["filled_qty"]) for p in per_parent]
    agg: dict[str, Any] = {}
    for k in COMPONENTS:
        agg[k] = {
            "simulated_bps": _wmean([p["simulated_bps"][k] for p in per_parent], w),
            "expected_bps": _wmean([p["expected_bps"][k] for p in per_parent], w),
            "gap_bps": _wmean([p["gap_bps"][k] for p in per_parent], w),
        }
    per_algo: dict[str, dict[str, Any]] = {}
    for algo in sorted({p["algo"] for p in per_parent}):
        grp = [p for p in per_parent if p["algo"] == algo]
        gw = [float(p["filled_qty"]) for p in grp]
        per_algo[algo] = {
            "n_parents": len(grp),
            "filled_qty": int(sum(gw)),
            **{f"{k}_gap_bps": _wmean([p["gap_bps"][k] for p in grp], gw) for k in COMPONENTS},
        }
    gap = agg["is"]["gap_bps"]
    alerts = []
    if gap is not None and abs(gap) > is_gap_alert_bps:
        alerts.append(
            f"aggregate implementation-shortfall gap {gap:+.3f} bps exceeds "
            f"+/-{is_gap_alert_bps} bps (simulated vs research cost model)"
        )
    return {
        "schema": 1,
        "weighting": "filled quantity",
        "expected_basis": "research CostModel on the filled qty at the decision mid / half "
        "spread; bps of filled notional at the decision mid",
        "n_parents_compared": len(per_parent),
        "skipped": skipped,
        "aggregate": agg,
        "per_algo": per_algo,
        "threshold": {"is_gap_alert_bps": is_gap_alert_bps},
        "alerts": alerts,
        "per_parent": per_parent,
    }


def merge(reports: Mapping[str, Mapping[str, Any]], is_gap_alert_bps: float) -> dict[str, Any]:
    """Aggregate per-symbol :func:`compare` reports into one (weights carried)."""
    rows = [p for rep in reports.values() for p in rep["per_parent"]]
    w = [float(p["filled_qty"]) for p in rows]
    agg = {
        k: {
            f: _wmean([p[src][k] for p in rows], w)
            for f, src in (
                ("simulated_bps", "simulated_bps"),
                ("expected_bps", "expected_bps"),
                ("gap_bps", "gap_bps"),
            )
        }
        for k in COMPONENTS
    }
    gap = agg["is"]["gap_bps"]
    alerts = [a for rep in reports.values() for a in rep["alerts"]]
    if gap is not None and abs(gap) > is_gap_alert_bps:
        alerts.append(f"all symbols: implementation-shortfall gap {gap:+.3f} bps")
    return {"n_parents_compared": len(rows), "aggregate": agg, "alerts": alerts}
