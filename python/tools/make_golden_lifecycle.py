#!/usr/bin/env python3
"""Generate tests/golden/expected_lifecycle.json (x-version 1).

Runs the three scripted scenarios of ``iap.lifecycle.golden`` (LC01 happy
path to retirement, LC02 leakage demotion, LC03 persistent paper failure +
manual retirement) through ``iap.lifecycle.machine.AlphaLifecycle`` under
the pinned policy (``configs/strategies/lifecycle.json`` +
``strategies.json`` ``adaptive.lifecycle``), checks every transition against
its JSON Schema and that the scripts end where they were designed to, and
writes the document with the config embedded inline.

Usage: PYTHONPATH=src python3 tools/make_golden_lifecycle.py [--force]

Refuses to overwrite an existing file unless ``--force`` is given: a
regeneration is a deliberate, versioned change.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from iap.contracts.types import LifecycleTransition
from iap.contracts.validate import validate_typed
from iap.lifecycle.config import load_policy_config
from iap.lifecycle.golden import golden_document, render_golden

REPO = Path(__file__).resolve().parents[2]
GOLDEN = REPO / "tests" / "golden" / "expected_lifecycle.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing golden file")
    args = parser.parse_args(argv)
    if GOLDEN.exists() and not args.force:
        print(f"refusing to overwrite {GOLDEN} (use --force)", file=sys.stderr)
        return 2
    config = load_policy_config()
    doc = golden_document(config)
    n_transitions = 0
    for steps in doc["scenarios"].values():
        for step in steps:
            tr = step["expected"]["transition"]
            if tr is not None:
                validate_typed(LifecycleTransition.from_dict(tr))
                n_transitions += 1
    text = render_golden(doc)
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(text, encoding="utf-8")
    print(f"wrote {GOLDEN} ({len(text)} bytes, {n_transitions} transitions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
