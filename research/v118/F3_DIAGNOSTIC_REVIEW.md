# F3 diagnostic review: findings (EXPLORATORY)

Dated 2026-10-24. Plan: `F3_DIAGNOSTIC_REVIEW_PLAN.md` (PR 333, committed before any result). Code, outputs, logs:
`research/v118/f3_diagnostic/` (code commit is the commit that adds this file). Everything here is exploratory
and post hoc on a failed protocol. **It changes nothing in the v3 record: v3 is FAIL, F3 stays open, no threshold,
level, generator or signed artefact was touched, no v4 is drafted, no claim about real-data FWER or any edge is made.**
Data read: `data/real/multi7` only (same seven sessions, registry `in_sample`, via `guard_or_exit`), plus the
stored result (sha256 `c489f28c...5210`, checked). Non-signing actor `claude-f3-diagnostic`.

## 1. Result in one paragraph

The evidence supports one main mechanism and one contributing one, and does not support a defective test.
(i) **The A0 count is the outcome of a single realised 7 x 7 matrix per hypothesis, and the rate of such a matrix
has a sampling spread (sd about 2.5-3 pp) as large as the excess it was used to detect.** When H0 holds by
construction, the unmodified CR1 + Webb procedure applied to all 1,854 derangements of a random matrix has a mean
rate near nominal but 28-40 % of null matrices exceed the 2.97 % acceptance line. The observed 4.3 % is inside that
spread. (ii) **The derangement distribution of the S2 statistic is not centred on zero** (shift 0.0 to 0.97 of its sd
per hypothesis), which inflates the rate for some hypotheses; removing it moves K3 79 to 66 only, so it is a
contributor, not the whole. (iii) The S2 standard error is not understated and the small-G test is not
over-sized in the synthetic checks; a mild upper-tail excess at the strict Holm levels exists in the plain normal
case (section 4).

## 2. What was run, and validation

| id | diagnostic | status |
|---|---|---|
| D1.1, D2.3, D3.1 | stored result only | run |
| matrices | 7 cyclic shifts x 3 label variants, 19 hypotheses | run; **validation gate passed**: counts 79 / 80 / 23 exact, max abs diff in mean, SE, p 5e-7 (the stored 6-decimal rounding) |
| D1.2, D2.1, D2.2, D1.4 | matrix analyses | run |
| D1.3 | null-matrix sampling distribution | run, reduced (deviation 2) |
| D3.2 | synthetic size at G = 7 | run |

## 3. Q1: the conditional randomisation estimand

* **Recorded (D1.1).** K3: union 79 vs sum of per-hypothesis counts 91; K6 80 vs 83; K16 23 vs 23. Excess is carried by
  few hypotheses (K3: pooled 10s 42 Holm rejections, 1s 30, 1m 19; K6: AAPL l1@10s 32, MSFT l5@10s 37).
* **Conditional mean (D1.2).** `E_pi m / sd_pi m` over the 1,854 derangements, per hypothesis: pooled 10s +0.35, pooled
  1s -0.12, pooled 1m +0.03, K6 l5@10s/MSFT +0.76, l1@10s/AAPL -0.02, up to +0.97 (l5@1m/MSFT, outside K3/K6). The
  protocol's descriptive heuristic did not flag it (its SE sd/sqrt(7) is not calibrated, as the protocol said).
* **Sampling spread of the A0 rate (D1.3, the central result).** 40 random null matrices per model, each enumerated over
  all 1,854 derangements, single hypothesis, raw p <= 0.025 (the cell level for one hypothesis):

| model (H0 true by construction) | mean rate | sd | median | 90th pct | share of matrices > 2.97 % |
|---|---|---|---|---|---|
| a: iid entries | 2.78 % | 2.5 pp | 1.9 % | 6.3 % | 32 % |
| b: row + column effects, unequal row scales | 2.24 % | 2.8 pp | 1.4 % | 4.6 % | 28 % |
| c: b fitted to the real matrix of imbalance_l1@10s pooled | 2.96 % | 2.9 pp | 2.5 % | 7.5 % | 40 % |

  At the stricter levels the Holm family needs (0.025/3): mean 0.99 / 0.55 / 1.02 %, sd 1.1 / 0.9 / 1.2 pp, 5-8 % of
  matrices above 2.97 % (nominal 0.83 %). The observed A0 values for that pooled 10s hypothesis (110 / 1,854 = 5.9 %
  at 0.025; 40 / 1,854 = 2.2 % at 0.025/3) sit about the 80th percentile and about one sd above the mean of
  model c. **Reading: a test that is correct on average would still fail a "count <= 55 of 1,854" line on a single
  dataset roughly a third of the time.** The criterion therefore cannot separate a defective test from an ordinary
  matrix; this is a defect of the A0 acceptance construction, shown by the null models, not a finding about the
  data. Limits: 40 matrices per model (mean estimated to about +-0.4 pp, quantiles coarse), one hypothesis
  (marginal, not the Holm family), parametric models that are my assumption (normal entries, independent across
  hypotheses).
* **Leave-one-session-out (D1.4, G = 6, 265 derangements, different G).** K3 10-17 rejections in every drop (3.8-6.4 %), K6
  0-8, K16 0-1. No single session carries the excess; but with six or seven units the rate is a statement about very
  few independent numbers.

## 4. Q2: removing session-level means; Q3: SE calibration at small K

**Q2 (D2.1, D2.2).** Rejections out of 1,854 (K3 / K6 / K16): recorded centred 79 / 80 / 23; uncentred labels (v2 generator A,
enumerated) 81 / 67 / 21; centred and common intraday profile removed 90 / 28 / 4; row, column, grand means removed
from the matrix 66 / 77 / 11. Row and column effects carry only 2-28 % (rows) and 4-28 % (columns) of the off-diagonal variance (interaction
and noise 54-86 %). Centring on session means is not what produced the excess: uncentred is no better for K3.
Profile removal helps K6 and K16 but not K3 and changes the sign of shifts (pooled 1m goes to +0.35), so the
profile is not a clean explanation. Even with zero conditional mean (last variant) K3 is 66 (3.6 %) and K6 77 (4.2 %):
the shift is a contributor, not the cause of the whole excess. The recorded 500-draw A_v2 gave 21 / 15 / 4.

**Q3 (D3.1, D3.2).** U (sd of m over SE) for the carrying hypotheses is 0.86-1.02 in A0 and 0.98-1.00 in B: the CR1 SE is
the right scale, not understated. Studentised t is heavier-tailed than t(6) in A0 where the shift exists (pooled 10s:
97.5th percentile 2.98 vs 2.45) and about right in B (2.29-2.54). Synthetic size of the unmodified test at G = 7 (20,000
replications each; Wilson 95 % half-width about 0.2 pp at 0.025): raw rate at 0.025: normal 2.61 %, unequal scales
2.71 %, t3 2.74 %, t3 unequal 2.46 %, skewed 0.16-0.21 % (very conservative). At 0.025/3 and 0.025/6 the plain normal
case is above nominal (0.97 % vs 0.83 %; 0.55 % vs 0.42 %), unequal scales and t3 about nominal or below. So there
is a mild upper-tail excess for the most benign data (up to about 1.3x nominal at the Holm floor), which is
consistent with v2's observation for B, and no sign that heteroscedasticity or heavy tails make the test worse.
**K is not separable from hypothesis in the record**: the failing K3 family is three pooled hypotheses that appear in no
other family, K6 is the single-symbol hypotheses at 10s and K16 adds the rest; what changes between K6 and K16 is
mainly the Holm divisor. Raw p <= 0.025/6 for the three pooled hypotheses: A0 20 / 20 / 14 against nominal 7.7 and B 12 / 9 / 13
(1.1-1.6x).

## 5. Mechanism: supported, suggestive, not shown

* **Supported (two independent routes: null-matrix models, and leave-one-out / variants on the real matrix):** the A0
  rate is a finite-population property of one dependent matrix with large sampling spread; the "count <= 55" criterion
  has a high false-fail probability under a correct test. This explains why a 4.3 % can occur without a defect in the
  inference step. It does **not** show the inference step is correctly sized.
* **Suggestive:** a nonzero conditional mean of the derangement statistic inflates the rate for specific hypotheses
  (route: matrix variants; the synthetic models carry random shifts, `|E/sd|` mean 0.35-0.46, but I did not isolate the shift
  effect there).
* **Not supported:** SE understatement; over-sizing from heteroscedasticity or heavy tails at G = 7; session-mean centring as
  the cause; a single outlying session.
* **Not shown / unknown:** the true unconditional size on real data (never estimable from seven sessions); whether B's
  2.5 % reflects real-data behaviour; the joint Holm-family behaviour in the null models (marginal only); the mild
  upper-tail excess of the plain normal case beyond G = 7; exchangeability of the label sessions (untested; the
  derangement null assumes it).

## 6. Is a replacement protocol warranted, and what must a spec define first

I do not recommend drafting a v4 now; the review supports that the A0 acceptance construction was ill-posed, not that the
estimator is bad, so the next step is a decision, not a run. If the programme wants F3, a spec must define, before any
run: (1) **target error rate and level** (per hypothesis, FWER or FDR, nominal level, what excess is tolerable and why);
(2) **dependence assumptions** (independent sessions, exchangeable label series, or block structure) and which generator
represents them; (3) **replication unit**: independent datasets (B-style or a justified resampling of sessions), since
one real dataset with 1,854 derangements has one replication and a sampling spread of about 3 pp; (4) **acceptance
criterion** that accounts for its own sampling error and states its false-fail probability under a correct test (here about
30 %), e.g. an interval for an averaged rate over many null datasets; (5) **stopping rules** and the rule on repeated looks
at the same dataset (this is the fourth). The unaffected options remain open: larger G, or a different null source.

## 7. Open questions for the outside reviewer

1. Is the finding that the A0 count criterion fails a correct test about a third of the time sufficient to retire A0 as a
   pass/fail gate, keeping it as a diagnostic only?
2. Should F3 rest on independent synthetic datasets (B-like) with a stated dependence model, and is B's homogeneous-session
   design acceptable as the unconditional arm?
3. Does the reviewer accept the mild upper-tail excess at G = 7 (up to about 1.3x at 0.025/6, plain normal) as inside the tolerance, or
   does it need CR2/CR3-type work (a new procedure, outside this review)?
4. What replication count and false-fail probability would the reviewer require of an acceptance criterion?

## 8. Deviations from the plan (all before or without reading the contested outcome of the affected diagnostic)

1. Validation tolerance: plan said 1e-9 relative; the stored result is rounded to 6 decimals, so the gate used 2e-6 absolute
   (observed 5e-7) and exact counts.
2. D1.3 reduced from about 200 to 40 matrices per model after timing (about 67 s per matrix with the unmodified Webb
   enumeration while another job ran): model c was fitted to one hypothesis (pooled imbalance_l1@10s, the largest K3
   carrier), not several; the same seeds were used for all hypotheses; marginal per hypothesis only (no joint Holm).
3. D3.3 as written (K3 hypotheses inside a K16-sized family) cannot be done from the record because the pooled hypotheses
   appear in no other family; it was replaced by raw-p tail counts at 0.025/k, k in 1, 3, 6, 16 (the 16 added), reported in
   section 4.
4. Smoke tests of `d32_synthetic_size.py` with 200 replications and the same seed ran before the full run; their output was
   discarded. `d_matrix.py` first call failed on a shell quoting error and was resubmitted unchanged in substance.
5. Seeds: the plan's 20271002 / 20271003 were used for models b and c; model a used 20271001; D3.2 used 20271004.
