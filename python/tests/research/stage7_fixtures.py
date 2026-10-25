"""Fixtures for the research-loop tests (imported by each test module; not a conftest, which would shadow tests/conftest.py)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
REPO_ROOT = HERE.parents[2]
FAMILIES = REPO_ROOT / "research" / "v2" / "families"
sys.path.insert(0, str(FIXTURES))

import daily_panel  # noqa: E402


@pytest.fixture(scope="session")
def manifest() -> dict:
    return json.loads(daily_panel.MANIFEST.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def panel_path(tmp_path_factory) -> Path:
    """The fixture CSV regenerated from its seed; ``write_fixture`` verifies the manifest's sha256."""
    out = tmp_path_factory.mktemp("daily_panel")
    return daily_panel.write_fixture(out)


@pytest.fixture(scope="session")
def manifest_path() -> Path:
    return daily_panel.MANIFEST
