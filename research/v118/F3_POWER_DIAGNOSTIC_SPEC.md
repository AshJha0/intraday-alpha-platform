# F3 power diagnostic: specification, revision 2 (UNSIGNED DRAFT, NON-GATING; nothing here has been run)

Dated 2026-10-24. Preparer: Claude. Status: revision 2 incorporating the outside reviewer's decision
`docs/release/REVIEWER_F3_POWER_DOMAIN_DECISION_2026-10-24.md` (approved with named changes for freeze/signature preparation). Revision 1
(PR 348) is superseded by this text. Gate F3 stays OPEN, null v3 stays FAILED, the synthetic acceptance result (27/27 PASS) is unchanged.
No power-study execution is authorised by the decision; this document and the freeze package built from it authorise nothing.

## 0. Standing statements (binding on this specification)

1. **Non-gating.** No pass/fail criterion and no power floor. Results **cannot change the completed F3 acceptance result
   (`research/v118/f3_accept/REPORT.md`) or any Type I criterion**, cannot reopen or rerun it, cannot motivate a new alpha_op, a new
   procedure or a CR2/CR3 comparison, and cannot close F3. If deployment needs practical power, that is a separate gate with its own preregistration.
2. **Synthetic only; no market data.** `data_versions = ["synthetic-no-data"]` is a sentinel only. Effect sizes are in synthetic units and say
   nothing about effects on real sessions.
3. **Descriptive.** Only statements of the form "estimated power was x (Wilson interval) under generator g" and "MDE80 was y (interval) under
   generator g" are allowed. Nothing may say P1 "has adequate power".

## 1. Question

For P1 (one-sided Student t(G-1) on the G session values of a hypothesis, then Holm over the K hypotheses of a family, at alpha_op = 0.020), how
large a planted constant mean shift is detected with probability 0.8, for G in {5, 7, 10}, K in {3, 6, 16}, and how much is lost under session
heterogeneity (b0, b1) relative to homogeneity (a0)?

## 2. Three distinct quantities (reviewer condition)

* **d**: the *planted standardised effect size*, on the noncentrality scale. It is a design input from the frozen grid.
* **Empirical power** p-hat(d) = k/n: the observed fraction of the n synthetic datasets in which Holm rejects the planted hypothesis.
* **MDE80**: the smallest d at which power reaches 80 %, *defined only by the frozen interpolation rule of section 6*. It is never extrapolated
  beyond the grid: if 80 % is not reached at d = 16 the result is reported as **"MDE80 > 16"**, and if it is already reached at the first grid
  point as **"MDE80 <= 0.5"**.

## 3. Effect definition (the formula, and how the two statements in the decision agree)

The planted shift added to every session value of the planted hypothesis (hypothesis index 0, in every family K3, K6, K16) is

    mu = d * sigma_RMS / sqrt(G),   sigma_RMS = sqrt( (1/G) * sum_{g=1..G} sigma_g^2 ),

with sigma_g the declared generator session scales (a0: all 1, so sigma_RMS = 1; b0 and b1: sigma_g = exp(linspace(0, ln 5, G)), whose session
values have unit variance before scaling, so sigma_g is the session standard deviation). The shift is **constant across sessions**; it is not
scaled per session. **d is the noncentrality-scale effect, not a raw per-session standardised shift**: for a0 the t statistic has noncentrality
exactly d (the mean of G values has standard error sigma_RMS/sqrt(G) = 1/sqrt(G)).

*Resolved wording issue.* The decision's earlier part 1 item 2 wrote Delta = d * sqrt((1/G) * sum sigma_g^2), without 1/sqrt(G). The reviewer's freeze decision
(`docs/release/REVIEWER_F3_POWER_FREEZE_DECISION_2026-10-24.md`, item 1) confirms that the earlier wording was inconsistent and that the authoritative definition is the
one above: **mu = d * sigma_RMS / sqrt(G)**, d the noncentrality-scale effect; the expression without 1/sqrt(G) is not used anywhere in the package (a test checks it).

## 4. Design

* **Generators (reused, bound by hash):** a0, b0, b1 from `research/v118/f3_accept/core.py` (not modified; the power runner imports its
  `draw_chunk`, `p1_pvalues` and `scales`). a1 and c are not used.
* **Grid:** d in {0.5, 1, 2, 3, 4, 5, 6, 7, 8, 10, 12, 16} (12 values; approved to 16). The original grid to 3 cannot reach 80 % anywhere: exact
  noncentral-t arithmetic for a0 gives at best 0.51 at d = 3 (G = 10, K = 3) and an analytic MDE80 of 4.0-5.4 (K 3), 4.5-6.5 (K 6), 5.2-8.4 (K 16).
  The grid is never extended after a look.
* **Cells.** 3 generators x 3 G = 9 stream cells; each cell evaluates all 12 values of d on the same null datasets (CRN, section 5);
  3 families each: **324 power estimates**.
* **Power.** Holm step-down at alpha_op over the first K hypotheses; p-hat counts datasets in which the planted hypothesis is rejected. NaN p
  counts as 1 (and is reported). The false-rejection rate of the 15 nulls in the same runs is a descriptive side table, not a Type I criterion.
* **Replications.** n = 10,000 independent datasets per stream cell (approved). Worst-case Wilson 95 % half-width (power 0.5) 0.0098; at 0.8 it is
  0.0078. This characterises broad power differences, **not fine distinctions near 80 %**; no MDE80 is reported with more than one decimal.
* **Reported:** per (generator, G, K, d): count, p-hat, Wilson 95 % interval (two-sided, pointwise, not simultaneous), for **every** estimate.

## 5. Common random numbers: frozen mapping, disclosure, paired inference

* **Mapping (frozen).** For a stream cell (regime, G): one SplitMix64 stream from the cell seed, consumed in chunks of 5,000 datasets (two chunks)
  and never reseeded; dataset i of the cell is the same null dataset for **every** d; the shifted dataset is `x_i` with `mu(d)` added to the
  session values of hypothesis 0 only, after generation, so the 15 null hypotheses are identical across d. Chunk size is part of the frozen identity.
* **Independence of replications.** Datasets i = 1..n are independent draws; only the d index shares randomness. Streams of different cells
  (regime, G) use different seeds and are independent.
* **Disclosure.** Reports state that power estimates at different d within a cell share the same datasets and are positively correlated; they are
  never treated as independent. No test or interval treats them so.
* **Paired inference (frozen).** Any reported difference of powers between two d in the same cell (the report lists adjacent-d differences) uses the
  paired bootstrap of section 6 (resample datasets jointly across d), not independent-proportion formulas.

## 6. FROZEN MDE80 rule, uncertainty, ratios (reviewer condition 4)

**6.1 Point MDE80.** Let d_1 < ... < d_12 be the grid and p_j = p-hat(d_j) for one (generator, G, K).
1. If p_1 >= 0.8: MDE80 = "<= 0.5" (boundary; no interpolation).
2. Else let j be the **first** index with p_j >= 0.8. If none: MDE80 = "> 16" (no extrapolation).
3. Else MDE80 = d_{j-1} + (0.8 - p_{j-1}) / (p_j - p_{j-1}) * (d_j - d_{j-1}) (linear in d; p_j > 0.8 > p_{j-1} so no division by zero), reported to one decimal.
4. **Non-monotone empirical curve.** The first-crossing rule above is used regardless. If any later p_k (k > j) is below 0.8, the cell is flagged
   "NONMONOTONE" in the report, with the offending d and p-hat; the flagged point value is kept but is not used in the heterogeneous-to-homogeneous
   MDE ratio. (With CRN and a truly monotone power curve this can only be noise in a flat region.)

**6.2 Uncertainty of MDE80 (both predeclared; the bootstrap is the primary).**
* *Paired bootstrap over independent dataset replications.* B = 1,000 resamples of the n datasets with replacement, the same resampled index set used
  for all 12 values of d (so pairing is respected) and all three K; for each resample recompute p_j and apply 6.1; seeded
  `20301500 + 10 * regime_index + G` (SplitMix64 indices). Order values as: "<= 0.5" < numbers < "> 16". The reported interval is the 2.5th and 97.5th
  percentiles of the ordered replicates; if either is a boundary label it is reported as that label. If fewer than 90 % of replicates are numeric the
  report says "interval dominated by boundary results". Intervals are shown to one decimal at most.
* *Pointwise Wilson bracket (secondary).* Apply 6.1 to the Wilson **upper** limits (giving a lower bracket end) and to the Wilson **lower** limits
  (an upper end); labelled a conservative bracket, not a confidence interval.

**6.3 Heterogeneous-to-homogeneous power ratio.** For b in {b0, b1}: R(G, K, d) = p-hat_b / p-hat_a0 (independent streams, same G, K, d).
* **Defined only if** both counts are >= 100 (power >= 0.01). Otherwise it is "not reported (count < 100)" and the counts are shown.
* Interval: delta method on log R, se = sqrt((1-p_b)/(n p_b) + (1-p_a)/(n p_a)), 95 % normal interval exponentiated.
* **MDE80 ratio** MDE_b / MDE_a0 is defined only when both are numeric, neither flagged NONMONOTONE; if MDE_b is "> 16" and MDE_a0 is numeric it is reported as
  "> 16 / MDE_a0" (a bound); in all other cases "undefined". Its interval is the percentile interval of the ratios of independent bootstrap replicates
  of the two cells, only over replicates where both are numeric; if fewer than 90 % are, no interval is reported.

## 7. Programming-integrity diagnostic (replaces the informal comparison; reviewer condition 3)

Not a hypothesis test and not a power result. For **a0** (analytic reference exists), each (G, K, d) of the 108 combinations:

    pi_ref   = P( T'(G-1, ncp = d) >= t_{1 - alpha_op/K}(G-1) )                                (noncentral t; exact arithmetic)
    excess   = 1 - (1 - alpha_op/K)^(K-1)                                                       (probability some null clears the first Holm step)
    band     = [ pi_ref - tau,  min(1, pi_ref + excess) + tau ],   tau = 0.025.

Rationale: Holm rejects the planted hypothesis whenever its p <= alpha_op/K, which gives pi_ref (a lower bound for power); it can also be rejected after
a null is rejected, which needs some null p <= alpha_op/K (a0 nulls are exact, independent of the planted one), which bounds the extra by `excess`.
tau = 0.025 is a **heuristic tolerance**: numerically five times the worst-case binomial standard error (0.005 at n = 10,000), but not a simultaneous confidence
guarantee across the 108 combinations and not evidence of calibration (reviewer clarification, 2026-10-24). `excess` is fixed per K in `declaration.json`
(`integrity.excess`: 0.0132889, 0.0165559, 0.0185868 for K = 3, 6, 16).

* **Failure handling (frozen).** The check is evaluated first, on the finished a0 cells, **before any power estimate, MDE80, ratio or table is
  computed or displayed**. If any combination is outside its band, the run is declared **VOID (programming integrity)**: DECISION.json records
  `status = VOID_INTEGRITY` and the offending (G, K, d), no power table or MDE80 is produced, no estimate from the run may be cited, the defect is
  documented, and a corrected package needs the full sign-off route again with **fresh seeds** (this is not a rerun for an unwelcome result).
  A pass is written as `integrity = PASS` and releases the report generation. The same band function is unit-tested in the freeze package, and a
  d = 0 consistency check (planted code at d = 0 reproduces `f3_accept.core.p1_pvalues` of the unshifted data bit for bit) runs in the dry run.
* The check also runs in the 200-dataset dry run with its tolerance widened to the n = 200 Monte Carlo error (tau_dry = 0.25), only to catch gross code
  errors; the dry-run output is discarded.

## 8. Seeds

Stream seed `20301100 + 10 * regime_index + G` (a0 = 0, b0 = 1, b1 = 2): 20301105, 20301107, 20301110, 20301115, 20301117, 20301120, 20301125,
20301127, 20301130. Dry-run seeds `20301900 + ...`; bootstrap seeds `20301500 + ...`. All disjoint from the pilot (20281100-20281199), the
acceptance study (20291100-20291199, 20291900-20291999) and the diagnostic seeds (20271001-20271004); the freeze check verifies this mechanically.

## 9. Freeze list (before any run)

Generator code (`f3_accept/core.py`, bound as input), the power runner and core module, the analytic band script, the declaration (every parameter: grid,
n, chunk, alpha_op, seeds, MDE rule constants, bootstrap B and seeds, tolerances, ratio thresholds), this specification, the reviewer decisions it relies on,
the CI-lock environment (`--require-ci-env`), analysis identity v2 with the complete identity validated on resume, a freeze-consistency check with tests,
a 200-dataset dry run with output discarded, sentinel-only `data_versions` and a "no market data consumed" record, one deterministic stream per cell,
and the source-grep / import allow-list / trapped-file-open no-data tests of the acceptance runner.

## 10. Sign-off route

1. Reviewer approved this specification with named changes (incorporated here). 2. Claude prepares the freeze package under `research/v118/f3_power/`
(unsigned). 3. The reviewer confirms the package for signature. 4. The owner signs locally; Claude never signs; the run is by `IAP_RUN_ACTOR=claude-queue-runner`;
`run` refuses a signer. 5. The reviewer reviews the **signed** protocol before any run. 6. One run. Nothing runs before step 5.

## 11. Stopping rules and repeated looks

n fixed; no interim look at any estimate; no optional stopping; no adding replications, seeds, d values, generators, G or K after any look. One run only.
A run that aborts before a DECISION record may be resumed at cell granularity only if every finished cell re-verifies against the complete signed analysis
identity and the single deterministic stream. A completed run (including a VOID_INTEGRITY run) is final under this prereg; any change needs a new
specification, fresh seeds and independent approval. 324 descriptive estimates, each with its own interval; no family-wise statement.

## 12. What the results may and may not be used for

May: describe the planted shift (in sigma_RMS/sqrt(G) units) that P1 at alpha_op 0.020 detects with 80 % probability under these synthetic generators and the loss
due to this form of heterogeneity; inform whether a later, separately preregistered power gate is worthwhile. May not: change, qualify or reinterpret
the acceptance result or any Type I criterion; set alpha_op or choose a procedure; support any claim about real-data power, effect sizes, alpha, serial
dependence (regime c is outside the domain), other G, K or generators, or a trading strategy.

## 13. Optional, non-gating, not part of the frozen run

A median-scale sensitivity analysis (shift in units of the median session scale) is allowed by the decision as optional. It is **not** in the freeze
package, not required for signature, and would need its own specification if wanted.

## 14. Questions remaining for the reviewer

1. Is the reading of Delta versus mu in section 3 correct? 2. Are the integrity band and tau = 0.025 acceptable? 3. Is the "VOID_INTEGRITY, fresh seeds" handling acceptable?
4. Are the ratio-definedness threshold (counts >= 100) and the bootstrap B = 1,000 acceptable?
