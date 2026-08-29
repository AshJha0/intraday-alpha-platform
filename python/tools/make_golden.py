#!/usr/bin/env python3
"""(Re)generate the cross-language golden vectors in tests/golden/.

Run from python/ with PYTHONPATH=src:

    PYTHONPATH=src python3 tools/make_golden.py

Writes: events_eq_mbo.jsonl, events_fx_quote.jsonl, expected_book_states.json,
expected_codec_sha256.json, splitmix64.json. Expected book states are only
written after the reference OrderBook and an INDEPENDENT brute-force rebuild
(tests/bruteforce_book.py) agree exactly at every pinned index.

Golden vectors are pinned: regenerate ONLY on a deliberate, versioned change
(see schemas/MIGRATIONS.md) — every other language must match these bytes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python" / "src"))
sys.path.insert(0, str(REPO / "python" / "tests"))

from bruteforce_book import BruteForceBook  # noqa: E402
from iap.core.codec import sha256_events_iap1, write_jsonl  # noqa: E402
from iap.core.rng import SplitMix64  # noqa: E402
from iap.marketdata.generator import (  # noqa: E402
    GOLDEN_EQ_SEED,
    GOLDEN_FX_SEED,
    generate_golden_eq,
    generate_golden_fx,
)
from iap.orderbook.book import OrderBook  # noqa: E402
from iap.reference.refdata import ReferenceData  # noqa: E402

GOLDEN_DIR = REPO / "tests" / "golden"
BOOK_STATE_INDICES = [100, 500, 1000, 1500, 2000]  # 1-based event counts


def splitmix64_reference(seed: int, n: int):
    """Independent inline SplitMix64 (do NOT import from iap for this)."""
    mask = (1 << 64) - 1
    state = seed & mask
    outs = []
    for _ in range(n):
        state = (state + 0x9E3779B97F4A7C15) & mask
        z = state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & mask
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & mask
        outs.append((z ^ (z >> 31)) & mask)
    return outs


def main() -> int:
    ref = ReferenceData.load(REPO / "configs")
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Event vectors.
    eq = generate_golden_eq(ref)
    fx = generate_golden_fx(ref)
    assert len(eq) == 2000 and len(fx) == 800

    # 2. Expected book states — cross-validated reference vs brute force.
    book = OrderBook(eq[0].instrument_id, eq[0].venue_id)
    brute = BruteForceBook()
    states = {}
    for i, ev in enumerate(eq, start=1):
        book.apply(ev)
        brute.apply(ev)
        if i in BOOK_STATE_INDICES:
            ref_state = book.state_summary()
            brute_state = brute.state_summary()
            if ref_state != brute_state:
                raise SystemExit(
                    f"VALIDATION FAILED at event {i}:\n"
                    f"reference:   {ref_state}\nbrute force: {brute_state}"
                )
            states[str(i)] = ref_state
    assert book.gaps_detected == 0 and book.duplicates_dropped == 0
    assert book.unknown_order_events == 0 and not book.stale

    # 3. Write everything.
    write_jsonl(GOLDEN_DIR / "events_eq_mbo.jsonl", eq)
    write_jsonl(GOLDEN_DIR / "events_fx_quote.jsonl", fx)

    book_states = {
        "description": "Exact-integer book state after applying the first N events "
                       "of events_eq_mbo.jsonl (N is the key; 1-based). Validated by "
                       "an independent brute-force rebuild before writing. "
                       "Tolerance: exact equality.",
        "vector": "events_eq_mbo.jsonl",
        "instrument_id": eq[0].instrument_id,
        "venue_id": eq[0].venue_id,
        "states": states,
    }
    with open(GOLDEN_DIR / "expected_book_states.json", "w") as f:
        json.dump(book_states, f, indent=2)
        f.write("\n")

    shas = {
        "description": "SHA-256 of the IAP1 encoding (schemas/FORMAT.md) of each "
                       "golden vector. Every language must encode to these exact bytes.",
        "events_eq_mbo.iap1": sha256_events_iap1(eq),
        "events_fx_quote.iap1": sha256_events_iap1(fx),
    }
    with open(GOLDEN_DIR / "expected_codec_sha256.json", "w") as f:
        json.dump(shas, f, indent=2)
        f.write("\n")

    # 4. SplitMix64 known-answer vector (independent inline implementation,
    #    cross-checked against iap.core.rng).
    seed = 42
    outs = splitmix64_reference(seed, 5)
    rng = SplitMix64(seed)
    lib_outs = [rng.next_u64() for _ in range(5)]
    if outs != lib_outs:
        raise SystemExit(f"SplitMix64 mismatch: {outs} != {lib_outs}")
    rng2 = SplitMix64(seed)
    uniforms = [rng2.uniform() for _ in range(5)]
    sm = {
        "description": "SplitMix64 known-answer test (conventions section 3). "
                       "first_5_u64 are the raw outputs for the given seed; "
                       "first_5_uniform are (out >> 11) * 2^-53 as exact doubles.",
        "seed": seed,
        "first_5_u64": outs,
        "first_5_uniform": uniforms,
        "golden_eq_seed": GOLDEN_EQ_SEED,
        "golden_fx_seed": GOLDEN_FX_SEED,
    }
    with open(GOLDEN_DIR / "splitmix64.json", "w") as f:
        json.dump(sm, f, indent=2)
        f.write("\n")

    print(f"golden vectors written to {GOLDEN_DIR}")
    print(f"  events_eq_mbo.jsonl   {len(eq)} events  sha {shas['events_eq_mbo.iap1'][:16]}...")
    print(f"  events_fx_quote.jsonl {len(fx)} events  sha {shas['events_fx_quote.iap1'][:16]}...")
    print(f"  book states validated at {BOOK_STATE_INDICES} (reference == brute force)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
