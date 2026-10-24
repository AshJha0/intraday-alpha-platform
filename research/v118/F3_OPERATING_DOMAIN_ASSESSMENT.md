# F3 operating-domain assessment, revision 2 (PLANNING DOCUMENT; UNSIGNED; every real-data diagnostic below is NOT RUN)

Dated 2026-10-24. Preparer: Claude. Revision 2 incorporates the outside reviewer's decision
`docs/release/REVIEWER_F3_POWER_DOMAIN_DECISION_2026-10-24.md`: approved **as a planning document**, with named clarifications; **real-data diagnostic
preregistration is not authorised**, nothing here reads real data, runs a diagnostic, signs or authorises anything, and F3 stays OPEN. The first step the
reviewer asks for, a metadata-only inventory of whether a sufficiently long in-sample history exists, is `F3_INSAMPLE_HISTORY_INVENTORY.md`.

## 0. Where we stand

The signed synthetic acceptance study (27/27 mandatory PASS, `research/v118/f3_accept/REPORT.md`; reviewer post-execution decision) showed that P1
(Student t(G-1) on the G session values of a hypothesis, Holm at alpha_op 0.020) has FWER <= 2.5 % with 95 % confidence **when sessions are independent**,
at G = 5, 7, 10, in regimes a0, b0, b1. That is a statement about generated data. It says nothing about whether real sessions are independent, whether
their heterogeneity resembles b0/b1, or whether a real p-value is valid. Serial dependence (regime c, AR(1) across sessions) is outside the validated
domain: in the pilot P1's FWER was 0.068-0.132 at phi = 0.3 and 0.194-0.579 at phi = 0.6 against a 0.025 target (`f3_spec/F3_PILOT_RESULTS.md`).

Three conditions would have to hold on real data: (i) **independence of the session-level values** across sessions, (ii) **session heterogeneity** inside
what b0/b1 cover, (iii) **valid individual p-values** (the t reference adequate for the real session-value distribution at the real G). None can be
*proved* from data; diagnostics can only fail to find a violation, and with few sessions their resolution is low. **Weak diagnostics are never treated as
proof of independence**: evidence of low autocorrelation is not independence.

## 1. Constraints on any real-data diagnostic

* **Registry and holdout rules.** Holdout and spent sessions (`research/holdout_registry.json`; `docs/DATA_SOURCES.md`) are never used for diagnostics, design
  or tuning; the fail-closed guard (REL-02) refuses unregistered or holdout dataset arguments. Each diagnostic that touches data needs its own signed prereg
  (analysis identity v2, `data_versions` listing the real datasets, CI-lock environment, runner/signer separation, reviewer review of the signed protocol
  before the run), declared before being run, with thresholds. A diagnostic declared after seeing a result is a look.
* **In-sample summaries only**, of sessions that are already in-sample, never from the holdout.
* **No selection.** A diagnostic outcome may narrow where P1 is used; it must not pick the hypotheses, the signal, G or the period that "work".
* **Sequence required by the reviewer.** First establish from metadata (no data opened, no statistics examined) whether a sufficiently long, genuinely
  in-sample history of comparable session-level observations exists (`F3_INSAMPLE_HISTORY_INVENTORY.md`). D1-D3 are not to be preregistered or run before that, and not
  at all without a further reviewer decision.

## 2. Proposed diagnostics (all NOT RUN)

### D1. Serial dependence across sessions (three-way decision)

* **Statistic.** Lag-1 (and lag-2) autocorrelation of the session-value series, per hypothesis and pooled, ordered by session date, over the longest
  genuinely in-sample, comparable and **contiguous-enough** history; companions: permutation (order shuffle) p-value and a pre-declared block-bootstrap interval.
* **Three outcomes.**
  * **Supportive** only if the pooled 95 % confidence interval for rho1 lies **entirely inside [-0.15, 0.15]**. A point estimate below 0.15 is not sufficient.
  * **Adverse** if the interval establishes a clear violation (excludes the tolerance on one side, pre-declared as: lower end > 0.15 or upper end < -0.15).
  * **Inconclusive** if the interval crosses either boundary and does not establish a clear violation. This is the expected result with a short history.
* **Tolerance.** |rho1| <= 0.15 is a provisional **screening threshold**, not proof of independence; chosen conservatively because phi = 0.3 already inflated FWER in the pilot.
* **Mean versus other dependence.** Autocorrelation of the session values (dependence in the mean) is only one form. The assessment must separately examine
  dependence in **volatility** (autocorrelation of absolute and squared standardised values, ARCH-type clustering), in **squared residuals**, and **common event exposures**
  (shared calendar events such as expiry, macro or earnings days, which move several sessions together). Near-zero lag-1 correlation of the mean does not establish
  independence; each of these is its own pre-declared item with its own three-way outcome and none is reduced to the lag-1 number.
* **Resolution.** About 40 or more sessions is a **planning heuristic only**, not a guarantee of adequate diagnostic power; actual resolution is assessed from the sample size,
  the dependence structure and the width of the confidence interval. With G of 5-10 the lag-1 standard error is about 1/sqrt(G) (0.3-0.45) with bias about -1/G.
* **Short history.** **D1 is uninformative and P1 is not usable for real-data inference claims.** The evidential standard is not lowered because fewer sessions
  are available; a short history supports description only and cannot show that the synthetic calibration's independence assumption is plausible.

### D2. Session heterogeneity

* **Statistic.** Dispersion of per-session scales (ratio max/min, coefficient of variation of the within-session standard error from in-sample intraday blocks) and the
  standardised skewness and kurtosis of the session values, versus the b0/b1 envelope (scale ratio about 5, normal / t3 / lognormal cycle); leave-one-session-out dominance check.
* **Supportive / adverse / inconclusive** in the same three-way form: supportive only if estimates **with** intervals sit inside the envelope; adverse if a ratio well above 5, a single
  dominating session, or tails heavier than t3 is established; inconclusive otherwise (the expected result for G <= 10).

### D3. Validity of individual p-values

* **D3(a), priority for any future preregistration: sign-flip (or permutation) reference versus Student t(G-1).** Compare, per hypothesis, the exact sign-flip p-value
  (2^G flips, exact for G <= 10) with the Student t(G-1) p-value, with a pre-declared maximum tolerated discrepancy. **Assumptions that must be stated and justified before it
  means anything:** (1) the sign-flip distribution is valid only under a **symmetry or invariance condition**: the session values must be symmetric about the null value (equivalently
  the null is invariant to sign changes of each session value), which fails for skewed session values such as the lognormal member of b1; (2) the sessions must be
  exchangeable or independent so that flips are not informative about dependence; (3) **destroying a signal does not automatically produce a valid null
  distribution**: demeaning, shuffling or sign-flipping a series that contains real signal, scale heterogeneity or dependence produces a reference set whose properties must themselves
  be argued; (4) agreement between sign-flip and t p-values is consistency between two procedures that share assumptions, not validation.
* **D3(b), OPTIONAL exploratory evidence, not a prerequisite and not a validation criterion.** Uniformity summary of p-values of in-sample hypotheses with no theoretical reason for an effect already in the
  ledger. The ledger is predominantly synthetic and many hypotheses share observations, so the effective number of independent null hypotheses is substantially smaller than the number of rows. D3(b) can expose
  obvious anomalies but cannot establish calibration of real-data p-values, and no decision-table row depends on it.

### D4. Dependence between hypotheses

The equicorrelated synthetic sensitivity (a1, rho 0.6) met the rule but is not a precondition; the correlation of hypothesis-level session values is summarised for transparency (NOT RUN).

## 3. Regimes remaining outside the validated domain

Serial dependence across sessions (regime c); volatility clustering and common event exposures (not generated by any validated regime); G < 5 or G > 10; scale ratios or tails beyond b0/b1;
K > 16; dependence through shared sessions across families; non-mean estimands; any selection of hypotheses after viewing data. The A0 residual-mean false-flag rate is a separate documented diagnostic.

## 4. Decision table (reviewer-confirmed three-way classification)

| Outcome across the required diagnostics | Permitted use of P1 on that real signal family |
|---|---|
| All required diagnostics **supportive** | Exploratory inference with explicit assumptions and limitations; **no real-data FWER guarantee**. The outcome is **not** described as validation of independence or of the full real-data operating domain |
| Any diagnostic **adverse** | P1 unsupported for that signal family; no substitute is validated |
| Any diagnostic **inconclusive** (including D1 uninformative from a short history) | No inferential claim; additional evidence required |

Even the first row does not close F3 and does not turn the synthetic calibration into a real-data FWER guarantee; closing F3 or authorising a real-data claim is the outside reviewer's decision.

## 5. What a real-data user could and could not claim

Could say: "Under the assumption of independent sessions within the validated heterogeneity range, the Holm-adjusted Student-t procedure at alpha_op 0.020 was shown, on synthetic data, to keep FWER below 2.5 %
with 95 % confidence at G = 5, 7, 10; our in-sample diagnostics (if and when run under signed preregs) were supportive / adverse / inconclusive."

Could **not** say: that real-data FWER is controlled or below any number; that sessions are independent (low autocorrelation is not independence); that results are robust to serial dependence, volatility
clustering or common event exposures; that a rejection indicates an edge or alpha; that P1 has any particular power; that it applies at other G or to non-mean statistics; or that passing the study closes F3.

## 6. Open points for the reviewer

1. Are the pre-declared adverse conditions for D1 (interval wholly beyond a boundary) and D2 acceptable? 2. Given the inventory result, do you want the domain work to stop at "D1 uninformative", or to define what additional in-sample sessions would be acquired and under what rules (the holdout rules constrain this)?
