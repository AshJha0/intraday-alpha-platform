"""docs/LLM_SAFETY.md demo: honest finding accepted, fabricated and derived numbers refused."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_llm_safety_demo_behaves_as_documented():
    out = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "llm_safety_demo.py")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert "[honest]" in out.stdout and "ACCEPTED" in out.stdout
    assert out.stdout.count("REFUSED") == 2
