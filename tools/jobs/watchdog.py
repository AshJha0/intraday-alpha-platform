#!/usr/bin/env python3
"""Watchdog for long detached real-data jobs (docs/RUNBOOKS.md).

Each registered job is a WMI-launched ``.cmd`` wrapper that appends to a log and
ends with an ``EXIT n`` line.  Every tick the watchdog classifies each job:

* ``running``  - a matching process is alive and the log grew recently
* ``stalled``  - process alive but the log has not grown for ``stall_minutes``
* ``done``     - no process, last exit marker is ``EXIT 0``
* ``failed``   - no process, last exit marker is ``EXIT n`` with n != 0
* ``died``     - no process and no exit marker after the last start
* ``missing``  - the log does not exist yet (never started)

``died`` jobs flagged ``restart_safe`` are relaunched (detached, the same WMI
``Win32_Process.Create`` route used by hand) up to ``max_restarts`` times.
Transitions are appended to the watchdog log and raised as a Windows toast.

The watchdog NEVER kills or signals a process: it only lists processes, reads
logs, launches wrappers and notifies.

Usage::

    python tools/jobs/watchdog.py --registry data/real/jobs.json --once
    python tools/jobs/watchdog.py --registry data/real/jobs.json --loop --interval 600
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

RUNNING, STALLED, DONE, FAILED, DIED, MISSING = (
    "running",
    "stalled",
    "done",
    "failed",
    "died",
    "missing",
)
RESTARTED = "restarted"
NOTIFY_ON = {DONE, FAILED, DIED, STALLED, RESTARTED}


@dataclass
class Job:
    name: str
    wrapper: str
    log: str
    process_pattern: str
    done_regex: str = r"^EXIT 0\s*$"
    exit_regex: str = r"^EXIT (-?\d+)\s*$"
    start_regex: str = r"^START\b"
    restart_safe: bool = False
    max_restarts: int = 0
    stall_minutes: float = 60.0
    enabled: bool = True


@dataclass
class Registry:
    jobs: list[Job]
    watchdog_log: str
    state_file: str


@dataclass
class Status:
    state: str
    detail: str = ""
    pids: list[int] = field(default_factory=list)


def _resolve(base: Path, p: str) -> str:
    q = Path(p)
    return str(q if q.is_absolute() else (base / q))


def load_registry(path: Path) -> Registry:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    base = Path(path).resolve().parent
    jobs = []
    for j in raw["jobs"]:
        job = Job(**j)
        job.wrapper = _resolve(base, job.wrapper)
        job.log = _resolve(base, job.log)
        jobs.append(job)
    names = [j.name for j in jobs]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate job names in {path}")
    return Registry(
        jobs=jobs,
        watchdog_log=_resolve(base, raw.get("watchdog_log", "watchdog.log")),
        state_file=_resolve(base, raw.get("state_file", "watchdog_state.json")),
    )


# --------------------------------------------------------------------- system


def list_processes() -> list[tuple[int, str]]:
    """(pid, command line) of every process, via CIM. Read-only."""
    ps = (
        "Get-CimInstance Win32_Process | Select-Object ProcessId,CommandLine"
        " | ConvertTo-Json -Compress"
    )
    out = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    ).stdout
    data = json.loads(out or "[]")
    if isinstance(data, dict):
        data = [data]
    return [(int(d["ProcessId"]), d.get("CommandLine") or "") for d in data]


def launch_detached(wrapper: str) -> None:
    """Start ``wrapper`` via WMI Win32_Process.Create, detached from this process tree."""
    cmdline = f'cmd.exe /c "{wrapper}"'.replace("'", "''")
    ps = (
        "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        f"-Arguments @{{CommandLine='{cmdline}'}}; "
        "if ($r.ReturnValue -ne 0) { exit $r.ReturnValue }"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
        check=True,
        timeout=120,
    )


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def notify(title: str, message: str) -> str:
    """Toast: BurntToast, else Windows.UI.Notifications, else msg.exe. Returns the route used."""
    t, m = _ps_quote(title), _ps_quote(message)
    burnt = (
        "if (-not (Get-Module -ListAvailable BurntToast)) { exit 3 }; "
        f"Import-Module BurntToast; New-BurntToastNotification -Text {t}, {m}"
    )
    native = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,"
        " ContentType = WindowsRuntime] > $null; "
        "$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
        "$n = $x.GetElementsByTagName('text'); "
        f"$n.Item(0).AppendChild($x.CreateTextNode({t})) > $null; "
        f"$n.Item(1).AppendChild($x.CreateTextNode({m})) > $null; "
        "$id = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe'; "
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id)"
        ".Show([Windows.UI.Notifications.ToastNotification]::new($x))"
    )
    for route, ps in (("burnttoast", burnt), ("winrt", native)):
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True,
                timeout=60,
            )
            if r.returncode == 0:
                return route
        except (OSError, subprocess.SubprocessError):
            pass
    if shutil.which("msg"):
        try:
            subprocess.run(["msg", "*", f"{title}: {message}"], timeout=30)
            return "msg"
        except (OSError, subprocess.SubprocessError):
            pass
    return "none"


# ------------------------------------------------------------------ classify


def _tail_text(path: Path, max_bytes: int = 1 << 20) -> str:
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        return f.read().decode("utf-8", errors="replace")


def classify(
    job: Job,
    procs: Iterable[tuple[int, str]],
    now: float,
    stat: Callable[[Path], tuple[int, float] | None] | None = None,
    read: Callable[[Path], str] = _tail_text,
) -> Status:
    """Pure classification of one job from a process list and its log."""
    stat = stat or _stat
    pat = re.compile(job.process_pattern, re.IGNORECASE)
    me = re.compile(r"tools[\\/]jobs[\\/]watchdog", re.IGNORECASE)
    pids = [pid for pid, cl in procs if cl and pat.search(cl) and not me.search(cl)]
    log = Path(job.log)
    st = stat(log)
    if pids:
        if st is not None and (now - st[1]) > job.stall_minutes * 60:
            mins = (now - st[1]) / 60
            return Status(STALLED, f"no log growth for {mins:.0f} min", pids)
        return Status(RUNNING, "", pids)
    if st is None:
        return Status(MISSING, f"log not found: {log}")
    text = read(log)
    lines = text.splitlines()
    exit_re, start_re = re.compile(job.exit_regex), re.compile(job.start_regex)
    done_re = re.compile(job.done_regex)
    last_exit = last_start = -1
    for i, line in enumerate(lines):
        if exit_re.search(line) or done_re.search(line):
            last_exit = i
        if start_re.search(line):
            last_start = i
    if last_exit < 0 or last_start > last_exit:
        return Status(DIED, "no process and no exit marker after last start")
    line = lines[last_exit]
    if done_re.search(line):
        return Status(DONE, line.strip())
    return Status(FAILED, line.strip())


def _stat(p: Path) -> tuple[int, float] | None:
    try:
        s = p.stat()
    except OSError:
        return None
    return s.st_size, s.st_mtime


# ---------------------------------------------------------------------- tick


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def tick(
    reg: Registry,
    *,
    procs_fn: Callable[[], list[tuple[int, str]]] = list_processes,
    launch_fn: Callable[[str], None] = launch_detached,
    notify_fn: Callable[[str, str], object] = notify,
    now_fn: Callable[[], float] = time.time,
    stat_fn: Callable[[Path], tuple[int, float] | None] | None = None,
    read_fn: Callable[[Path], str] = _tail_text,
    dry_run: bool = False,
) -> dict[str, str]:
    """One pass over the registry. Returns {job: state (or 'restarted')}."""
    now = now_fn()
    procs = procs_fn()
    state_path = Path(reg.state_file)
    state = _load_state(state_path)
    out: dict[str, str] = {}
    events: list[str] = []
    for job in reg.jobs:
        if not job.enabled:
            continue
        js = state.setdefault(job.name, {"last": None, "restarts": 0})
        st = classify(job, procs, now, stat=stat_fn, read=read_fn)
        result = st.state
        detail = st.detail
        if st.state == DIED and job.restart_safe:
            if js["restarts"] < job.max_restarts:
                if dry_run:
                    detail += " (dry-run: would restart)"
                else:
                    try:
                        launch_fn(job.wrapper)
                        js["restarts"] += 1
                        result = RESTARTED
                        detail = f"relaunched {job.wrapper} ({js['restarts']}/{job.max_restarts})"
                    except Exception as e:  # noqa: BLE001 - report, never crash the loop
                        detail += f"; restart failed: {e}"
            else:
                detail += f"; restart budget exhausted ({job.max_restarts})"
        if result != js["last"] or result == RESTARTED:
            events.append(f"{job.name}: {result} {detail}".rstrip())
            if result in NOTIFY_ON:
                try:
                    notify_fn(f"IAP job {job.name}: {result}", detail or result)
                except Exception:  # noqa: BLE001
                    pass
        js["last"] = result
        js["detail"] = detail
        js["checked"] = datetime.fromtimestamp(now, UTC).isoformat()
        out[job.name] = result
    stamp = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    log_lines = [f"{stamp} {e}" for e in events] or [f"{stamp} tick: no changes"]
    if not dry_run:
        Path(reg.watchdog_log).parent.mkdir(parents=True, exist_ok=True)
        with open(reg.watchdog_log, "a", encoding="utf-8") as f:
            f.write("\n".join(log_lines) + "\n")
        tmp = state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(state_path)
    for line in log_lines:
        print(line)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--registry", required=True, type=Path)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=float, default=600.0, help="seconds between ticks")
    ap.add_argument(
        "--dry-run", action="store_true", help="classify only: no restart, no log/state writes"
    )
    ap.add_argument("--no-notify", action="store_true")
    a = ap.parse_args(argv)
    reg = load_registry(a.registry)
    kw = {"dry_run": a.dry_run}
    if a.no_notify:
        kw["notify_fn"] = lambda *_: None
    if a.once:
        tick(reg, **kw)
        return 0
    while True:
        try:
            tick(reg, **kw)
        except Exception as e:  # noqa: BLE001 - keep watching
            print(f"tick error: {e}", file=sys.stderr)
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
