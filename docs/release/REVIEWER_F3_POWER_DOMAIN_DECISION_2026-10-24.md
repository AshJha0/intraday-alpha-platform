# Outside reviewer decision: F3 power diagnostic and operating-domain assessment (received 2026-10-24)

Transcribed from the text the owner pasted into the session on 2026-10-24 and relayed by the coordinator; no citation markers appeared in the
text. The text arrived as one continuous paragraph; line breaks and headings were added only to separate the numbered parts, the wording is
otherwise unchanged. Subject: intraday-alpha-platform v1.18.0 post-release; reviewed drafts: `research/v118/F3_POWER_DIAGNOSTIC_SPEC.md`
and `research/v118/F3_OPERATING_DOMAIN_ASSESSMENT.md` (PR 348).

---

Outside reviewer - F3 power diagnostic and operating-domain assessment.
24 October 2026 | intraday-alpha-platform v1.18.0 | Two unsigned drafts.

Decision: Approve the power-study specification with named changes for signature preparation. Approve the operating-domain assessment as a planning document, but do not authorise real-data diagnostics yet.

The two workstreams are appropriately separated from the completed F3 acceptance study. The reported 27/27 synthetic acceptance PASS remains unchanged, null v3 remains FAILED, and F3 remains OPEN. The power diagnostic should answer how useful P1 is under the stated synthetic models, not reopen its Type I error acceptance verdict. The operating-domain assessment should determine what can be learned about real-session assumptions without treating weak diagnostics as proof of independence.

## 1. Power diagnostic - answers to questions 1-4

1. Extended effect-size grid to d = 16: Approved. The original upper limit of d = 3 would make the proposed MDE80 calculation uninformative if no configuration could reach 80% power. Extending the grid to 16 is justified by the reported noncentral-t calculations. However, the final specification should clearly distinguish: a standardized planted effect size of d; empirically estimated power under the synthetic generator; MDE80, defined as the smallest effect size reaching 80% power under the specified interpolation rule. Do not extrapolate beyond the grid. If a cell still fails to reach 80% at d = 16, report MDE80 > 16 rather than inventing an estimate.

2. Planted shift using root-mean-square session scale: Approved as the primary definition. For heterogeneous sessions, a fixed planted mean shift based on the root-mean-square session standard deviation is preferable to scaling each session's shift individually. A constant shift preserves a common mean-effect estimand across sessions. A session-specific shift would introduce heterogeneity into the effect itself, potentially confounding the interpretation of power. Use: Delta = d * sqrt((1/G) * sum_{g=1..G} sigma_g^2) where sigma_g are the declared generator session scales. A median-scale sensitivity analysis is optional and non-gating. It is not required for signature approval.

3. Ten thousand datasets per stream cell: Approved for a non-gating diagnostic. At 10,000 independent datasets, the maximum approximate 95% binomial half-width is 0.98 percentage points. This is sufficient to characterise broad power differences, but not fine distinctions near 80%. Report Wilson intervals for every power estimate. For MDE80, include an uncertainty indication derived from the confidence intervals or a predeclared bootstrap over independent dataset replications. Do not present interpolated MDE80 values with unjustified precision.

4. Common random numbers across effect sizes: Keep them. Common random numbers can reduce variance when comparing different effect sizes within the same regime-G-K configuration. They are appropriate provided: the same underlying null datasets are reused across effect sizes according to a frozen mapping; independent dataset replications remain independent; the shared-randomness structure is disclosed; uncertainty calculations for differences in power respect the paired design. Do not treat power estimates at different effect sizes as statistically independent.

## 2. Operating-domain assessment - answers to questions 5-8

5. Is |rho1| <= 0.15 a reasonable tolerance? Accept provisionally as a screening threshold, not as proof of independence. The threshold is a reasonable conservative starting point, given the pilot's severe FWER inflation at phi=0.3. However, the current D1 wording needs refinement: the pooled point estimate being below 0.15 is insufficient. Require a confidence interval entirely contained within [-0.15,0.15] to classify the diagnostic as supportive. A confidence interval crossing either boundary must be inconclusive, unless it establishes a clear violation. Also distinguish serial dependence in the mean from serial dependence in volatility, squared residuals and common event exposures. Near-zero lag-1 correlation does not establish independence.

6. What if there is no long in-sample history? Accept the proposed outcome: D1 uninformative; P1 not usable for real-data inference claims. Do not lower the required evidential standard because fewer sessions are available. A short history can support descriptive analysis, but it cannot establish that the independence assumptions underlying the synthetic calibration are plausible. The proposed requirement of approximately 40 or more sessions is a useful planning heuristic, not a guarantee of adequate diagnostic power. Actual resolution must be assessed from the sample size, dependence structure and confidence interval.

7. Should D3(b), null-like ledger hypotheses, remain? Retain as optional exploratory evidence, not a prerequisite or validation criterion. The ledger is predominantly synthetic, and many hypotheses share observations. Consequently, the effective number of independent null hypotheses is substantially smaller than the number of ledger rows. D3(b) can expose obvious anomalies but cannot establish calibration of real-data p-values. For a future preregistration, prioritise D3(a), with its assumptions explicitly stated. In particular, sign-flip inference requires an appropriate symmetry or invariance condition; destroying a signal does not automatically produce a valid null distribution.

8. Is the decision table appropriate? Yes, with one clarification. The table's conservative three-way classification is appropriate: All required diagnostics supportive -> Exploratory inference with explicit assumptions and limitations; no real-data FWER guarantee. Any diagnostic adverse -> P1 unsupported for that signal family. Any diagnostic inconclusive -> No inferential claim; additional evidence required. The supportive outcome should not be described as validation of independence or of the full real-data operating domain.

## 3. Required changes before proceeding

Power diagnostic - signature preparation approved with conditions. The power specification may proceed to freeze-package preparation once the following are incorporated:

1. Clarify that the effect definition is mu = d * sigma_RMS / sqrt(G), as specified in the actual document. Here d is the noncentrality-scale effect, not a raw per-session standardised shift.
2. Retain the extended grid through 16, fixed 10,000 datasets, common random numbers and the non-gating interpretation.
3. Make the analytic cross-check a programming-integrity diagnostic, with an explicit tolerance and failure-handling rule, rather than an informal comparison with a Wilson interval.
4. Freeze the MDE interpolation and uncertainty calculation, including handling of nonmonotone empirical curves, boundary crossings and undefined heterogeneous-to-homogeneous ratios.

Operating-domain assessment - approved as a planning document, with changes. The operating-domain assessment should be updated to distinguish evidence of low autocorrelation from independence, retain D1 as a three-way decision, make D3(b) optional, and explicitly state the assumptions needed for D3(a). I do not recommend preregistering or running D1-D3 yet. First establish whether a sufficiently long, genuinely in-sample history of comparable session-level observations exists. That inventory can be performed from metadata without opening holdout data or examining statistical results.

## Final written disposition

F3 power diagnostic - Approved with named changes for freeze/signature preparation. Extended effect grid to 16 - Approved. RMS-scale planted effect - Approved. 10,000 datasets per cell - Approved. Common random numbers - Approved. Operating-domain assessment - Approved as planning document, with named clarifications. Real-data diagnostic preregistration - Not yet authorised. F3 gate - OPEN. Null v3 - FAILED, unchanged. Synthetic acceptance - 27/27 PASS, unchanged. No power-study execution, real-data diagnostic, holdout access, new acceptance run or real-data FWER-control claim is authorised by this decision.
