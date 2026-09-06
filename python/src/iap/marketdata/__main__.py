"""End-to-end synthetic market-data pipeline entry point.

Usage (from ``python/`` with ``PYTHONPATH=src``, or with ``iap`` installed):

    python3 -m iap.marketdata [--config CONFIG] [--configs-dir DIR]
                              [--out DATA_DIR] [--seed SEED]

Runs: generator -> data/raw/*.jsonl, then the raw->normalized pipeline ->
data/normalized/ (*.normalized.jsonl, *.normalized.iap1, events.parquet,
qc_report.json). Prints run stats as JSON on stdout. Fully deterministic for
a given seed/config.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from iap.marketdata.generator import MarketDataGenerator, load_generator_config
from iap.marketdata.normalize import normalize_run
from iap.reference.refdata import ReferenceData

_REPO_ROOT = Path(__file__).resolve().parents[4]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m iap.marketdata",
        description="Generate synthetic raw market data and normalize it.",
    )
    parser.add_argument(
        "--configs-dir", default=str(_REPO_ROOT / "configs"),
        help="configs/ directory (instruments.json, venues.json, generator.json)",
    )
    parser.add_argument(
        "--config", default=None,
        help="generator config JSON (default: <configs-dir>/generator.json)",
    )
    parser.add_argument(
        "--out", default=str(_REPO_ROOT / "data"),
        help="data directory root (raw/ and normalized/ are created inside)",
    )
    parser.add_argument("--seed", type=int, default=None,
                        help="override the config seed")
    parser.add_argument(
        "--allow-default-config", action="store_true",
        help="run with the built-in generator defaults when the config file "
             "is absent (default: fail fast — a silently defaulted config "
             "changes the dataset version without a trace)",
    )
    args = parser.parse_args(argv)

    configs_dir = Path(args.configs_dir)
    config_path = Path(args.config) if args.config else configs_dir / "generator.json"
    # Fail fast (PLATFORM_CONVENTIONS.md §8/§12.2, SPEC §26): a missing or
    # typo'd generator.json used to fall back to the built-in defaults
    # silently. That is identical to the committed file TODAY, so a ConfigMap
    # that omits the key produces a dataset nobody notices is different the
    # moment the two diverge (anomaly rates, sessions, seed) — and the
    # dataset version is part of the reproducibility chain.
    if not config_path.exists():
        if not args.allow_default_config:
            raise FileNotFoundError(
                f"generator config not found: {config_path}. Pass --config with "
                "a real path, or --allow-default-config to run with the "
                "built-in defaults on purpose."
            )
        cfg = load_generator_config(None)
    else:
        cfg = load_generator_config(config_path)
    if args.seed is not None:
        cfg["seed"] = args.seed

    refdata = ReferenceData.load(configs_dir)
    out_root = Path(args.out)
    raw_dir = out_root / "raw"
    normalized_dir = out_root / "normalized"

    t0 = time.perf_counter()
    gen = MarketDataGenerator(refdata, cfg)
    gen_stats = gen.generate_run(raw_dir)
    t1 = time.perf_counter()
    qc = normalize_run(raw_dir, normalized_dir)
    t2 = time.perf_counter()

    summary = {
        "seed": cfg["seed"],
        "sessions": cfg["sessions"],
        "raw_dir": str(raw_dir),
        "normalized_dir": str(normalized_dir),
        "generator": gen_stats,
        "qc_totals": qc["totals"],
        "parquet_rows": qc["parquet"]["rows"],
        "timing_s": {
            "generate": round(t1 - t0, 3),
            "normalize": round(t2 - t1, 3),
            "total": round(t2 - t0, 3),
        },
    }
    json.dump(summary, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
