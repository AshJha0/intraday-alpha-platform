"""Track C forward shadow book (v2.0 U0 stage 10; U13 accrual rule, protocol section 8).

``python -m iap.paper.shadow_book run`` runs once after each US close.  For the
next session of the registered calendar it reads the frozen combined-book
configuration's target positions for that session (one JSON file per session in
the inbox, written upstream by the frozen configuration), turns each decision's
target weights into paper orders against the paper position, prices every order
at the one EN4 fill convention (:func:`iap.execution.sim.simulate`) under the
three cost columns (base, conservative, stress; each column is its own paper
book), and appends ONE hash-chained row per session.

Paper only.  This module imports no broker, OMS, socket or HTTP code (a test
scans its imports) and :func:`assert_paper_only` refuses to run in a process
that has loaded a known broker client.  Nothing here can place an order.

No peeking.  A session's P&L is written to ``sealed/<session>.json``; the chain
row carries only counts and that file's SHA-256, so the chain binds the values
without showing them.  :func:`status` and the CLI print counts and the accrual
state only.  :func:`read_series` is the single read: it refuses any identity but
the registered runner, refuses before the accrual state is ``complete``, refuses
a second read, and appends the read record to the chain before returning.

Fail closed.  Rows are appended strictly in calendar order: the next session's
input missing, or an input for a later session, is a refusal, never a gap.  A
session whose data is genuinely lost is recorded on purpose with
``mark-missing`` (attempted, not usable).  Every input path passes the data-read
choke point (:func:`iap.core.guard.check`) and the paper forbidden-dataset list.

Idempotent.  Re-running a recorded session with the same input bytes is a
no-op; different bytes for a recorded session are a refusal (the chain is
append-only).

Usability (protocol section 8): a decision unit is usable when every name it
targets had a valid quote at arrival under the conservative column; a session is
usable when it has at least ``m_min`` usable units covering at least
``min_time_coverage`` of the session by time and ``min_grid_coverage`` of the
registered grid points.  The thresholds come from the config, fixed before T0.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import math
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from iap.contracts.versions import canonical_json, content_hash
from iap.core import guard
from iap.execution.sim import COLUMNS, Market, Quote, simulate

__all__ = [
    "GENESIS",
    "RUNNER_IDENTITY",
    "ShadowBookError",
    "ShadowConfig",
    "accrual_state",
    "assert_paper_only",
    "mark_missing",
    "read_series",
    "run_session",
    "status",
    "verify_chain",
]

GENESIS = "0" * 64
#: The registered runner identity (CR1); the only identity :func:`read_series` serves.
RUNNER_IDENTITY = "iap-runner"
#: Column whose fills decide usability and whose net P&L is the U13 series (protocol section 3).
DECLARED_COLUMN = "conservative"
#: Broker / order-routing clients that must never share a process with the shadow book.
_BROKER_MODULES = (
    "ib_insync",
    "ibapi",
    "alpaca_trade_api",
    "alpaca",
    "tradier",
    "ccxt",
    "quickfix",
)
ROW_KINDS = ("session", "missing", "read")


class ShadowBookError(ValueError):
    """A refusal: the shadow book did not append (the message says why)."""


def assert_paper_only() -> None:
    """Refuse to run when a broker or order-routing client is loaded in this process."""
    loaded = sorted(m for m in _BROKER_MODULES if m in sys.modules)
    if loaded:
        raise ShadowBookError(f"broker / order-routing modules loaded: {loaded}; paper only")


# --------------------------------------------------------------------------- config
@dataclass(frozen=True, slots=True)
class ShadowConfig:
    """The registration of the forward window (fixed before T0; ``configs/paper/shadow_book.json``)."""

    frozen_configuration_sha256: str
    capital: float
    calendar: tuple[str, ...]
    T_min: int
    m_min: int
    grid_points: int
    min_time_coverage: float
    min_grid_coverage: float
    participation_cap: float
    min_ticket: int
    forbidden_datasets: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.frozen_configuration_sha256) != 64:
            raise ShadowBookError("frozen_configuration_sha256 is a 64-hex SHA-256")
        if not math.isfinite(self.capital) or self.capital <= 0:
            raise ShadowBookError("capital > 0")
        if list(self.calendar) != sorted(set(self.calendar)) or not self.calendar:
            raise ShadowBookError("calendar: sorted, unique, non-empty session dates")
        if self.T_min < 1 or self.m_min < 1 or self.grid_points < self.m_min:
            raise ShadowBookError("T_min >= 1, m_min >= 1, grid_points >= m_min")
        for v in (self.min_time_coverage, self.min_grid_coverage):
            if not 0 < v <= 1:
                raise ShadowBookError("coverage thresholds in (0, 1]")

    @property
    def cap(self) -> int:
        """Sessions attempted before the window closes (protocol section 8: 1.25 * T_min)."""
        return math.floor(1.25 * self.T_min)

    @classmethod
    def load(cls, path: Path) -> ShadowConfig:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        cal_path = Path(d["calendar"])
        if not cal_path.is_absolute():
            cal_path = Path(path).parent / cal_path
        calendar = tuple(json.loads(cal_path.read_text(encoding="utf-8"))["sessions"])
        return cls(
            frozen_configuration_sha256=d["frozen_configuration_sha256"],
            capital=float(d["capital"]),
            calendar=calendar,
            T_min=int(d["T_min"]),
            m_min=int(d["m_min"]),
            grid_points=int(d["grid_points"]),
            min_time_coverage=float(d["min_time_coverage"]),
            min_grid_coverage=float(d["min_grid_coverage"]),
            participation_cap=float(d["participation_cap"]),
            min_ticket=int(d["min_ticket"]),
            forbidden_datasets=tuple(d.get("forbidden_datasets", ())),
        )


# --------------------------------------------------------------------------- chain
def _sha(text: str | bytes) -> str:
    return hashlib.sha256(text.encode("utf-8") if isinstance(text, str) else text).hexdigest()


def _row_hash(body: Mapping[str, Any]) -> str:
    return _sha(canonical_json({k: v for k, v in body.items() if k != "row_sha256"}))


def _rows(out: Path) -> list[dict[str, Any]]:
    p = out / "chain.jsonl"
    if not p.is_file():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line]


def verify_chain(out: Path) -> list[dict[str, Any]]:
    """Every row's hash, its link to the previous row, its seq and its sealed file; returns the rows."""
    rows = _rows(out)
    prev = GENESIS
    for i, r in enumerate(rows):
        if r.get("seq") != i or r.get("prev_sha256") != prev:
            raise ShadowBookError(f"chain broken at row {i}: seq or prev_sha256")
        if r.get("row_sha256") != _row_hash(r):
            raise ShadowBookError(f"chain broken at row {i}: row_sha256 does not match its body")
        if r.get("kind") == "session":
            sealed = out / "sealed" / f"{r['session']}.json"
            if not sealed.is_file() or _sha(sealed.read_bytes()) != r["sealed_sha256"]:
                raise ShadowBookError(f"sealed record of {r['session']} missing or altered")
        prev = r["row_sha256"]
    return rows


def _append(out: Path, rows: list[dict[str, Any]], body: dict[str, Any]) -> dict[str, Any]:
    body = {
        "seq": len(rows),
        "prev_sha256": rows[-1]["row_sha256"] if rows else GENESIS,
        **body,
    }
    body["row_sha256"] = _row_hash(body)
    with (out / "chain.jsonl").open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(canonical_json(body) + "\n")
    return body


# --------------------------------------------------------------------------- accrual
def accrual_state(usable: int, attempted: int, T_min: int) -> str:
    """Protocol section 8 on counts only: ``accruing``, ``complete`` or ``pending``.

    Mirrors ``iap.validation.pf3.accrual_status`` (branch ``u13-pf3-combined-book``);
    rewire to it once that lands.
    """
    if usable >= T_min:
        return "complete"
    if attempted >= math.floor(1.25 * T_min):
        return "pending"
    return "accruing"


def status(out: Path, cfg: ShadowConfig) -> dict[str, Any]:
    """Counts only: what the development identity may see while the window fills."""
    rows = verify_chain(out)
    sessions = [r for r in rows if r["kind"] in ("session", "missing")]
    usable = sum(1 for r in sessions if r.get("usable"))
    return {
        "attempted": len(sessions),
        "usable": usable,
        "dropped": len(sessions) - usable,
        "T_min": cfg.T_min,
        "cap": cfg.cap,
        "state": accrual_state(usable, len(sessions), cfg.T_min),
        "read_done": any(r["kind"] == "read" for r in rows),
        "last_session": sessions[-1]["session"] if sessions else None,
        "next_session": _next_session(rows, cfg),
    }


def _next_session(rows: Sequence[Mapping[str, Any]], cfg: ShadowConfig) -> str | None:
    done = {r["session"] for r in rows if r["kind"] in ("session", "missing")}
    return next((s for s in cfg.calendar if s not in done), None)


# --------------------------------------------------------------------------- one session
def _check_input(path: Path, cfg: ShadowConfig) -> None:
    for part in path.resolve().parts:
        if any(fnmatch.fnmatch(part, pat) for pat in cfg.forbidden_datasets):
            raise ShadowBookError(f"{path}: under a forbidden (sealed) dataset {part!r}")
    try:
        guard.check(path, "shadow book input")
    except PermissionError as exc:
        raise ShadowBookError(f"{path}: refused by the data-read guard: {exc}") from exc


def _market(name: str, m: Mapping[str, Any]) -> Market:
    q = m.get("quote")
    quote = (
        None
        if q is None
        else Quote(
            int(q["ts"]), float(q["bid"]), float(q["ask"]), int(q["bid_size"]), int(q["ask_size"])
        )
    )
    return Market(name, quote, int(m["volume_5m"]), bool(m.get("halted", False)))


def _price_session(doc: Mapping[str, Any], cfg: ShadowConfig) -> tuple[dict[str, Any], int, float]:
    """Paper orders and fills for every decision under every column; returns (sealed, usable units, time cov)."""
    C = cfg.capital
    carried_in, prev_close = doc["_carried_in"], doc["_prev_close"]
    pos = {c: dict(carried_in.get(c, {})) for c in COLUMNS}
    cash = {c: 0.0 for c in COLUMNS}
    costs = {c: {"spread": 0.0, "impact": 0.0, "fees": 0.0} for c in COLUMNS}
    counts = {c: {"orders": 0, "fills": 0, "refused": 0} for c in COLUMNS}
    usable_units = 0
    decisions = sorted(doc["decisions"], key=lambda d: int(d["ts"]))
    for d in decisions:
        ts = int(d["ts"])
        mids = {n: float(v) for n, v in d["mids"].items()}
        unit_ok = True
        for col in COLUMNS:
            book = pos[col]
            for n in sorted(set(d["targets"]) | set(book)):
                w = float(d["targets"].get(n, 0.0))
                target = int(round(w * C / mids[n])) if w else 0
                qty = target - book.get(n, 0)
                if qty == 0:
                    continue
                counts[col]["orders"] += 1
                out = simulate(
                    ts_decision=ts,
                    name=n,
                    qty=qty,
                    mid_decision=mids[n],
                    market=_market(n, d["markets"][n]),
                    column=col,
                    participation_cap=cfg.participation_cap,
                    min_ticket=cfg.min_ticket,
                    mandatory=target == 0,
                )
                if out.fill is None:
                    counts[col]["refused"] += 1
                    if col == DECLARED_COLUMN and ("quote" in out.reason or out.reason == "halt"):
                        unit_ok = False
                    continue
                f = out.fill
                counts[col]["fills"] += 1
                book[n] = book.get(n, 0) + f.qty
                cash[col] -= f.qty * f.price + f.fee
                costs[col]["spread"] += abs(f.qty) * f.half_spread
                costs[col]["impact"] += abs(f.qty) * f.impact
                costs[col]["fees"] += f.fee
        usable_units += unit_ok
    close = {n: float(v) for n, v in doc["close_mids"].items()}
    columns: dict[str, Any] = {}
    for col in COLUMNS:
        start = carried_in.get(col, {})
        # carried inventory is valued from the prior close to this close; new trades from their fill price
        mtm = sum(q * close[n] for n, q in pos[col].items()) - sum(
            q * prev_close[n] for n, q in start.items()
        )
        net = cash[col] + mtm
        columns[col] = {
            "net_pnl": net,
            "return_on_capital": net / C,
            **costs[col],
            **counts[col],
            "end_positions": {n: q for n, q in sorted(pos[col].items()) if q},
        }
    step = int(doc["step_ns"])
    time_cov = min(1.0, usable_units * step / int(doc["session_ns"]))
    sealed = {
        "session": doc["session"],
        "close_mids": dict(sorted(close.items())),
        "columns": columns,
    }
    return sealed, usable_units, time_cov


def _carried(
    out: Path, rows: Sequence[Mapping[str, Any]]
) -> tuple[dict[str, dict[str, int]], dict[str, float]]:
    """End positions per column and close mids of the last recorded session (carried inventory)."""
    last = next((r for r in reversed(rows) if r["kind"] == "session"), None)
    if last is None:
        return {}, {}
    sealed = json.loads((out / "sealed" / f"{last['session']}.json").read_text(encoding="utf-8"))
    return {c: v["end_positions"] for c, v in sealed["columns"].items()}, sealed["close_mids"]


def run_session(
    session: str, inbox: Path, out: Path, cfg: ShadowConfig
) -> tuple[str, dict[str, Any]]:
    """Append the row of ``session`` (must be the next calendar session); returns (outcome, counts-only row)."""
    assert_paper_only()
    out.mkdir(parents=True, exist_ok=True)
    (out / "sealed").mkdir(exist_ok=True)
    rows = verify_chain(out)
    if any(r["kind"] == "read" for r in rows):
        raise ShadowBookError("the window has been read; the chain is closed")
    if session not in cfg.calendar:
        raise ShadowBookError(f"{session} is not in the registered calendar")
    path = inbox / f"{session}.json"
    recorded = next((r for r in rows if r.get("session") == session), None)
    if recorded is not None:
        if recorded["kind"] == "missing":
            raise ShadowBookError(f"{session} was recorded as missing; the chain is append-only")
        _check_input(path, cfg)
        if not path.is_file() or _sha(path.read_bytes()) != recorded["input_sha256"]:
            raise ShadowBookError(f"{session} already recorded from different input bytes")
        return "already recorded", _public(recorded)
    expected = _next_session(rows, cfg)
    if session != expected:
        raise ShadowBookError(f"{session} is out of order: the next session is {expected}")
    st = status(out, cfg)
    if st["state"] != "accruing":
        raise ShadowBookError(f"the window is {st['state']}; no further sessions are appended")
    _check_input(path, cfg)
    if not path.is_file():
        raise ShadowBookError(f"input for {session} is missing: {path} (refused, not skipped)")
    raw = path.read_bytes()
    doc = json.loads(raw)
    if doc.get("session") != session:
        raise ShadowBookError(f"{path} declares session {doc.get('session')!r}")
    if doc.get("frozen_configuration_sha256") != cfg.frozen_configuration_sha256:
        raise ShadowBookError(f"{path} was not produced by the frozen configuration")
    carried, prev_close = _carried(out, rows)
    doc = {**doc, "_carried_in": carried, "_prev_close": prev_close}
    sealed, units, time_cov = _price_session(doc, cfg)
    grid_cov = units / cfg.grid_points
    fails = [
        msg
        for ok, msg in (
            (units >= cfg.m_min, f"usable units {units} < m_min {cfg.m_min}"),
            (time_cov >= cfg.min_time_coverage, "time coverage below the registered minimum"),
            (grid_cov >= cfg.min_grid_coverage, "grid coverage below the registered minimum"),
        )
        if not ok
    ]
    sealed_text = canonical_json(sealed)
    (out / "sealed" / f"{session}.json").write_text(sealed_text, encoding="utf-8", newline="\n")
    row = _append(
        out,
        rows,
        {
            "kind": "session",
            "session": session,
            "input_sha256": _sha(raw),
            "frozen_configuration_sha256": cfg.frozen_configuration_sha256,
            "decisions": len(doc["decisions"]),
            "usable_units": units,
            "usable": not fails,
            "drop_reasons": fails,
            "sealed_sha256": _sha(sealed_text),
        },
    )
    return "recorded", _public(row)


def mark_missing(session: str, reason: str, out: Path, cfg: ShadowConfig) -> dict[str, Any]:
    """Record the next calendar session as lost on purpose (attempted, not usable)."""
    assert_paper_only()
    if not reason.strip():
        raise ShadowBookError("a missing session carries its written reason")
    out.mkdir(parents=True, exist_ok=True)
    rows = verify_chain(out)
    if session != _next_session(rows, cfg):
        raise ShadowBookError(f"{session} is not the next session to record")
    return _public(
        _append(
            out,
            rows,
            {
                "kind": "missing",
                "session": session,
                "usable": False,
                "drop_reasons": [f"missing: {reason}"],
            },
        )
    )


def _public(row: Mapping[str, Any]) -> dict[str, Any]:
    keep = ("seq", "kind", "session", "usable", "usable_units", "drop_reasons", "row_sha256")
    return {k: row[k] for k in keep if k in row}


# --------------------------------------------------------------------------- the single read
def read_series(out: Path, cfg: ShadowConfig, identity: str) -> dict[str, Any]:
    """The one read at completion (runner identity only); appends the read record first."""
    if identity != RUNNER_IDENTITY:
        raise ShadowBookError(f"identity {identity!r} may see counts only")
    st = status(out, cfg)
    if st["read_done"]:
        raise ShadowBookError("the series has already been read once")
    if st["state"] != "complete":
        raise ShadowBookError(f"accrual state is {st['state']}; the series is not readable")
    rows = verify_chain(out)
    series = []
    for r in rows:
        if r["kind"] == "session" and r["usable"]:
            sealed = json.loads(
                (out / "sealed" / f"{r['session']}.json").read_text(encoding="utf-8")
            )
            series.append(
                {
                    "session": r["session"],
                    **{c: v["return_on_capital"] for c, v in sealed["columns"].items()},
                }
            )
    doc = {"declared_column": DECLARED_COLUMN, "series": series}
    _append(
        out,
        rows,
        {
            "kind": "read",
            "identity": identity,
            "usable": st["usable"],
            "attempted": st["attempted"],
            "series_sha256": content_hash(doc),
        },
    )
    return doc


# --------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m iap.paper.shadow_book", description=__doc__.split("\n")[0]
    )
    p.add_argument("command", choices=("run", "status", "mark-missing", "verify"))
    p.add_argument("--config", required=True)
    p.add_argument("--inbox", help="directory of <session>.json target files")
    p.add_argument("--out", required=True, help="shadow book directory (chain.jsonl, sealed/)")
    p.add_argument("--session", help="default: the next calendar session")
    p.add_argument("--reason", default="")
    a = p.parse_args(argv)
    out = Path(a.out)
    try:
        cfg = ShadowConfig.load(Path(a.config))
        if a.command == "verify":
            print(f"chain ok: {len(verify_chain(out))} rows")
        elif a.command == "status":
            print(canonical_json(status(out, cfg)))
        elif a.command == "mark-missing":
            print(canonical_json(mark_missing(a.session, a.reason, out, cfg)))
        else:
            if not a.inbox:
                raise ShadowBookError("run needs --inbox")
            session = a.session or status(out, cfg)["next_session"]
            if session is None:
                print("calendar exhausted")
                return 0
            if a.session is None and session > dt.date.today().isoformat():
                print(f"next session {session} not yet due")
                return 0
            outcome, row = run_session(session, Path(a.inbox), out, cfg)
            print(f"{outcome}: {canonical_json(row)}")
            print(canonical_json(status(out, cfg)))
    except (ShadowBookError, OSError, KeyError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
