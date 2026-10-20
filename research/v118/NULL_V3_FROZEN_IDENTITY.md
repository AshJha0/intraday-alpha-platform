# Null protocol v3: frozen candidate identity (UNSIGNED, NOT RUN)

Candidate code head (all bound files final at this commit; commits after it only touch this note, which is outside the bound set):
`3742d3671afbc33a17a78ba3771acf97b9a752bd` (branch `v118-nullv3-runner-rev2`, base main `f023286a`).

| item | value |
|---|---|
| protocol | `research/v118/NULL_PROTOCOL_v3.md`, normalised SHA-256 `92a8559d1a6ba5b17469b2b2df440cfeb77a1dc9237e78017fc70b786845a7f9` |
| runner configuration | `research/v118/known_null_v3_config.json`, `78320d9f826546e098ce360415c50dacc7ae99631842d58c4dbd05429002d244` |
| prereg path (to be created at signing) | `research/v118/null_protocol_v3_prereg.json` (draft: `null_protocol_v3_prereg_DRAFT.json`, `protocol_sha256_at_draft` is tracking only and is refilled at signing) |
| `tools/rel06_known_null.py` | `148b21605ed562fd4b8613e5b2201f6f90f47f266b1ee73c76b10f906c35f4ad` |
| `tools/rel06_known_null_v3.py` | `766fa937384aec59b39728ac2252766a41fdca912314af5c908bbf9f3797afc5` |
| `known_null.py` | `510902c3bd05a87b91ef948172da528921cd24f94dc05c8e3e58bc509d7737c0` |
| `known_null_diag.py` | `096449c7974e516286b947ce48a41f52c7b449d9d2cc006304235ddae01460c2` |
| `known_null_v3.py` | `a9465d0a23724ada9a25c3146e67d8cc2e13ce1ce7a42bd8ede52cd560726cf1` |

Hashes are `known_null_v3._norm_sha` (line-ending normalised), computed on a clean clone of the candidate head. Any later
change to a bound file changes these and requires a new review.

## Clean-checkout report (fresh `git clone` of the candidate head, Python 3.12, `PYTHONPATH=python/src`)

* `pytest -n 2 -q python/tests`: **3894 passed, 115 skipped, 1 xfailed, 0 failed** in 9.6 min.
  History: on the earlier head `0bd377c0` one test failed, `python/tests/test_research_store_safety.py::
  test_two_processes_lose_no_ledger_update` (ledger/research-store safety suite). Cause: two writer processes each construct
  `ExperimentLedger`, whose load is unlocked; on Windows that read can hit the other process's `os.replace` and raise
  `PermissionError` (an OS reader/replace race, not a lost update; saves are under the lock). It is unrelated to the null v3
  code and passed alone. Fixed test-only in `3742d367` (the writer script retries the unlocked read on `PermissionError`;
  no `src` file changed); the file then passed 5 of 5 loops alone and the full suite is clean.
* `ruff check .`: all checks passed. `ruff format --check python tools`: 649 files formatted.
* `mypy` (in `python/`, CI config): success, no issues in 129 source files.
