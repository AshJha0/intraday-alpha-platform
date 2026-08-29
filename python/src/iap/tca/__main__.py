"""CLI: generate research/tca/TCA_REPORT.md from the bundled simulation.

Usage (from python/): ``PYTHONPATH=src python3 -m iap.tca``
"""

from __future__ import annotations

import argparse
from pathlib import Path

from iap.tca.report import generate_report


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the TCA report")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="output directory (default research/tca)")
    parser.add_argument("--golden-dir", type=Path, default=None,
                        help="golden vectors directory (default tests/golden)")
    args = parser.parse_args()
    path = generate_report(args.out_dir, args.golden_dir)
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
