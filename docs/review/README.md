# Outside-review evidence (v1.18)

| Finding | File |
|---|---|
| 5 simulator correctness | [SIMULATOR_CORRECTNESS.md](SIMULATOR_CORRECTNESS.md) (PR for finding 5) |
| 6 reproduction record | [REPRODUCTION_BASELINE_2026-10-18.md](REPRODUCTION_BASELINE_2026-10-18.md) + `.json` |

## v1.19 (stage 0)

| item | file |
|---|---|
| defect register (D-01 to D-38, Class A/B/C, blocks column) | [DEFECT_REGISTER_v1.19.md](DEFECT_REGISTER_v1.19.md) |
| baseline identities and document states | [BASELINE_v1_19.md](BASELINE_v1_19.md) |
| data-route priced proposal (stage 0-A, B2a) | [DATA_ROUTE_PROPOSAL_v1.19.md](DATA_ROUTE_PROPOSAL_v1.19.md) |
| v2.0 DP1 data pricing (stage 0; method, G4 checklist, licence summaries, cost columns awaiting key) | [DATA_PRICING_v2.md](DATA_PRICING_v2.md) |

## v2.0 stage 1, session A (R0-R5)

| item | file |
|---|---|
| R0 confirmatory test specification, frozen (reviewer freeze decision 2026-10-24, sha256 `900d9e73...` of the text behind the path header; `python/tests/spec/test_r0_frozen.py` re-hashes it) | [../../python/tests/spec/CONFIRMATORY_TEST_SPEC.md](../../python/tests/spec/CONFIRMATORY_TEST_SPEC.md) |

## Reproduction result (2026-10-18, `tools/reproduce_baseline.py`)

- **At the bound commit `8fb189b` (REL-01): REPRODUCED.** Fresh clone from GitHub, venv from the
  CI lock with `--require-hashes` (36 packages, CPython 3.12.10, Windows 11), `--check` exit 0,
  regeneration exit 0, 0 discrepancies. The only difference is the provenance field `commit`
  (the committed file names `8a43652`, the commit it was generated on before the final docs commit
  `8fb189b`; the research inputs are identical). Not byte-identical for that reason alone.
- **At current main `83f0821`: NOT REPRODUCED, as expected.** 27 differences, all in inputs that
  changed after the freeze: the look ledger grew (5,732 -> 6,826 looks, 422 -> 640 entries, new
  dataset versions and the `execution_study` kind) and the CI lock / `ci.yml` hashes moved. The
  baseline is a frozen snapshot of `8fb189b`; reproduce it there. Record:
  [REPRODUCTION_BASELINE_MAIN_83f0821.md](REPRODUCTION_BASELINE_MAIN_83f0821.md).

Not covered: the signature verifier (reviewer session A), REL-04b and REL-08 reproduction (placeholders
in [../REVIEW_PACK.md](../REVIEW_PACK.md) section 3), Rust/C++ (no local toolchain; CI only),
Linux/macOS (the script is portable but was run on Windows only).
