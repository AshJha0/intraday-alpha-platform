# F3 diagnostic review: plan (written before any diagnostic is run)

**Status: PLAN, dated 2026-10-24, committed and opened as a PR before any diagnostic result exists.** Nothing in
this file is a result. The review that follows is exploratory and is labelled so. It signs nothing, drafts no
replacement protocol, and changes no threshold, parameter or signed artefact.

## 0. Why, and what is fixed

REL-06 known-null protocol v3 FAILED its pre-declared rate criterion (`known_null_v3_RESULT.md`): A0 conditional
rejections in the S2 cells 79 / 1,854 (K3) and 80 / 1,854 (K6) against a tolerance of 55 (3.0 %), alpha 0.025;
generator B 50 / 2,000 and 49 / 2,000 (pass); loss gate NOT_TRIGGERED. The outside reviewer (close-out of
2026-10-23) kept gate F3 open, did not say which component is defective, and asked for the mechanism to be
investigated under a separately specified plan before any replacement protocol, naming three subjects: (1) the
conditional randomisation estimand, (2) the consequences of removing session-level means, (3) the S2
standard-error calibration at small K. The owner confirmed this order (F3 diagnostic first, then REL-08 rule v2).

Fixed, not reopened: the v3 verdict (FAIL), its thresholds, level 0.025, generators, the signed prereg, the
v1.18.0 tag. No diagnostic result changes the v3 record. Existing code `python/src/iap/research/known_null*.py`
is imported unmodified (it is the code under test); new code lives only under `tools/` and
`research/v118/f3_diagnostic/`.

## 1. Facts to keep in view (from the recorded result and the protocol, not yet analysed)

* A0 enumerates all 1,854 derangements of 7 sessions. Every outcome is a function of one 7 x 7 matrix per
  hypothesis, `M[i, j]` = per-session mean gross P&L of decision session i traded against the labels of session j.
  The S2 statistic of a derangement pi is `mean_i M[i, pi(i)]`. One dataset, 49 numbers per hypothesis.
* The failing cells are K3 and K6; K16 (23 / 1,854) and all S1 cells are within tolerance. The K3 family is the
  three pooled (AAPL+MSFT+QQQ) imbalance hypotheses, whose S2 SE is small (many trips per session); K16 contains
  single-symbol hypotheses. So K (family size) and hypothesis identity are confounded in the record. The plan
  separates them (Q3).
* Recorded per-trial, per-hypothesis values exist for A0, B and A_v2: S1/S2 mean, CR1 SE, raw p (Webb full
  enumeration, G = 7), Holm p. Per-session means and the matrices `M` are not stored.

## 2. Questions, candidate causes, diagnostics, evidence

Candidate causes (not exclusive, none presumed): **C1** the A0 rate is a property of one finite, dependent
matrix, not a test defect; **C2** the derangement null is not centred where the test assumes (nonzero conditional
mean of m, e.g. an intraday label profile that session-level centring leaves in); **C3** the small-G CR1 +
Webb reference is miscalibrated for session means that are heteroscedastic or heavy-tailed; **C4** the effect of
removing session means on the SE (U < 1 at the median yet over-rejection implies shape, not scale); **C5**
dependence between hypotheses inside Holm (union versus sum).

### Q1. The conditional randomisation estimand (reviewer subject 1)

* **D1.1 (recorded result only).** Recompute the cell counts from the stored p_holm / mean; per-hypothesis marginal
  rejection counts and their overlap (union vs sum) in A0, B, A_v2. Supports C5 if A0 union is much smaller than
  sum; B is the reference.
* **D1.2 (real data, in sample, matrices).** Rebuild `M` per hypothesis (section 3), verify it reproduces the
  recorded 1,854 A0 records exactly, and report the exact conditional mean and sd of m over derangements per
  hypothesis (`E_pi m`, in units of mean SE). Supports C2 if `E_pi m` is a nonzero fraction of the SE for the
  hypotheses that carry the excess.
* **D1.3 (synthetic, parametric null at the matrix level).** Draw 7 x 7 matrices from a null model, enumerate the
  1,854 derangements, apply the unmodified `cluster_inference`, and record the A0-style rate per matrix. Models:
  (a) iid N(0,1); (b) row and column effects plus row-specific scale; (c) model (b) fitted to the real `M` of the
  excess-carrying hypotheses (sd of row effects, column effects, row scales, residual correlation). This is the
  sampling distribution of the A0 rate under a null where H0 holds by construction. Supports C1 if the null
  distribution of the rate has a mean at or below about 2.5 % and a spread such that 4.3 % lies inside its central
  90 %. Contradicts C1, and implicates the construction or the test, if the null mean itself is above 3 %.
* **D1.4 (matrix, leave-one-session-out).** Rate for each 6-session submatrix (265 derangements). Labelled as a
  different G. Supports C1/C2 if one session carries the excess; shows how few independent units sit behind the 4.3 %.

### Q2. Consequences of removing session-level means (reviewer subject 2)

* **D2.1 (matrix).** Two-way decomposition of `M` (row effect, column effect, interaction) per hypothesis; share of
  the variance of the per-session values `x_i = M[i, pi(i)]` that each carries. Row and column main effects add the
  same constant to every derangement mean but add to the cross-session variance the SE estimates. Supports C4 if
  main effects are large (SE overstated, U < 1) while rejection still exceeds nominal.
* **D2.2 (matrix, three re-runs of the same rule).** Same CR1 + Webb + Holm 0.025 on: (i) the recorded A0 matrix;
  (ii) the matrix built from uncentred labels (the v2 generator A, enumerated); (iii) the A0 matrix with the common
  intraday label profile also removed (labels centred per name, horizon and time-of-day cell across sessions);
  (iv) `M` double-centred (row, column and grand means removed; interaction kept). Each is a labelled diagnostic
  and is not a candidate method. Supports C2 if (iii) or (iv) moves the rate to or below 3.0 % and (i) to (ii)
  moves it; supports C4/C3 if no re-centring moves it.
* **D2.3 (recorded result).** A_v2 (500 random derangements, uncentred) counts and understatement ratios against A0.

### Q3. S2 standard-error calibration at small K (reviewer subject 3)

* **D3.1 (recorded result).** Per hypothesis and generator: sd of m, mean CR1 SE, ratio U, empirical 95 % / 97.5 %
  / 99.2 % quantiles of t = m / SE against t(6), and raw-p rejection fractions at 0.025, 0.025/3, 0.025/6 (valid:
  at most nominal). Separates the SE (scale) from the reference distribution (shape).
* **D3.2 (synthetic, no market data).** Size of the unmodified CR1 + Webb one-sided test at G = 7 for session means
  that are independent with: equal normal; scale ratios up to 5:1; t(3); skewed. 20,000 replications per scenario,
  thresholds 0.025, 0.025/3, 0.025/6. Supports C3 if heteroscedastic or heavy-tailed rows exceed nominal where the
  homogeneous case does not; B passing is then explained by its homogeneous sessions.
* **D3.3 (hypothesis identity versus K).** Rate of each K3 and K16 hypothesis individually, and the K3 hypotheses
  evaluated inside a K16-sized family built from the recorded raw p. Supports "K is not the driver" if the excess
  follows the hypothesis (pooled, small-SE) rather than the family size.

## 3. Data, generators, determinism

* **Synthetic only for D1.3, D3.2:** SplitMix64 seeds `20271001` (D1.3 a), `20271002` (b), `20271003` (c),
  `20271004` (D3.2). Recorded in the output JSON next to the code commit.
* **Recorded result for D1.1, D2.3, D3.1, D3.3:** `known_null_v3_result.json`, sha256
  `c489f28c63975e1b62c75fb6e092db1e774254c3f1783db868a659bc99e25210` (44 MB, not in git, read from the run
  worktree); the hash is checked before use and reported.
* **Real data for matrices (D1.2, D1.4, D2.1, D2.2):** only `data/real/multi7`, the same seven sessions as the
  signed run, registry state `in_sample`. The read goes through the runner's own builder (`rel06_known_null.build_study`
  and `known_null_v3.build_a0_source`), which calls `holdout_registry.guard_or_exit`; the plan adds a second explicit
  `guard_or_exit` call before the build. No `spent` or unread session is read. If the guard refuses, the real-data
  stage is not run and the review says so.
* **Matrix construction:** `labels_a` with the 7 cyclic shifts `pi_k(s) = (s + k) mod 7`, `k = 0..6`, gives
  `M[i, (i + k) mod 7]` for all 49 cells in 7 trial evaluations per label variant (about 2.4 s each in the recorded
  run). The shift k = 0 (the real pairing) is evaluated only to fill the diagonal `M[i, i]` for the decomposition
  and is never used as a null draw. **Validation gate:** the matrices must reproduce the recorded A0 means, SEs and
  rejection counts (79, 80, 23 and all per-hypothesis counts) exactly (float tolerance 1e-9 relative). On any
  mismatch the real-data stage stops and the mismatch is the finding.
* Any statistic computed from `M` is evaluated by importing `iap.research.known_null.cluster_inference` and `holm`
  unmodified, never a re-implementation.

## 4. What counts as support, and as "not shown"

A cause is **supported** only if the diagnostic pre-listed for it above gives the stated direction and the same
direction holds in at least one independent route (recorded result versus matrix versus synthetic). One route is
reported as **suggestive**. Contradictory routes are reported as contradictory. A cause that no diagnostic can
separate from another is reported as unidentified. Intervals: binomial / Wilson for independent synthetic
replications; for the 1,854 derangements only exact counts (the protocol's rule, outcomes are dependent). Nothing
is called a p-value or a size of the real data.

## 5. Stop rules and budget

* Stop real-data stage on: guard refusal; validation-gate mismatch; free RAM below 4 GB (pause, resume when above);
  a single stage past 2 h wall-clock.
* No diagnostic is added or altered after a result is seen. A change is recorded in a dated "Deviations" section of
  the findings with the reason; a diagnostic that was dropped is listed as dropped.
* No result in this review, however it comes out, changes any v3 threshold, level, generator or the verdict.
* Compute: recorded-result stages and D3.2 minutes; matrices 28 trial evaluations plus one study build (estimated
  under 30 min, one process); D1.3 about 200 matrices x 3 models x 1,854 derangements x about 2 ms for the Webb
  enumeration, about 40 min, run detached; leave-one-out and re-runs minutes. Total about 1.5 h, one worker
  (never more than 2), free RAM checked before each stage. Anything over 10 minutes is launched detached with the
  queue launcher, resumable per stage, with a START line confirmed.

## 6. Explicitly out of scope

A v4 or any replacement protocol, draft or signed; any change to alpha, tolerance, K, G, generators, estimator
(CR2/CR3, wild maxT, permutation family tests are not evaluated as candidates); re-running the v3 study with any
change; reading any `spent` or unread session or any new data; any claim on real-data FWER, alpha, profitability or
the instrument; REL-08 rule v2 (it follows this review). The findings document states, as a conclusion, whether a
replacement protocol is warranted and which quantities such a specification must define first (target error rate,
dependence assumptions, replication unit, acceptance criterion, stopping rules). It does not define their values.

## 7. Outputs

`research/v118/F3_DIAGNOSTIC_REVIEW.md` (findings), `research/v118/f3_diagnostic/` (code, small result JSONs, seeds,
commit), a hand-over note in the shared notes folder. Open questions for the outside reviewer are listed in the
findings.
