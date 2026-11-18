---
name: agent-evals
description: Run the offline AI guardrail evals (number-verifier golden and held-out sets, prompt-injection suite, research-memory retrieval eval) and read their committed results. Use when someone asks to run the agent evals, check the verifier or injection gates, check retrieval quality, or asks how well the guardrails measure.
---

# Offline agent evals

No model is called and nothing is spent: every suite is a fixed file of labelled cases.

## Setup

Run from the repository root with the project interpreter and `PYTHONPATH=python/src`.

## Steps

1. Run the gates (each exits non-zero when a rate got worse than the committed result):

   ```
   python -m iap.llm.verifier_eval --check
   python -m iap.llm.injection_eval --check
   python -m iap.memory eval --check --no-write
   ```

   With `--check` (and `--no-write` for memory) nothing is rewritten. Rewrite a result
   file only when the person asks, by running the same command without `--check`, and
   then show `git diff research/`.
2. Read the results from the committed files, not from memory:
   - `research/agents/evals/verifier_golden_result.json` (`overall`, `heldout`):
     false-accept and false-reject counts with Wilson intervals.
   - `research/agents/evals/injection_result.json`: pattern-flag detection and false
     positives, and the off-allowlist calls executed by the tool loop.
   - `research/memory/eval_result.json`: `recall_at_5` against `recall_at_5_floor`, `mrr`.
3. Report each number with the file it came from. The meaning of each measure and its
   limits are in `docs/AGENT_EVALS.md` and `docs/RESEARCH_MEMORY.md`.

## Guardrails

- No live model eval: never call a model API (zero-spend rule).
- Do not edit the case files or their labels to make a gate pass.
- No signing, registration or holdout read.
- No number that is not copied from a result file or the command output.
