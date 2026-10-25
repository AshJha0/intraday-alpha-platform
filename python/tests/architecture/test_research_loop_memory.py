"""Memory budget of the research loop on the synthetic fixture (v2.0 stage 11; U0 stage 11).

``iap.research.loop.run_family`` (stage 7-8) is run in a FRESH interpreter on the committed daily
panel fixture (``tests/research/fixtures/daily_panel.py``), and the child's peak resident set size
must stay below ``RSS_BUDGET_MB``.  A fresh process, not this one: under xdist the worker's lifetime
peak includes every earlier test.  The budget is declared here and in the CHANGELOG; raise it only
with the measurement that motivates it.

Peak RSS: ``resource.getrusage(RUSAGE_SELF).ru_maxrss`` (KB on Linux, bytes on macOS) or, on
Windows, ``GetProcessMemoryInfo().PeakWorkingSetSize``.  Skips while ``iap.research.loop`` is not in
the tree (D-66).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from conftest import REPO_ROOT

pytest.importorskip(
    "iap.research.loop", reason="iap.research.loop lands with the stage 7-8 PR (D-66)"
)

RSS_BUDGET_MB = (
    1536  # declared budget for one family on the synthetic fixture (60 names x 560 sessions)
)
FIXTURE = REPO_ROOT / "python" / "tests" / "research" / "fixtures" / "daily_panel.py"

CHILD = r"""
import json, sys
sys.path.insert(0, sys.argv[1])          # python/src
sys.path.insert(0, sys.argv[2])          # tests/research/fixtures
import daily_panel
from iap.research import loop

panel = daily_panel.build_panel() if hasattr(daily_panel, "build_panel") else daily_panel.panel()
report = loop.run_family("d1", "development", panel=panel) if "panel" in loop.run_family.__code__.co_varnames else loop.run_family("d1", "development")

def peak_mb():
    if sys.platform == "win32":
        import ctypes, ctypes.wintypes as w
        class PMC(ctypes.Structure):
            _fields_ = [("cb", w.DWORD), ("PageFaultCount", w.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]
        pmc = PMC(); pmc.cb = ctypes.sizeof(PMC)
        ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
        return pmc.PeakWorkingSetSize / 2**20
    import resource
    ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return ru / 2**20 if sys.platform == "darwin" else ru / 1024

print(json.dumps({"peak_rss_mb": peak_mb(), "ok": report is not None}))
"""


def test_research_loop_peak_rss_on_the_synthetic_fixture_is_within_budget(tmp_path):
    assert FIXTURE.is_file(), FIXTURE
    env = dict(os.environ, PYTHONUTF8="1", IAP_RESEARCH_OUT=str(tmp_path))
    res = subprocess.run(
        [sys.executable, "-c", CHILD, str(REPO_ROOT / "python" / "src"), str(FIXTURE.parent)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
        timeout=900,
    )
    assert res.returncode == 0, res.stderr[-4000:]
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out["ok"]
    assert out["peak_rss_mb"] < RSS_BUDGET_MB, (
        f"research loop peak RSS {out['peak_rss_mb']:.0f} MB exceeds the declared budget {RSS_BUDGET_MB} MB"
    )
