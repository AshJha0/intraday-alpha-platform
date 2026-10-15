"""v1.17 F7 part 1: the C++ parity goldens are pinned to the Python reference.

``python/tools/make_golden_f7.py`` writes ``tests/golden/expected_f7_*.json``
from the Python rule code; the C++ tests ``cpp/tests/test_f7_*.cpp`` compare
their canonical bytes. This test regenerates every document in-process and
pins the committed bytes, so a change to a reference rule (or to the
generator) cannot drift from what the C++ port is held to: regenerate with
``--force`` and commit the new bytes together with the port.

Named with ``golden`` so the cross-language golden group (``pytest -k
golden``, tests/harness/run_all.sh) runs it.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from f7_golden import GOLDEN_DIR, dumps  # noqa: E402
from make_golden_f7 import MODULES, first_diff  # noqa: E402


@pytest.mark.parametrize("name", MODULES)
def test_f7_golden_is_pinned_to_the_reference(name: str) -> None:
    mod = importlib.import_module(f"f7_golden.{name}")
    path = GOLDEN_DIR / mod.NAME
    assert path.exists(), f"{mod.NAME}: run PYTHONPATH=src python tools/make_golden_f7.py"
    committed = path.read_bytes()
    regenerated = dumps(mod.document())
    assert committed == regenerated, f"{mod.NAME}: {first_diff(committed, regenerated)}"
