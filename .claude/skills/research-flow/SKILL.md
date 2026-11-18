---
name: research-flow
description: Run the wired research graph (hypothesis, prior-art check, unsigned pre-registration draft, human approval, in-sample run, code triage, verified narrative) for a new hypothesis, stop at the approval step, show the draft, and resume only on the person's decision. Use when someone wants to take a hypothesis through the research workflow, draft a pre-registration, or continue/approve/reject a paused graph run.
---

# Research flow

## Setup

Run from the repository root with the project interpreter and `PYTHONPATH=python/src`.

## Steps

1. Write the hypothesis to a JSON file with `alpha_id`, `horizon` (`1m 5m 15m 30m 60m 1d`),
   `universe` (`name`, `symbols`, `datasets` = dataset labels from
   `research/holdout_registry.json`), `expected_sign` (`1` or `-1`) and `mechanism`
   (at least 20 characters). Ask the person for anything missing; do not invent it.
2. Run to the approval stop into a new, empty directory:

   ```
   python -m iap.graph run --hypothesis <hypothesis.json> --out <run dir>
   ```

   The in-sample runner is a stub returning fixed numbers unless the person names one
   with `--runner module:function`. Say so when the stub is used: its numbers are not results.
3. Read the output:
   - `prior art: same test already in ...` means the run stopped as a duplicate. Report the
     citation and stop.
   - `check by hand: same alpha and horizon, universe not recorded` lists ledger entries
     the rule cannot judge (most entries record no universe). Show them to the person and
     ask whether any is the same test before going on.
   - `invalid` means the hypothesis failed validation. Report the errors and stop.
   - `awaiting: human approval` means the unsigned draft is printed. Show it to the person
     as printed, with the prior-art citations, and ask them to approve or reject.
4. Resume only on the person's explicit decision, recording who decided:

   ```
   python -m iap.graph resume <run dir> --approve --by <name>
   python -m iap.graph resume <run dir> --reject --by <name>
   ```

5. Report the outcome line. `refused datasets (holdout registry)` means a dataset was not
   in-sample in the registry (unknown datasets are refused too): report it, do not work
   around it. The narrative is printed with its source: `model` passed the number
   verifier; `fallback` is the code-written text used because the writer's text did not.

## Guardrails

- Never approve on the person's behalf, and never pass `--approve` without their decision
  in this conversation.
- Approving the draft is not a pre-registration: do not sign, register or record it anywhere.
- Do not read holdout data or edit `research/holdout_registry.json`.
- Quote no number that is not printed by the run or copied from a cited artefact.
