# Outside reviewer decision: F3 power-diagnostic freeze package (received 2026-10-24)

Transcribed from the text the owner pasted into the session on 2026-10-24 and relayed by the coordinator; no citation markers appeared in the
text. The text arrived as one continuous paragraph; line breaks and headings were added only to separate the numbered parts, the wording is
otherwise unchanged. Subject: intraday-alpha-platform v1.18.0 post-release; reviewed package: `research/v118/f3_power/` (PR 353).

---

Outside reviewer decision - F3 power-diagnostic freeze package.
24 October 2026 | intraday-alpha-platform v1.18.0 | Unsigned freeze package.

Decision: Approved with named changes for signature preparation.

The proposed freeze package addresses the four methodological requirements from my previous review. I accept the non-gating design, subject to the clarifications below. This is a review of the supplied evidence summary, not an independent execution of the repository tests.

1. Effect definition - approved. Your interpretation is correct. The earlier wording was inconsistent. The authoritative definition for this study is: mu = d * sigma_RMS / sqrt(G), where sigma_RMS = sqrt((1/G) * sum_{g=1..G} sigma_g^2). Here, d is the noncentrality-scale effect, not the raw session-standardised mean shift. The alternative expression without 1/sqrt(G) should not be used. No change to the implemented formula is required.

2. Analytic integrity band and VOID handling - approved with one clarification. The proposed a0 integrity band, pi_ref - tau <= pi_hat <= min(1, pi_ref + excess) + tau, tau = 0.025, is acceptable as a broad programming-integrity check, provided excess is fixed in the declaration and justified by the intended analytic comparison. It is not a statistical acceptance test or evidence of calibration. VOID_INTEGRITY is appropriate when the check fails. The runner must preserve the failure evidence, suppress inferential estimates, and prohibit rerunning under the same signed preregistration. Named clarification: Do not describe tau = 0.025 as five times the worst-case Monte Carlo standard error without qualification. For a binomial estimate with n = 10,000, the worst-case standard error is 0.005, so the statement is numerically correct, but it is a heuristic tolerance rather than a simultaneous confidence guarantee across the grid.

3. Ratio threshold and bootstrap uncertainty - approved. The minimum of 100 relevant observations for reporting a ratio is an acceptable numerical-stability rule for this exploratory study, provided the report makes clear which counts must exceed 100. The paired bootstrap with 1,000 resamples is adequate for approximate, non-gating uncertainty assessment. The following restrictions should remain: ratios must be labelled undefined where denominator estimates are zero or the frozen eligibility conditions fail; MDE80 ratios must not be calculated from extrapolated or nonmonotone curves; boundary results must retain their censored labels, such as MDE80 > 16; bootstrap intervals must respect common random numbers and the pairing of datasets across effect sizes. Do not interpret a ratio passing the count threshold as evidence of economically meaningful power.

4. Signature readiness - approved with two named documentation checks. The package may proceed to local signature preparation after confirming: 1. The frozen declaration and protocol use the corrected mu = d*sigma_RMS/sqrt(G) definition consistently, and the analytic integrity band's excess term is fully specified. 2. The signed identity binds the imported acceptance-study core.py by its exact hash, alongside the new power-study code, declaration, protocol and dependency environment. These checks do not require changing the statistical design, seeds, grid, sample count, bootstrap procedure or acceptance-study artifacts.

## Final disposition

Effect definition - Approved. Extended effect grid - Approved. 10,000 datasets and CRN - Approved. Analytic integrity band - Approved with clarification. VOID_INTEGRITY finality - Approved. Ratio threshold >=100 - Approved. Paired bootstrap B=1,000 - Approved. Freeze-package signature preparation - Approved with two documentation checks. Power-study execution - Not authorised. F3 acceptance result - 27/27 PASS, unchanged. F3 programme gate - OPEN. Null v3 - FAILED, unchanged. No additional pilot or methodological redesign is requested. Once the two documentation checks are satisfied, the owner may sign the frozen protocol locally and submit the signed artifact for the agreed pre-execution review. This decision does not authorise executing the power diagnostic, accessing real data, closing F3, or claiming real-data FWER control.
