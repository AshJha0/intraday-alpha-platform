"""Deterministic monitoring state machine (docs/MONITORING_POLICY.md).

:func:`evaluate` is a pure function of ``(config, snapshot, memory,
acknowledgement)``: the same arguments always give the same
:class:`Evaluation`.  There is no wall clock (time is
``snapshot.evaluated_at_ns``), no randomness and no free-text input.

Precedence, highest first:

1. **HALT** — latched once reached; a hard trigger computed from a valid
   channel (drawdown at its limit, exposure over its limit, reconciliation
   exceptions persisting) raises it even when another channel is invalid.
2. **UNKNOWN** — any required input missing, stale beyond its declared
   freshness, invalid, or the sample below ``min_sessions``.  UNKNOWN never
   maps to ACTIVE and never permits new risk.
3. **REVIEW / WATCH / ACTIVE** — the highest metric level that has held
   for its declared persistence, with hysteresis: a level steps down only
   after ``clear_after`` consecutive evaluations below it.

Leaving HALT needs an :class:`Acknowledgement` bound to this policy hash
and the evaluation that halted, AND current evidence with no HALT trigger;
it moves to REVIEW, never straight to ACTIVE.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from enum import Enum
from statistics import NormalDist
from typing import Any

import numpy as np

from iap.research.study_stats import mde

from .config import MonitoringConfig
from .inputs import Snapshot, validate_snapshot

__all__ = [
    "ACTIONS",
    "Acknowledgement",
    "Alert",
    "Evaluation",
    "MonitorMemory",
    "State",
    "cusum_lower",
    "evaluate",
    "permits_new_risk",
    "run_stream",
]


class State(str, Enum):
    ACTIVE = "ACTIVE"
    WATCH = "WATCH"
    REVIEW = "REVIEW"
    HALT = "HALT"
    UNKNOWN = "UNKNOWN"


_RANK = {State.ACTIVE: 0, State.WATCH: 1, State.REVIEW: 2, State.HALT: 3}
_BY_RANK = {v: k for k, v in _RANK.items()}

#: The required action per state (also the policy document's table).
ACTIONS: dict[State, str] = {
    State.ACTIVE: "trade as registered; daily report",
    State.WATCH: "trade unchanged, no size increase; owner reads the report within one session",
    State.REVIEW: "reduce-only, no new positions; owner review before the next session",
    State.HALT: "cancel open orders, flatten per runbook, kill switch; resume only via acknowledgement to REVIEW",
    State.UNKNOWN: "no new orders (reduce-only); restore the failing inputs; escalate if it persists",
}


def permits_new_risk(state: State) -> bool:
    """Only ACTIVE and WATCH may add risk; UNKNOWN never does."""
    return state in (State.ACTIVE, State.WATCH)


@dataclass(frozen=True)
class Alert:
    metric: str
    level: State
    evidence: dict[str, Any]
    action: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "severity": self.level.value,
            "evidence": self.evidence,
            "action": self.action,
        }


@dataclass(frozen=True)
class MonitorMemory:
    """State carried between evaluations (all derived from earlier evidence)."""

    held: State = State.ACTIVE
    streaks: dict[str, dict[str, int]] = field(default_factory=dict)
    clear_streak: int = 0
    unknown_streak: int = 0
    recon_streak: int = 0
    halted_at: int | None = None
    evaluations: int = 0


@dataclass(frozen=True)
class Acknowledgement:
    """A structured HALT release: who, under which policy hash, for which
    halting evaluation.  It cannot pick the next state."""

    reviewer: str
    config_sha256: str
    halted_at_evaluation: int


@dataclass(frozen=True)
class Evaluation:
    state: State
    action: str
    alerts: tuple[Alert, ...]
    metrics: dict[str, dict[str, Any]]
    input_problems: dict[str, list[str]]
    memory: MonitorMemory
    config_sha256: str
    policy_id: str
    evaluated_at_ns: int


_Z975 = NormalDist().inv_cdf(0.975)


def cusum_lower(z: np.ndarray, k: float) -> float:
    """One-sided lower CUSUM ``C_t = max(0, C_{t-1} - z_t - k)`` over the
    standardised stream ``z``; returns the final ``C``."""
    c = 0.0
    for v in z:
        c = max(0.0, c - float(v) - k)
    return c


def _level_from(value: float, watch: float, review: float) -> State:
    if value > review:
        return State.REVIEW
    if value > watch:
        return State.WATCH
    return State.ACTIVE


def _cusum_metric(
    x: np.ndarray, mu: float, sd: float, cfg: MonitoringConfig
) -> tuple[State, dict[str, Any]]:
    z = (x - mu) / sd
    c = cusum_lower(z, cfg.cusum_k)
    level = _level_from(c, cfg.cusum_h_watch, cfg.cusum_h_review)
    w = x[-cfg.window_sessions :]
    n = int(w.size)
    se = sd / math.sqrt(n)
    zw = (float(w.mean()) - mu) / se
    if level is State.ACTIVE and zw > cfg.upside_watch_z:
        level = State.WATCH  # far better than registered: check for a bug or leak
    return level, {
        "cusum": round(c, 6),
        "h_watch": cfg.cusum_h_watch,
        "h_review": cfg.cusum_h_review,
        "window_n": n,
        "window_mean": float(w.mean()),
        "expected": mu,
        "se": se,
        "ci95": [float(w.mean()) - _Z975 * se, float(w.mean()) + _Z975 * se],
        "z_vs_expected": zw,
        "mde": mde(sd, n),
    }


def _metrics(
    snap: Snapshot, cfg: MonitoringConfig, problems: dict[str, list[str]]
) -> dict[str, tuple[State, dict[str, Any]]]:
    """Raw (pre-persistence) level per metric, only from valid channels."""
    out: dict[str, tuple[State, dict[str, Any]]] = {}
    exp = snap.expectation
    if not problems["positions"]:
        p = snap.positions
        assert p is not None
        util = max(abs(p.gross_exposure) / p.gross_limit, abs(p.net_exposure) / p.net_limit)
        lvl = (
            State.HALT
            if util > 1.0
            else (State.WATCH if util >= cfg.exposure_watch_utilisation else State.ACTIVE)
        )
        out["exposure"] = (
            lvl,
            {
                "utilisation": util,
                "gross": p.gross_exposure,
                "net": p.net_exposure,
                "gross_limit": p.gross_limit,
                "net_limit": p.net_limit,
            },
        )
    if not problems["orders"]:
        o = snap.orders
        assert o is not None
        exc = o.reconciliation_exceptions
        lvl = State.ACTIVE if exc == 0 else State.REVIEW
        out["reconciliation"] = (lvl, {"open_orders": o.open_orders, "exceptions": exc})

    sessions_ok = not problems["sessions"] or all(
        "insufficient sample" in r or "stale" in r for r in problems["sessions"]
    )
    if exp is not None and not problems["expectation"] and snap.sessions and sessions_ok:
        net = np.array([s.net_pnl for s in snap.sessions], dtype=float)
        cum = np.concatenate([[0.0], np.cumsum(net)])
        dd = float(np.max(np.maximum.accumulate(cum) - cum))
        frac = dd / exp.drawdown_limit
        lvl = (
            State.HALT
            if frac >= 1.0
            else State.REVIEW
            if frac >= cfg.drawdown_review_frac
            else State.WATCH
            if frac >= cfg.drawdown_watch_frac
            else State.ACTIVE
        )
        out["drawdown"] = (lvl, {"drawdown": dd, "limit": exp.drawdown_limit, "limit_use": frac})
    if problems["sessions"] or problems["expectation"] or exp is None:
        return out

    s = snap.sessions
    w = s[-cfg.window_sessions :]
    out["net_pnl"] = _cusum_metric(
        np.array([o.net_pnl for o in s], float), exp.net_pnl_mean, exp.net_pnl_sd, cfg
    )
    out["signal_strength"] = _cusum_metric(
        np.array([o.signal_strength for o in s], float), exp.signal_mean, exp.signal_sd, cfg
    )
    excess = np.array([o.realised_cost_bps - o.expected_cost_bps for o in w], float)
    m = float(excess.mean())
    sd = float(excess.std(ddof=1))
    se = sd / math.sqrt(excess.size)
    z = m / se if se > 0 else (math.inf if m > 0 else 0.0)
    out["costs"] = (
        _level_from(z, cfg.band_watch_z, cfg.band_review_z),
        {
            "mean_excess_bps": m,
            "se_bps": se,
            "z": z,
            "window_n": int(excess.size),
            "realised_bps": float(np.mean([o.realised_cost_bps for o in w])),
            "expected_bps": float(np.mean([o.expected_cost_bps for o in w])),
            "se_method": "session-clustered (one value per session)",
        },
    )
    ratio = float(np.mean([o.turnover for o in w])) / exp.turnover
    lo_w, hi_w = cfg.turnover_watch
    lo_r, hi_r = cfg.turnover_review
    lvl = (
        State.REVIEW
        if not lo_r <= ratio <= hi_r
        else State.WATCH
        if not lo_w <= ratio <= hi_w
        else State.ACTIVE
    )
    out["turnover"] = (
        lvl,
        {"ratio_to_expected": ratio, "watch_band": [lo_w, hi_w], "review_band": [lo_r, hi_r]},
    )
    fill = float(np.mean([o.fill_rate for o in w]))
    short = (exp.fill_rate - fill) / exp.fill_rate
    out["fill_quality"] = (
        _level_from(short, cfg.fill_watch_shortfall, cfg.fill_review_shortfall),
        {"fill_rate": fill, "expected": exp.fill_rate, "shortfall": short},
    )
    cmin = float(min(o.completeness for o in w))
    out["data_quality"] = (
        State.WATCH if cmin < cfg.watch_below_completeness else State.ACTIVE,
        {"min_completeness": cmin, "watch_below": cfg.watch_below_completeness},
    )
    return out


def evaluate(
    cfg: MonitoringConfig,
    snap: Snapshot,
    memory: MonitorMemory | None = None,
    ack: Acknowledgement | None = None,
) -> Evaluation:
    """One deterministic evaluation (module docs)."""
    mem = memory if memory is not None else MonitorMemory()
    problems = validate_snapshot(snap, cfg)
    unknown = any(problems.values())
    raw = _metrics(snap, cfg, problems)

    # persistence counters: consecutive evaluations with raw level >= L
    streaks: dict[str, dict[str, int]] = {}
    for name in set(raw) | set(mem.streaks):
        prev = mem.streaks.get(name, {})
        if name not in raw:
            streaks[name] = dict(prev)  # no evidence this time: freeze, do not reset
            continue
        lvl = raw[name][0]
        streaks[name] = {
            L: (prev.get(L, 0) + 1 if _RANK[lvl] >= _RANK[State(L)] else 0)
            for L in ("WATCH", "REVIEW", "HALT")
        }
    recon_streak = mem.recon_streak
    if "reconciliation" in raw:
        recon_streak = recon_streak + 1 if raw["reconciliation"][1]["exceptions"] > 0 else 0
        if recon_streak >= cfg.recon_halt_after:
            raw["reconciliation"] = (
                State.HALT,
                {**raw["reconciliation"][1], "consecutive": recon_streak},
            )
            streaks["reconciliation"]["HALT"] = max(streaks["reconciliation"]["HALT"], recon_streak)

    effective: dict[str, State] = {}
    for name in raw:
        eff = State.ACTIVE
        for L in ("WATCH", "REVIEW", "HALT"):
            if streaks[name][L] >= cfg.persistence[L]:
                eff = State(L)
        effective[name] = eff
    candidate = max((effective[n] for n in effective), key=lambda s: _RANK[s], default=State.ACTIVE)
    if unknown:
        # only hard (HALT) triggers from valid channels count while inputs are missing
        candidate = State.HALT if candidate is State.HALT else State.ACTIVE

    halted_at = mem.halted_at
    held = mem.held
    clear = mem.clear_streak
    alerts: list[Alert] = []
    if halted_at is not None:
        if (
            ack is not None
            and ack.config_sha256 == cfg.sha256
            and ack.halted_at_evaluation == halted_at
            and candidate is not State.HALT
            and not unknown
        ):
            halted_at, held, clear = None, State.REVIEW, 0
        else:
            held = State.HALT
    elif candidate is State.HALT:
        halted_at, held, clear = mem.evaluations, State.HALT, 0
    elif not unknown:
        if _RANK[candidate] >= _RANK[held]:
            held, clear = candidate, 0
        else:
            clear += 1
            if clear >= cfg.clear_after:
                held, clear = _BY_RANK[_RANK[held] - 1], 0

    if held is State.HALT:
        state = State.HALT
    elif unknown:
        state = State.UNKNOWN
    else:
        state = held
    unknown_streak = mem.unknown_streak + 1 if unknown else 0

    for name in sorted(raw):
        lvl, ev = raw[name]
        if lvl is not State.ACTIVE:
            alerts.append(
                Alert(
                    name,
                    effective[name] if effective[name] is not State.ACTIVE else State.WATCH,
                    {
                        **ev,
                        "raw_level": lvl.value,
                        "persisted_level": effective[name].value,
                        "streaks": streaks[name],
                    },
                    ACTIONS[max(lvl, effective[name], key=lambda s: _RANK[s])],
                )
            )
    for chan in sorted(problems):
        if problems[chan]:
            sev = State.REVIEW if unknown_streak >= cfg.unknown_escalate_after else State.WATCH
            alerts.append(
                Alert(
                    f"input:{chan}",
                    State.UNKNOWN,
                    {
                        "reasons": problems[chan],
                        "unknown_streak": unknown_streak,
                        "escalated": sev is State.REVIEW,
                    },
                    ACTIONS[State.UNKNOWN],
                )
            )
    new_mem = replace(
        mem,
        held=held,
        streaks=streaks,
        clear_streak=clear,
        unknown_streak=unknown_streak,
        recon_streak=recon_streak,
        halted_at=halted_at,
        evaluations=mem.evaluations + 1,
    )
    return Evaluation(
        state=state,
        action=ACTIONS[state],
        alerts=tuple(alerts),
        metrics={
            n: {"level": raw[n][0].value, "persisted_level": effective[n].value, **raw[n][1]}
            for n in sorted(raw)
        },
        input_problems={k: list(v) for k, v in problems.items()},
        memory=new_mem,
        config_sha256=cfg.sha256,
        policy_id=cfg.policy_id,
        evaluated_at_ns=snap.evaluated_at_ns,
    )


def run_stream(cfg: MonitoringConfig, snapshots: list[Snapshot]) -> list[Evaluation]:
    """Fold :func:`evaluate` over ``snapshots`` in order."""
    out: list[Evaluation] = []
    mem: MonitorMemory | None = None
    for s in snapshots:
        e = evaluate(cfg, s, mem)
        out.append(e)
        mem = e.memory
    return out
