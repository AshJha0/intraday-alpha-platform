# Outside reviewer decision: REL-08 v2 calibration, revision 3 (received 2026-10-24)

Transcribed from the text the owner pasted into the session on 2026-10-24. No citation markers appeared in the text; the
wording is unchanged (line breaks added between sections and list items only). Subject: intraday-alpha-platform v1.18.0, revision 3 of the REL-08 v2
calibration plan (unsigned, freeze-ready).

---

Outside reviewer decision - REL-08 v2 calibration, revision 3. 24 October 2026 | Unsigned freeze-ready plan | Pre-execution review.

Decision: Approved with named conditions for freeze and execution preparation. Not yet authorised to launch the 1,750-screen calibration. Revision 3 addresses the five changes requested in my previous review. In particular, adopting Option B with a fixed 200-replication final sample removes the optional-stopping concern, and retaining all six mandatory null cells preserves the intended scope of the calibration. I do not require another statistical redesign.

## Assessment of the revised plan

- Six mandatory null cells, including N3 - Approved.
- Fixed R = 200 for each mandatory cell - Approved.
- Non-decisional checkpoint at R = 100 - Approved.
- Automatic continuation regardless of checkpoint - Required and accepted.
- N4: 400 null screens - Approved as non-gating.
- Power: 150 screens - Approved as non-gating.
- Total: 1,750 screens - Reconciled.
- Power seed relocation - Approved, subject to disjointness verification.
- Short-circuit equivalence - Satisfactory evidence as reported.
- Three-way evidence/claim separation - Approved.
- Stopped first launch disclosure - Accepted.

The revised screen accounting is consistent: 6(200)+400+150 = 1,750. The fixed-sample policy is now appropriate. The checkpoint at 100 must be operational only: it may record progress, resource usage and integrity status, but must not expose interim false-continuation counts, confidence bounds or PASS/FAIL assessments.

## Named conditions before execution

1. Freeze the exact implementation and plan. Record the commit, manifest SHA-256, calibration-runner hash, dependency identity, null definitions, replication counts, seeds and final decision rule. Verify that the frozen implementation is the one used for execution.
2. Verify seed disjointness. Confirm that the relocated power seeds beginning at 20268500 do not overlap any mandatory, N4, diagnostic or previously consumed calibration streams. The original null-cell seeds must remain unchanged.
3. Preserve the fixed-sample design. All six mandatory cells must reach 200 valid replications, regardless of checkpoint observations. A statistical failure at 100 cannot terminate the study. Resource or integrity failures may interrupt execution only under a predefined, reproducible resume policy.
4. Define finality and checkpoint recovery. Freeze how partial outputs are verified, how interruptions resume without reseeding or duplicate observations, and what constitutes a completed or void study. No discretionary reruns after a completed verdict.
5. Record the inferential decision exactly. The machine-readable result must distinguish the preregistered gate from the six Bonferroni-adjusted confidence bounds supplied for information. Do not replace the original gate with a stricter or looser criterion after observing results.
6. Retain the execution audit trail. Record the previous two-replication aborted attempt, execution identity, environment, checkpoints, final counts and all 1,750 outcomes. The prior discarded replications must not enter the new calibration results.

These are freeze and execution-integrity requirements, not requests to change the statistical design.

## Important statistical limitation

Even if all six mandatory cells pass, the result will establish only that the REL-08 v2 continuation decision satisfied its specified synthetic calibration screen. It will not demonstrate nominal 5% family-wise error control. In particular, the continuation rule is a composite decision involving economic, execution and statistical criteria, and a low false-continuation rate can arise because the screen rarely permits trading - not because the underlying inference is well calibrated. The report should therefore separately disclose the frequency of passing each criterion and the complete continuation decision, wherever the frozen protocol provides those measurements.

## Final disposition

- Revision 3 statistical design - Approved.
- Option B, R = 200 - Approved.
- Non-decisional checkpoint - Approved.
- Six mandatory null cells - Approved.
- Manifest and revised power seeds - Approved subject to verification.
- Short-circuit optimisation - Accepted as reported.
- Freeze preparation - Authorised.
- Calibration launch - Pending final freeze verification.
- REL-08 v2 signature - Not authorised.
- Real-data execution - Not authorised.
- F3 - OPEN.

Next step: Complete the six execution-integrity conditions, produce a frozen execution manifest and return its identity and verification evidence for final launch authorisation. No additional synthetic calibration pilot or methodological redesign is required. This decision is based on the supplied cover note; I have not independently inspected or executed the repository implementation.
