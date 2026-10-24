# F3 acceptance study protocol: UNSIGNED DRAFT FOR REVIEWER REVIEW; nothing here is run for acceptance

Dated 2026-10-24. Preparer: Claude (non-signing actor `claude-f3-spec`). Signer: the owner, locally. Gate F3 is OPEN, null v3 is
FAILED and untouched, no v4 exists. Basis: `research/v118/F3_SPEC_DRAFT.md` (section 11, revision 2), pilot
`research/v118/f3_spec/F3_PILOT_RESULTS.md` and the outside reviewer decision
`docs/release/REVIEWER_F3_SPEC_DECISION_2026-10-24.md` (approved with named changes; P1 is the candidate to be evaluated, not a
validated replacement). Parameters live in `declaration.json` (bound by the prereg); this text and that file must agree.

## 1. What is tested

Procedure P1: one-sided Student t(G-1) on the CR1 statistic of the G session values of a hypothesis (CR1 = CR2 = CR3 here), then Holm
over the K hypotheses of a family, applied at the operating level `alpha_op = 0.020`. The pilot verdict **NO ELIGIBLE PROCEDURE**
is preserved; P1 is selected prospectively by the reviewer on methodological grounds (exact t distribution for independent normal
sessions), not by reinterpreting the pilot.

## 2. Estimand, quantities

* `alpha_op = 0.020`: operational family-wise one-sided threshold. `FWER_target = 0.025`: the maximum error rate to be demonstrated.
* Replication unit: one independently generated dataset (G sessions x 16 hypotheses, every true mean 0) running the complete
  pipeline. Families K3, K6, K16 are the first 3, 6, 16 hypotheses of the same dataset. Under the complete null Holm rejects something
  iff min p <= alpha_op / K (checked against `known_null.holm` in the dry run).
* `FWER_observed = k / n`, with k the number of datasets with at least one Holm rejection, n = 50,000 per cell.
* `UCB95 = Beta^-1(0.95; k + 1, n - k)` (one-sided 95 % Clopper-Pearson). An evaluation **passes iff UCB95 <= FWER_target**, i.e.
  k <= 1,192 at n = 50,000.

## 3. Regimes and cells

Mandatory: a0 (homogeneous independent sessions, hypotheses independent), b0 (session scales 1..5 log-spaced, normal), b1
(same scales, session distribution cycling normal / t3 / centred lognormal), each at G in {5, 7, 10}: **9 cells, 27 evaluations**
(x K in {3, 6, 16}), 450,000 datasets. Reported sensitivity, not a criterion: a1 (equicorrelated hypotheses rho = 0.6) at the same G
(150,000 datasets). Regime c (serial dependence) is **outside the validated operating domain** and is not run. Generator
definitions: `declaration.json` `generators` and `core.py` (the pilot generators `research/v118/f3_spec/generators.py` are bound as
an input and the dry run checks the new code reproduces them bit-for-bit at the pilot seeds).

## 4. Decision rule and joint false-fail bound

Overall PASS iff all 27 mandatory evaluations pass. The false-fail probability at a true FWER of 2.0 % (design target) is bounded
by the union bound over the 27 evaluations (no independence assumed): 27 x P(Binomial(50,000, f) > 1,192); f = 0.020 gives **3.1e-8**;
the budget 5 % holds for any uniform true FWER up to 0.02193. The table is `JOINT_BOUND.md`, produced by `joint_bound.py` (bound by hash).
A fail is a fail: no rerun with new seeds under this prereg; no tuning of alpha_op, procedure, generators or seeds after any look.

## 5. Seeds and fixed design

Acceptance seeds `20291100 + 10 * regime_index + G`; dry-run seeds `20291900 + ...`; both disjoint from the pilot (20281100-20281199)
and the diagnostic (20271001-20271004). Datasets are drawn in chunks of 10,000 from one SplitMix64 stream per cell. n is fixed, no
interim looks, one run only, at most 3 worker processes, per-cell checkpoints (a crash keeps finished cells; they are skipped on resume
only if produced under the same prereg sha256).

## 6. Environment and roles

Registered and run from the CI-lock virtual environment (`bind` refuses otherwise, `--require-ci-env` semantics); the prereg binds the
code tree, the declared scripts and inputs, the dependency lock, installed packages and interpreter. Claude prepares and runs
(`IAP_RUN_ACTOR=claude-queue-runner`, no key material in the environment); the owner signs locally; the reviewer approves the signed
protocol before the run; `run` refuses a signer as runner.

## 7. What a pass and a fail mean

Pass: calibration (FWER <= 2.5 % with 95 % confidence) of P1 at alpha_op 0.020 under the synthetic regimes a0, b0, b1 for independent
sessions at G = 5, 7, 10. It supports **no** real-data error-rate or independence claim, nothing about serial dependence, other G, other
generators or power, and it does not by itself close F3's release gate. A fail: P1 is not certified for the failing cell(s); the
domain may be narrowed or a new, separately specified study defined; nothing is tuned on this study.

## 8. Resume, failure finality, stream

* **One deterministic stream per cell.** Each regime-G cell creates one SplitMix64 stream from its seed and consumes it chunk after
  chunk; chunking never resets or reseeds the generator. The chunk size (10,000), the ordering of draws within a chunk and the
  `core.py` generator implementation are part of the frozen identity (a different chunk size gives different a1 and b1 data).
* **Resume is permitted only as follows.** An interrupted run may be resumed at cell granularity. Every finished cell file is
  re-verified by recomputing the cell under the CURRENT verified signed identity: the sha256 of the complete `analysis_identity` block, the
  prereg sha256, seed, chunk, n, the final RNG state and the digest of the per-chunk counts must all equal the recomputed values; any
  difference refuses the run. Unfinished cells are computed from the start of their stream. Nothing else may be resumed or reused.
* **A failed study is final.** A completed run whose DECISION.json says FAIL (or any run whose result is unwelcome) is final under this
  prereg: no rerun, no new seeds, no changed thresholds, generators or sample counts. A second attempt requires a new preregistration and
  independent approval. A run that already holds a DECISION.json is refused.

## 9. No market data

The study consumes **no market data**. The runner, `core.py` and `joint_bound.py` import only numpy, scipy, the standard library and the
pinned `iap.core.rng`; they read no file other than the declaration and the signed prereg, call no holdout-registry or dataset-reader code and
never touch `data/real` (a test greps the sources and runs a cell with all file opens trapped). `data_versions = ["synthetic-no-data"]` in the
signed identity is a declared **sentinel only**: it is not a dataset identifier and not a holdout exemption, and the declaration, the identity
family string and this text all say that no market data are consumed.

## 10. Reporting and closure

`REPORT.md` / `DECISION.json` list the 27 mandatory results (a0, b0, b1 x G x K3/K6/K16) and, separately, the nine sensitivity results
(a1 rho 0.6 x G x K3/K6/K16, 50,000 datasets per G) with count, FWER observed and UCB95 for each. Sensitivity results are reported in full, are
not part of the mandatory verdict and are never used to redefine the operating domain after inspection. A statistical pass **does not
automatically close F3 and does not authorise any real-data claim**; the outside reviewer decides.

## 11. Frozen parameters (machine-readable; `check_freeze.py` compares this block with `declaration.json`)

```json freeze-params
{
  "alpha_op": 0.02,
  "fwer_target": 0.025,
  "ucb_conf": 0.95,
  "n_per_cell": 50000,
  "sensitivity_n_per_cell": 50000,
  "chunk": 10000,
  "families": [3, 6, 16],
  "mandatory_regimes": ["a0", "b0", "b1"],
  "sensitivity_regimes": ["a1"],
  "G": [5, 7, 10],
  "seed_base": 20291100,
  "dry_run_seed_base": 20291900,
  "rho_a1": 0.6,
  "scale_ratio": 5.0,
  "k_max": 1192,
  "mandatory_evaluations": 27,
  "sensitivity_evaluations": 9
}
```

## 12. Out of scope here

The power diagnostic (reviewer change 4) is a separate, non-gating protocol with its own seeds and sign-off; its results cannot alter
this study's criteria. The A0 residual-mean false-flag rate stays documented and is not a gate.
