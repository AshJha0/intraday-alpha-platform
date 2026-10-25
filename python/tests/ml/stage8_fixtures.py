"""Fixtures for the ML framework tests (imported by each test module; not a conftest, which would shadow tests/conftest.py)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
FIXTURES = REPO_ROOT / "python" / "tests" / "research" / "fixtures"
FAMILIES = REPO_ROOT / "research" / "v2" / "families"
sys.path.insert(0, str(FIXTURES))

import daily_panel  # noqa: E402


@pytest.fixture(scope="session")
def manifest() -> dict:
    return json.loads(daily_panel.MANIFEST.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def panel_path(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("daily_panel_ml")
    return daily_panel.write_fixture(out)


@pytest.fixture(scope="session")
def manifest_path() -> Path:
    return daily_panel.MANIFEST
