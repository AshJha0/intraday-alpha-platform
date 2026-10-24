# F3 power diagnostic: specification (UNSIGNED DRAFT, NON-GATING; nothing here has been run)

Dated 2026-10-24. Preparer: Claude. Status: draft for outside-reviewer review. Gate F3 stays OPEN, null v3 stays FAILED.
Background: reviewer change 4 of `docs/release/REVIEWER_F3_SPEC_DECISION_2026-10-24.md`, `F3_SPEC_DRAFT.md` section 11 item 4, and the
reviewer's post-execution decision (PR 347: P1 accepted for the synthetic study only; no power claim; no further CR2/CR3 comparison; no
acceptance rerun).

## 0. Standing statements (binding on this specification)

1. **Non-gating.** This study has no pass/fail criterion and no power floor. Its results **cannot change the completed F3 acceptance
   result (`research/v118/f3_accept/REPORT.md`, 27/27 mandatory PASS) or any Type I criterion**, cannot reopen or rerun it, cannot motivate a
   new alpha_op, a new procedure or a CR2/CR3 comparison, and cannot close F3. If deployment needs practical power, that becomes a
   separate gate with its own preregistration.
2. **Synthetic only.** No market data are read; `data_versions = ["synthetic-no-data"]` is a sentinel only, as in the acceptance study.
   Effect sizes are in units of a synthetic scale and say nothing about effects achievable on real sessions.
3. **Descriptive.** The output is a table of estimated powers with intervals and a derived minimum detectable effect (MDE). No sentence
   in any report may say a procedure "has adequate power"; only "estimated power was x (Wilson interval) under generator g".

## 1. Question

For procedure P1 (one-sided Student t(G-1) on the G session values of a hypothesis, then Holm over the K hypotheses of a family, applied at
alpha_op = 0.020), how large a planted per-session mean shift is rejected with probability 0.8, for G in {5, 7, 10}, K in {3, 6, 16},
and how much of that is lost when sessions are heterogeneous (b0, b1) rather than homogeneous (a0)?

## 2. Design

* **Generators (reused, bound by hash):** a0, b0, b1 from `research/v118/f3_accept/core.py` (same definitions as the acceptance study:
  b0 session scales log-spaced 1..5, b1 the same scales with the normal / t3 / centred-lognormal cycle). a1 and c are not used.
* **Planted effect.** One non-null hypothesis (hypothesis index 0, present in every family K3, K6, K16) among 15 nulls. A constant shift
  `mu = d * sigma_bar / sqrt(G)` is added to the G session values of hypothesis 0 only, where `sigma_bar = sqrt(mean_g s_g^2)` is the
  root-mean-square session scale of the cell (a0: 1; b0 and b1: from the 1..5 scales) and d is the standardised effect. For a0 the
  t-statistic has noncentrality exactly d, which gives an analytic reference.
* **Grid, revised from the section 11 draft.** The draft grid d in {0.5, 1, 1.5, 2, 3} cannot reach 80 % power anywhere: exact noncentral-t
  arithmetic for the homogeneous case (no simulation) gives the largest power at d = 3 as 0.51 (G = 10, K = 3) and an 80 % MDE of
  d = 4.0-5.4 for K = 3, 4.5-6.5 for K = 6, and 5.2-8.4 for K = 16 (G = 10 down to 5). Heterogeneity can only push the MDE out. The
  grid is therefore **d in {0.5, 1, 2, 3, 4, 5, 6, 7, 8, 10, 12, 16}** (12 values). An MDE beyond d = 16 is reported as "not reached
  within the grid"; the grid is never extended after a look.
* **Cells.** 3 generators x 3 G = 9 stream cells; every stream cell evaluates all 12 values of d on the **same** null draws (common random
  numbers; the shift is added after generation), so power curves are monotone-comparable and the MDE interpolation is not noisy across d.
  Evaluation count: 9 x 12 x 3 families = 324 power estimates.
* **Power definition.** The Holm step-down at alpha_op over the first K hypotheses rejects hypothesis 0 (the planted one). Rejections of
  null hypotheses are not power; the null false-rejection rate of the same runs is reported as a side table (descriptive, not a Type I criterion).
* **Reported quantities.** Per (generator, G, K, d): count, power = k/n, Wilson 95 % interval (two-sided, per estimate, not simultaneous).
  Per (generator, G, K): MDE80 by linear interpolation of power in d between grid points, with an interval from interpolating the Wilson
  lower and upper limits (so the MDE interval is the d-range over which the Wilson interval crosses 0.8). Per (b0 or b1, G, K, d): the
  **heterogeneous-to-homogeneous power ratio** power_het / power_a0 at the same G, K, d (independent streams), with a delta-method 95 %
  interval on the log ratio, plus the MDE80 ratio. Cells where either power is below 0.01 are reported with the count only (no ratio).
* **Analytic cross-check (non-gating, in the dry run):** for a0 the Monte Carlo power at each d must lie within the Wilson interval of
  the noncentral-t value P(T(G-1, d) >= t_{1-alpha_op/K}) up to a negligible step-down term; a miss is a programming-error signal reported
  in the run log, it never changes the reported estimates.

## 3. Replication count and Monte Carlo precision

n = **10,000 datasets per stream cell** (two chunks of 5,000, one SplitMix64 stream per cell, never reseeded; chunk size is part of the
frozen identity). Precision: the worst-case Wilson 95 % half-width (power 0.5) is **0.0098**, i.e. about one percentage point; at power
0.8 it is 0.0078, at power 0.1 it is 0.0059. For the ratio of two powers near 0.5 each the standard error of the log ratio is
sqrt(2 x 0.5/(10,000 x 0.5)) = 0.014, a 95 % half-width of about 2.8 % relative. The MDE interval width is driven by the slope of the power curve, which is not known in advance for b0 and b1; it is reported per cell, not promised. This precision is judged sufficient for a descriptive study whose interpretation does not depend on differences below one percentage point. Compute is not the limit: the pilot cost of P1 was about 0.03 CPU-s per 1,000 datasets, so 9 x 10,000 datasets x 12 shifts is of the order of tens of CPU-seconds plus generation (an estimate for the dry run to confirm, not a result). Fixed n; no interim looks.

## 4. Seeds

Stream seed `20301100 + 10 * regime_index + G` with regime_index a0 = 0, b0 = 1, b1 = 2 (nine seeds, 20301105 ... 20301130); dry-run seeds
`20301900 + 10 * regime_index + G`. All disjoint from the pilot (20281100-20281199), the acceptance study (20291100-20291199 and the
dry-run 20291900-20291999), the null-v3 diagnostic seeds (20271001-20271004) and the F3 diagnostic seeds. The freeze check verifies
disjointness and 18 distinct seeds mechanically.

## 5. Freeze list (before any run)

Final generator code (sha256), the power runner and its core module, the analytic cross-check script, the declaration (every parameter,
including grid, n, chunk, alpha_op, seeds, interpolation rule, interval formulas), this specification, the reviewer's decisions it relies on,
the CI-lock environment (`--require-ci-env`), analysis identity v2 with the complete identity bound at resume, a freeze-consistency check
(as `check_freeze.py` for the acceptance study), a 200-dataset dry run with output discarded, a sentinel-only `data_versions` and an explicit
"no market data consumed" record. No reader path, holdout-registry call or `data/real` access in the runner (enforced by the same
source-grep, import allow-list and trapped-file-open tests as the acceptance runner).

## 6. Sign-off route

1. Reviewer reviews **this specification** (unsigned) and either approves it for signature preparation or names changes.
2. Claude prepares the freeze package under `research/v118/f3_power/` (not created by this document) and the signing instructions.
3. The owner signs locally with their key; Claude never signs. The runner runs as `IAP_RUN_ACTOR=claude-queue-runner`, and the run refuses
   a signer as runner.
4. The reviewer reviews the **signed** protocol before any run. Only then is the single run executed.
5. Results are filed with the reviewer's post-execution reading. Nothing is run before step 4.

## 7. Stopping rules and repeated looks

* n is fixed; no interim look at any estimate; no optional stopping; no adding replications, seeds, d values, generators, G or K after any
  look, whatever the result.
* One run only. A run that aborts **before** a DECISION record exists may be resumed at cell granularity, and only if every finished cell
  re-verifies against the complete signed analysis identity and the single deterministic stream (same rule as acceptance protocol section 8).
  A completed run is final: a disappointing MDE, an unexpected ratio or an analytic-cross-check miss does not license a rerun. Any change
  (grid, n, seeds, generators, definition of power or MDE) needs a new specification, new seeds and independent approval.
* If the cross-check flags a programming error, the run is declared **void before inspection of any power estimate** (the check is
  evaluated first and in isolation), the defect is documented, and a corrected package needs the full sign-off route again with fresh seeds.
* Multiplicity: 324 descriptive estimates, each with its own interval; no family-wise statement is made and none may be inferred.

## 8. What the results may and may not be used for

May: describe, for these synthetic generators, the size of a planted shift (in sigma_bar/sqrt(G) units) that P1 at alpha_op 0.020 detects
with 80 % probability, and how much of that is lost to session heterogeneity of this specific form. May inform whether a later, separately
preregistered power gate is worth specifying.

May not: change, qualify or reinterpret the acceptance result or any Type I criterion; set alpha_op or choose a procedure; support any claim
about real-data power, real effect sizes, alpha, serial dependence (regime c is outside the domain, and power under c is meaningless when size
is not controlled), other G, other K, other generators, or about a trading strategy.

## 9. Questions for the reviewer

1. Is the extended grid (to d = 16) acceptable given that the section 11 draft grid cannot reach 80 % power anywhere (analytic arithmetic above)?
2. Is the definition of the planted shift (constant across sessions, in units of root-mean-square session scale) acceptable, or should it
   be in units of the median scale or of each session's own scale (which would make a heterogeneous session carry more of the signal)?
3. Is 10,000 datasets per cell enough, or is one-percentage-point worst-case precision too coarse?
4. Should common random numbers across d (same null draws, shift added) stay, accepting that estimates across d are positively correlated?
