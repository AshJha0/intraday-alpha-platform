# Outside reviewer decision: REL-08 v2 calibration-plan amendment (received 2026-10-24)

Transcribed from the text the owner pasted into the session on 2026-10-24. No citation markers appeared in the text; the
wording is unchanged (line breaks added between sections only). Subject: intraday-alpha-platform v1.18.0, PR #358 (amendment
proposal, unsigned).

---

Outside reviewer decision - REL-08 v2 calibration-plan amendment. 24 October 2026 | Revision 2 | PR #358 | Unsigned.

Decision: Approve with named changes - Option B remains the preferred confirmatory design; Option A may be used only as a separately labelled preliminary calibration stage. I accept the runtime problem as genuine. However, reducing the number of null replications changes the statistical strength of the gate, even when the null models, seeds, thresholds and decision under test remain unchanged. The distinction is important: a shorter study may provide useful engineering and statistical evidence, but it should not silently inherit the evidential status of the original plan.

## 1. Assessment of the four options

A - 100 gating replications: Lower cost, substantially weaker resolution and reduced probability of passing when the true false-continuation rate is around 1% - Accept for preliminary screening only.

B - 200 gating replications: Stronger resolution and more reliable confirmation - Preferred confirmatory design.

C - 100 followed by 200: Efficient if continuation is governed by a prospective, statistically valid rule - Conditionally acceptable.

D - remove N3: Excludes an important dependence-related null - Reject.

I would not remove N3. Dependence in outcomes is precisely the kind of null configuration that can expose weaknesses in the continuation rule's bootstrap inference. The proposed runtime optimisation that skips book calculations only after the decision is mathematically settled is acceptable if equivalence has been demonstrated across the full screen, including borderline cases.

## 2. Statistical conditions for an amended plan

The main concern is not whether 100 replications can produce a PASS. It is whether that PASS is sufficiently informative for the purpose of the gate. Under the proposed rule, a cell with one false continuation in 100 screens has an approximately 4.66% one-sided 95% upper bound. That satisfies a 5% threshold, but only narrowly. With six mandatory cells, individual 95% bounds are also not a simultaneous 95% guarantee across all cells.

The final plan must explicitly distinguish the following:

Observed calibration: the false-continuation counts and confidence bounds for each null cell.

Decision-level evidence: whether the six-cell screening family satisfies its prospectively declared rule.

Inferential limitations: no claim of nominal 5% FWER control, no day-level generalisation, and no demonstrated trading edge.

Option C requires special care: I would approve a staged design only if the continuation rule is frozen before any calibration results are inspected. For example, a defensible approach is:

1. Run the first 100 replications for each mandatory cell as a non-decisional checkpoint.
2. Continue automatically to 200 replications per mandatory cell, regardless of the interim results.
3. Make the confirmatory decision only from the completed 200-replication dataset.

This preserves the originally planned fixed-sample decision. I would not approve an arrangement in which the study stops and declares PASS at 100 when favourable, but extends to 200 when unfavourable, while applying ordinary fixed-sample confidence bounds. Such optional continuation changes the operating characteristics and requires its own prospectively calibrated sequential decision rule.

## 3. The stopped launch

The reported two-replication launch is not automatically a statistical look. I accept its classification as a runtime-aborted attempt if the preserved log demonstrates that no calibration outcomes were inspected, the partial results were not used for tuning, and the restarted study will use the frozen calibration seeds. However, the final audit record should not state simply that nothing was run. It should disclose that two replications began, were terminated, and were discarded unread. The timing benchmark using seeds outside the calibration range is appropriate for estimating computational cost.

## 4. Named changes before the next review

The next plan revision should:

1. Preserve all six mandatory null cells, including N3, and the complete decision rule.
2. Choose one confirmatory sample-size and stopping policy before execution. My preference is Option B, or the non-decisional staged implementation of C described above.
3. Provide a machine-readable accounting of all null cells, replication counts, seeds, sensitivity cells and power-study cells. Reconcile the stated screen totals with the actual run manifest.
4. Retain and document the decision-equivalence tests for the short-circuit optimisation, including threshold-boundary and solver-fallback cases.
5. Separate the prospective calibration protocol from the still-incomplete signer, rule loader, real-data loader and ledger controls. No real-data execution is authorised.

The runtime constraint is legitimate, but approximately 60 hours of computation is not, by itself, a sufficient statistical reason to weaken a confirmatory gate.

## Final disposition

- Runtime evidence - Accepted as reported.
- Decision-neutral short-circuit - Accept, subject to equivalence evidence.
- Option A - Preliminary diagnostic only.
- Option B - Preferred for confirmation.
- Option C - Accept only with fixed final sample or separately calibrated sequential rule.
- Option D - Rejected.
- Calibration execution - Not yet authorised.
- REL-08 v2 signature - Not authorised.
- Real-data execution or signal promotion - Not authorised.
- F3 - OPEN.

Recommended next action: revise PR #358 to retain the 200-replication confirmatory gate, with an optional non-decisional checkpoint at 100, and return the amended freeze-ready calibration plan for review. This decision does not reopen REL-08 v1, alter the F3 synthetic acceptance result, or authorise any real-data inference claim.
