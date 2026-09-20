#!/usr/bin/env python3
"""Generate tests/golden/expected_experiment_golden_frame.json (x-version 1).

Runs the pinned golden experiment (``iap.research.golden``: EQ03 at 5 s on
instrument 1 of ``tests/golden/events_eq_mbo.jsonl``) through
``iap.research.ExperimentRunner`` on a fresh in-memory ledger and pins the
ExperimentSpec (hence the experiment id) and the ExperimentResult.
``python/tests/test_research_golden.py`` reproduces it: floats at 1e-9
abs/rel, everything else exactly.

Usage: PYTHONPATH=src python3 tools/make_golden_research.py [--force]

Refuses to overwrite an existing file unless ``--force`` is given: a
regeneration is a deliberate, versioned change (a changed golden vector,
feature registry, alpha, validation rule or runner mapping).
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from iap.research.golden import (
    golden_document,
    golden_frames,
    golden_result,
    golden_spec,
    render_golden,
)

REPO = Path(__file__).resolve().parents[2]
GOLDEN_DIR = REPO / "tests" / "golden"
CONFIGS_DIR = REPO / "configs"
GOLDEN = GOLDEN_DIR / "expected_experiment_golden_frame.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing golden file")
    args = parser.parse_args(argv)
    if GOLDEN.exists() and not args.force:
        print(f"refusing to overwrite {GOLDEN} (use --force)", file=sys.stderr)
        return 2
    frames = golden_frames(GOLDEN_DIR, CONFIGS_DIR)
    spec = golden_spec(GOLDEN_DIR, frames)
    with tempfile.TemporaryDirectory() as scratch:
        result = golden_result(spec, frames, CONFIGS_DIR, Path(scratch))
    text = render_golden(golden_document(spec, result))
    GOLDEN.write_text(text, encoding="ascii")
    print(f"wrote {GOLDEN} ({len(text)} bytes): experiment {spec.experiment_id} "
          f"verdict {result.verdict.value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
