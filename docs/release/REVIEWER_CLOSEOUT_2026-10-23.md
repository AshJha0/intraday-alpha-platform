# Outside reviewer assessment: v1.18 close-out (received 2026-10-23)

Transcribed from the text the owner pasted into the session on 2026-10-23. The source-link labels that
appeared inside the pasted text (the word "GitHub" and "+1" after sentences) are citation markers from the
reviewer's tooling and are omitted; the wording is otherwise unchanged. The reviewer did not rerun the
benchmarks (see "Reproduction status").

---

The new null v3 result changes the decision materially. The known-null method has not demonstrated the
required 2.5% false-rejection control, and gate F3 must remain open. The recorded study reports 79/1,854 and
80/1,854 conditional rejections for A0 in S2 at K3 and K6 respectively, approximately 4.3% in each cell,
versus the 2.5% target. Generator B's corresponding rates are 2.50% and 2.45%. The loss gate was not
triggered. These are recorded as failed results, without parameter tuning.

My provisional statistical interpretation is that the A0 excess is evidence of a calibration problem in the
tested configuration, but it does not yet establish whether the cause is the conditional test at small K, an
instrument-sizing defect, or an interaction with the generator and dependence structure. The distinction
matters: a failed empirical calibration does not by itself identify the defective component.

The appropriate immediate release position is to keep F3 open, make no claim of real-data family-wise
error-rate control, and avoid drafting or running a replacement protocol until the failure mechanism has been
investigated under a separately specified review plan.

## 1. B8 v2 result: reproduced from the committed report, not independently rerun

The committed B8 v2 report records six policy-symbol comparisons against pinned Nasdaq, evaluated over three
sessions. All six have the verdict no_evidence (underpowered). None establishes that the alternative routing
policy is better. The inference has only three day clusters and two degrees of freedom, with very wide
family-adjusted intervals.

| Policy | Symbol | Cost difference, bps | Family-adjusted 95% CI, bps | Verdict |
|---|---|---|---|---|
| pin_iex | AAPL | -0.78 | [-2.25, 0.68] | No evidence |
| pin_iex | MSFT | -7.55 | [-57.46, 42.37] | No evidence |
| sor_sweep | AAPL | -0.18 | [-5.70, 5.34] | No evidence |
| sor_sweep | MSFT | +1.71 | [-18.50, 21.92] | No evidence |
| sor | AAPL | -0.57 | [-6.50, 5.37] | No evidence |
| sor | MSFT | -0.67 | [-12.49, 11.15] | No evidence |

Source: committed B8 v2 report. Negative cost differences favour the alternative policy; none of the reported
intervals excludes zero.

Reproduction limitation: I inspected the committed report and its JSON artifact, but I have not independently
rerun the benchmark. The repository's review instructions require the original ITCH and IEX session data, as
well as the signing-commit checkout. A report read from GitHub is not equivalent to a clean reproduction of
the JSON.

## 2. Alpha-track scorecard: REL-08, 30 January 2019

The committed scorecard and JSON give a consistent, negative/inconclusive result for the seven registered
variants. Every variant is marked underpowered; none permits promotion.

| Variant | Mean rank IC | Net P&L (bps) | Verdict |
|---|---|---|---|
| V1, VWAP deviation, market-neutral | 0.0401 | -0.02 | Underpowered |
| V2, VWAP deviation, sector-neutral | 0.0401 | -0.01 | Underpowered |
| V5, Residual reversion, market-neutral | 0.0359 | -9.47 | Underpowered |
| V6, Residual reversion, sector-neutral | 0.0359 | -1.24 | Underpowered |
| V9, ETF catch-up, 1-minute horizon | 0.0046 | -228.62 | Underpowered |
| V10, ETF catch-up, 3-minute horizon | 0.0102 | -257.11 | Underpowered |
| V11, ETF catch-up, 5-minute horizon | 0.0108 | -182.22 | Underpowered |

This is a one-session hypothesis screen, not an out-of-sample demonstration of an alpha. The registered rule
explicitly prevents promotion from this screen. The scorecard records seven looks before results and
correctly warns that one session cannot support day-clustered inference.

Reproduction limitation: I verified the published scorecard against its committed JSON fields, rather than
executing the score-generation pipeline. Independent computational reproduction remains unverified.

## 3. Findings memo: preregistration, simulator rules and holdout registry

### A. Pre-registration and analysis-identity binding (F1)

Assessment: sound design in principle; independent end-to-end verification incomplete.

The identity implementation binds the analysis to script hashes, the python/src/iap code tree, commit, data
versions, hypothesis family, metrics and thresholds. Version 2 also covers configuration files, reference
data, additional inputs, dependency locks and the execution environment. Signature verification checks the
trust-root key and the canonical signed body.

The separation between signer and runner is appropriate, but the implementation itself acknowledges that
software cannot prove who is physically operating the machine.

Specific discrepancy requiring resolution: the published REL-08 scorecard says the continuation rule is
signed, and the screen JSON says "signed": true. However, the current root-level
research/v118/rel08_rule_signed.json retrieved from main says "status": "DRAFT, unsigned" and says the screen
refuses that file.

This may be a path or artifact-version mismatch, but it must be resolved before I can endorse the signed-rule
chain. Verify the exact rule file consumed by the screen, its signature against the trust root, and the
identity bound to the run.

### B. Holdout registry (F4)

Assessment: strong fail-closed design; coverage of every data-access route not independently demonstrated
here.

The registry uses content hashes rather than path names, tracks dataset and session/symbol scope,
distinguishes in-sample, spent and unread data, and rejects unknown real datasets. The implementation also
describes a hash-chained access log, a registry integrity hash and a low-level reader guard intended to
protect against missed checks in higher-level entry points.

These are appropriate controls against renaming a holdout directory, copying it, selecting a date under a
different label or silently reusing a spent session.

The residual governance risk is explicit: a maintainer who changes the registry and reseals it, or removes a
guard, requires review and CI controls to detect. I have inspected the implementation but have not run the
holdout test suite or traced every supported and unsupported reader path. Therefore, I cannot independently
certify that no bypass exists.

### C. Simulator rules and sim-to-real limitations (F8/F10)

Assessment: acceptable for deterministic replay research with explicit limits; not validated as a real-world
fill model.

The simulator's documented rules and mapped tests cover several important failure modes:

- Displayed liquidity must not be consumed twice.
- Fills must respect order arrival, activation, expiry and cancellation.
- Trade-through fills must be bounded by available traded volume and queue position.
- Stale or halted venues must not generate fills.
- Incomplete orders must retain visible residual quantity; the residual is never fabricated into a fill.
- Replay must be deterministic and conserve quantities.

The central limitation is decisive: these tests show consistency with the implemented rules, not that the
rules accurately predict venue behaviour. The project itself marks Level 2 validation as incomplete because
it has no real orders and fills against which to validate the model. Absolute passive-fill probabilities,
live market impact, capacity and live implementation shortfall are consequently unsupported claims.

I support retaining these boundaries in the release notes. I do not approve describing the simulator as
empirically validated against live execution.

## 4. Written decision

Known-null method: rejected for acceptance as the mandatory F3 gate in its current form.

The v3 result fails its predeclared conditional rejection-rate criterion in two A0 cells. Generator B's
acceptable observed rates do not rescue the combined result or prove real-data FWER control. The result must
remain recorded as failed; no threshold change or parameter tuning should be retroactively applied.

The next step should be a diagnostic review, not an automatic v4. Specifically, examine the conditional
randomisation estimand, the consequences of removing session-level means, and the S2 standard-error
calibration at small K. Any replacement method should define its target error rate, dependence assumptions,
independent replication unit, acceptance criterion and diagnostic stopping rules before it is signed or run.

Release recommendation: keep F3 open; do not claim real-data FWER control. Keep simulator conclusions
explicitly limited to deterministic replay, and resolve the REL-08 signed-rule discrepancy before treating
its scorecard as fully verified.

## Reproduction status

| Deliverable | Status |
|---|---|
| Known-null decision | Rejected for F3 acceptance |
| B8 v2 JSON | Committed report and JSON inspected; independent rerun not completed because the required market-data inputs and bound execution environment were not available in this review |
| REL-08 scorecard | Committed Markdown and JSON inspected and their published verdict fields agree; score-generation pipeline not independently rerun |
| Preregistration, simulator and holdout memo | Findings documented above; full test-suite execution and end-to-end signature verification remain outstanding |

This is a substantive review of the accessible repository evidence, not a claim that all programme 9.3
reproduction requirements have been completed. The null v3 failure is a genuine negative result and should
remain visible in the release decision.

---

## Project response to the discrepancy in section 3A (added by the project, not the reviewer)

`research/v118/rel08_rule_signed.json` has `"signed": true` and a valid ed25519 signature; the `status`
string "DRAFT, unsigned: the screen refuses this file; tools/rel08_rule_sign.py writes the signed copy" is
stale text inherited from the unsigned template. `python tools/verify_signatures.py` returns OK for the file
(checked 2026-10-23). The file is left unedited because the signed body covers it.
