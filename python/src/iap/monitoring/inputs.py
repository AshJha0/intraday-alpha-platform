"""Typed inputs of the monitoring state machine.

Every input is a frozen dataclass of numbers and timestamps; there is no
free-text field the machine reads (``source_id`` and ``session`` are labels
carried into the report, never parsed).  :func:`validate_snapshot` returns
the reasons a snapshot cannot be assessed; any reason makes the state
UNKNOWN.

Channel hooks:

* ``positions`` is the exposure-and-limits view (REL-17 portfolio/risk
  supplies the limits; :meth:`PositionsSnapshot.from_mapping` takes its
  JSON form).
* ``orders`` is the open-order and reconciliation view (REL-18 live-path
  reconciliation supplies ``reconciliation_exceptions``;
  :meth:`OrdersSnapshot.from_mapping` takes its JSON form).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

from .config import MonitoringConfig

__all__ = [
    "Expectation",
    "OrdersSnapshot",
    "PositionsSnapshot",
    "SessionObs",
    "Snapshot",
    "validate_snapshot",
]

NS_PER_S = 1_000_000_000


def _is_num(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)


def _is_ts(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


@dataclass(frozen=True)
class Expectation:
    """The registered expectation a signal is monitored against (from its
    prereg or promotion evidence, identified by ``source_id``): per-session
    net P&L mean / sd, per-session signal strength (e.g. realised IC) mean /
    sd, turnover and fill rate per session, and the drawdown limit (same
    units as net P&L)."""

    source_id: str
    net_pnl_mean: float
    net_pnl_sd: float
    signal_mean: float
    signal_sd: float
    turnover: float
    fill_rate: float
    drawdown_limit: float

    def problems(self) -> list[str]:
        out = []
        if not isinstance(self.source_id, str) or not self.source_id:
            out.append("expectation.source_id: empty")
        for f in fields(self):
            if f.name == "source_id":
                continue
            v = getattr(self, f.name)
            if not _is_num(v):
                out.append(f"expectation.{f.name}: not a finite number")
            elif (
                f.name in ("net_pnl_sd", "signal_sd", "turnover", "fill_rate", "drawdown_limit")
                and v <= 0
            ):
                out.append(f"expectation.{f.name}: must be > 0")
        return out


@dataclass(frozen=True)
class SessionObs:
    """One completed session of the monitored signal.  ``as_of_ns`` is when
    the observation was produced; costs in bps of traded notional;
    ``completeness`` is the fraction of expected input records present."""

    session: str
    as_of_ns: int
    gross_pnl: float
    net_pnl: float
    realised_cost_bps: float
    expected_cost_bps: float
    turnover: float
    signal_strength: float
    fill_rate: float
    completeness: float

    def problems(self, i: int) -> list[str]:
        out = []
        if not _is_ts(self.as_of_ns):
            out.append(f"sessions[{i}].as_of_ns: not a timestamp")
        for f in fields(self):
            if f.name in ("session", "as_of_ns"):
                continue
            if not _is_num(getattr(self, f.name)):
                out.append(f"sessions[{i}].{f.name}: not a finite number")
        if _is_num(self.completeness) and not 0.0 <= self.completeness <= 1.0:
            out.append(f"sessions[{i}].completeness: outside [0, 1]")
        if _is_num(self.fill_rate) and not 0.0 <= self.fill_rate <= 1.0:
            out.append(f"sessions[{i}].fill_rate: outside [0, 1]")
        if _is_num(self.turnover) and self.turnover < 0:
            out.append(f"sessions[{i}].turnover: negative")
        return out


@dataclass(frozen=True)
class PositionsSnapshot:
    as_of_ns: int
    gross_exposure: float
    net_exposure: float
    gross_limit: float
    net_limit: float

    @classmethod
    def from_mapping(cls, m: Mapping[str, Any]) -> PositionsSnapshot:
        return cls(**{f.name: m.get(f.name) for f in fields(cls)})  # type: ignore[arg-type]

    def problems(self) -> list[str]:
        out = [] if _is_ts(self.as_of_ns) else ["positions.as_of_ns: not a timestamp"]
        for name in ("gross_exposure", "net_exposure", "gross_limit", "net_limit"):
            v = getattr(self, name)
            if not _is_num(v):
                out.append(f"positions.{name}: not a finite number")
            elif name.endswith("limit") and v <= 0:
                out.append(f"positions.{name}: must be > 0")
        return out


@dataclass(frozen=True)
class OrdersSnapshot:
    as_of_ns: int
    open_orders: int
    reconciliation_exceptions: int

    @classmethod
    def from_mapping(cls, m: Mapping[str, Any]) -> OrdersSnapshot:
        return cls(**{f.name: m.get(f.name) for f in fields(cls)})  # type: ignore[arg-type]

    def problems(self) -> list[str]:
        out = [] if _is_ts(self.as_of_ns) else ["orders.as_of_ns: not a timestamp"]
        for name in ("open_orders", "reconciliation_exceptions"):
            if not _is_ts(getattr(self, name)):
                out.append(f"orders.{name}: not a non-negative integer")
        return out


@dataclass(frozen=True)
class Snapshot:
    """Everything one evaluation sees.  ``None`` means the input is missing."""

    evaluated_at_ns: int
    expectation: Expectation | None
    sessions: tuple[SessionObs, ...]
    positions: PositionsSnapshot | None
    orders: OrdersSnapshot | None
    heartbeat_ns: int | None


def _stale(name: str, as_of: int, now: int, cfg: MonitoringConfig) -> list[str]:
    if as_of > now:
        return [f"{name}: as_of {as_of} is after evaluated_at {now} (clock)"]
    age_s = (now - as_of) / NS_PER_S
    if age_s > cfg.freshness_s[name]:
        return [f"{name}: stale, age {age_s:.0f} s > {cfg.freshness_s[name]:.0f} s"]
    return []


def validate_snapshot(snap: Snapshot, cfg: MonitoringConfig) -> dict[str, list[str]]:
    """Per-channel reasons the snapshot cannot be assessed (empty lists when
    every required input is present, valid and fresh).  Channels:
    ``expectation``, ``sessions``, ``positions``, ``orders``, ``heartbeat``."""
    now = snap.evaluated_at_ns
    out: dict[str, list[str]] = {
        c: [] for c in ("expectation", "sessions", "positions", "orders", "heartbeat")
    }
    if not _is_ts(now):
        out["heartbeat"].append("evaluated_at_ns: not a timestamp")
        return out
    if snap.expectation is None:
        out["expectation"].append("expectation: missing")
    else:
        out["expectation"] += snap.expectation.problems()

    s = out["sessions"]
    if not snap.sessions:
        s.append("sessions: missing")
    else:
        seen: set[str] = set()
        prev = -1
        for i, obs in enumerate(snap.sessions):
            if not isinstance(obs, SessionObs):
                s.append(f"sessions[{i}]: not a SessionObs")
                continue
            s += obs.problems(i)
            if obs.session in seen:
                s.append(f"sessions[{i}]: duplicate session {obs.session!r}")
            seen.add(obs.session)
            if _is_ts(obs.as_of_ns):
                if obs.as_of_ns <= prev:
                    s.append(f"sessions[{i}]: as_of_ns not increasing")
                prev = obs.as_of_ns
            if _is_num(obs.completeness) and obs.completeness < cfg.invalid_below_completeness:
                s.append(
                    f"sessions[{i}].completeness {obs.completeness:.3f} < "
                    f"{cfg.invalid_below_completeness} (invalid)"
                )
        if not s:
            s += _stale("sessions", snap.sessions[-1].as_of_ns, now, cfg)
            if len(snap.sessions) < cfg.min_sessions:
                s.append(
                    f"sessions: {len(snap.sessions)} < min_sessions {cfg.min_sessions} "
                    "(insufficient sample)"
                )

    for name, obj in (("positions", snap.positions), ("orders", snap.orders)):
        if obj is None:
            out[name].append(f"{name}: missing")
        else:
            out[name] += obj.problems()
            if not out[name]:
                out[name] += _stale(name, obj.as_of_ns, now, cfg)

    if snap.heartbeat_ns is None:
        out["heartbeat"].append("heartbeat: missing")
    elif not _is_ts(snap.heartbeat_ns):
        out["heartbeat"].append("heartbeat: not a timestamp")
    else:
        out["heartbeat"] += _stale("heartbeat", snap.heartbeat_ns, now, cfg)
    return out
