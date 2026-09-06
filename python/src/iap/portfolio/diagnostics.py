"""Portfolio diagnostics: constraint audit, turnover, realized-vs-target vol.

Every optimizer solve should be followed by ``constraint_audit`` — the audit
lists each active constraint with its value, bound, slack and whether it
BINDS (slack below tolerance), so a research result is never quoted without
knowing which constraints shaped it (spec §15: "constraint-auditable").
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np

from iap.portfolio.optimizer import Constraints

_BIND_TOL = 1e-6


def constraint_audit(
    w: np.ndarray,
    cons: Constraints,
    w_prev: np.ndarray,
    Sigma: Optional[np.ndarray] = None,
    bind_tol: float = _BIND_TOL,
) -> Dict[str, Any]:
    """Audit a weight vector against the constraint set.

    Returns ``{"constraints": [...], "turnover": .., "gross": .., "net": ..,
    "realized_vol": .., "target_vol": .., "n_binding": .., "feasible": ..,
    "max_violation": ..}``; each constraint row carries name / value / bound
    / slack / binding. ``feasible`` is ``max_violation <= bind_tol`` — an
    INFEASIBLE solve (API_PORTFOLIO_TCA.md §1.3) audits ``w_prev`` and shows
    negative slack on the violated rows.
    """
    w = np.asarray(w, dtype=np.float64)
    w_prev = np.asarray(w_prev, dtype=np.float64)
    n = len(w)
    cons.validate(n)
    rows: List[Dict[str, Any]] = []

    def row(name: str, value: float, bound: float) -> None:
        slack = bound - value
        rows.append({
            "name": name,
            "value": float(value),
            "bound": float(bound),
            "slack": float(slack),
            "binding": bool(slack <= bind_tol),
        })

    for i in range(n):
        if w[i] - cons.w_min[i] <= bind_tol:
            row(f"position_min[{i}]", float(-w[i]), float(-cons.w_min[i]))
        if cons.w_max[i] - w[i] <= bind_tol:
            row(f"position_max[{i}]", float(w[i]), float(cons.w_max[i]))
    if cons.participation is not None:
        trade = np.abs(w - w_prev)
        for i in range(n):
            if cons.participation[i] - trade[i] <= bind_tol:
                row(f"participation[{i}]", float(trade[i]),
                    float(cons.participation[i]))
    if cons.net_cap is not None:
        row("net_exposure", abs(float(w.sum())), float(cons.net_cap))
    if cons.gross_cap is not None:
        row("gross_exposure", float(np.abs(w).sum()), float(cons.gross_cap))
    if cons.turnover_cap is not None:
        row("turnover", float(np.abs(w - w_prev).sum()),
            float(cons.turnover_cap))
    if cons.currency_matrix is not None:
        expo = cons.currency_matrix @ w
        for c in range(len(expo)):
            row(f"currency[{c}]", abs(float(expo[c])),
                float(cons.currency_bounds[c]))
    realized_vol = None
    if Sigma is not None:
        realized_vol = float(np.sqrt(max(w @ Sigma @ w, 0.0)))
        if cons.vol_target is not None:
            row("volatility", realized_vol, float(cons.vol_target))

    max_violation = max([0.0] + [-r["slack"] for r in rows])
    return {
        "constraints": rows,
        "n_binding": int(sum(r["binding"] for r in rows)),
        "feasible": bool(max_violation <= bind_tol),
        "max_violation": float(max_violation),
        "turnover": float(np.abs(w - w_prev).sum()),
        "gross": float(np.abs(w).sum()),
        "net": float(w.sum()),
        "realized_vol": realized_vol,
        "target_vol": (float(cons.vol_target)
                       if cons.vol_target is not None else None),
    }
