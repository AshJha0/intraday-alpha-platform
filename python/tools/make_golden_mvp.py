#!/usr/bin/env python3
"""Generate tests/golden/expected_mvp.json (x-version 1) — the MVP golden.

Runs ``python -m iap.mvp run`` semantics (seeded feed + the full loop) on
``configs/mvp/mvp.json`` into a temporary directory and writes the document
of :mod:`iap.mvp.golden`: the configuration hash, the event-stream sha256 +
data_version, the event count, the trace digest, the first / last three
trace ids, the counts by risk rule / venue / algo and the FULL report
(floats compared at 1e-9 by ``python/tests/test_mvp_golden.py``; every
other value exactly).  A Java / C++ / Rust port of the loop must reproduce
this file.

Usage: PYTHONPATH=src python3 tools/make_golden_mvp.py [--force]

Refuses to overwrite an existing file unless ``--force`` is given: a
regeneration is a deliberate, versioned change (schemas/MIGRATIONS.md).
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from iap.mvp.config import load_config
from iap.mvp.feed import generate_feed
from iap.mvp.golden import golden_document, render
from iap.mvp.session import run_session

REPO = Path(__file__).resolve().parents[2]
GOLDEN = REPO / "tests" / "golden" / "expected_mvp.json"


def build() -> Dict[str, Any]:
    """Run the golden session in a temporary directory and build the document."""
    cfg = load_config()
    with tempfile.TemporaryDirectory(prefix="iap-mvp-golden-") as tmp:
        out = Path(tmp) / "run"
        feed = generate_feed(cfg, out)
        return golden_document(run_session(cfg, feed, out))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="overwrite an existing golden file")
    args = parser.parse_args(argv)
    if GOLDEN.exists() and not args.force:
        print(f"refusing to overwrite {GOLDEN} (use --force)", file=sys.stderr)
        return 2
    text = render(build())
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(text, encoding="utf-8")
    print(f"wrote {GOLDEN} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
