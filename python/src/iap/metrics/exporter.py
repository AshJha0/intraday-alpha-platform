"""The Python ``/metrics`` producer (v1.17 WS-F item F9, DECISION_N7 closed).

:class:`PlatformExporter` rebuilds a :class:`~iap.metrics.registry.MetricsRegistry`
from three file-backed sources on every refresh, so the metrics are read
from the artefacts the Python platform actually writes, never invented:

1. **configs** — ``configs/risk/risk.json`` gives the live limits
   ``risk_limit{limit="max_daily_loss"|"max_strategy_daily_loss"|
   "max_gross_notional"|"max_net_notional"}`` (PLATFORM_CONVENTIONS.md
   §12.6: alerts divide by the exported limit, never a hand-copied constant);
2. **the session marker** ``<state-dir>/session_exit.json`` — the document
   :func:`write_session_marker` (and ``python -m iap.metrics run-session``)
   writes when a session starts (RUNNING), ends (FINISHED / FAILED) or is
   stopped (STOPPED); the same contract the retired Java
   ``SessionStateExporter`` read: ``{"code", "event_cursor", "state",
   "unix_time"}``.  Exported as ``platform_persisted_session_state`` (-1 no
   marker, -2 unreadable), ``platform_persisted_session_unixtime`` and
   ``platform_persisted_session_event_cursor``;
3. **the newest MVP run** under ``<runs-root>`` (``python -m iap.mvp run
   --out``: ``config.json`` at start, ``report.json`` last, canonical and
   sorted) — every counter and gauge of the live set below is a field of
   ``report.json`` (``iap.mvp.report``), read with the report's own names.

Every metric name the retired Java container exposed and Prometheus scraped
is accounted for in :data:`LIVE_METRICS` (a Python producer exists) or
:data:`RETIRED_METRICS` (no Python producer: exposed as ``<name>_available 0``
so the name is still discoverable at the scrape and its retirement is
explicit; the rules that read them were removed from
``deployment/prometheus/`` and are listed there under RETIRED).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from iap.metrics.registry import Histogram, MetricsRegistry

__all__ = [
    "LIVE_METRICS",
    "RETIRED_METRICS",
    "SESSION_MARKER",
    "SESSION_STATES",
    "PlatformExporter",
    "read_session_marker",
    "write_session_marker",
]

SESSION_MARKER = "session_exit.json"
#: ``platform_session_state`` codes (PLATFORM_CONVENTIONS.md §12.6).
SESSION_STATES = {"STARTING": 0, "RUNNING": 1, "FINISHED": 2, "FAILED": 3, "STOPPED": 4}
NO_MARKER = -1
UNREADABLE = -2

#: Metric families the Python exporter produces, with their source.
LIVE_METRICS: dict[str, str] = {
    "md_events_total": "report counts.n_events",
    "md_sequence_gaps_total": "report counts.counters.sequence_gaps",
    "md_duplicates_total": "report counts.qc_totals.duplicates",
    "md_last_event_unixtime": "report run.last_ts (event time, ns -> s)",
    "md_last_event_wallclock_unixtime": "mtime of the newest run artefact",
    "platform_mode": "asap=1 (the MVP replays unpaced), realtime=0",
    "platform_session_state": "session marker code, else 1 while a run has no report.json, 2 once it has",
    "platform_persisted_session_state": "session marker (-1 none, -2 unreadable)",
    "platform_persisted_session_unixtime": "session marker unix_time",
    "platform_persisted_session_event_cursor": "session marker event_cursor",
    "alpha_signals_total": "report alpha.ensemble.n_signals",
    "alpha_rolling_ic": "report alpha.per_alpha[alpha].realized_ic (session realized IC, label alpha)",
    "portfolio_solves_total": "report portfolio.solves",
    "portfolio_gross_notional": "|pnl.final_position| x pnl.final_mark",
    "portfolio_net_notional": "pnl.final_position x pnl.final_mark",
    "portfolio_drawdown": "report pnl.max_drawdown",
    "exec_orders_submitted_total": "report counts.n_child_orders_submitted",
    "exec_fills_total": "report counts.n_fills",
    "exec_child_orders_rejected_total": "report counts.counters.risk_rejected (the pre-trade check is per child)",
    "risk_events_total": "report risk.audit_events",
    "risk_decisions_total": "report risk.allowed + risk.rejected",
    "risk_allowed_total": "report risk.allowed",
    "risk_rejected_total": "report risk.rejected",
    "risk_realized_pnl": "report pnl.realized",
    "risk_unrealized_pnl": "report pnl.unrealized",
    "risk_daily_pnl": "report pnl.risk_daily (realized + unrealized, reporting ccy)",
    "risk_kill_switch_engaged": "report risk.kill_switch_engaged",
    "risk_limit": "configs/risk/risk.json global.* and per_strategy.max_daily_loss (label limit)",
    "trace_records_total": "report trace.n_traces",
    "order_path_latency_ns": "traces.jsonl: per parent order arrival_ts - decision_ts (log2 histogram)",
    "trace_tca_skipped_total": "report counts.counters.parents_without_tca",
}

#: Java-only metric families with no Python producer: ``<name>_available 0``.
RETIRED_METRICS: dict[str, str] = {
    "md_event_time_gap_seconds": "per-event gap of the live Java feed thread",
    "book_stale": "per-instrument stale flag of the Java books",
    "risk_session_restarts_total": "Java --resume restarts",
    "admin_requests_total": "Java kill-switch admin API",
    "admin_auth_rate_limited_total": "Java kill-switch admin API",
    "admin_audit_suppressed_total": "Java kill-switch admin API",
    "exec_orders_blocked_kill_pending_total": "Java admin kill race",
    "risk_routed_venue_mismatch_total": "Java RiskWiring venue check",
    "risk_resume_open_orders_released_total": "Java --resume",
    "decode_latency_ns": "Java feed decode histogram",
    "book_update_latency_ns": "Java book apply histogram",
    "exec_slippage_bps": "Java per-fill slippage histogram",
    "jvm_gc_pause_ns": "JVM garbage collector",
    "alpha_live_vs_backtest_drift": "Java com.iap.adaptive.DriftMonitor (PSI)",
    "alpha_lifecycle_state": "Java com.iap.adaptive.LifecycleGauge",
}

_LIMITS = (
    ("max_daily_loss", ("global", "max_daily_loss")),
    ("max_strategy_daily_loss", ("per_strategy", "max_daily_loss")),
    ("max_gross_notional", ("global", "max_gross_notional")),
    ("max_net_notional", ("global", "max_net_notional")),
)


# ----------------------------------------------------------------- marker ---


def write_session_marker(
    state_dir: Path, state: str, event_cursor: int = 0, unix_time: int | None = None
) -> Path:
    """Atomically write ``<state_dir>/session_exit.json`` (the Java contract)."""
    if state not in SESSION_STATES:
        raise ValueError(f"unknown session state {state!r}")
    state_dir.mkdir(parents=True, exist_ok=True)
    doc = {
        "code": SESSION_STATES[state],
        "event_cursor": int(event_cursor),
        "state": state,
        "unix_time": int(time.time()) if unix_time is None else int(unix_time),
    }
    target = state_dir / SESSION_MARKER
    tmp = state_dir / (SESSION_MARKER + ".tmp")
    tmp.write_text(json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return target


def read_session_marker(state_dir: Path) -> dict[str, Any] | None:
    """The marker, ``None`` when absent; ``ValueError`` when malformed."""
    p = state_dir / SESSION_MARKER
    if not p.is_file():
        return None
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{p}: {exc}") from exc
    if not isinstance(doc, dict):
        raise ValueError(f"{p}: top level must be an object")
    for key in ("code", "event_cursor", "unix_time"):
        if not isinstance(doc.get(key), int) or isinstance(doc.get(key), bool):
            raise ValueError(f"{p}: {key!r} must be an integer")
    if not isinstance(doc.get("state"), str):
        raise ValueError(f"{p}: 'state' must be a string")
    return doc


# --------------------------------------------------------------- exporter ---


def newest_run(runs_root: Path) -> Path | None:
    """The MVP run directory with the newest ``report.json`` (else
    ``config.json``) under ``runs_root``, or ``runs_root`` itself when it is
    a run directory; ``None`` when nothing is there."""
    if not runs_root.is_dir():
        return None
    if (runs_root / "config.json").is_file() or (runs_root / "report.json").is_file():
        return runs_root
    best: tuple[float, Path] | None = None
    for d in sorted(p for p in runs_root.iterdir() if p.is_dir()):
        marker = d / "report.json"
        if not marker.is_file():
            marker = d / "config.json"
            if not marker.is_file():
                continue
        mtime = marker.stat().st_mtime
        if best is None or mtime > best[0]:
            best = (mtime, d)
    return best[1] if best else None


@dataclass
class ExporterStatus:
    configs_dir: str
    state_dir: str
    runs_root: str
    run_dir: str | None
    session_state: int
    persisted_session_state: int
    refreshed_unixtime: int
    errors: list[str]

    def json(self) -> str:
        return json.dumps(
            {
                "component": "platform-metrics",
                "status": "running" if not self.errors else "degraded",
                **self.__dict__,
            },
            sort_keys=True,
        )


class PlatformExporter:
    """Rebuild the registry from configs, the session marker and the newest
    MVP run (module docstring)."""

    def __init__(self, configs_dir: Path, state_dir: Path, runs_root: Path) -> None:
        self.configs_dir = Path(configs_dir)
        self.state_dir = Path(state_dir)
        self.runs_root = Path(runs_root)
        self.registry = MetricsRegistry()
        self._traces_cache: tuple[tuple[int, int, str], Histogram] | None = None
        self.status = ExporterStatus(
            str(self.configs_dir),
            str(self.state_dir),
            str(self.runs_root),
            None,
            0,
            NO_MARKER,
            0,
            [],
        )
        self.refreshes = 0

    # -- sources ----------------------------------------------------------

    def _limits(self, reg: MetricsRegistry, errors: list[str]) -> None:
        path = self.configs_dir / "risk" / "risk.json"
        try:
            risk = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            errors.append(f"risk.json: {exc}")
            return
        for label, (section, key) in _LIMITS:
            value = (risk.get(section) or {}).get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                reg.gauge("risk_limit", limit=label).set(float(value))
            else:
                errors.append(f"risk.json: {section}.{key} missing")

    def _marker(self, reg: MetricsRegistry, errors: list[str]) -> int:
        state, unixtime, cursor = NO_MARKER, 0, 0
        try:
            doc = read_session_marker(self.state_dir)
        except ValueError as exc:
            errors.append(str(exc))
            state = UNREADABLE
        else:
            if doc is not None:
                state, unixtime, cursor = doc["code"], doc["unix_time"], doc["event_cursor"]
        reg.gauge("platform_persisted_session_state").set(state)
        reg.gauge("platform_persisted_session_unixtime").set(unixtime)
        reg.gauge("platform_persisted_session_event_cursor").set(cursor)
        return state

    def _run(
        self, reg: MetricsRegistry, errors: list[str], marker_state: int
    ) -> tuple[Path | None, int]:
        run_dir = newest_run(self.runs_root)
        session_state = marker_state if marker_state in SESSION_STATES.values() else 0
        reg.gauge("platform_mode", mode="asap").set(1)
        reg.gauge("platform_mode", mode="realtime").set(0)
        if run_dir is None:
            reg.gauge("platform_session_state").set(session_state)
            return None, session_state
        report_path = run_dir / "report.json"
        if not report_path.is_file():
            if marker_state not in SESSION_STATES.values():
                session_state = SESSION_STATES["RUNNING"]
            reg.gauge("platform_session_state").set(session_state)
            wall = (
                (run_dir / "config.json").stat().st_mtime
                if (run_dir / "config.json").is_file()
                else 0
            )
            reg.gauge("md_last_event_wallclock_unixtime").set(int(wall))
            return run_dir, session_state
        try:
            rep = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            errors.append(f"{report_path}: {exc}")
            reg.gauge("platform_session_state").set(session_state)
            return run_dir, session_state
        if marker_state not in SESSION_STATES.values():
            session_state = SESSION_STATES["FINISHED"]
        reg.gauge("platform_session_state").set(session_state)
        reg.gauge("md_last_event_wallclock_unixtime").set(int(report_path.stat().st_mtime))
        self._from_report(reg, rep, errors)
        self._from_traces(reg, run_dir / "traces.jsonl", errors)
        return run_dir, session_state

    @staticmethod
    def _from_report(reg: MetricsRegistry, rep: dict[str, Any], errors: list[str]) -> None:
        def get(*path: str, default: Any = None) -> Any:
            node: Any = rep
            for key in path:
                if not isinstance(node, dict) or key not in node:
                    errors.append(f"report.json: {'.'.join(path)} missing")
                    return default
                node = node[key]
            return node

        def num(*path: str) -> float:
            v = get(*path, default=0)
            return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0

        def total(name: str, *path: str) -> None:
            reg.counter(name).set(int(num(*path)))

        total("md_events_total", "counts", "n_events")
        total("md_sequence_gaps_total", "counts", "counters", "sequence_gaps")
        total("md_duplicates_total", "counts", "qc_totals", "duplicates")
        reg.gauge("md_last_event_unixtime").set(num("run", "last_ts") / 1e9)
        total("alpha_signals_total", "alpha", "ensemble", "n_signals")
        per_alpha = get("alpha", "per_alpha", default={}) or {}
        for alpha_id, block in sorted(per_alpha.items()):
            ic = (block or {}).get("realized_ic")
            if isinstance(ic, (int, float)) and not isinstance(ic, bool):
                reg.gauge("alpha_rolling_ic", alpha=alpha_id).set(float(ic))
        total("portfolio_solves_total", "portfolio", "solves")
        position = num("pnl", "final_position")
        mark = num("pnl", "final_mark")
        reg.gauge("portfolio_gross_notional").set(abs(position) * mark)
        reg.gauge("portfolio_net_notional").set(position * mark)
        reg.gauge("portfolio_drawdown").set(num("pnl", "max_drawdown"))
        total("exec_orders_submitted_total", "counts", "n_child_orders_submitted")
        total("exec_fills_total", "counts", "n_fills")
        total("exec_child_orders_rejected_total", "counts", "counters", "risk_rejected")
        total("risk_events_total", "risk", "audit_events")
        allowed, rejected = int(num("risk", "allowed")), int(num("risk", "rejected"))
        reg.counter("risk_decisions_total").set(allowed + rejected)
        reg.counter("risk_allowed_total").set(allowed)
        reg.counter("risk_rejected_total").set(rejected)
        reg.gauge("risk_realized_pnl").set(num("pnl", "realized"))
        reg.gauge("risk_unrealized_pnl").set(num("pnl", "unrealized"))
        reg.gauge("risk_daily_pnl").set(num("pnl", "risk_daily"))
        reg.gauge("risk_kill_switch_engaged").set(
            1 if get("risk", "kill_switch_engaged", default=False) is True else 0
        )
        total("trace_records_total", "trace", "n_traces")
        total("trace_tca_skipped_total", "counts", "counters", "parents_without_tca")

    def _from_traces(self, reg: MetricsRegistry, traces: Path, errors: list[str]) -> None:
        """``order_path_latency_ns``: one sample per parent order in the run's
        decision traces, ``arrival_ts - decision_ts`` (decision -> pre-trade
        risk -> venue arrival; event-time nanoseconds). Rebuilt only when the
        file changes (size + mtime), so a scrape is cheap."""
        if not traces.is_file():
            return
        st = traces.stat()
        key = (st.st_size, int(st.st_mtime_ns), str(traces))
        if self._traces_cache is not None and self._traces_cache[0] == key:
            hist = self._traces_cache[1]
        else:
            hist = Histogram()
            try:
                with traces.open(encoding="utf-8") as fh:
                    for line in fh:
                        if not line.strip():
                            continue
                        rec = json.loads(line)
                        for parent in (rec.get("stages") or {}).get("parent_orders") or []:
                            a, d = parent.get("arrival_ts"), parent.get("decision_ts")
                            if isinstance(a, int) and isinstance(d, int) and a >= d:
                                hist.record(a - d)
            except (OSError, ValueError) as exc:
                errors.append(f"{traces}: {exc}")
                return
            self._traces_cache = (key, hist)
        if hist.total:
            target = reg.histogram("order_path_latency_ns")
            target.counts = list(hist.counts)
            target.total = hist.total
            target.sum = hist.sum

    # -- refresh ----------------------------------------------------------

    def refresh(self) -> MetricsRegistry:
        reg = MetricsRegistry()
        errors: list[str] = []
        self._limits(reg, errors)
        marker_state = self._marker(reg, errors)
        run_dir, session_state = self._run(reg, errors, marker_state)
        for name in sorted(RETIRED_METRICS):
            reg.gauge(f"{name}_available").set(0)
        self.registry = reg
        self.refreshes += 1
        self.status = ExporterStatus(
            configs_dir=str(self.configs_dir),
            state_dir=str(self.state_dir),
            runs_root=str(self.runs_root),
            run_dir=None if run_dir is None else str(run_dir),
            session_state=session_state,
            persisted_session_state=marker_state,
            refreshed_unixtime=int(time.time()),
            errors=errors,
        )
        return reg

    def render(self) -> str:
        return self.refresh().render()

    def healthy(self) -> bool:
        """Liveness: the exporter can read its configs (the one input it
        cannot do without); a missing run or marker is a state, not a fault."""
        return not any(e.startswith("risk.json") for e in self.status.errors)

    def ready(self) -> bool:
        """Readiness: at least one refresh succeeded and no source is malformed."""
        return self.refreshes > 0 and not self.status.errors
