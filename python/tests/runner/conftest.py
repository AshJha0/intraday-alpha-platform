"""The CR1 tests import the runner package from ``tools/`` and the D2 fixtures
from ``tests/isolation``; put both on the path before any test module loads."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
for p in (REPO_ROOT / "tools", REPO_ROOT / "python" / "tests" / "isolation"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
