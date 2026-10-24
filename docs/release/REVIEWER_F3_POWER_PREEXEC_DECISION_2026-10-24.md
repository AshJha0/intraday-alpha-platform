# Outside reviewer decision: F3 power diagnostic, signed protocol, pre-execution review (received 2026-10-24)

Transcribed from the text the owner pasted into the session on 2026-10-24; citation markers omitted, wording unchanged.
Subject: intraday-alpha-platform v1.18.0, PR #361.

---

**Decision: Conditionally approved for one execution, subject to successful verification of the signed artifact at the execution checkout.**

The signed protocol as described is consistent with the freeze package previously approved for signature preparation. Both outstanding documentation requirements appear satisfied: the effect-size definition is consistent, and the acceptance-study core.py is bound by the exact hash previously used.

I accept the disclosed signing arrangement under the owner's explicit authorisation, subject to successful signature verification. Owner re-signing is not required solely because Claude operated the signing command. However, absence of signing-key environment variables is not proof of filesystem-level isolation.

## Responses to the four approval questions

| Question | Decision |
|---|---|
| (a) Signed identity matches the approved freeze | Consistent with reported identity; final hash verification required |
| (b) Pre-execution verification list | Sufficient, with evidence retention requirements below |
| (c) Single execution, up to three workers | Conditionally authorised |
| (d) Non-gating report wording and censored estimates | Approved |

The signed declaration must remain unchanged, including the effect definition mu = d * sigma_RMS / sqrt(G), the grid through d = 16, 10,000 datasets per cell, common random numbers, the analytic integrity band, and the 1,000-resample paired bootstrap.

## Required execution-checkout verification

Immediately before execution, the non-signing runner must verify and retain evidence that:

1. The signed preregistration has SHA-256 af2ffefc8920700c47972f90ae067bd16debe5e1bf49215aba42324d6fd88830, and the signature validates against the trusted maintainer key.
2. Bound commit 4f39f3a8a5905207ac0ac2c49c2c54b3bd6f04e8 is an ancestor of the clean execution checkout, including after PR #361 is merged.
3. Every bound script, imported acceptance core.py, protocol, declaration, configuration, dependency lock and interpreter identity matches the signed preregistration.
4. check_freeze.py passes all 30 checks and the prescribed dry run passes, without creating study observations.
5. The execution actor is claude-queue-runner, signing-key environment variables are absent, and no completed decision or study cell file already exists.
6. The declared memory prerequisite is satisfied and the final execution evidence records the checkout, actor, preflight results, launch time and exit status.

A preflight failure must prevent execution. Preserve any refused-launch log and do not treat a refused launch as a completed statistical run if no study computation occurred.

## Execution and reporting conditions

Authorisation covers one completed execution of the exact signed, non-gating synthetic power study, using no more than three workers.

The analytic integrity check must be evaluated before releasing power estimates. If it fails, record VOID_INTEGRITY as final, preserve the evidence and do not rerun under the same signature.

If it succeeds, report the full power grid, Wilson intervals, paired uncertainty estimates, MDE80 with censored boundaries, and ratios only where the frozen definedness rules permit them. Report nonmonotone curves and limitations without discretionary correction.

The results must return for a separate post-execution review.

## Final disposition

| Item | Decision |
|---|---|
| Signed protocol | Conditionally approved for execution |
| Freeze design | Unchanged |
| Signing arrangement | Accepted with disclosed limitation |
| Pre-execution verification | Required at actual execution checkout |
| Single non-gating run | Authorised after verification |
| Real market data | Not authorised |
| F3 acceptance | 27/27 PASS, unchanged |
| Null v3 | FAILED, unchanged |
| F3 programme gate | OPEN |

No further methodological changes are requested. This approval relies on the reported freeze and signing evidence; I have not independently verified the exact signed preregistration bytes or executed its tests.

The study may proceed once the required preflight checks pass.
