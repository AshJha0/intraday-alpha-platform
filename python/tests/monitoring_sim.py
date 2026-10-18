"""Synthetic streams for the REL-16 monitoring tests (no real data)."""

from __future__ import annotations

import numpy as np
from iap.monitoring import (
    Expectation,
    OrdersSnapshot,
    PositionsSnapshot,
    SessionObs,
    Snapshot,
    State,
)
from iap.monitoring.machine import MonitorMemory, evaluate

DAY_NS = 86_400 * 1_000_000_000
T0_NS = 1_700_000_000 * 1_000_000_000

EXPECTATION = Expectation(
    source_id="synthetic-prereg",
    net_pnl_mean=100.0,
    net_pnl_sd=400.0,
    signal_mean=0.05,
    signal_sd=0.04,
    turnover=1.0e6,
    fill_rate=0.9,
    drawdown_limit=12_000.0,
)


def sessions(
    seed: int, n: int, decay_at: int | None = None, decay_sd: float = 1.0
) -> list[SessionObs]:
    """Noise around EXPECTATION; from ``decay_at`` on, net P&L and signal
    strength means drop by ``decay_sd`` registered sds."""
    rng = np.random.default_rng(seed)
    e = EXPECTATION
    out = []
    for i in range(n):
        shift = decay_sd if decay_at is not None and i >= decay_at else 0.0
        net = e.net_pnl_mean - shift * e.net_pnl_sd + e.net_pnl_sd * rng.standard_normal()
        cost_exp = 2.0
        out.append(
            SessionObs(
                session=f"S{i:04d}",
                as_of_ns=T0_NS + i * DAY_NS,
                gross_pnl=float(net + 200.0),
                net_pnl=float(net),
                realised_cost_bps=float(cost_exp + 0.5 * rng.standard_normal()),
                expected_cost_bps=cost_exp,
                turnover=float(e.turnover * (1.0 + 0.1 * rng.standard_normal())),
                signal_strength=float(
                    e.signal_mean - shift * e.signal_sd + e.signal_sd * rng.standard_normal()
                ),
                fill_rate=float(np.clip(e.fill_rate + 0.02 * rng.standard_normal(), 0.0, 1.0)),
                completeness=1.0,
            )
        )
    return out


def snapshot(obs: list[SessionObs], **over) -> Snapshot:
    now = obs[-1].as_of_ns + 60 * 1_000_000_000 if obs else T0_NS
    base = dict(
        evaluated_at_ns=now,
        expectation=EXPECTATION,
        sessions=tuple(obs),
        positions=PositionsSnapshot(now - 10**9, 4.0e5, 1.0e5, 1.0e6, 5.0e5),
        orders=OrdersSnapshot(now - 10**9, 3, 0),
        heartbeat_ns=now - 10**9,
    )
    base.update(over)
    return Snapshot(**base)


def first_at_least(cfg, obs: list[SessionObs], level: State, start: int = 1) -> int | None:
    """Index (number of sessions seen) of the first evaluation at >= ``level``
    (HALT counts), evaluating once per completed session."""
    rank = {State.ACTIVE: 0, State.WATCH: 1, State.REVIEW: 2, State.HALT: 3, State.UNKNOWN: -1}
    mem: MonitorMemory | None = None
    for t in range(start, len(obs) + 1):
        ev = evaluate(cfg, snapshot(obs[:t]), mem)
        mem = ev.memory
        if rank[ev.state] >= rank[level]:
            return t
    return None
