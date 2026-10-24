# F3 power diagnostic protocol: UNSIGNED DRAFT FOR REVIEWER REVIEW; NON-GATING; nothing here is run

Dated 2026-10-24. Preparer: Claude (non-signing actor). Signer: the owner, locally. Gate F3 is OPEN, null v3 is FAILED, the synthetic acceptance
result (27/27 mandatory PASS) is unchanged. Basis: `research/v118/F3_POWER_DIAGNOSTIC_SPEC.md` (revision 2) and the outside reviewer decision
`docs/release/REVIEWER_F3_POWER_DOMAIN_DECISION_2026-10-24.md` (approved with named changes for freeze/signature preparation; it authorises no execution).
Parameters live in `declaration.json` (bound by the prereg); this text and that file must agree (`check_freeze.py`).

## 1. What is estimated

Power of P1 (one-sided Student t(G-1) on the G session values, Holm over the first K hypotheses at alpha_op 0.020) to reject one planted hypothesis
(index 0) among 15 nulls, for G in {5, 7, 10}, K in {3, 6, 16}, generators a0, b0, b1 (the acceptance generators, imported unchanged), 12 values of the
standardised effect d. **Three distinct quantities:** d (planted noncentrality-scale effect), empirical power k/n, and MDE80 (smallest d reaching 80 % by
the frozen rule below, never extrapolated: "MDE80 > 16" or "MDE80 <= 0.5" are results).

## 2. Effect, grid, replications

* `mu = d * sigma_RMS / sqrt(G)`, `sigma_RMS = sqrt(mean_g sigma_g^2)`, constant across sessions, added after generation to hypothesis 0 only.
  d is the noncentrality-scale effect (a0: the t statistic has noncentrality exactly d), not a raw per-session standardised shift.
* Grid d in {0.5, 1, 2, 3, 4, 5, 6, 7, 8, 10, 12, 16}; n = 10,000 independent datasets per (regime, G) cell; 9 cells; 324 power estimates; Wilson 95 % interval for every one.

## 3. Common random numbers and streams

One SplitMix64 stream per cell, chunks of 5,000, never reseeded. Dataset i is the same null dataset for every d (frozen mapping); replications are independent; the sharing
is disclosed in every report; differences between d use the paired bootstrap. Chunk size, draw order and the generator implementation are part of the frozen identity.

## 4. Frozen MDE80 rule, uncertainty, ratios

First-crossing linear interpolation (`power_math.mde_point`); non-monotone curves flagged with the first crossing kept; paired bootstrap B = 1,000 over replications
(same index set for every d and K; percentile index rule in `declaration.json`), Wilson bracket as secondary; power ratio het/hom defined only when both counts >= 100
(delta method on the log ratio); MDE ratio defined only for numeric, monotone curves, otherwise a bound or "undefined".

## 5. Programming-integrity diagnostic (evaluated first)

For every a0 (G, K, d): `pi_ref - tau <= power <= min(1, pi_ref + excess) + tau`, tau = 0.025 (five times the worst-case Monte Carlo standard error). On any violation the
run is **VOID (programming integrity)**: DECISION.json records `VOID_INTEGRITY` and the offending combinations, no power table, MDE80 or ratio is produced, no estimate may
be cited, and a corrected package needs the full sign-off route with fresh seeds. It is a code check, not a hypothesis test.

## 6. Seeds, environment, roles

Stream seeds `20301100 + 10 * regime_index + G`, dry-run `20301900 + ...`, bootstrap `20301500 + ...`; disjoint from the pilot, the acceptance study and the diagnostic seeds.
Registered and run from the CI-lock virtual environment; Claude prepares and runs (`IAP_RUN_ACTOR=claude-queue-runner`, no key material in the environment); the owner signs
locally; the reviewer approves the signed protocol before the run; `run` refuses a signer as runner. At most 3 worker processes; per-cell checkpoints.

## 7. Resume, finality, stream

* **One deterministic stream per cell**; chunking never resets or reseeds the generator.
* **Resume permitted only at cell granularity:** every finished cell is re-verified by recomputing it under the CURRENT verified signed identity; the sha256 of the complete
  `analysis_identity` block, the prereg sha256, seed, chunk, n, counts, the final RNG state, the chunk-count digest and the indicator digest must all equal the recomputed values;
  any difference refuses the run.
* **A completed run is final** (including a VOID_INTEGRITY run): no rerun, no new seeds, no changed grid, n, generators or definitions. A run that already holds a DECISION.json is refused.

## 8. No market data

The study consumes **no market data**. The runner, `power_core.py` and `power_math.py` import only numpy, scipy, the standard library, the pinned `iap.core.rng` and the acceptance
study's `core.py`; they read no file other than the declaration and the signed prereg, call no holdout-registry or dataset-reader code and never touch `data/real`.
`data_versions = ["synthetic-no-data"]` in the signed identity is a declared **sentinel only**, not a dataset identifier and not a holdout exemption.

## 9. Non-gating, and what results may be used for

No pass/fail and no power floor. Results cannot change the completed F3 acceptance result or any Type I criterion, set alpha_op, choose a procedure, close F3, or support any
real-data, practical-power, alpha or trading claim. They describe estimated power under the declared synthetic generators and nothing else. A median-scale sensitivity
analysis is optional, non-gating and NOT part of this package.

## 10. Frozen parameters (machine-readable; `check_freeze.py` compares this block with `declaration.json`)

```json freeze-params
{
  "alpha_op": 0.02,
  "n_per_cell": 10000,
  "chunk": 5000,
  "families": [3, 6, 16],
  "regimes": ["a0", "b0", "b1"],
  "G": [5, 7, 10],
  "grid": [0.5, 1, 2, 3, 4, 5, 6, 7, 8, 10, 12, 16],
  "planted_index": 0,
  "power_target": 0.8,
  "seed_base": 20301100,
  "dry_run_seed_base": 20301900,
  "bootstrap_seed_base": 20301500,
  "scale_ratio": 5.0,
  "bootstrap_B": 1000,
  "ratio_min_count": 100,
  "integrity_tau": 0.025,
  "integrity_tau_dry": 0.25,
  "stream_cells": 9,
  "power_estimates": 324
}
```
