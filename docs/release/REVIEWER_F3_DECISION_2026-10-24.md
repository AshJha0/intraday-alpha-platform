# Outside reviewer decision: F3 diagnostic review (received 2026-10-24)

Transcribed from the text the owner pasted into the session on 2026-10-24. Citation markers that appeared inside the
pasted text ("GitHub", "+1") are omitted; the wording is otherwise unchanged. Subject: intraday-alpha-platform v1.18.0.

---

**Decision:** Accept the diagnostic findings as sufficient to retire A0 as a pass/fail gate, subject to verification of
the committed evidence. Keep F3 OPEN. Do not draft or execute v4 yet.

The distinction is important: the diagnostic provides a credible explanation for why the existing A0 acceptance criterion
is unreliable, but it does not establish that the underlying conditional test is correctly sized.

The reported exact reproduction of the A0 counts (79, 80, 23), the null-matrix simulations, the examination of statistic
centring, and the negative checks for standard-error understatement form a coherent investigation. I would not require
additional exploratory runs of the failed v3 protocol before making the methodological decision.

My responses to the five questions follow.

## 1. Retire A0 as a pass/fail gate?

Yes.

A criterion that rejects an otherwise valid null procedure approximately 28-40% of the time on a single null dataset is
unsuitable as an acceptance gate.

The fundamental problem is the distinction between:
- Repeated randomisations conditional on one observed 7 x 7 matrix.
- Repeated independently generated datasets under a specified null model.

The former estimates a property of the realised matrix and conditional randomisation distribution. It does not, by itself,
estimate the unconditional Type I error rate across independently sampled datasets.

Retain A0 for diagnostics, including conditional rejection counts, derangement-statistic distributions and sensitivity to
session structure. Remove its current count <= 55/1854 criterion from future pass/fail decisions.

This does not mean discarding the observed failure or retroactively changing v3's verdict. Null v3 remains FAILED.

## 2. Should F3 use independent synthetic datasets? Is B acceptable?

Yes to independent synthetic datasets. B is acceptable as one unconditional reference arm, but not as the only arm.

The revised acceptance framework should distinguish at least three null-generating regimes:

| Regime | Purpose | Acceptance role |
|---|---|---|
| Homogeneous independent sessions (B-like) | Verify nominal size under ideal assumptions | Mandatory baseline |
| Heterogeneous session scales and distributions | Examine robustness to session differences | Mandatory if robustness is claimed |
| Serially dependent or common-factor sessions | Examine sensitivity to non-exchangeability | Diagnostic, or mandatory if included in the claimed operating domain |

A homogeneous-session design is useful because it provides a controlled setting where exchangeability assumptions can be
satisfied by construction.

However, passing B alone would support only a narrow claim: calibration under that synthetic null model. It would not
demonstrate unconditional control for heterogeneous market sessions or real trading data.

For each regime, define the null hypothesis, dependence structure, independent replication unit, randomisation procedure
and error-rate estimand before running.

## 3. Is the mild upper-tail excess at G = 7 acceptable?

Not automatically. It needs targeted calibration work if the method is intended to claim strict nominal control.

The reported plain-normal results are:

| One-sided level | Nominal | Observed | Relative size |
|---|---|---|---|
| 0.025 | 2.50% | 2.61% | 1.04x |
| 0.025/3 | 0.833% | 0.97% | 1.16x |
| 0.025/6 | 0.417% | 0.55% | 1.32x |

These are not equally concerning. The difference at 0.025 is small, but the strictest tail is approximately 32% above
nominal. With 20,000 replications, that warrants investigation rather than dismissal as harmless Monte Carlo noise.

I recommend a bounded methodological comparison of the existing CR1 + Webb procedure against CR2/CR3 with appropriate
small-sample corrections, where mathematically applicable. Include exact or more extensively enumerated reference
procedures if feasible.

Do not automatically adopt CR2 or CR3. At seven clusters, their leverage sensitivity, degrees-of-freedom corrections and
numerical stability must be evaluated. The chosen method should be the one that demonstrates adequate calibration under the
declared assumptions, not the one that produces the fewest rejections.

## 4. Required replication count and false-fail probability

I recommend a predeclared two-level acceptance design.

Proposed acceptance specification:

| Item | Proposal |
|---|---|
| Target family-wise error rate | 2.5% |
| Independent datasets per mandatory null regime | 50,000 minimum |
| Monte Carlo acceptance test | One-sided 95% binomial upper confidence bound <= 2.5% |
| Design target for true FWER | <= 2.0%, to provide practical acceptance margin |
| False-fail probability at design target | <= 5% per regime |
| Multiplicity across mandatory regimes | Account for it in the overall acceptance design |

Each independently generated dataset must undergo the complete inference and multiplicity-adjustment procedure.
Individual derangements, hypotheses and Monte Carlo randomisation draws are not independent dataset replications.

The upper-confidence-bound criterion is deliberately conservative: it provides statistical evidence of control rather than
accepting an empirical rate merely because it is below 2.5%.

For multiple mandatory regimes, define whether the 5% false-fail requirement applies separately to each or jointly to the
complete acceptance programme. A joint requirement needs an explicit allocation of the error budget and power calculation.

These are proposed design requirements, not retroactive criteria for v3.

## 5. Is the diagnostic sufficient to stop here?

Yes, for the failure-mechanism investigation. No, for F3 acceptance.

The committed diagnostic review is appropriately labelled exploratory, distinguishes supported explanations from unproven
claims, and identifies the high false-fail probability of the original A0 criterion. The report also explicitly limits its
null-matrix experiment to 40 matrices per model and a single hypothesis, rather than the full Holm family.

I do not require another diagnostic study before retiring the current A0 gate. However, the evidence is not sufficient to
approve the inference procedure for F3.

Before a future F3 protocol is specified, I require three design decisions: define the precise unconditional error-rate
estimand and operating domain; select the candidate small-sample inference procedures to compare; and predeclare the
replication, multiplicity and acceptance criteria.

The unresolved A0 residual-mean false-flag rate (0.2-35%) should remain documented. It must not become a new acceptance
gate without separate calibration.

## 6. REL-08 rule v2

The REL-08 v2 draft should not be signed merely because the F3 diagnostic has explained the previous gate failure.

I support reviewing the unsigned REL-08 v2 draft separately. Its continuation rule must specify whether it relies on the
same CR1 + Webb inference, which assumptions are needed, how multiplicity is handled, and what claims are permitted with
only one session.

A one-session hypothesis screen can support exploratory triage, but it cannot establish robust out-of-sample performance or
day-clustered statistical significance.

## Final written disposition

| Item | Decision |
|---|---|
| F3 diagnostic findings | Accepted as a sufficient exploratory explanation for retiring the A0 gate |
| A0 conditional count pass/fail criterion | Retire; retain as diagnostic only |
| Generator B homogeneous-session design | Accept as baseline, not sole validation |
| CR1 + Webb at G = 7 | Not yet approved for strict nominal-control claims |
| CR2/CR3 investigation | Require bounded comparison before choosing a replacement |
| Drafting v4 immediately | Not authorised |
| Gate F3 | OPEN |
| Real-data FWER-control claim | Not authorised |
| REL-08 v2 signing | Await separate draft review |

This decision does not change the signed v3 protocol or its recorded FAIL result. It authorises no new statistical run, no
replacement protocol, no release-gate closure and no alpha-promotion claim.

The diagnostic phase may close. The next step is methodological specification and independent review, not further post hoc
tuning of the failed study.
