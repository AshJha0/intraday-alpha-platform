# F3 operating-domain assessment before P1 may be used on real data (UNSIGNED DRAFT; every real-data diagnostic below is NOT RUN)

Dated 2026-10-24. Preparer: Claude. Status: draft for outside-reviewer review; F3 stays OPEN. Nothing here reads real data, runs a
diagnostic, signs or authorises anything. Every proposed real-data diagnostic is labelled **NOT RUN**.

## 0. Where we stand

The signed synthetic acceptance study showed (27/27 mandatory PASS, `research/v118/f3_accept/REPORT.md`; reviewer post-execution
decision, PR 347) that P1 (Student t(G-1) on the G session values of a hypothesis, Holm at alpha_op 0.020) has FWER <= 2.5 % with 95 %
confidence **when sessions are independent**, at G = 5, 7, 10, in regimes a0, b0, b1. That is a statement about generated data. It says
nothing about whether real sessions are independent, whether their heterogeneity resembles b0/b1, or whether a real p-value is valid.
Serial dependence (regime c, AR(1) across sessions) is outside the validated domain: in the pilot P1's FWER was 0.068-0.132 at phi = 0.3 and
0.194-0.579 at phi = 0.6 against a 0.025 target (`f3_spec/F3_PILOT_RESULTS.md`), and every procedure was affected.

Three conditions would have to hold on real data for P1's calibration to transfer: (i) **independence of the session-level values**
across sessions, (ii) a **session-heterogeneity** pattern inside what b0/b1 cover (symmetric-ish or moderately skewed, scale ratio of the order
of 5), and (iii) **valid individual p-values**, meaning the t reference is adequate for the actual session-value distribution at the
actual G. None can be *proved* from data; diagnostics can only fail to find a violation, and with G in 5-10 their power is low. That limit is the central
fact of this assessment.

## 1. Constraints on any real-data diagnostic

* **Registry and holdout rules.** Holdout sessions (`research/holdout_registry.json`, `data/real/r7_holdout`, the 2026 holdout; see
  `docs/DATA_SOURCES.md`) are never used for diagnostics, design or tuning; the fail-closed guard (REL-02) refuses unregistered or holdout
  dataset arguments. Each diagnostic that touches data requires its own signed prereg (analysis identity v2, `data_versions` listing the real
  datasets, CI-lock environment, runner/signer separation, reviewer review of the signed protocol before the run). Reusing results seen in
  an earlier look counts as a look; diagnostics are declared before being run, with thresholds.
* **In-sample summaries only.** Each diagnostic below needs only per-session summary statistics (the session values the test would use, plus
  coarser or finer in-sample summaries of the same sessions), computed from sessions that are already in-sample, never from the holdout.
* **No selection.** A diagnostic outcome may narrow where P1 is used; it must not be used to pick the hypotheses, the signal, G or the
  period that "work".

## 2. Proposed diagnostics (all NOT RUN)

### D1. Independence of sessions (serial dependence)

* **Statistic.** Lag-1 (and lag-2) autocorrelation of the session-value series, per hypothesis and pooled across hypotheses (mean of the
  standardised autocorrelations), ordered by session date, over the **longest in-sample history of session values available**, not only the
  G sessions to be tested. Companions: a permutation (session-order shuffle) p-value, and a pre-declared block-bootstrap interval.
* **Why a longer history.** With G = 5-10 sessions the lag-1 autocorrelation has standard error about 1/sqrt(G) (0.3-0.45) and a negative bias
  of about -1/G; it cannot distinguish phi = 0 from phi = 0.3. Only a history of order 40+ sessions has useful resolution (standard error about
  0.16). If no such in-sample history exists, D1 **cannot be informative** and that is the finding.
* **Evidence for.** Pooled lag-1 estimate with a two-sided 95 % interval entirely inside a pre-declared tolerance (proposal: |rho1| <= 0.15,
  chosen because phi = 0.3 already gave FWER above 0.068 in the pilot), plus no individually extreme hypothesis. This is evidence of
  *no large* dependence in that history, not of independence in the tested window.
* **Evidence against.** Interval excluding the tolerance, or a pooled estimate above it. P1 then stays unusable on that signal family (no
  validated procedure covers regime c).
* **Ambiguous.** Interval straddling the tolerance (the expected result at small N): stated as ambiguous; P1 is not used for a claim.
* **Related, also NOT RUN:** cross-sectional clustering by calendar structure (weekday, expiry, event days) as a proxy for common shocks.

### D2. Session heterogeneity

* **Statistic.** Dispersion of per-session scales, e.g. the ratio max/min and the coefficient of variation of the sessions' within-session
  standard error (computed from in-sample intraday blocks), and the standardised skewness and kurtosis of the session values; compared with
  the b0/b1 envelope (scale ratio about 5, normal / t3 / lognormal cycle).
* **Evidence for.** Estimated scale ratio and tail behaviour inside the envelope, and a leave-one-session-out check that no single session
  dominates the mean.
* **Evidence against / ambiguous.** Ratio well above 5, a single dominating session, or tails heavier than t3, in which case the validated
  heterogeneity domain does not cover the data. With G <= 10 the shape estimates are themselves very imprecise; this is reported, not hidden.

### D3. Validity of individual p-values

* **Statistic.** (a) A calibration check on in-sample data with the signal destroyed: the permutation or sign-flip reference distribution for
  the same G session values (2^G sign flips, exact for G <= 10) compared with the Student t(G-1) p-value, per hypothesis; pre-declared
  maximum tolerated discrepancy. (b) QQ / uniformity summary of the p-values of in-sample hypotheses with no theoretical reason for an
  effect (for example hypotheses on shuffled or pre-registered-null-like labels already in the ledger), using KS and a proportion below 0.05 / 0.02.
* **Evidence for.** Discrepancies within tolerance and a p-value histogram consistent with uniform at the diagnostic's resolution.
* **Evidence against.** A p-value excess below alpha_op beyond the interval implied by the number of hypotheses (counting hypotheses
  correlated through shared sessions as fewer effective tests), or sign-flip and t p-values that disagree materially.
* **Caution.** The ledger is largely synthetic and several hypotheses share sessions (see the raw-artefact audit in the v1.18 review), so D3(b) has far fewer effective
  tests than its row count; it can reveal gross miscalibration only, which is why its absence of a finding is not stated as evidence of validity.

### D4. Dependence between hypotheses

Equicorrelated hypotheses (a1, rho 0.6) were a reported synthetic sensitivity and also met the rule, so D4 is **not a precondition** here; the
correlation matrix of hypothesis-level session values is summarised for transparency (NOT RUN).

## 3. Regimes remaining outside the validated domain

Serial dependence across sessions (regime c); G < 5 or G > 10; scale ratios or tails beyond b0/b1; K > 16; dependence through shared
sessions across *families* (the study validates one family per dataset); non-mean estimands (the study is the one-sided mean test only);
anything involving selection of the tested hypotheses after viewing the data. The A0 residual-mean false-flag rate is a separate documented
diagnostic and not covered.

## 4. Decision table (proposed, for the reviewer)

| Outcome across D1-D3 | Permitted use of P1 on that real signal family |
|---|---|
| All three "evidence for", with registered diagnostics run on in-sample data under a signed prereg | Exploratory p-values and Holm decisions may be reported **with the stated limits below**; still no real-data error-rate claim |
| Any "evidence against" | P1 not used for that family; no substitute is validated |
| Any "ambiguous", or D1 uninformative (insufficient history) | Report as inconclusive; no claim; collect more in-sample sessions or narrow the question |

Even the first row **does not** close F3 and does not turn the synthetic calibration into a real-data FWER guarantee. Closing F3 or
authorising a real-data claim is the outside reviewer's decision, not an output of these diagnostics.

## 5. What a real-data user could and could not claim

Could say: "Under the assumption of independent sessions within the validated heterogeneity range, the Holm-adjusted Student-t procedure at
alpha_op 0.020 was shown, on synthetic data, to keep FWER below 2.5 % with 95 % confidence at G = 5, 7, 10 (acceptance study, signed prereg);
our in-sample diagnostics D1-D3 (if and when run, under signed preregs) found / did not find evidence against the assumptions."

Could **not** say: that the real-data FWER is controlled or below any number; that sessions are independent; that results are robust to
serial dependence; that a rejection indicates an edge or alpha; that the procedure has any particular power (power results are a separate,
non-gating study, `F3_POWER_DIAGNOSTIC_SPEC.md`); that the procedure applies at other G or to non-mean statistics; or that passing the study
closes F3.

## 6. Questions for the reviewer

1. Is the tolerance |rho1| <= 0.15 for D1 a reasonable pre-declared line, or should it be derived differently (for example from the size
   inflation the pilot measured at phi = 0.3)?
2. If no long enough in-sample session history exists, do you accept "D1 uninformative, P1 not usable for real claims" as the stated outcome rather than an
   alternative design (such as higher-frequency sub-sessions treated as clusters, which would change the unit and need its own study)?
3. Is D3(b) (null-like ledger hypotheses) worth proposing given the ledger's mostly synthetic content, or should D3 be restricted to the sign-flip comparison?
4. Is the decision table in section 4 the right shape, in particular that no row yields a real-data error-rate claim?
