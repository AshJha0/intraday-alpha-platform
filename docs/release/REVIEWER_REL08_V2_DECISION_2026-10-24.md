# Outside reviewer decision: REL-08 continuation rule v2 (received 2026-10-24)

Transcribed from the text the owner pasted into the session on 2026-10-24. No citation markers appeared in the text; the
wording is unchanged (line breaks added between sections and list items only). Subject: intraday-alpha-platform v1.18.0,
PR #342 (unsigned draft, merged as a draft only; nothing signed, nothing run on real data).

---

Outside reviewer decision - REL-08 continuation rule v2. 24 October 2026 | PR #342 | Unsigned draft.

Decision: Approved with named changes for further development, but NOT approved for signature. The proposed C1 correction is conceptually justified. The changes to the book optimiser are reasonable candidates for a synthetic-only calibration, but the supplied evidence does not establish that they are appropriate for real execution. More importantly, the unchanged inference method, the absence of an implemented v2 runner, and the limited null-calibration evidence prevent signature approval. The original v1 rule and its historical results must remain unchanged. The revised rule must not be presented as a correction that retroactively validates the previous screen.

## 1. Review of the proposed changes

- C1: gaps between usable rebalances within each day - Accept in principle.
- Five-of-six session coverage - Accept, with precise definition required.
- alpha_bps: 5 -> 20 - Provisional synthetic setting only.
- risk_aversion: 1000 -> 100 - Provisional synthetic setting only.
- Six in-sample session dates - Accept as a proposed evaluation set, not as validation data.
- K = 7, alpha = 0.05 - Retain as declared, subject to inference review.
- No promotion from screen - Required and accepted.

The C1 change fixes a genuine measurement problem: time before the first usable rebalance should not be treated as a gap between consecutive usable rebalances. However, a session with only one usable rebalance has no observable inter-rebalance gap. It must not pass C1 vacuously. Define a minimum number of usable rebalances per qualifying day, and explicitly specify whether the five-of-six rule applies to coverage, C1, or both.

The two optimiser parameters must be treated as frozen experimental settings rather than calibrated market parameters. The unexplained V1/V5 trade-size disparity is material and should be investigated before any interpretation of C2 or C3 as meaningful economic evidence.

## 2. Inference - principal blocker

I do not approve retaining the moving-block bootstrap without further methodological justification. The fact that REL-08 uses a different procedure from F3 does not automatically make it invalid. It does mean that the F3 synthetic acceptance PASS provides no calibration evidence for REL-08's bootstrap. The following issues require resolution:

Six sessions are insufficient for a credible general claim of day-level independence or asymptotic cluster inference.

Allowing bootstrap blocks to cross session boundaries can create artificial dependence across distinct trading days.

A Bonferroni correction across seven hypotheses controls family-wise error only when the underlying individual p-values or confidence bounds are valid. A lower confidence bound at 1-0.05/7 is not, by itself, evidence that the underlying bootstrap achieves the stated coverage.

I recommend a separate synthetic-null calibration of the exact REL-08 continuation decision, including the bootstrap, multiplicity correction, coverage requirement and C1/C2/C3 conjunction. This must be specified prospectively, before examining new calibration outcomes. It need not be a large programme, but it must establish whether the complete screen has an acceptable false-continuation rate under the declared null model.

## 3. Named changes required before signature

1. Implement the v2 rule in production code. The current python/src/iap implementation still uses the v1 gap calculation. Implement the revised within-session gap, the minimum-rebalance rule, the five-of-six coverage check, and a six-session driver. Add tests for missing days, single-rebalance days, session boundaries and incomplete features.
2. Prevent cross-session bootstrap blocks. Resample within sessions, preserving intraday dependence without constructing artificial blocks across overnight boundaries. Specify whether sessions are resampled independently or the six sessions are treated as fixed. Changing the resampling method requires fresh calibration; do not claim it is statistically equivalent to v1.
3. Prospectively calibrate the complete continuation decision. Freeze a synthetic-null test plan covering the exact bootstrap, K = 7 multiplicity, C1-C4 decision logic and six-session structure. Report both per-variant and family-level false-continuation rates with uncertainty. The existing 0/24 screens are insufficient.
4. Clarify C1 sufficiency. Require at least two usable rebalances per qualifying session, and define how missing sessions affect the five-of-six coverage criterion. A day with one rebalance must not contribute a zero gap that makes C1 appear satisfied.
5. Investigate optimiser behaviour. The dry-run report attributes non-monotone turnover to the solver's step size and fallback-to-hold behaviour. Add deterministic tests of solver convergence, turnover caps, objective improvement and sensitivity to iteration count. Document the unresolved real V1/V5 trade-size difference.
6. Freeze the implementation and evidence before signing. Bind the revised rule, JSON, implementation, driver, dependency environment, tests and calibration evidence to an exact commit. Preserve signer/runner separation and the holdout guard. No real session should be read before the required protocol is signed.

## 4. Final disposition

- C1 conceptual correction - Approved.
- Five-of-six session coverage - Approved with clarification.
- Book parameters 20 / 100 - Provisionally accepted for testing.
- Existing moving-block bootstrap - Not approved for v2 inference without calibration.
- Existing 24-screen null check - Insufficient.
- v2 signature - Not approved.
- v2 real-data execution - Not authorised.
- Alpha promotion - Not authorised.
- F3 - OPEN.

The revised rule is a reasonable development direction, but signature approval requires the implemented rule and a defensible inference-calibration package. The next submission should contain the implementation PR, a prospective synthetic calibration plan, and the associated verification evidence. It should not contain real-data results from the six sessions. No additional threshold tuning or real-data exploration is authorised by this decision.
