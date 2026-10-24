# Outside reviewer decision: F3 acceptance-study freeze package approved for signature (received 2026-10-24)

Transcribed from the text the owner pasted into the session on 2026-10-24; citation markers omitted, wording unchanged.

---

**approved for signature**

Candidate: F3 acceptance-study freeze package, revision 2, PR #343, dated 24 October 2026.

The four previously named conditions are satisfactorily addressed by the evidence reported in the revised cover note:

1. Freeze consistency: The 19/19 consistency checks, bound-file verification, negative drift test and signing/verification test address the required consistency controls.
2. Checkpoint integrity: Complete signed analysis-identity binding, deterministic stream validation, tamper detection and explicit resume restrictions address the checkpoint requirements.
3. No-data enforcement: Restricted imports, file-access tests and explicit synthetic-only declarations adequately address the no-market-data requirement.
4. Acceptance interpretation: Separate reporting of all 27 mandatory and nine sensitivity evaluations, correct verdict isolation and explicit prohibition on automatic F3 closure address the interpretation requirements.

No further statistical-design changes are requested before signature.

Scope of approval: The owner may sign the frozen preregistration locally, provided the signed artifact matches the reviewed freeze package. The signed protocol must then be submitted for the agreed pre-execution review.

This decision does not authorise the acceptance run, close F3, reverse the null v3 failure, or establish real-data FWER control.

The approval relies on the reported verification evidence; it is not a claim that I independently executed the repository tests.
