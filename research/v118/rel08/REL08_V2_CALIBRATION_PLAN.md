# REL-08 v2 prospective synthetic-null calibration plan (UNSIGNED, written before any calibration number exists)

> **REVISION NOTICE (2026-10-24):** superseded for execution by `REL08_V2_CALIBRATION_PLAN_REV2.md` (staged fixed-sample design with a non-decisional checkpoint, power seeds moved, manifest, short-circuit evidence). The text below is kept unchanged for the record; where it differs from REV2, REV2 governs.

**Status: plan only, 2026-10-24.** No calibration run, timing run or smoke run of the decision specified here has produced
a number that was looked at when this file was written. It answers named change 3 of the outside reviewer's decision
(`docs/release/REVIEWER_REL08_V2_DECISION_2026-10-24.md`). It reads **no real session**, tunes **no threshold**, and
changes neither the v1 rule nor its recorded result. Anything changed in this plan after the first calibration number
exists is a deviation and is listed as one in the results file.

## 1. What is under test: the exact v2 decision

The decision is the whole REL-08 v2 screen for one family, as specified in `REL08_CONTINUATION_RULE_v2.md` with the
changes this plan fixes (the rule document is updated to match before signing; until then this plan governs the
calibration):

* **Structure:** six sessions (one trading day each), seven variants (V1, V2, V5, V6, V9, V10, V11), K = 7.
* **Usable rebalance, qualifying session:** as the rule (>= 60 names with fresh score and complete label). A session is
  **qualifying** iff it has **>= 2 usable rebalances**. Rebalances of a non-qualifying session are **excluded** from every
  statistic and count (they cannot contribute a gap, an IC or a net-bps value).
* **Five-of-six coverage:** at least **5 of the 6 sessions qualify**; otherwise every variant is `underpowered`. This
  rule applies to **coverage only**.
* **C1 (sufficiency) with v2 gap:** over **qualifying sessions only**, the usable-rebalance count floors (>= 50 at 5 m,
  >= 20 at 15 m, pooled), the 70-name breadth condition, and `max_gap_min` <= 30 where `max_gap_min` is the largest gap
  between consecutive usable rebalances **within the same session** (no leading gap, no gap across sessions). A single
  rebalance in a session cannot reach C1 because that session does not qualify.
* **Resampling (changed from v1): sessions are treated as FIXED, blocks are resampled WITHIN each session
  independently.** Per bootstrap draw and per session, a moving-block bootstrap with 30-minute blocks resamples that
  session's usable rebalances; no block spans an overnight boundary; the session sample sizes are preserved; the draw's
  statistic is the mean over the pooled resampled rebalances. Inference is therefore **conditional on the six observed
  days**: it says nothing about day-to-day variation (section 6).
* **Statistics and thresholds:** unchanged (9,999 draws, seed scheme below, lower bounds at level 1 - 0.05/7, MDE =
  2.49 x SE, ESS from Newey-West within session, C2-C8 thresholds as the rule). C1-C4 gate the outcome, C5-C8 as the rule.
* **Book:** `alpha_bps` 20, `risk_aversion` 100, `solver_iters` 20, notional $1M, rest as v1 (provisional synthetic
  settings, not calibrated market parameters).
* **Outcome of one screen:** `continue` for variant *v* iff the rule's decision map gives `continue` (C1-C8 all hold).
  The **family-level false continuation** event is "at least one of the seven variants is `continue`" (before the
  at-most-3 cap, which can only reduce it).

The implementation under test is the production code of the implementation PR (PR 3 of this programme), run with the
exact seeds and settings below. Calibration is run **only after** this plan is merged **and** that code is merged, from a
commit recorded in the results file. A pre-implementation harness is not used for gating.

## 2. Declared null models (no real data, no signal)

All use the dry-run generator family (`research/v118/rel08/dryrun/rel08_v2_dryrun.py`, 80 names: 78 equities on 2
reference ETFs, 4 sectors, 390-minute sessions, 30 s grid) with **no predictable component** in any variant's target: the
population mean net bps, rank IC and gross edge are 0 before costs; costs are the registered model, so population net
bps is negative. Each cell is a null model x spread regime.

| id | null | purpose |
|---|---|---|
| N1 | martingale prices, common factor, i.i.d. idiosyncratic noise (the dry-run `null` mode) | baseline |
| N2 | N1 plus volatility clustering (GARCH(1,1)-type common and idiosyncratic scale, persistence 0.95) and microstructure noise with negative lag-1 autocorrelation (bid-ask bounce) | serial dependence inside a session |
| N3 | N1 plus uninformative but persistent scores: each name's score is its true feature multiplied by a slowly varying random sign process (half-life 20 minutes) independent of returns | dependence in rebalance-level outcomes without signal |
| N4 | N1 with **day-level heterogeneity**: each session has its own volatility multiplier (log-normal, sd 0.4) and its own spread multiplier (log-normal, sd 0.4), a mean-zero common return shock per session, still no signal | stress on the fixed-session assumption; a mis-sized result here is reported, not tuned away |

Spread regimes: **S1** = quoted equity spreads 1-4 bps, **S4** = 4-16 bps (the dry-run regimes). A session may fail
coverage by chance on synthetic data (data thin-out is not modelled); that outcome counts as `underpowered`, i.e.
not a false continuation.

Not modelled and so not covered: real microstructure, real cross-name dependence beyond the common factor and sectors,
data holes, auction effects, real fill behaviour. See section 7.

## 3. Replications, seeds and Monte Carlo precision

* **R = 200** independent six-session synthetic datasets per cell, 8 cells (N1-N4 x S1, S4): **1,600 screens**, each
  evaluating all 7 variants with 9,999 bootstrap draws.
* **Seeds:** dataset seed = 20261100 + 1000 x cell_index + r (r = 0..199); the six sessions of a dataset come from one
  generator call with that seed; the bootstrap seed is 20261020 as in the rule (fixed, same for every variant/replication,
  so replications differ only through data). Cell order: N1S1, N1S4, N2S1, N2S4, N3S1, N3S4, N4S1, N4S4.
* **Precision:** exact (Clopper-Pearson) one-sided 95 % upper bounds. With R = 200, 0 events gives an upper bound of
  1.5 %, 4 events 4.5 %, 5 events 5.3 %. For the per-variant rate, 7 x 200 = 1,400 variant-screens per cell are pooled;
  variants share data, so this interval is **optimistic** and is accompanied by a replication-level bootstrap interval
  (resampling the 200 replications, 2,000 draws, seed 20261101).
* The calibration cannot resolve a rate below about 1.5 % per cell and cannot distinguish 3 % from 5 %. It can only reject
  gross mis-sizing. This is stated in the results.

## 4. Quantities reported (for every cell and pooled over cells N1-N3 and over all four)

1. **Family-level false-continuation rate:** share of replications with >= 1 `continue`, point estimate, Clopper-Pearson
   95 % two-sided interval and one-sided 95 % upper bound.
2. **Per-variant false-continuation rate:** per variant and pooled over variants, same intervals plus the replication-level
   bootstrap interval.
3. **Per-variant C3 lower-bound false-pass rate** (lower bound > 0 and net >= +1 bps; the Bonferroni-corrected coverage
   check): the component the multiplicity argument rests on. Also the one-sided non-coverage of the lower bound for the
   true population net bps (which is known in the generator for N1-N3 as the cost-only value of the book; reported only
   if that quantity is computed deterministically, otherwise omitted and said so).
4. Share of replications `underpowered` by coverage, by C1, by C2, by C4 (diagnostic, shows how often the conjunction is
   decided by sufficiency rather than by the statistic).
5. Bootstrap diagnostics: the mean bootstrap SE and the empirical standard deviation of the point estimate across
   replications (ratio), per variant, to flag SE misstatement.

## 5. Acceptance criterion and stop rules (decided now)

**Calibration PASS** requires all of:

* **A.** In each of cells N1, N2, N3 (both spread regimes, six cells): the family-level one-sided 95 % upper bound <= 5 %
  (that is at most 4 family events in 200).
* **B.** In the same six cells: the per-variant pooled C3 false-pass rate upper bound (Clopper-Pearson, 1,400 trials) <= 1.43 %
  (twice the per-variant Bonferroni level 0.05/7); approximately <= 12 events in 1,400.
* **C.** Pooled over the six N1-N3 cells, the family point estimate <= 5 %.
* **D.** No cell with a computing failure, no cell with fewer than 200 completed replications.

N4 is **reported, not gating**: it is the declared stress for the fixed-sessions conditional inference. If N4's family
upper bound exceeds 5 %, the results file says plainly that the screen is not size-controlled for day-level
heterogeneity, and the permitted-claims section of the rule must say so.

**Calibration FAIL** is any criterion missed. A fail means: v2 is **not** signed with this inference, no parameter or
threshold is tuned to rescue it, and the finding is recorded as it is. Whether to try a different inference is a new
decision with a new plan (and a new null).

**Stop rules:**

* One execution of the 1,600-screen plan. A crash or environment failure is repaired and the **identical** cells re-run
  with the same seeds; the failure is recorded. No cell is re-run because of its result, no seed is changed, no cell is
  dropped or added, R is not increased after a result is seen (a larger confirmation would be a new plan).
* If an interim look shows a criterion already impossible (family events > 4 in a gating cell), the run still
  completes (the other cells are information) and the verdict is FAIL; no cell is re-seeded.
* A timing run on 3 replications of N1S1 may be made to size the job. Its outputs are discarded unread (only the elapsed time is kept) and
  its seeds (20269000 + i) are not among the plan seeds.
* Resource: free RAM >= 4 GB, at most 2 workers, jobs over ~10 minutes launched detached.

## 6. Power-side sanity check (NON-GATING)

A separate, labelled non-gating check: planted effects `revert` with amp in {6, 12, 24} bps and `lead` with amp in
{0.3, 0.6}, spread S1 only, **R = 30** replications each, same seeds scheme (base 20268000), same implementation. Reports
the share of replications with C3 pass and with `continue` per variant. Its only purpose is to show whether the screen can
fire at all at the frozen book; **it cannot affect the verdict, the parameters or the thresholds**, and a low rate is
reported as low power, not as a defect to repair by tuning. (The v1 dry run found no C3 pass up to rank IC about 0.12.)

## 7. What the result can and cannot mean

* A PASS means: under these four synthetic generators, with six fixed independent days and no signal, the full v2 decision
  (within-session bootstrap, K = 7, C1-C8 conjunction) continued a null screen no more often than the gating
  precision allows. It does **not** mean nominal 5 % family-wise control, does not cover real market dependence, and says
  nothing about the real V1/V5 trade-size difference or about any real return.
* The bootstrap is conditional on the six observed days. Even with a PASS, **no claim about day-level generalisation,
  regime robustness, or a population mean is supported**; N4 only probes a stressed version of that.
* A PASS is not evidence of an edge and does not authorise execution or promotion: signing and execution remain
  separately gated by the reviewer, the F3 status (OPEN) and the identity-bound signature protocol. A FAIL
  blocks signing of v2 with this inference.
* The reviewer's remaining items (production implementation, solver tests, frozen evidence binding) are separate
  deliverables and are not satisfied by this calibration.
* The 0/24 screens of the dry run are superseded as null evidence by this plan.

## 8. Outputs and commit discipline

Results go to `research/v118/rel08/calibration/` (per-cell JSON with one record per replication x variant: outcome,
criteria flags, lower bounds, SE, seed; REPORT.md with the section 4 tables and the verdict against section 5). Small
files only (summary tables, no raw return series). The results PR states the commit of the code under test, the plan
commit, and lists any deviation from this plan. Real sessions are not read at any stage; the holdout registry is not
touched.
