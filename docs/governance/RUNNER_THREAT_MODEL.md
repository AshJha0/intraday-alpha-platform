# The registered runner: threat model and the six properties

v2.0 stage 2, CR1 (U0 section 2, ninth review G2). The runner is
[`.github/workflows/registered-run.yml`](../../.github/workflows/registered-run.yml) executed by a self-hosted
GitHub Actions runner whose service runs as `iap-runner`, driving [`tools/runner/`](../../tools/runner/)
against the sealed service of [SEALED_DATA.md](SEALED_DATA.md). Tests:
[`python/tests/runner/`](../../python/tests/runner/). Settings the owner must set: [REPO_SETTINGS.md](REPO_SETTINGS.md)
section 7. Install: [RUNNER_SETUP.md](RUNNER_SETUP.md).

## Actors and assets

- **Adversary of record: the development identity** (the owner's normal user, every agent session, every
  research script) acting through the repository, the workstation's file system as that user, and GitHub as the
  repository owner's account *without* the approval step. The model does not defend against the workstation's
  administrator or against GitHub itself (section "custody").
- **Assets**: the sealed partitions and any derivative (a listing, a row, a summary statistic, a cache); the
  one-run-per-registration rule; the integrity of a result once produced.

## The procedure

Signed prereg merged (it carries `registered_run` = partition, dataset hash id, pinned image digest, command,
optionally the bound `dataset_files`) -> the owner starts `registered-run` naming the prereg and the bound commit
-> GitHub waits for the `sealed` environment approval -> the self-hosted runner (as `iap-runner`) checks out the
bound commit -> `tools/runner/run.py run`: identity, signature, image digest, `analysis_identity` (freeze
hashes, bound commit is HEAD or an ancestor), dataset id bound -> one `read` to the partition's service (its own
checks, ledger, hash verification, write-before-serve) -> served hashes re-computed and compared with the bound
`dataset_files` -> the registered command runs once in a private cache -> `hashes.json` -> `store_result` ->
commit to the protected `results` branch as `iap-runner` -> `publish` the checkpoint -> the redacted log is the
only artifact -> the cache is removed.

## Property 1: `iap-dev` cannot read sealed inputs or derivatives

| route | control | test |
|---|---|---|
| direct read of a partition | ACL: owner account and SYSTEM only | `test_identity_partitions.py` (dev x {a,b,c,runner}: read denied) |
| directory enumeration | same ACL | same (enumerate denied) |
| indirect: a report path under a partition | same ACL; the research code has no privilege | same (report denied) |
| indirect: a cache directory under a partition | same ACL; `iap-runner` uses its own private cache, removed after the run | same (cache denied); `test_private_cache_is_removed_and_holds_nothing_after_the_run` |
| the job log and artifacts | `RedactingLog`: every line passes `redact` (paths, pipe names, accounts, user and host names, e-mails); the analysis's stdout/stderr are never copied; the only artifact is the redacted log | `test_log_carries_no_path_account_host_or_data`, `test_redact_filter`, `test_workflow_...` (one upload, the log) |
| debug modes | `ACTIONS_STEP_DEBUG` / `ACTIONS_RUNNER_DEBUG` forced `"false"` in the job; must not exist as environment variables or secrets of `sealed` (REPO_SETTINGS.md 7.2) | workflow test; settings check reproduced with `gh api` |
| the pipe | the service answers only the `iap-runner` account (OS identity); a developer process connecting is refused and audited | `test_only_the_runner_account_is_served`, `test_service_refuses_a_caller_that_is_not_the_runner` |
| the result itself | `result.json` is what the registered analysis chose to write; the prereg's command is reviewed before signing and is part of the signed document | signature tests |

Residual: a registered command that writes sealed rows into `result.json` would publish them. The guard is
review of the command before signing, not code.

## Property 2: `iap-dev` cannot change the workflow, image digest, prereg or secrets without an approval it does not hold

| object | control | test or check |
|---|---|---|
| `.github/workflows/registered-run.yml`, `tools/runner/`, `tools/sealed_service.py`, `tools/admin/`, `research/audit/` | `CODEOWNERS` lines owned by `@AshJha0`; the `main` ruleset requires code-owner review (REPO_SETTINGS.md 7.1) | `test_codeowners_cover_the_workflow_and_the_runner`; `gh api` check of the ruleset |
| the image digest | bound inside the signed prereg (`registered_run.image_digest`) and compared with `deployment/docker/IMAGE_DIGEST` at the checked-out commit | `test_image_digest_mismatch_is_refused` |
| the prereg | Ed25519 signature under the trust root; any edit invalidates it | `test_tampered_signature_is_refused_before_any_request`, `test_unsigned_prereg_is_refused`, `test_untrusted_signing_key_is_refused` |
| secrets | there are none: the workflow reads no `secrets.*`; the pipe authenticates by OS identity | workflow test (`secrets.` absent) |
| the environment approval | `sealed` requires the owner's review; the runner group is restricted to this repository and to `workflow_dispatch` runs | REPO_SETTINGS.md 7.2, `gh api` |

Residual: the owner's GitHub account approves and the owner's user develops: the same person. See custody.

## Property 3: one run per registration

The service's ledger is keyed by the canonical sha256 of the signed prereg and indexed by prereg id. A second
request with the same hash, or the same prereg id signed again at a new commit, is refused and audited; a new
registration is a new prereg id. Tests: `test_one_run_per_registration` (service),
`test_dry_registration_runs_once_and_is_refused_the_second_time` (end to end, including the re-signed-at-a-new-
commit case).

## Property 4: hash verification precedes any read; no retry without a new registration

Runner side (before any request): signature, `registered_run` block, image digest, `analysis_identity`
(scripts, code tree, configs, reference data, lock, installed packages, interpreter, bound commit), dataset id
bound. Service side (before any byte): every dataset file re-hashed against its record; a mismatch is a
**failed** registration. Runner side (before the analysis): served file hashes recomputed and compared with
the bound `dataset_files`. Tests: `test_changed_bound_script_fails_the_freeze_hashes`,
`test_bound_commit_that_is_not_an_ancestor_of_head_is_refused`, `test_dataset_id_must_be_bound_by_the_identity`,
`test_hash_mismatch_fails_the_registration_with_no_retry` (service),
`test_tampered_sealed_bytes_fail_the_registration_with_no_retry` and
`test_served_dataset_that_differs_from_the_bound_files_does_not_run` (end to end).

## Property 5: results are append-only

Each run adds one commit to the `results` branch from the `iap-runner` git identity, in a separate worktree:
`results/<prereg>/<seq>/result.json`, `hashes.json` (result hashes, prereg hash, dataset id and files, seq,
chain head, image digest, commit, evidence class) and one line in `research/audit/CHECKPOINTS.jsonl`. The branch
ruleset blocks force pushes and deletion (REPO_SETTINGS.md 7.3). The service stores its own copy of the result
hash; `tools/runner/run.py verify-result` compares a committed `hashes.json` with it. Tests:
`test_results_branch_appends_and_never_overwrites`, `test_rewritten_result_is_detected`,
`test_result_hash_is_stored_once_and_verified`.

## Property 6: custody

While the owner holds development, administration, approval and sealed credentials alone, a runner result is
**mechanically isolated**, not **independently confirmed**. The runner writes the words into every
`hashes.json` (`"evidence_class": "mechanically isolated"`); release notes and result documents use the same
words (`test_custody_label_is_written_into_every_result`). The class upgrades when an approval or custody
authority other than the developer exists (owner item O-3): a second required reviewer on `sealed`, or a
service and runner host administered by someone else.

## What this model does not cover

- The workstation administrator (the owner). Every control above is a file ACL, a pipe identity check or a
  GitHub setting the administrator can change; see SEALED_DATA.md section 4.
- GitHub as a platform, and the self-hosted runner software itself.
- A registered command that is itself the exfiltration (reviewed before signing).
- The repository is public: GitHub advises against self-hosted runners for public repositories because a
  pull request from a fork can run on them. This workflow has no `pull_request` trigger, the runner group is
  restricted to this repository and to selected workflows, and fork pull requests require approval before any
  workflow runs (REPO_SETTINGS.md 7.4; defect D-47).

## Open items (defect register)

D-45 process-image query between standard users · D-46 non-development identities need stored credentials to
test · D-47 self-hosted runner on a public repository · D-48 the results ruleset cannot bind an administrator ·
D-49 encryption at rest · D-50 the pinned Linux image cannot execute under `iap-runner` with pipe access, so the
runner verifies and records the digest and executes in the identity-bound host environment · D-51 the sealed
service needs read access to the repository code and a pinned trust-root copy · D-52 routes that relied on the
removed `IAP_HOLDOUT_AUTHORISED` inheritance.
