# Null protocol v3: frozen candidate identity (UNSIGNED, NOT RUN) - single authoritative record (rev 3)

This note supersedes every earlier version of itself, including the ones that named `0bd377c0` (an earlier head, 3893
passed / 1 failed) or `3742d367` (the earlier code commit, 3894 passed / 0 failed).

## The candidate

**Candidate commit `04d6b661b0c8d72cbf654d9e45cb6a1320a3caf9`** (branch `v118-nullv3-audit-rev3`, parent = main `aa2d4b82`, i.e.
main after PR #308). Every bound file below is final at this commit. The commit after it (this note, nothing else in
`python/`, `tools/` or `configs`) is a documentation commit: it adds/changes only `research/v118/NULL_V3_FROZEN_IDENTITY.md`.
The statistical code and config were first frozen at `3742d367` (PR #307) and are byte-identical at the candidate.

Verify (all must print nothing / zero):

```
# 1. bound code, config and tests: identical between the earlier code commit and the candidate
git diff --stat 3742d367 04d6b661 -- python/src/iap/research/known_null.py python/src/iap/research/known_null_diag.py \
  python/src/iap/research/known_null_v3.py tools/rel06_known_null.py tools/rel06_known_null_v3.py \
  research/v118/known_null_v3_config.json python/tests/test_known_null_v3.py python/tests/test_known_null_diag.py
# 2. what changed in the candidate relative to main aa2d4b82: only these three docs/draft files
git diff --name-only aa2d4b82 04d6b661
#    research/v118/NULL_PROTOCOL_v3.md, research/v118/known_null_v3_blocker_calibration_ROWS.md,
#    research/v118/null_protocol_v3_prereg_DRAFT.json
# 3. the final documentation head changes no code: python/, tools/, configs between candidate and the PR head
git diff --stat 04d6b661 HEAD -- python tools configs research/v118/known_null_v3_config.json research/v118/NULL_PROTOCOL_v3.md \
  research/v118/null_protocol_v3_prereg_DRAFT.json research/v118/known_null_v3_blocker_calibration.json
```

Check 1 was run and is empty. Between `3742d367` and the candidate, `python/` and `tools/` differ in ten files, none bound
(REL-04/05/04c work merged to main meanwhile: `marketdata/iex.py`, `ingest.py`, `process_iex.py`,
`research/execution_benchmark.py`, `research/rel04c_job.py`, and the tests `test_analysis_identity_v2.py`,
`test_execution_benchmark.py`, `test_iex_capture_ts.py`, `test_rel05_runner.py`, `test_v118_rel04c.py`). The signature binds
`python/src/iap` by tree hash at signing, so the signing commit must be recorded then; this note does not pre-empt that.

## Bound files and SHA-256 (`known_null_v3._norm_sha`, line-ending normalised; computed at the candidate)

| item | path | sha256 |
|---|---|---|
| protocol | `research/v118/NULL_PROTOCOL_v3.md` | `809866cb093eb6ea464987e859f37bb78ed0d2f3207255ad3af5c90a72f1ccab` |
| prereg draft | `research/v118/null_protocol_v3_prereg_DRAFT.json` (final prereg path `research/v118/null_protocol_v3_prereg.json` created at signing; `protocol_sha256_at_draft` is tracking only) | `642a77a05aab2049c9cf450e99c715451fc804daad18c5c107b74b4195e1de8f` |
| runner configuration | `research/v118/known_null_v3_config.json` | `78320d9f826546e098ce360415c50dacc7ae99631842d58c4dbd05429002d244` |
| runner | `tools/rel06_known_null_v3.py` | `766fa937384aec59b39728ac2252766a41fdca912314af5c908bbf9f3797afc5` |
| runner base | `tools/rel06_known_null.py` | `148b21605ed562fd4b8613e5b2201f6f90f47f266b1ee73c76b10f906c35f4ad` |
| library | `python/src/iap/research/known_null_v3.py` | `a9465d0a23724ada9a25c3146e67d8cc2e13ce1ce7a42bd8ede52cd560726cf1` |
| library base | `python/src/iap/research/known_null.py` | `510902c3bd05a87b91ef948172da528921cd24f94dc05c8e3e58bc509d7737c0` |
| diagnostics | `python/src/iap/research/known_null_diag.py` | `096449c7974e516286b947ce48a41f52c7b449d9d2cc006304235ddae01460c2` |
| tests (v3) | `python/tests/test_known_null_v3.py` | `1e80795fe85a4a2b149a7189fa4e8aed208b3047885e9b19683f2a3a773e9a18` |
| tests (diag) | `python/tests/test_known_null_diag.py` | `9c84e047b351f84c92e54c303df3a8d88cdf98fd6138ef86c1041423519c4d62` |
| **B calibration evidence** | `research/v118/known_null_v3_blocker_calibration.json` (unchanged; row ids in `known_null_v3_blocker_calibration_ROWS.md`) | `cfeb6a6d27dbacc0fc9f36232c1deb508e1016488d6cc7c160c78930c987f2ae` |

Code, config and tests hashes are the same as recorded at `3742d367`. The protocol and prereg-draft hashes are new
(section 12 rewritten, calibration table gets row/seed columns, draft gains `frozen_evidence`); no threshold, level,
seed or generator text changed.

**Calibration hash binding.** The hash is added to the prereg draft under `frozen_evidence.b_blocker_calibration`, so the
prereg signature covers it. It is NOT part of the code-level `analysis_identity` block: `identity_args_v3` passes no
`inputs`, so binding it there would need a code change, which was not made. The runner does not read the calibration file.

## Clean-checkout report on the exact candidate `04d6b661`

Fresh `git clone --no-hardlinks` checked out at `04d6b661`, Python 3.12.10, `PYTHONPATH=python/src`:

* `pytest -n 2 -q python/tests`: **3935 passed, 115 skipped, 1 xfailed, 1 FAILED** in 26 min 23 s (machine heavily loaded by
  other sessions). The failure is `python/tests/test_maker_real_parallel.py::test_parallel_unit_matches_serial_and_resumes`
  (`assert b["workers"] == 2 and a["workers"] == 1`, got 1 worker: the parallel path fell back to one worker under load). It
  is unrelated to null v3 (maker-real parallel unit, not in the bound set, unchanged by this PR) and **passes when the file
  is re-run alone (2 passed)**. It is reported as a failure of the full run, not as a pass.
* `ruff check .`: all checks passed. `ruff format --check python tools`: 651 files already formatted.
* `mypy` (in `python/`, CI config): success, no issues in 129 source files.
* The null-v3 bound tests (`test_known_null_v3.py`, `test_known_null_diag.py`): 70 passed (run on the working tree before the
  candidate commit, whose bound code is byte-identical).

The earlier report at `3742d367` was 3894 passed, 115 skipped, 1 xfailed, 0 failed; the additional passes are the REL-04/05/04c
tests merged to main since.

## Audit close-out against merged `main` (2026-10-20, coordinator; docs only, answers reviewer conditions 1 to 3)

**1. Merged main preserves the candidate.** Main `87371e13` (after PR #310). For each of the eleven items in the table above, the
line-ending-normalised SHA-256 (`known_null_v3._norm_sha` semantics) was recomputed from `git show 04d6b661:<path>` and from
`git show origin/main:<path>`: **all eleven are identical at both refs and equal to the table**
(protocol 809866cb..., prereg draft 642a77a0..., config 78320d9f..., runner 766fa937..., runner base 148b2160..., library a9465d0a...,
library base 510902c3..., diagnostics 096449c7..., tests v3 1e80795f..., tests diag 9c84e047..., calibration cfeb6a6d...).
`git diff 04d6b661 origin/main -- python tools configs research/v118` changes only this note. The signing commit and the
`python/src/iap` tree hash must still be recorded at signing time (see above).

**2. Calibration hash and prereg coverage.** `sha256(research/v118/known_null_v3_blocker_calibration.json)` =
`cfeb6a6d27dbacc0fc9f36232c1deb508e1016488d6cc7c160c78930c987f2ae` at the candidate and at main, equal to
`frozen_evidence.b_blocker_calibration.sha256` in `null_protocol_v3_prereg_DRAFT.json`. The prereg signature covers it through
`frozen_evidence`; the code-level `analysis_identity` block does not carry it (`identity_args_v3` passes no `inputs`).

**3. The two test runs, side by side.**

| | run A | run B |
|---|---|---|
| commit | `04d6b661` | `04d6b661` |
| environment | Python 3.12.10, `PYTHONPATH=python/src`, machine heavily loaded by other sessions | Windows 11 10.0.26200, Python 3.12.10, pytest 9.1.1, pytest-xdist 3.8.0, numpy 2.4.6, pandas 3.0.6, scipy 1.17.1, `.venv-ci`, machine idle |
| result | 3935 passed, 115 skipped, 1 xfailed, **1 failed** (`test_maker_real_parallel.py::test_parallel_unit_matches_serial_and_resumes`, worker-count assertion under load; passes alone) | 3917 passed, 123 skipped, **0 failed** (824 s) |

The pass and skip counts differ because the environments differ in optional dependencies and data, so run B is a successful
clean-checkout run in its own recorded environment, not identical validation evidence. For run B only the tail of the output was
retained; a re-run that keeps the full log is scheduled on an idle machine and will be attached before signature.

## Full-log clean-checkout run on the candidate (2026-10-20 16:00 to 16:22; attached)
Fresh clone at exactly `04d6b661b0c8d72cbf654d9e45cb6a1320a3caf9`, clean tree, `.venv-ci` (Python 3.12.10, pytest 9.1.1, xdist 3.8.0,
numpy 2.4.6, pandas 3.0.6, scipy 1.17.1, Windows 11 10.0.26200), `pytest python/tests -n 2 -q -p no:cacheprovider -rfE`:
**3917 passed, 123 skipped, 0 failed, 1284 s.** The machine was NOT idle: the REL-04 capture-ts re-download/ingest ran at the same
time (recorded in the log header); the earlier load-sensitive failure (`test_maker_real_parallel.py`) did not recur.
Complete output: `research/v118/evidence/pytest_04d6b661_full_20261020.log`, sha256 `98a93a85fd15dab97e3a3535ae46711fca8c20e80a353700bfda107a3ea0baba`.
Run A (3935 passed / 1 failed, heavy load) and run B (3917 passed / 0 failed, idle) above remain on record; this third run
is the retained-log record. Differing pass and skip counts across runs reflect differing environments (optional dependencies and data).

## Signing-procedure note
`prereg_bind_identity.py bind` writes a new prereg and does not carry `frozen_evidence` or `prior_looks` from the draft. For the
calibration hash to be covered by the prereg signature, those two keys are copied verbatim from `null_protocol_v3_prereg_DRAFT.json`
into the bound file between `bind` and `sign` (the signature is over the whole document; `verify_binding` ignores extra keys). The
insertion refuses unless the normalised sha256 of `known_null_v3_blocker_calibration.json` equals the draft's value
(`cfeb6a6d...f2ae`). The signing commit and the `python/src/iap` tree hash are recorded after signing.
