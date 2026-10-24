# Outside reviewer decision: F3 signed protocol, pre-execution decision (received 2026-10-24)

Transcribed from the text the owner pasted into the session on 2026-10-24; citation markers omitted, wording unchanged.
Subject: intraday-alpha-platform v1.18.0.

---

**Decision: Approved with one named pre-execution condition.**

The signed protocol as described is consistent with the previously approved freeze design. I do not require a change to the statistical procedure, generator definitions, operating alpha, FWER target, sample size, seeds or acceptance criterion.

The disclosure concerning signing is important. I accept that Claude executed the signing commands under the owner's explicit authorisation, provided the maintainer key was used legitimately and the runner remains a separate actor without access to signing credentials. This is a deviation from the originally described signing procedure, but it does not, by itself, invalidate the statistical preregistration.

The outstanding condition is verification of the exact signed artifact and its bound identity at the execution checkout, rather than reliance on the cover note alone.

## Named condition: execution-checkout verification

Before the first acceptance-study execution, the non-signing runner must verify and record:

1. The exact preregistration SHA-256: `abe85c3ad93ebdd7381a6a907b018f840dc626638b157c01f408a72f01af2607`.
2. Successful cryptographic verification of the maintainer signature against the trusted key.
3. The bound commit `24eac89be177a357b6df54c4992142467c6351ff` is an ancestor of the execution checkout.
4. All bound script, protocol, configuration, dependency and analysis-identity hashes match the signed preregistration.
5. The runner is `claude-queue-runner`, with no signing credentials available, and the acceptance study has not already produced a final decision.

The repository's PR #345 confirms the signed-preregistration submission and the declared signing arrangement, but the exact JSON could not be retrieved independently from `main` at review time because the PR was still open.

**Execution authorisation:** Granted conditionally for exactly one acceptance study under the signed protocol, once the five checks above succeed and are retained in the execution evidence.

The authorisation covers 450,000 mandatory and 150,000 sensitivity synthetic datasets, with no parameter changes, no interim analysis and no discretionary rerun following a completed verdict.

F3 remains OPEN. Null v3 remains FAILED. The acceptance results must be reported as found and submitted for a separate post-execution decision. A statistical PASS will not automatically close F3 or authorise real-data FWER-control claims.
