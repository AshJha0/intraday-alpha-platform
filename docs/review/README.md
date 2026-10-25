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
| v2.0 DP1 data pricing (stage 0; method, G4 checklist, licence summaries, cost columns from public price lists, DR-6 no purchase) | [DATA_PRICING_v2.md](DATA_PRICING_v2.md) |
| v2.0 DD1 free data route coverage (stage 2; counts and ranges only, G4 checklist per family, survivorship test design) | [DATA_COVERAGE_FREE_ROUTE.md](DATA_COVERAGE_FREE_ROUTE.md) |

## v2.0 stage 7-8, session B (U3, RL1, U6)

| item | file |
|---|---|
| U3 family specifications (D1-D5, H2-H4), one file each, complete before any fit; README index with the evidence-class column; D5 data-ineligible on daily data, H3 / H4 feasibility notes | [../../research/v2/families/README.md](../../research/v2/families/README.md) |
| RL1 research loop: development / validation partitions only (identity and path checks), features through the contracts, M0 registered, engine, E5 report with hash, analysis event with outcome exposure in the selection ledger; synthetic fixture with pinned report hashes | [../../python/src/iap/research/loop.py](../../python/src/iap/research/loop.py), [../../tools/research/run_family.py](../../tools/research/run_family.py), [../../python/tests/research/fixtures/manifest.json](../../python/tests/research/fixtures/manifest.json) |
| U6 ML protocol, revision 1, frozen (sha256 `b37cc588...`, re-hashed by `python/tests/ml/test_protocol_frozen.py`; delta placeholders synthetic, D-58) | [../../research/v2/ML_PROTOCOL.md](../../research/v2/ML_PROTOCOL.md) |
| U6 framework `iap.ml` (splits with purge and embargo, trial ledger, M1 ridge and elastic net, admission test, Holm within tier, lexicographic choice, M1 demonstration on the fixture: no candidate admitted, M0 carried) | [../../python/src/iap/ml/](../../python/src/iap/ml/) |

## v2.0 stage 1, session A (R0-R5)

| item | file |
|---|---|
| R0 confirmatory test specification, frozen (reviewer freeze decision 2026-10-24, sha256 `900d9e73...` of the text behind the path header; `python/tests/spec/test_r0_frozen.py` re-hashes it) | [../../python/tests/spec/CONFIRMATORY_TEST_SPEC.md](../../python/tests/spec/CONFIRMATORY_TEST_SPEC.md) |
| R1 canonical confirmatory gate (defects D-01, D-02): `iap.validation.confirmatory.decide` is the only PROMOTE constructor; walk-forward gates capped at ITERATE; R1-A..E against SciPy and textbook references (statsmodels when importable, D-40) | [../../python/tests/test_confirmatory.py](../../python/tests/test_confirmatory.py) |
| R2 one MDE implementation (defect D-03): exact noncentral-t inversion; convention table; REL-09 / REL-10 columns re-emitted with a dated correction (`research/v119/reemit_mde.py`), R7_POWER regenerated at N = 297 | [../RESEARCH_VALIDITY.md](../RESEARCH_VALIDITY.md) section 4i |
| R3 adversarial Monte Carlo evidence (R0 section 8): 66 cells, 29 claimed size cells pass (max k 433 of 20,000), both power cells pass (4,487 and 4,486 of 5,000), D-BLK WITHDRAWN for v1.19 (6 of 12 candidate cells fail; rho 0.2 and 0.3 inflate past 463), stress cells reported, R3-G fixtures (a)-(j) refuse | [../../research/validation/R0_RESULTS.md](../../research/validation/R0_RESULTS.md) + `.json` |
| R3 ledger family dependency map (defect D-08): 34 families, the 13 external entries / 1,224 real looks enumerated (not merged), dependent families by shared real dataset version, read by `prereg_gate.selection_history` | [../../research/validation/LEDGER_FAMILY_MAP.json](../../research/validation/LEDGER_FAMILY_MAP.json) |
| R4 planted dry run before signature (defect D-09): `iap.agents.planted_dry_run`; `tools/prereg_bind_identity.py` and `tools/rel08_rule_sign.py` refuse without a bound dry run; the REL-08 C1, REL-08c n = 0 and A0 fixtures refuse | [../../python/tests/test_planted_dry_run.py](../../python/tests/test_planted_dry_run.py) |
| R5 real-data dependence audit: predeclared plan (parts A and B, the plan 5.12 decision rule) and runner; NOT RUN, no sealed or spent session opened | [../../research/v119/R5_DEPENDENCE_PLAN.md](../../research/v119/R5_DEPENDENCE_PLAN.md) |

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
