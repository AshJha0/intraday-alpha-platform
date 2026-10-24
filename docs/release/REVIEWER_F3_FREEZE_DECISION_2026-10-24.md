# Outside reviewer decision: F3 acceptance-study freeze review (received 2026-10-24)

Transcribed from the text the owner pasted into the session on 2026-10-24. The text arrived as one continuous paragraph; line breaks
and headings were added only to separate the numbered items, the wording is otherwise unchanged. No citation markers appeared in the
text. Subject: intraday-alpha-platform v1.18.0 post-release; reviewed package: `research/v118/f3_accept/` (PR 341).

---

Outside reviewer - F3 acceptance-study freeze review.
24 October 2026 | Candidate P1 | Unsigned preregistration.

Provisional disposition: Approved with named conditions for signature preparation, not yet for execution.

The freeze package addresses the six changes from my previous decision at the design level. The distinction between a prospective acceptance study and the failed v3 investigation is preserved, and the proposed statistical controls are appropriate in principle.

My answers to the four specific questions:

1. A1 sensitivity: Confirmed. Run a1 at rho=0.6 with 50,000 independent synthetic datasets per G, report all K3/K6/K16 outcomes, and exclude those results from the mandatory acceptance verdict. Its results must not be suppressed or used to redefine the operating domain after inspection.

2. Chunking: Accept 10,000 datasets per chunk, provided chunking does not reset or reseed the random-number generator. Each regime-G cell must consume a single deterministic stream. Chunk size, ordering and generator implementation are part of the frozen identity.

3. `synthetic-no-data`: Acceptable as a declared sentinel for `data_versions`, provided the signed identity also records that no market data are consumed. This is a synthetic-only experiment, and the sentinel must not be interpreted as a real dataset identifier or holdout exemption.

4. Failure finality: Confirmed. A failed signed acceptance study is final for that preregistration. New seeds, modified thresholds, altered generators, changed sample counts or a second attempt require a new preregistration and independent approval. An interrupted run may be resumed only if the original protocol explicitly permits it and the resumed execution preserves the same dataset stream and analysis identity.

Final reviewer disposition: Approved with named changes for signature preparation. The published protocol reflects the proposed separation of mandatory and sensitivity regimes, the 27-evaluation joint bound, the fixed seed design and the signer/runner separation.

Before I can issue unconditional approval for signature, the following checks remain:

1. Freeze consistency: Verify that PROTOCOL.md, declaration.json, core.py, the runner and the signed preregistration contain consistent parameters and that all required hashes are bound.

2. Checkpoint integrity: Confirm that a resumed run validates the complete signed analysis identity and preserves the deterministic random stream, not merely the preregistration hash.

3. No-data enforcement: Confirm that the runner has no market-data dependency or reader path and that `synthetic-no-data` is used only as an explicit sentinel.

4. Acceptance interpretation: Report each of the 27 mandatory results and nine sensitivity results. A statistical pass must not automatically close F3 or authorise real-data claims.

These are verification requirements, not requests to change the statistical design.

Signature approval status: Conditional, pending the above checks. The signed protocol must be submitted for its separate pre-execution review. F3 remains OPEN. Null v3 remains FAILED. No acceptance execution is authorised by this decision.
