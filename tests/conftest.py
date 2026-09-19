"""Root-level pytest configuration for tests/integration and tests/replay.

Makes ``python3 -m pytest -q tests/integration tests/replay`` work from the
repository root without an explicit ``PYTHONPATH=python/src``: the Python
reference package (``iap``) is inserted at the front of ``sys.path`` so the
tests import the working-tree implementation, not an installed copy.

The per-language unit suites are NOT collected from here: ``python/tests``
has its own ``conftest.py`` and is run with ``cd python && PYTHONPATH=src
python3 -m pytest -q`` (PLATFORM_CONVENTIONS.md §9).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON_SRC = REPO_ROOT / "python" / "src"
GOLDEN_DIR = REPO_ROOT / "tests" / "golden"
CONFIGS_DIR = REPO_ROOT / "configs"

if str(PYTHON_SRC) not in sys.path:
    sys.path.insert(0, str(PYTHON_SRC))


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def golden_dir() -> Path:
    assert GOLDEN_DIR.is_dir(), f"missing {GOLDEN_DIR}"
    return GOLDEN_DIR


@pytest.fixture(scope="session")
def configs_dir() -> Path:
    assert (CONFIGS_DIR / "instruments" / "instruments.json").is_file(), \
        f"missing nested configs tree under {CONFIGS_DIR}"
    return CONFIGS_DIR
