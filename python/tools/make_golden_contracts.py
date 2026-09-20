#!/usr/bin/env python3
"""Generate tests/golden/expected_contracts_examples.json (x-version 1).

The document holds one canonical instance per contract type
(``iap.contracts.examples``), each validated against its JSON Schema before
writing, plus the pinned ``explain()`` rendering of the example
``DecisionTrace``, the pinned trace id and a ``canonical_json`` known
answer.  Everything is a pure function of pinned constants, so the file is
reproducible byte-for-byte (``tests/test_contracts_schema_golden.py``).

Usage: PYTHONPATH=src python3 tools/make_golden_contracts.py [--force]

Refuses to overwrite an existing file unless ``--force`` is given: a
regeneration is a deliberate, versioned change (schemas/MIGRATIONS.md).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from iap.contracts.examples import all_examples, golden_document, render_golden
from iap.contracts.validate import validate_typed

REPO = Path(__file__).resolve().parents[2]
GOLDEN = REPO / "tests" / "golden" / "expected_contracts_examples.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing golden file")
    args = parser.parse_args(argv)
    if GOLDEN.exists() and not args.force:
        print(f"refusing to overwrite {GOLDEN} (use --force)", file=sys.stderr)
        return 2
    for name, inst in all_examples().items():
        validate_typed(inst)
        if type(inst).from_dict(inst.to_dict()) != inst:
            raise RuntimeError(f"{name}: to_dict/from_dict round trip differs")
    text = render_golden(golden_document())
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(text, encoding="utf-8")
    print(f"wrote {GOLDEN} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
