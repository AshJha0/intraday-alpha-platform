"""tools/jobs/watchdog.py: classification, restart policy, notifications.

Everything system-facing (process list, launcher, toast, clock) is mocked:
no process is listed, launched or killed for real.
"""

from __future__ import annotations

import importlib.util
import json
import sys

import pytest
from conftest import REPO_ROOT


def _load():
    spec = importlib.util.spec_from_file_location("watchdog", REPO_ROOT / "tools/jobs/watchdog.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["watchdog"] = mod  # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(mod)
    return mod


wd = _load()
NOW = 1_800_000_000.0


def _registry(tmp_path, **over):
    job = {
        "name": "maker",
        "wrapper": "run_maker.cmd",
        "log": "maker.log",
        "process_pattern": r"iap\.research\.maker_real",
        "restart_safe": True,
        "max_restarts": 2,
        "stall_minutes": 30,
    }
    job.update(over)
    p = tmp_path / "jobs.json"
    p.write_text(json.dumps({"jobs": [job]}), encoding="utf-8")
    return wd.load_registry(p)


def _log(tmp_path, text, age_s=0.0):
    import os

    p = tmp_path / "maker.log"
    p.write_text(text, encoding="utf-8")
    os.utime(p, (NOW - age_s, NOW - age_s))
    return p


PROC = [(42, "python -m iap.research.maker_real --data-root x")]


class Harness:
    def __init__(self, procs=(), launch_raises=False):
        self.procs = list(procs)
        self.launched, self.toasts = [], []
        self.launch_raises = launch_raises

    def launch(self, w):
        if self.launch_raises:
            raise RuntimeError("boom")
        self.launched.append(w)

    def run(self, reg, **kw):
        return wd.tick(
            reg,
            procs_fn=lambda: self.procs,
            launch_fn=self.launch,
            notify_fn=lambda t, m: self.toasts.append((t, m)),
            now_fn=lambda: NOW,
            **kw,
        )


def test_running(tmp_path):
    reg = _registry(tmp_path)
    _log(tmp_path, "START\nworking\n", age_s=60)
    h = Harness(PROC)
    assert h.run(reg) == {"maker": "running"}
    assert h.toasts == [] and h.launched == []


def test_stalled_notifies_but_never_kills(tmp_path):
    reg = _registry(tmp_path)
    _log(tmp_path, "START\n", age_s=31 * 60)
    h = Harness(PROC)
    assert h.run(reg) == {"maker": "stalled"}
    assert len(h.toasts) == 1 and h.launched == []
    assert (
        not hasattr(wd, "kill")
        and "terminate" not in (REPO_ROOT / "tools/jobs/watchdog.py").read_text()
    )


def test_done_and_notify_once(tmp_path):
    reg = _registry(tmp_path)
    _log(tmp_path, "START\nresult\nEXIT 0\n")
    h = Harness()
    assert h.run(reg) == {"maker": "done"}
    assert h.run(reg) == {"maker": "done"}
    assert len(h.toasts) == 1 and h.launched == []


def test_failed(tmp_path):
    reg = _registry(tmp_path)
    _log(tmp_path, "START\nTraceback\nEXIT 1\n")
    h = Harness()
    assert h.run(reg) == {"maker": "failed"}
    assert h.launched == [] and "EXIT 1" in h.toasts[0][1]


def test_died_restarts_up_to_budget(tmp_path):
    reg = _registry(tmp_path)
    _log(tmp_path, "START\nhalfway\n")
    h = Harness()
    assert h.run(reg) == {"maker": "restarted"}
    assert h.run(reg) == {"maker": "restarted"}
    assert h.run(reg) == {"maker": "died"}
    assert h.launched == [str(tmp_path / "run_maker.cmd")] * 2
    state = json.loads((tmp_path / "watchdog_state.json").read_text())
    assert state["maker"]["restarts"] == 2
    assert "budget exhausted" in state["maker"]["detail"]
    assert len(h.toasts) == 3
    assert "restarted" in (tmp_path / "watchdog.log").read_text()


def test_restart_after_earlier_exit_counts_as_died(tmp_path):
    reg = _registry(tmp_path)
    _log(tmp_path, "START\nEXIT 1\nSTART\nmore\n")
    assert wd.classify(reg.jobs[0], [], NOW).state == "died"


def test_died_not_restart_safe(tmp_path):
    reg = _registry(tmp_path, restart_safe=False)
    _log(tmp_path, "START\n")
    h = Harness()
    assert h.run(reg) == {"maker": "died"}
    assert h.launched == [] and len(h.toasts) == 1


def test_restart_failure_is_reported(tmp_path):
    reg = _registry(tmp_path)
    _log(tmp_path, "START\n")
    h = Harness(launch_raises=True)
    assert h.run(reg) == {"maker": "died"}
    assert "restart failed: boom" in h.toasts[0][1]


def test_dry_run_writes_nothing(tmp_path):
    reg = _registry(tmp_path)
    _log(tmp_path, "START\n")
    h = Harness()
    assert h.run(reg, dry_run=True) == {"maker": "died"}
    assert h.launched == []
    assert not (tmp_path / "watchdog.log").exists()
    assert not (tmp_path / "watchdog_state.json").exists()


def test_missing_log(tmp_path):
    reg = _registry(tmp_path)
    assert Harness().run(reg) == {"maker": "missing"}


def test_watchdog_own_process_is_ignored(tmp_path):
    reg = _registry(tmp_path, process_pattern="maker")
    _log(tmp_path, "START\nEXIT 0\n")
    procs = [(7, "python tools/jobs/watchdog.py --registry maker.json --once")]
    assert wd.classify(reg.jobs[0], procs, NOW).state == "done"


def test_example_registry_loads():
    reg = wd.load_registry(REPO_ROOT / "tools/jobs/jobs.example.json")
    assert {j.name for j in reg.jobs} == {"maker_real", "r7_insample_chain"}
    assert all(not j.restart_safe for j in reg.jobs)


def test_duplicate_names_rejected(tmp_path):
    p = tmp_path / "jobs.json"
    j = {"name": "a", "wrapper": "w", "log": "l", "process_pattern": "x"}
    p.write_text(json.dumps({"jobs": [j, j]}))
    with pytest.raises(ValueError):
        wd.load_registry(p)


def test_notify_fallback_chain(monkeypatch):
    calls = []

    class R:
        def __init__(self, rc):
            self.returncode = rc

    def fake_run(cmd, **kw):
        calls.append(cmd[0])
        return R(1)

    monkeypatch.setattr(wd.subprocess, "run", fake_run)
    monkeypatch.setattr(wd.shutil, "which", lambda n: "msg.exe")
    assert wd.notify("t", "m") == "msg"
    assert calls == ["powershell", "powershell", "msg"]


def test_launch_uses_wmi_create(monkeypatch):
    seen = {}
    monkeypatch.setattr(wd.subprocess, "run", lambda cmd, **kw: seen.setdefault("cmd", cmd))
    wd.launch_detached(r"C:\x\run it.cmd")
    ps = seen["cmd"][-1]
    assert "Win32_Process" in ps and "Create" in ps and r"C:\x\run it.cmd" in ps
