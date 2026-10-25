# R5: real-data dependence audit, predeclared plan (v1.19 / v2.0 stage 1, session A; NOT RUN)

Status: plan committed before any session statistic is read. Runner: `research/v119/r5_dependence.py`
(`selftest` runs on synthetic values only; `run` is the audit, later, as one ledger look). Specification:
v1.19 specification revision 9, stage 1 "R5"; frozen R0 (`python/tests/spec/CONFIRMATORY_TEST_SPEC.md`)
sections 2 and 4; plan of record section 5.12 (the R5 decision rule). Defect register: D-13.

This document is the predeclaration. Every estimand, lag, split, band and seed below is fixed here; the
result document (`R5_DEPENDENCE_RESULT.md`, written by the runner) reports them in the two parts of section 6
and nothing else. A change to this plan after a result has been seen is a new plan revision, not an edit.

## 1. Question and non-question

R5 asks: **do the session-level statistics the confirmatory gate will read behave, across the available real
sessions, in a way that is compatible with the gate's validated domain (D-IND of R0 section 2), and what can the
available sessions not tell us?** It feeds Authorisation B (F2) and the X0 domain justification memo.

R5 does **not** ask whether the sessions are independent and cannot answer it. With the sessions available
(10 in-sample 2019-2021 sessions; the 2025-2026 sessions are spent for the three registered names and
sealed for the rest, `research/holdout_registry.json`) the lag-1 autocorrelation has a white-noise standard
error of about 1/sqrt(G) >= 0.25, so a non-rejection is uninformative. **Not detecting dependence is never
described, here or in the result, as evidence of independence** (plan 5.12, reviewer condition 4; R0-2).

## 2. What is read, and what is refused

- Input: a **session-value panel**, G sessions x K hypotheses, written by a separate, guarded producer under
  `iap.research.holdout_registry.guard` with purpose `r5_dependence_audit`. The values are exactly the session
  statistics the canonical gate (`iap.validation.confirmatory.decide`, R1) consumes for the registered labels:
  one value per session per hypothesis, computed from usable units only with the registered estimator (R0
  section 3). The producer is written when R1's session statistics exist; R5 does not define a new estimator.
- The runner re-checks every session of the panel against the holdout registry before computing anything:
  a session is accepted only if it is `in_sample` for the panel's symbols, or `spent` by the prereg id the
  panel names (a replay of a look already taken). An `unread` (sealed) or unregistered session is refused
  before any statistic. No sealed data is read by R5 under any flag.
- One ledger look (`research/experiments.json`, kind `r5_dependence_audit`, family `real`) is recorded when
  the audit runs on real data; the synthetic self-test records nothing.
- Hypotheses: the K = 16 registered family of the H1 track (`variants.K16` as frozen for null v3 is the
  reference list; the panel names its hypotheses and the result reports them). Sessions in registered date
  order.

## 3. Part A: diagnostics (descriptive)

Part A may reveal a violation. It never establishes membership of any domain.

### 3.1 Estimands
For each hypothesis k, the session series x_{k,1..G}; its sample mean, sd, and the statistics below.

### 3.2 Temporal dependence, lag grid 1-5
Sample autocorrelation r_h (mean-corrected, full-sum-of-squares denominator) for h = 1, ..., 5 per
hypothesis, each with the white-noise band r_h +- 1.96 / sqrt(G) (Bartlett), and the pooled mean and spread
of r_h over the K hypotheses. Sensitivity to lag choice is the grid itself: every lag is reported, none is
selected after the fact.

### 3.3 Common-factor and cross-hypothesis dependence
First principal component of the standardised G x K panel: share of variance explained, loadings, and the
mean off-diagonal correlation (equicorrelation estimate). Reported for power (Holm is valid under arbitrary
dependence between valid p-values; a common factor reduces power, R0 section 5.2); never disqualifying.

### 3.4 Nonstationarity and unequal sessions
Stationarity split **2019-2021 vs 2025-2026** per hypothesis: period means and sds, Welch t with its df, and
the variance ratio. Both periods are tiny; the split is descriptive and no test is declared decisive.
Unequal usable counts per session are reported by the producer (R0 section 3 m_min) beside the panel.

### 3.5 Session-block permutation diagnostic
Statistic: the mean over hypotheses of the lag-1 autocorrelation. The unit permuted is the **whole session**
(its K values travel together), so within-session and cross-hypothesis structure is preserved and only the
temporal order is broken. 2,000 permutations, SplitMix64 seed 20301900, Fisher-Yates on the session index.
Reported: the share of permutations whose statistic is at least the observed one. A small share may reveal
temporal dependence; a large share with G sessions this small is weak evidence and is **not proof of
independence**.

### 3.6 What G sessions cannot identify (stated in the result verbatim)
Serial dependence below about 1/sqrt(G) in absolute value; nonlinear dependence that leaves the lag-1 to
lag-5 rank correlations of squared values near zero; regime change within a period; any property of
sessions outside the sample.

## 4. Part B: affirmative evidence for domain membership (R0 section 4 bands)

Part B applies the disqualifying-condition screen of R0 section 4, element by element, each with the band it
is judged against. **Passing establishes only that no known disqualifying condition was detected.** A missing
element is a missing element and leads to refusal, never to a pass.

| element | disqualifying if | role |
|---|---|---|
| session-value excess kurtosis (bias-corrected G2) with its 95 % interval (normal-theory SE) | the interval excludes 0 on the high side by more than +1 (lower bound > 1) | screen only; passing is not evidence of normality |
| design independence (from the X1 manifest: non-overlapping sessions, no label crossing a session boundary, within-session conditioning) | any of the three guarantees absent | design guarantee for what it covers; a declaration, never inferred from data |
| serial dependence | lag-1 point estimate above 0.3, or any lag 1-5 Bartlett interval excluding 0 | screen only; a low estimate is not evidence for independence |
| nonlinear dependence | Spearman autocorrelation of the squared session values at any lag 1-5 rejecting at 5 % (t approximation) | screen only |
| common-factor loading | never | affects power, not the domain |
| G | below the registered power-table minimum or below 10 | hard |

The numbers are the proposed bands of R0 section 4; the H1 prereg fixes them before T0, and a change there
is applied to this plan as a new revision before the audit runs.

**Multiplicity of the proposed screen (recorded, not resolved here; defect D-39).** The R0 band is stated per
hypothesis and per lag. Applied to K = 16 hypotheses and 5 lags it flags *independent* data often: at the
nominal 5 % per test about 16 x (1 - 0.95^5) = 3.6 hypotheses are expected to show a lag interval excluding 0
when nothing is wrong, and the family-level false-alarm rate of "any hypothesis disqualified" is about 98 %. The
runner therefore reports, beside the R0 screen, a **companion** at a Sidak-adjusted family level of 0.05 over the
K x 5 (or K) tests, and the expected number of false per-hypothesis flags under independence. The companion is
not an R0 band: **the conclusion row reads the R0 per-hypothesis screen as written (fail-closed)**; the companion
and the expected-flag count are inputs to the X0 memo and to the H1 prereg's choice of registered bands. A
disqualification that the companion does not reproduce is reported as such, never waived.

## 5. The decision rule (one row of plan 5.12, from part B only, applied in this order)

1. any element missing or unavailable -> **missing or incomplete evidence** -> no real-data FWER claim;
2. else any element disqualifying -> **material dependence detected** -> the synthetic F3 claim may stand;
   real-data transfer prohibited;
3. else G < 44 (lag-1 SE above 0.15, the planning heuristic of `F3_INSAMPLE_HISTORY_INVENTORY.md`) ->
   **dependence not detected, low power** -> the synthetic F3 claim may stand; real-data transfer remains
   unproven;
4. else -> **stronger evidence supporting the assumptions** -> real-data use may be considered only after a
   separate inference validation.

With the sessions available today the rule cannot reach row 4 (G <= 19 even if every registered 2025-2026
session were admitted), and reaches row 1 whenever the X1 design manifest is absent. Part A never enters the
rule. **Passing F3 and not detecting dependence in R5 does not authorise PROMOTE**; PROMOTE needs the canonical
gate with a registered power table on the hypothesis's own confirmation data, and real-data eligibility needs
the reviewer-approved X0 memo bound into the prereg (R0 section 4 item 2). Part B is an input to that memo.

## 6. The result document

`R5_DEPENDENCE_RESULT.md` has exactly three sections: **Part A: diagnostics** with its own conclusion
(descriptive), **Part B: affirmative evidence for domain membership** with its own conclusion, and
**Conclusion**: the one row of section 5 with its permitted conclusion and the sentence of section 5's last
paragraph. The JSON twin `r5_dependence_result.json` carries every number. The runner refuses to write a
result that lacks either part.

## 7. Synthetic self-test

`python research/v119/r5_dependence.py selftest --out <dir>` runs the whole pipeline on independent N(0, 1)
values (SplitMix64 seed 20301901, G = 16, K = 16, a fake 2019 / 2025 calendar, a complete design declaration).
Because the R0 screen is per hypothesis (section 4, multiplicity note), independent data lands on row 2 when any
of the 16 hypotheses shows a chance exclusion and on row 3 otherwise; the self-test must show the companion
family-adjusted screen clear, the number of per-hypothesis flags within its binomial 99 % bound, and the row
consistent with the flags. A single-hypothesis independent panel (K = 1) lands on row 3. Tests in
`python/tests/test_r5_dependence.py` check the statistics on planted AR(1), equicorrelated and heavy-tailed
panels, the refusals, and that the result document separates the two parts and never says non-detection proves
independence.
