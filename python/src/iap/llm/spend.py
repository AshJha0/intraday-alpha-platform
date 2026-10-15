"""The LLM spend ledger and the caps enforced on it (v1.17 E1).

An append-only JSONL file **outside the repository**, one row per API call,
shared by every process that spends: it is the state the monthly cap is
checked against, on every call, in every process.

Where it lives: ``IAP_LLM_SPEND_LEDGER`` when set, else
``%LOCALAPPDATA%/iap/llm_spend.jsonl`` on Windows and
``~/.local/state/iap/llm_spend.jsonl`` elsewhere.  A path inside the
repository is refused (the ledger must never be committed).

Row (``iap.llm.spend/1``)::

    {"schema": "iap.llm.spend/1", "utc": "2026-10-16T09:12:03Z",
     "session": "<session id>", "run": "<run id>", "role": "research",
     "model": "claude-sonnet-5-5", "pinned": "2026-10-16", "prices_checked": "2026-10-06",
     "tokens_in": 1200, "tokens_out": 150, "tokens_cache_read": 0, "tokens_cache_write": 0,
     "usd": 0.00390, "month_usd": 1.2345}

``month_usd`` is the month-to-date **after** this row (UTC calendar month of
``utc``).  Appends take :class:`iap.experiment.locking.FileLock` on the
ledger, re-read the month inside the lock, then write one line and fsync, so
two processes never interleave or double-count.

Corrupt rows: a line that is not JSON, not an object, not this schema, or
whose ``utc`` / ``usd`` are missing or malformed is **skipped and counted**
(:attr:`SpendLedger.corrupt_rows` after a read), never repaired and never
fatal - a torn trailing line from a killed process must not stop the next
session, and skipped spend errs on the side of under-counting only that row.
A read-modify-write under the lock plus ``fsync`` keeps torn lines to the
unlikely case of a crash mid-write.

Caps (:class:`SpendPolicy`, defaults from ``configs/agents/llm.json``):
``monthly_cap_usd`` (30), ``per_run_budget_usd`` (1, the
:class:`~iap.llm.budget.Budget` session cap) and ``alert_fraction`` (0.8).
:meth:`SpendLedger.check_call` refuses a call *before* it is made when the
month-to-date plus its maximum cost would exceed the cap
(:class:`MonthlyCapExceeded`).  Once the month-to-date reaches the alert
fraction, :meth:`SpendLedger.record` raises the alert exactly once per month
across processes (an ``O_EXCL`` marker file next to the ledger): a logged
WARNING plus the operator notification route of ``tools/jobs/watchdog.py``
(``notify``: BurntToast / WinRT toast / ``msg``), reused from that file, when
it is present and the platform is Windows.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from iap.experiment.locking import FileLock
from iap.llm import prices as _prices

__all__ = [
    "LEDGER_ENV",
    "SCHEMA",
    "MonthlyCapExceeded",
    "SpendLedger",
    "SpendPolicy",
    "SpendRow",
    "default_ledger_path",
    "main",
    "utc_month",
]

SCHEMA = "iap.llm.spend/1"
LEDGER_ENV = "IAP_LLM_SPEND_LEDGER"
_REPO = Path(__file__).resolve().parents[4]
log = logging.getLogger("iap.llm.spend")


class MonthlyCapExceeded(RuntimeError):
    """The month-to-date plus the next call's maximum cost would pass the cap."""


def default_ledger_path() -> Path:
    env = os.environ.get(LEDGER_ENV)
    if env:
        return Path(env)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
    else:
        root = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return root / "iap" / "llm_spend.jsonl"


def utc_month(ts: float) -> str:
    return time.strftime("%Y-%m", time.gmtime(ts))


def _utc(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


@dataclass(frozen=True)
class SpendPolicy:
    monthly_cap_usd: float = 30.0
    per_run_budget_usd: float = 1.0
    alert_fraction: float = 0.8

    @classmethod
    def from_config(cls, cfg: _prices.LlmConfig | None = None) -> SpendPolicy:
        c = cfg or _prices.load_llm_config()
        return cls(c.monthly_cap_usd, c.per_run_budget_usd, c.alert_fraction)

    def __post_init__(self) -> None:
        if self.monthly_cap_usd < 0 or self.per_run_budget_usd < 0:
            raise ValueError("caps must be >= 0")
        if not 0 < self.alert_fraction <= 1:
            raise ValueError("alert_fraction must be in (0, 1]")


@dataclass(frozen=True)
class SpendRow:
    utc: str
    session: str
    run: str
    role: str
    model: str
    pinned: str
    prices_checked: str
    tokens_in: int
    tokens_out: int
    tokens_cache_read: int
    tokens_cache_write: int
    usd: float
    month_usd: float

    @property
    def month(self) -> str:
        return self.utc[:7]


def _parse_row(line: str) -> SpendRow | None:
    try:
        d = json.loads(line)
    except ValueError:
        return None
    if not isinstance(d, dict) or d.get("schema") != SCHEMA:
        return None
    try:
        utc = str(d["utc"])
        if len(utc) != 20 or utc[4] != "-" or utc[7] != "-" or not utc.endswith("Z"):
            return None
        usd = float(d["usd"])
        return SpendRow(
            utc=utc,
            session=str(d.get("session", "")),
            run=str(d.get("run", "")),
            role=str(d.get("role", "")),
            model=str(d.get("model", "")),
            pinned=str(d.get("pinned", "")),
            prices_checked=str(d.get("prices_checked", "")),
            tokens_in=int(d.get("tokens_in", 0)),
            tokens_out=int(d.get("tokens_out", 0)),
            tokens_cache_read=int(d.get("tokens_cache_read", 0)),
            tokens_cache_write=int(d.get("tokens_cache_write", 0)),
            usd=usd,
            month_usd=float(d.get("month_usd", 0.0)),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _watchdog_notify() -> Callable[[str, str], str] | None:
    """``tools/jobs/watchdog.py::notify`` when the repo checkout is present
    (reused, not copied); None otherwise or off Windows."""
    if sys.platform != "win32":
        return None
    src = _REPO / "tools" / "jobs" / "watchdog.py"
    if not src.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location("iap_llm_spend_watchdog", src)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        fn = getattr(mod, "notify", None)
        return fn if callable(fn) else None
    except Exception:  # pragma: no cover - a broken watchdog must not block spend
        return None


class SpendLedger:
    """The append-only ledger (see the module docs).

    ``notifier(title, message)`` is the toast route for the 80 % alert; the
    default resolves the watchdog's ``notify`` lazily.  ``clock`` returns a
    POSIX timestamp (injected by the month-rollover tests)."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        policy: SpendPolicy | None = None,
        notifier: Callable[[str, str], Any] | None = None,
        clock: Callable[[], float] = time.time,
        allow_in_repo: bool = False,
    ) -> None:
        p = Path(path) if path is not None else default_ledger_path()
        p = p.expanduser()
        if not allow_in_repo:
            r = p.resolve()
            if r == _REPO or _REPO in r.parents:
                raise ValueError(
                    f"{p}: the spend ledger must live outside the repository ({_REPO}); "
                    f"set {LEDGER_ENV} to a path outside it"
                )
        self.path = p
        self.policy = policy if policy is not None else SpendPolicy()
        self._notifier = notifier
        self.clock = clock
        self.corrupt_rows = 0

    @classmethod
    def default(cls, **kw: Any) -> SpendLedger:
        """The operator ledger with the caps from ``configs/agents/llm.json``."""
        kw.setdefault("policy", SpendPolicy.from_config())
        return cls(None, **kw)

    # -- reading ------------------------------------------------------------

    def rows(self) -> Iterator[SpendRow]:
        """Every well-formed row in file order; corrupt lines are skipped and
        counted in :attr:`corrupt_rows`."""
        self.corrupt_rows = 0
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = _parse_row(line)
                if row is None:
                    self.corrupt_rows += 1
                    continue
                yield row

    def month_to_date(self, month: str | None = None) -> float:
        """USD spent in ``month`` (``YYYY-MM``, default: the current UTC month)."""
        m = month or utc_month(self.clock())
        return float(sum(r.usd for r in self.rows() if r.month == m))

    def last(self, n: int = 20) -> list[SpendRow]:
        out: list[SpendRow] = []
        for r in self.rows():
            out.append(r)
            if len(out) > n:
                del out[0]
        return out

    # -- the caps -----------------------------------------------------------

    def check_call(self, model: str, prompt_tokens: int, max_tokens: int, **kw: Any) -> float:
        """Refuse the next call *before* it is made when month-to-date plus its
        maximum cost (``prompt_tokens`` x input price + ``max_tokens`` x output
        price) would exceed the monthly cap.  Reads the shared ledger every
        time.  Returns the estimate."""
        est = _prices.max_call_cost(model, prompt_tokens, max_tokens, **kw)
        mtd = self.month_to_date()
        cap = self.policy.monthly_cap_usd
        if mtd + est > cap:
            raise MonthlyCapExceeded(
                f"monthly cap: ${mtd:.4f} spent this month; the next call could reach "
                f"${mtd + est:.4f} > ${cap:.2f} ({self.path})"
            )
        return est

    def record(
        self,
        *,
        model: str,
        usage: Any,
        session: str = "",
        run: str = "",
        role: str = "",
        pin: _prices.ModelPin | None = None,
        table: _prices.PriceTable | None = None,
    ) -> SpendRow:
        """Append one row for a call that was made; then raise the once-a-month
        alert when the month-to-date has reached the alert fraction."""
        tbl = table or _prices.load_prices()
        usd = _prices.usage_cost(model, usage, tbl)
        ts = self.clock()
        month = utc_month(ts)
        with FileLock(self.path):
            mtd = self.month_to_date(month) + usd
            row = SpendRow(
                utc=_utc(ts),
                session=session,
                run=run,
                role=role,
                model=model,
                pinned=pin.pinned if pin else "",
                prices_checked=pin.prices_checked if pin else tbl.checked,
                tokens_in=_prices._u(usage, "input_tokens"),
                tokens_out=_prices._u(usage, "output_tokens"),
                tokens_cache_read=_prices._u(usage, "cache_read_input_tokens"),
                tokens_cache_write=_prices._u(usage, "cache_creation_input_tokens"),
                usd=round(usd, 8),
                month_usd=round(mtd, 8),
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps({"schema": SCHEMA, **row.__dict__}, sort_keys=True) + "\n"
            with self.path.open("ab") as fh:
                if fh.tell() and not self._ends_with_newline():
                    fh.write(b"\n")  # a torn last line (crash mid-write) stays its own row
                fh.write(line.encode("utf-8"))
                fh.flush()
                os.fsync(fh.fileno())
        self._maybe_alert(month, mtd)
        return row

    def _ends_with_newline(self) -> bool:
        with self.path.open("rb") as fh:
            fh.seek(-1, os.SEEK_END)
            return fh.read(1) == b"\n"

    def alert_marker(self, month: str) -> Path:
        return self.path.with_name(f"{self.path.name}.alert-{month}")

    def _maybe_alert(self, month: str, mtd: float) -> bool:
        cap = self.policy.monthly_cap_usd
        if cap <= 0 or mtd < self.policy.alert_fraction * cap:
            return False
        try:
            fd = os.open(str(self.alert_marker(month)), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False  # another call or process already alerted this month
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"{_utc(self.clock())} month_usd={mtd:.4f} cap={cap:.2f}\n")
        pct = int(round(100 * self.policy.alert_fraction))
        msg = f"LLM spend {month}: ${mtd:.2f} of the ${cap:.2f} monthly cap ({pct}% alert)"
        log.warning("%s", msg)
        notifier = self._notifier if self._notifier is not None else _watchdog_notify()
        if notifier is not None:
            try:
                notifier("IAP LLM spend alert", msg)
            except Exception:  # pragma: no cover - a failed toast never blocks spend
                log.warning("spend alert notification failed", exc_info=True)
        return True

    def status(self) -> dict[str, Any]:
        rows = list(self.rows())
        month = utc_month(self.clock())
        mtd = float(sum(r.usd for r in rows if r.month == month))
        cap = self.policy.monthly_cap_usd
        return {
            "ledger": str(self.path),
            "month": month,
            "month_to_date_usd": round(mtd, 6),
            "monthly_cap_usd": cap,
            "remaining_usd": round(max(cap - mtd, 0.0), 6),
            "per_run_budget_usd": self.policy.per_run_budget_usd,
            "alert_fraction": self.policy.alert_fraction,
            "alerted_this_month": self.alert_marker(month).exists(),
            "rows": len(rows),
            "corrupt_rows": self.corrupt_rows,
        }


def main(argv: list[str] | None = None) -> int:
    """``python -m iap.llm spend [--ledger PATH] [--last N] [--json]``."""
    import argparse

    ap = argparse.ArgumentParser(prog="iap.llm spend", description="LLM spend ledger status")
    ap.add_argument(
        "--ledger", type=Path, default=None, help=f"default: ${LEDGER_ENV} or the OS state dir"
    )
    ap.add_argument("--last", type=int, default=20)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    policy = SpendPolicy.from_config()
    ledger = SpendLedger(a.ledger, policy=policy)
    st = ledger.status()
    rows = ledger.last(a.last)
    if a.json:
        print(json.dumps({**st, "last": [r.__dict__ for r in rows]}, indent=2, sort_keys=True))
        return 0
    print(f"ledger: {st['ledger']}")
    print(
        f"{st['month']}: month-to-date ${st['month_to_date_usd']:.4f}  cap ${st['monthly_cap_usd']:.2f}"
        f"  remaining ${st['remaining_usd']:.4f}  per-run ${st['per_run_budget_usd']:.2f}"
        f"  alert at {int(round(100 * st['alert_fraction']))}%"
        f"{' (alerted)' if st['alerted_this_month'] else ''}"
    )
    if st["corrupt_rows"]:
        print(f"corrupt rows skipped: {st['corrupt_rows']}")
    print(f"last {len(rows)} of {st['rows']} rows:")
    for r in rows:
        print(
            f"  {r.utc} {r.role or '-':<8} {r.model:<18} in={r.tokens_in} out={r.tokens_out}"
            f" cached={r.tokens_cache_read}/{r.tokens_cache_write} ${r.usd:.5f}"
            f" month=${r.month_usd:.4f} session={r.session} run={r.run}"
        )
    return 0
