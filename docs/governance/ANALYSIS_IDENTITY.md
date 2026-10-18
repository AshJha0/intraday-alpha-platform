# Analysis identity: what a signed prereg binds

Review finding 1 (outside reviewer, 2026-10-18): prove that a registration covers every input
that can change a result, and that no entry point skips the check. Code:
[`iap.agents.analysis_identity`](../../python/src/iap/agents/analysis_identity.py); tests:
[`test_analysis_identity_v2.py`](../../python/tests/test_analysis_identity_v2.py).

## Inputs that can change a result, and how each is bound (identity v2)

| Input | Where it lives | Bound by |
|---|---|---|
| Analysis scripts | the runner and study scripts | `scripts`: path -> sha256 (LF-normalised) |
| Library code: features (registry, spec), labels, cost, fill and venue models | `python/src/iap/**` (every file, package data included) | `code_tree_sha256` |
| Configs: instruments, venues, fee tiers, regulatory fees, venue latency, execution, risk, strategies, prereg templates | `configs/**` | `configs_tree_sha256` |
| Reference data: feature registry JSON | `data/reference/**` | `reference_tree_sha256` |
| Study files outside those trees (calibrations, venue files, predeclarations under `research/`) | declared with `prereg_bind_identity.py bind --inputs` | `inputs`: path -> sha256 |
| Dependencies | `python/requirements-ci.txt` (hashed lock) | `deps_lock_sha256` |
| Installed packages | the runner's environment | `environment`: installed version of every package the lock pins |
| Interpreter | the runner process | `interpreter`: implementation and `major.minor.micro` |
| Datasets | `data/real/<dataset>` | `data_versions`: content hashes of the normalized files; the holdout guard and `dataset_version_of` re-hash the files against the manifest, so a tampered tree is refused, and a re-ingested tree is a different version |
| Hypothesis family, metrics, thresholds | the prereg | `family`, `metrics`, `thresholds` (the runner declares them; they must match) |
| Commit | git | `commit`: HEAD or an ancestor of HEAD |

The check is all-or-nothing: `verify_identity` lists every mismatch and refuses.

## Versions

A block with no `identity_version` is **v1** (scripts, `*.py` code tree, commit, data versions,
family, metrics, thresholds) and is verified exactly as it was when signed. The three v1 documents
signed on main (`research/preregs/b8v2_prereg.json`, `research/v118/null_protocol_prereg.json`,
`research/v118/rel08_rule_signed.json`) keep verifying under v1 and are not re-signed. Their v1
coverage does not include configs, reference data, the lock, the environment or the interpreter;
that gap is stated, not closed retroactively. Every new bind writes v2. An unknown version is
refused.

## Entry points

| Runner | Identity check |
|---|---|
| `tools/r7_confirm.py` | `verify_analysis_identity` -> `verify_identity` (legacy R7 prereg: no block, look spent) |
| `tools/rel06_known_null.py` | `verify_prereg` -> `verify_identity` |
| `research/execution/b8v2/run_b8v2.py` | `verify` -> `verify_identity` |
| `python -m iap.research.rel08_screen` | `gate` -> `rel08_rule.check_identity` -> `verify_identity` |
| `python -m iap.research run` (blackboard preregs) | `prereg_gate.require` -> `verify_identity` |
| REL-08c (`iap.auction.rel08c_extract`), REL-09 (`stale_quote`), REL-10 (`volume_clock_ofi`) | no confirmatory runner on main yet (no signed prereg exists); a runner added later consumes a signed document, which the coverage test forces into the list above |

Two backstops make a skipped check fail closed:

1. **The holdout guard.** A restricted (spent / unread) read authorised by an identity-bound prereg
   document re-checks the bound scripts, trees, lock, environment and interpreter, and that the
   dataset version is one the prereg bound (`holdout_registry.check_prereg_identity`). An entry
   point that forgets its own check still cannot read the holdout under that prereg.
2. **The coverage test.** `test_no_prereg_consumer_skips_the_identity_check` scans `tools/`,
   `research/` and `python/src/iap/` for every file that consumes a signed prereg or rule and fails
   unless it is a listed runner whose identity call is present, or an explicitly listed non-runner
   (binders, signers, `verify_signatures.py`).

## What code cannot enforce

Legacy preregs signed before v1.18 bind nothing; they authorise replays of spent looks only, never
new confirmatory evidence. In-sample reads need no prereg, so the identity binds confirmatory runs,
not exploration. Someone who edits a runner AND this test is stopped by review only.
