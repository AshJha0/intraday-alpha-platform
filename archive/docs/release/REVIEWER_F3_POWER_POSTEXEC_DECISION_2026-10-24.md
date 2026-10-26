# Outside reviewer decision: F3 power diagnostic, post-execution review (received 2026-10-24)

Transcribed from the text the owner pasted into the session on 2026-10-24; citation markers omitted, wording unchanged. One element of the pasted text did not come through:
the MDE80 summary table in section 2 arrived without its values (only the row labels "Lowest MDE80 / Highest MDE80" and a string of characters); the values it summarises are those in
`research/v118/f3_power/REPORT.md` (MDE80 3.7 to 8.6 in noncentrality-scale units, as quoted in the approved wording below). Subject: intraday-alpha-platform v1.18.0, PR #364 / #365.

---

**Decision: Accept the reported power-diagnostic result, subject to artifact-level verification. No rerun or statistical redesign requested.**

The reported study is consistent with the authorised protocol: one completed execution, successful preflight, a passing programming-integrity check, and no reported changes to the frozen analysis.

The results provide useful evidence about P1's statistical power under the declared synthetic generators. They do not change the earlier acceptance verdict or establish suitability for real-market inference.

## 1. Execution and integrity assessment

| Requirement | Reported evidence | Assessment |
|---|---|---|
| Signed preregistration | SHA-256 and signature verified | Satisfied as reported |
| Bound commit and inputs | Verified against execution checkout | Satisfied as reported |
| Freeze consistency | 30/30 checks | Satisfied as reported |
| Dry run | PASS; outputs discarded | Satisfied as reported |
| Signer/runner separation | Distinct actor, no signing-key environment variables | Accepted with disclosed limitation |
| Single execution | Exit 0, approximately 17 seconds | Accepted as reported |
| Programming-integrity band | PASS across 108 a0 combinations | Satisfied as reported |
| Finality | COMPLETE, no tuning or rerun | Accepted as reported |

The programming-integrity PASS supports consistency with the frozen analytic cross-check. It is not an independent validation of the statistical assumptions.

The private-key file remaining accessible on the same machine continues to be a security limitation, but does not by itself invalidate the reported synthetic results.

## 2. Statistical interpretation

I also inspected the published REPORT.md and confirmed that its MDE80 table is consistent with the headline ranges in the cover note.

MDE80 by synthetic regime: range across G = 5, 7, 10 and K = 3, 6, 16; lower means less planted effect needed for 80% detection. (Table values not preserved in the pasted text; see the note at the top.)

Three findings are worth retaining in the final research record.

First, multiplicity and small G impose a substantial power cost. MDE80 is highest at G = 5, K = 16, and lowest at G = 10, K = 3. This is consistent with the expected difficulty of detecting a planted signal using few independent sessions and a more stringent multiple-testing correction.

Second, b0 exhibits similar power to a0. The heterogeneous-to-homogeneous MDE80 ratios near 1 indicate little difference for the particular synthetic generator and RMS-normalised effect. This is not evidence that arbitrary real-world heterogeneity has no power cost.

Third, b1 reaches 80% power at somewhat smaller effects. Its ratios of 0.849-0.927 should be reported as generator-specific behaviour. Heavier tails do not generally improve detection power, and the result should not be extrapolated to market returns.

Importantly, MDE80 values of 3.7-8.6 are expressed in the study's noncentrality-scale units. They are not basis points, rank IC, Sharpe ratios or expected trading returns.

## 3. What may now be claimed

The following wording is approved for the project record:

> The preregistered, non-gating F3 power diagnostic completed successfully under the signed synthetic-only protocol. The programming-integrity check passed. Across the declared synthetic regimes, P1's estimated MDE80 ranged from approximately 3.7 to 8.6 in the preregistered noncentrality-scale effect units, depending on session count, multiplicity and generator. The study provides evidence about power under those synthetic models only. It does not establish practical detection power, real-data FWER control, trading profitability or signal validity.

## 4. What should happen next

I recommend closing the synthetic F3 research workstream without another simulation round, while keeping the broader F3 gate open.

The next useful step is an operating-domain feasibility inventory, not another synthetic comparison. Determine from metadata alone whether sufficiently long, comparable in-sample session histories exist for the intended signal families, and whether dependence diagnostics could have meaningful resolution. Do not inspect holdout data or run unapproved real-data diagnostics.

If adequate data do not exist, record the operating-domain assessment as inconclusive and leave P1 unsupported for real-data inference claims. There is no requirement to manufacture evidence by lowering the diagnostic standard.

## Final disposition

| Item | Decision |
|---|---|
| Power diagnostic result | Accepted as reported |
| Programming-integrity check | PASS |
| Synthetic power estimates | Accepted within declared scope |
| Further power-study rerun | Not required or authorised |
| F3 synthetic acceptance | 27/27 PASS, unchanged |
| Null v3 | FAILED, unchanged |
| F3 programme gate | OPEN |
| Real-data diagnostics | Not authorised |
| Real-data FWER or trading claims | Not authorised |

The synthetic calibration and power-characterisation phases are complete. The remaining F3 question is whether the assumptions behind P1 can be justified for the intended real-data operating domain. That question must be resolved separately; the successful synthetic studies cannot answer it.
