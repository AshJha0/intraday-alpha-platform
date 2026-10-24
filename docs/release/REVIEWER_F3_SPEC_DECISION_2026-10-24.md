# Outside reviewer decision: F3 methodological specification and bounded pilot (received 2026-10-24)

Transcribed from the text the owner pasted into the session on 2026-10-24. The citation marker that appeared inside the
pasted text ("GitHub", after the Q5 paragraph) is omitted; the wording is otherwise unchanged, including the garbled
phrase "G ∈ 10" in answer 4 (read with answer 6 and the specification as G ∈ {5, 7, 10}). Subject:
intraday-alpha-platform v1.18.0 post-release. The reviewed documents are `research/v118/F3_SPEC_DRAFT.md` and
`research/v118/f3_spec/F3_PILOT_RESULTS.md`.

---

Outside reviewer decision — F3 methodological specification and bounded pilot
Disposition: Approved with named changes to the unsigned methodological specification. P1 is acceptable as the preferred candidate for a new, preregistered acceptance study, but is not yet approved as a calibrated production procedure. F3 remains OPEN.
This decision is based on the reported pilot results and specification summary. It is not an independent reproduction of the per-cell JSON, code or committed evidence.

## Responses to the six questions

### 1. Alpha operating level 0.020 with a 0.025 FWER claim
Accept, with precise wording.
Running the complete Holm procedure at an operating family-wise alpha of 0.020 and testing whether its actual FWER is no greater than 0.025 is a defensible conservative design.
However, distinguish:

* `alpha_op = 0.020`: the operational significance threshold.
* `FWER_target = 0.025`: the maximum error rate to be demonstrated.
* `FWER_observed`: the Monte Carlo estimate across independently generated null datasets.
* `UCB95`: the one-sided confidence bound on that error rate.

Passing the acceptance test would establish calibration under the specified synthetic null-generating regimes, not universal FWER control or control on real market data.
The reported calculation that 50,000 replications give a maximum certifiable true FWER of approximately 2.27% at 95% acceptance probability is consistent with the need for a safety margin.

### 2. Accept P1 despite the mechanical pilot selection returning NONE?
Accept P1 as a candidate, but do not retroactively override the pilot result.
The original pilot outcome remains NO ELIGIBLE PROCEDURE.
The reported P1 failure at G = 10, K3 (0.0280 versus 0.0275) is relatively small, but that alone does not justify treating the failed pilot criterion as passed.
P1 has a stronger theoretical justification than the other procedures in the homogeneous, independent, normally distributed session model. With one observation per independent session and an intercept-only mean test, the Student t statistic has an exact t distribution under the corresponding normality assumptions.
That supports selecting P1 for a new prospective protocol, provided the new selection is explicitly attributed to methodological review, not to a reinterpretation of the pilot.
No further pilot is necessary solely to justify selecting P1. Its suitability must instead be evaluated under a newly frozen acceptance design.

### 3. Is conservatism in heterogeneous regimes acceptable?
Yes for Type I error control, but report power separately.
Conservative rejection rates are acceptable when the mandatory criterion concerns false-positive control. Nevertheless, extreme conservatism can make a procedure scientifically uninformative.
I recommend a separate, predeclared power diagnostic using specified non-null effect sizes and session-scale configurations. Report power, minimum detectable effects and differences between homogeneous and heterogeneous regimes.
I would not introduce a power floor as an unplanned F3 pass/fail criterion. If practical power is required for deployment, define it as a separate gate with its own predeclared operating assumptions and acceptance rule.

### 4. Confirm G = 5, 7, 10 and exclude regime c?
Confirmed, with a limitation on the operating domain.
Use G ∈ 10 as mandatory cluster counts for the synthetic acceptance programme.
Serially dependent regime c should remain outside the claimed domain because the pilot indicates severe size inflation. Its exclusion must be explicit in the specification and in any release claims.
In particular, the method must not be applied to real session data under an assumption of independence unless that assumption is separately justified. Excluding serial dependence from synthetic acceptance tests does not demonstrate that real sessions are independent.

### 5. Confirm per-cell false-fail allocation
Accept the per-cell approach, with two corrections.
First, the 5% false-fail budget should apply to the joint mandatory acceptance programme, not merely to an isolated cell.
Second, the specification must account for all mandatory decisions, including K3, K6 and K16 within each regime–G cell. Those family outcomes share simulated datasets, so they are correlated; independence must not be assumed.
A union-bound calculation is acceptable and does not require independence.
I accept the proposed design provided the final preregistration contains an exact or conservative upper bound on the probability that any mandatory criterion fails when its true FWER is at the designated design target.
The draft's stated extremely small false-fail probability at a true 2.0% FWER provides ample margin, assuming the calculations cover all mandatory comparisons.

### 6. Is regime a1 acceptable as sensitivity only?
Yes.
Regime a1, with equicorrelated hypotheses at \(\rho=0.6\), is appropriate as a sensitivity analysis.
The primary acceptance programme can use a0, b0 and b1 across G = 5, 7 and 10, giving nine mandatory regime–G cells, each evaluated at K3, K6 and K16.
This is acceptable because Holm's strong FWER control does not require independence between valid individual p-values. However, the pilot does not establish that those p-values remain valid under every dependence structure.
A1 must be reported even if it is not a mandatory acceptance criterion.

## Named changes required before freezing the acceptance specification

1. Preserve the pilot verdict NO ELIGIBLE PROCEDURE, and explicitly document the reviewer-directed prospective selection of P1.
2. Define `alpha_op = 0.020` and `FWER_target = 0.025` separately, including the exact Clopper–Pearson acceptance rule.
3. Establish a joint false-fail bound covering all 27 mandatory family evaluations.
4. Add a separate power diagnostic without changing the F3 Type I error acceptance criteria.
5. State that serial dependence is outside the validated operating domain, and that no real-data independence or FWER claim follows from synthetic validation.
6. Freeze the final generator definitions, seeds, code/configuration hashes, environment and signing/runner separation before any acceptance run.

## Final disposition

Approved with named changes for prospective specification and preregistration preparation.
P1 is approved as the candidate procedure to be evaluated, not as a statistically validated replacement. The acceptance study may proceed only after the named changes are incorporated, the final protocol receives separate signature approval, and the owner completes the required local signing process.
F3 remains OPEN. Null v3 remains FAILED. No v4 has been approved, no acceptance run is authorised by this decision, and no real-data FWER-control claim is permitted.
