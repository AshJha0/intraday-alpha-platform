# Outside reviewer decision: F3 acceptance study, post-execution decision (received 2026-10-24)

Transcribed from the text the owner pasted into the session on 2026-10-24; citation markers omitted, wording unchanged.
Subject: intraday-alpha-platform v1.18.0, PR #346.

---

**Decision: Accept the reported statistical PASS provisionally, subject to independent verification of the committed result artifacts. F3 remains OPEN.**

The reported result satisfies the frozen acceptance criterion: all 27 mandatory evaluations have one-sided 95% upper confidence bounds below the 2.5% target. The nine a1 sensitivity evaluations also pass, although they correctly remain outside the mandatory verdict.

The evidence supports a narrower conclusion than general validation of the inference procedure: P1, Student t(G-1) with Holm at an operating alpha of 0.020, has passed the specified synthetic-null calibration programme for G = 5, 7 and 10.

It does not establish calibration on real market data, under serial dependence, or outside the declared synthetic operating domain.

## 1. Execution integrity and the refused first launch

I accept the distinction between the refused launch and the completed acceptance run, subject to the recorded log confirming the sequence.

The 16:28 attempt exited on the predeclared minimum-free-RAM rule before producing any study observations, cell artifacts or final decision. Closing other applications to satisfy that environmental prerequisite does not constitute statistical tuning or a second acceptance attempt.

The 16:31 execution is therefore appropriately described as the single completed acceptance run.

Two audit limitations should remain visible:
- The first preflight evidence file was overwritten. Both preflights are reported as passing, but only the second is retained as a standalone artifact.
- The maintainer's private-key file remained on the same machine. Separate process identity and absent signing-key environment variables are useful controls, but they do not independently prove that the runner could not access that file.

Neither limitation, as described, invalidates the statistical result. They should remain documented rather than being represented as stronger separation guarantees than were demonstrated.

## 2. Statistical result

| Evaluation | Reported result | Reviewer decision |
|---|---|---|
| 27 mandatory evaluations | All passed | Accept provisionally |
| Largest mandatory observed FWER | 2.026% | Below 2.5% target |
| Largest mandatory UCB95 | 2.133% | Below 2.5% target |
| Nine a1 sensitivity evaluations | All passed | Report separately |
| Protocol modifications | None reported | Accept |
| Interim looks or tuning | None reported | Accept |
| Real-data FWER control | Not tested | Not established |

The reported maximum upper confidence bound provides a margin of approximately 0.367 percentage points below the specified target.

This is meaningful evidence of calibration under the tested synthetic null distributions. However, the confidence bounds are individual bounds for the specified evaluations, not a universal guarantee across all possible data-generating processes.

## 3. Final post-execution disposition

**Approved: the F3 acceptance-study result is accepted as a PASS under the signed synthetic-only protocol.**

PR #346 is now merged into main, and the committed REPORT.md confirms that all 27 mandatory evaluations meet the frozen acceptance rule. The nine sensitivity evaluations are reported separately and do not influence the verdict.

This approval is based on review of the published result and execution record, not an independent computational rerun or cryptographic re-verification.

### Permitted claims

The project may state:

> The preregistered F3 acceptance study passed all 27 mandatory synthetic-null calibration evaluations for P1 (Student t with G-1 degrees of freedom and Holm correction at operating alpha 0.020), using 50,000 independent synthetic datasets per regime-cluster-count cell. The result supports FWER calibration below the 2.5% acceptance target under the specified synthetic null-generating models.

It must not claim that this establishes real-market-data FWER control, serial-dependence robustness, adequate statistical power, or alpha profitability.

### What I require next

1. Power diagnostic: Prepare a separate, non-gating specification for power across prespecified effect sizes, G values and heterogeneous-session configurations. Do not modify the completed acceptance result.
2. Operating-domain assessment: Document how independence, session heterogeneity and the validity of individual p-values would be assessed before using P1 on real data.
3. No further CR2/CR3 comparison is required for this synthetic acceptance decision. Reconsider alternative inference procedures only if the intended operating domain expands beyond the assumptions supported by P1.

### Gate decision

| Item | Decision |
|---|---|
| Signed F3 synthetic acceptance study | PASS accepted |
| P1 synthetic-null calibration | Accepted within the declared domain |
| Original null v3 | FAILED, unchanged |
| F3 programme gate | OPEN |
| Real-data FWER-control claim | Not authorised |
| Power diagnostic | Next separate workstream |
| Further acceptance rerun | Not required or authorised |

The statistical acceptance phase can close with a recorded PASS. The broader F3 gate remains open pending the operating-domain and power assessments and a separate release-gate decision.
