# Repository and operational settings (not enforced by files)

`GOVERNANCE.md` and `SECURITY.md` describe controls. Some of them are enforced
by a file in this repository (CI, `CODEOWNERS`, the structural checks in
`tests/harness/check_deployment.py`); the ones below are **repository or
operations settings that live outside the tree**. Until one is configured, the
corresponding sentence in the governance documents is a requirement, not a fact.

**How this page stays true (v2.0 stage 11, S4).** Section 1 is generated from the live settings:
`python tools/repo_settings_snapshot.py write` reads them with `gh api` (branch rules, rulesets,
Actions permissions, secret scanning, vulnerability reporting) into
[repo_settings_live.json](repo_settings_live.json); `python/tests/test_repo_settings_doc.py` fails
when the matrix below and that snapshot disagree, and the CI `governance` job warns when the live
settings drift from the snapshot. The snapshot was taken on **2026-10-25**; the v1.19 audit
(plan section 2.3) had found 13 contexts documented, 6 in the JSON block and 17 live. Everything
below is run by the repository owner (`AshJha0`), once, with an authenticated `gh` that has admin
rights on the repository. Nothing in this repository applies these settings for you, by design.

## 1. Branch ruleset for `main`: the required-check matrix

Live on 2026-10-25: one ruleset, `protect-main` (active, last edited 2026-10-18), rules `deletion`,
`non_fast_forward`, `pull_request` (0 approving reviews, no code-owner review, no thread-resolution
requirement, merge / squash / rebase allowed) and `required_status_checks` with the **strict**
policy (the branch must be up to date with `main`) over **17 required** contexts. The context name
is the job's `name:` if set, else its id. Each row says which change class the check guards
(research correctness, data integrity, execution, infrastructure) and the risk it catches; the
per-PR cost is the job's wall time on the stage 11 CI run (filled from that run's job table).

<!-- required-check matrix: generated contexts -->
| context | job (workflow) | change class | risk the check catches | per-PR cost |
|---|---|---|---|---|
| `python` | `python` matrix 3.11 (ci.yml) | research correctness, execution, data integrity | the Python reference semantics, the confirmatory gate (R1), the data contracts and leakage canaries, the guard-hook AST test, mypy; the fast suite must RUN on the seeded dataset, not skip | ~10 min |
| `python-312` | `python` matrix 3.12 (ci.yml) | infrastructure | interpreter drift between CI (3.11) and the pinned reproduction image (CPython 3.12.10): a hash or float that moves with the interpreter | ~10 min |
| `python-slow` | `python-slow` (ci.yml) | research correctness | the study-length tests (replays, dependence audits, equivalence runs) that the fast suite excludes | ~12 min |
| `integration` | `integration` (ci.yml) | execution, infrastructure | cross-component runs and same-seed byte determinism of the replay | ~5 min |
| `cpp` | `cpp` (ci.yml): build + ctest since stage 11 | execution (parity-only port, D5(a)) | a Python execution-rule change not ported, or a C++ change that breaks its own tests; benchmark guards, sanitizers and coverage moved weekly (S2) | ~6 min |
| `rust` | `rust` (ci.yml): `risk` + `features` since stage 11 | data integrity, risk | the normative risk engine (PLATFORM_CONVENTIONS section 11.1) and the pyo3 feature engine; the nine frozen crates run weekly, their goldens per PR in `golden` | ~6 min |
| `golden` | `golden` (ci.yml) | execution, data integrity | promotion gate 10: byte-for-byte parity of every language's golden group, including every Rust crate's golden target | ~12 min |
| `deployment` | `deployment` (ci.yml) | infrastructure, documentation truth | structural validation of deployment/, the polyglot FROZEN policy, the headline numbers re-derived from the CI artefacts | ~4 min |
| `CodeQL` | codeql.yml summary check | infrastructure (security) | static analysis findings on Python, C++ and Rust | ~15 min |
| `analyze (rust)` | `analyze` matrix entry rust (codeql.yml) | infrastructure (security) | the Rust extractor must see the real build (D5(a) note in codeql.yml) | ~15 min |
| `governance` | `governance` (ci.yml) | research integrity | an unsigned or stale-signed key registry, a broken blackboard chain, a promoted model without its content hash, a prereg citing an unlisted dataset, a ledger that shrank; the live-settings drift warning (S4) | ~3 min |
| `advisory` | alias of `lint-and-secrets` (ci.yml) | infrastructure, architecture | gitleaks over the whole history, ruff, the import-linter architecture contracts (S3): all blocking. The job was renamed in stage 11; the alias reports under the old context until owner action 1 below | ~3 min |
| `rust-pyo3` | `rust-pyo3` (ci.yml) | data integrity | the Rust feature engine's wheel builds and its parity tests RUN (a skip fails) | ~8 min |
| `supply-chain` | `supply-chain` (ci.yml) | infrastructure (security) | pip-audit of both hashed locks, cargo audit and deny, trivy on lockfiles and Dockerfiles; ignores only with reason and expiry | ~5 min |
| `agent-evals` | `agent-evals` (ci.yml) | research integrity (agents) | a stub-model agent evaluation that regressed against its committed baseline | ~3 min |
| `docs` | `docs` (ci.yml): `check_docs.py --strict` since stage 11 | documentation truth | a dead relative link, an `iap.*` API name that no longer imports, a status phrase older than the last tag | ~3 min |
| `reproducible-build` | `reproducible-build` (ci.yml) | reproduction | the wheel built twice must match (the image compare is report-only) | ~10 min |
<!-- end required-check matrix -->

Not required, by design: `images` (skipped on most PRs; a skipped required check blocks the merge),
`coverage` (the whole-suite floor; to be required once it has a run history), the weekly
`cpp-full`, `cpp-sanitizers` and `rust-workspace` jobs of `weekly-structural.yml` (not on the PR
path), `regenerate` and `risk-fuzz` (dispatch and nightly). Wall time: the v1.19 audit measured
about 14 minutes and about 52 job-minutes per run before stage 11; the stage 11 run's figures are
in the CHANGELOG entry of this stage.

`required_approving_review_count` is 0 because GitHub does not let an author approve their own
pull request and this repository has one maintainer: a count of 1 would make every merge
impossible. The documented policy (GOVERNANCE.md section 1: 1 to 2 reviewers by change class)
needs a second maintainer; when there is one, change the count to 1 (or 2 for the risk-engine and
contracts classes), set `require_code_owner_review` to true and give the second maintainer a line
in `CODEOWNERS`.

### Owner actions on the ruleset (proposed in stage 11, none applied)

1. **Rename the context `advisory` to `lint-and-secrets`** (then delete the alias job in
   `ci.yml`). The rename is the one change; every other context stays as live. `PUT` replaces the
   whole ruleset, so the payload carries all 17 contexts:

   ```bash
   gh api -X PUT repos/AshJha0/intraday-alpha-platform/rulesets/24455225 --input - <<'JSON'
   {
     "name": "protect-main", "target": "branch", "enforcement": "active",
     "conditions": { "ref_name": { "include": ["~DEFAULT_BRANCH"], "exclude": [] } },
     "rules": [
       { "type": "deletion" }, { "type": "non_fast_forward" },
       { "type": "pull_request", "parameters": {
           "required_approving_review_count": 0, "dismiss_stale_reviews_on_push": false,
           "require_code_owner_review": false, "require_last_push_approval": false,
           "required_review_thread_resolution": false,
           "require_extra_approval_for_unattributed_changes": false,
           "allowed_merge_methods": ["merge", "squash", "rebase"] } },
       { "type": "required_status_checks", "parameters": {
           "strict_required_status_checks_policy": true, "do_not_enforce_on_create": false,
           "required_status_checks": [
             { "context": "python", "integration_id": 15368 }, { "context": "python-312", "integration_id": 15368 },
             { "context": "python-slow" }, { "context": "integration", "integration_id": 15368 },
             { "context": "cpp", "integration_id": 15368 }, { "context": "rust", "integration_id": 15368 },
             { "context": "golden", "integration_id": 15368 }, { "context": "deployment", "integration_id": 15368 },
             { "context": "CodeQL", "integration_id": 57789 }, { "context": "analyze (rust)", "integration_id": 15368 },
             { "context": "governance", "integration_id": 15368 }, { "context": "lint-and-secrets", "integration_id": 15368 },
             { "context": "rust-pyo3", "integration_id": 15368 }, { "context": "supply-chain", "integration_id": 15368 },
             { "context": "agent-evals" }, { "context": "docs" }, { "context": "reproducible-build" } ] } } ] }
   JSON
   # check, then refresh the snapshot and the matrix row:
   gh api repos/AshJha0/intraday-alpha-platform/rules/branches/main --jq '.[] | select(.type=="required_status_checks") | .parameters.required_status_checks[].context'
   python tools/repo_settings_snapshot.py write
   ```

2. **`cpp` as a required context: keep.** S2 asked for C++ out of the PR path; the job now costs
   build + ctest only, and dropping the context would leave the C++ port's own tests unrun until
   the weekly job while the port is still the measured hot path in the documentation. Decide
   again when C++ is retired or the golden gate alone is judged enough; the ruleset edit is the
   owner's either way.
3. **The "release tags" ruleset of section 2 does not exist live** (only `protect-main` does):
   apply it or delete section 2's claim.
4. Consider `coverage` as a required context once it has a run history (unchanged recommendation).
5. Actions settings read on 2026-10-25: `allowed_actions: all` and `sha_pinning_required: false`
   (every workflow pins actions by SHA and `check_deployment.py` enforces it, so turning
   `sha_pinning_required` on costs nothing); private vulnerability reporting is **disabled**
   although section 3 asks for it; Dependabot security updates, secret scanning and push
   protection are enabled.

## 2. Release tags

Release tags (`v*`) trigger `.github/workflows/release.yml`, which publishes
images. Make them immutable so a published tag cannot be moved or deleted:

```bash
gh api -X POST repos/AshJha0/intraday-alpha-platform/rulesets --input - <<'JSON'
{
  "name": "release tags",
  "target": "tag",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["refs/tags/v*"], "exclude": [] } },
  "rules": [ { "type": "update" }, { "type": "deletion" }, { "type": "non_fast_forward" } ]
}
JSON
```

**Signed tags are a practice, not an enforced control.** Create release tags
with `git tag -s vX.Y.Z` and verify with `git tag -v vX.Y.Z`. Neither GitHub
rulesets nor any workflow in this repository checks the signature, and none of
the existing tags (`v1.0.1`, `v1.1.0`, `v1.1.1`, `v1.2.0`) is claimed to be
signed. What the release workflow does provide is a build-provenance
attestation per image (see section 5).

## 3. Dependabot alerts, security updates, secret scanning

Version updates are configured by `.github/dependabot.yml` (a file). Alerts and
security updates are settings:

```bash
# Dependabot alerts
gh api -X PUT repos/AshJha0/intraday-alpha-platform/vulnerability-alerts

# Dependabot security updates (automatic PRs for vulnerable dependencies)
gh api -X PUT repos/AshJha0/intraday-alpha-platform/automated-security-fixes

# Secret scanning + push protection (already enabled on 2026-10-03; idempotent)
gh api -X PATCH repos/AshJha0/intraday-alpha-platform --input - <<'JSON'
{ "security_and_analysis": {
    "secret_scanning": { "status": "enabled" },
    "secret_scanning_push_protection": { "status": "enabled" } } }
JSON

# Private vulnerability reporting (gives SECURITY.md section 5 a real channel)
gh api -X PUT repos/AshJha0/intraday-alpha-platform/private-vulnerability-reporting
```

CodeQL needs no setting: `.github/workflows/codeql.yml` uploads results once the
workflow has run on `main`.

## 4. Actions permissions

Workflows declare least-privilege `permissions:` themselves. Also restrict what
Actions may do by default:

```bash
# default GITHUB_TOKEN is read-only; Actions cannot approve pull requests
gh api -X PUT repos/AshJha0/intraday-alpha-platform/actions/permissions/workflow --input - <<'JSON'
{ "default_workflow_permissions": "read", "can_approve_pull_request_reviews": false }
JSON
```

## 5. Release procedure (what the files do and what stays manual)

1. Merge to `main` with `ci` green.
2. `git tag -s vX.Y.Z && git push origin vX.Y.Z`. `release.yml` verifies the
   commit has a green `ci` run, builds the three images, pushes
   `ghcr.io/ashjha0/intraday-alpha-platform-{python,cpp,rust}:vX.Y.Z`
   (the `java` image was retired in v1.17, F9),
   attests build provenance for each, writes `release-manifest.json` (image
   digests) and attaches it to the GitHub release (creating the release with
   generated notes if it does not exist; existing notes are left alone).
3. **Scripted (v1.17 F2):** `python tools/release.py publish --version X.Y.Z`
   downloads `release-manifest.json` from the release and rewrites the
   Kubernetes manifests (`deployment/k8s/{platform-metrics,cronjob-data-pipeline,cronjob-state-backup}.yaml`)
   to `<name>:vX.Y.Z@sha256:...` (`pin-digests` alone, dry-run by default);
   open the "release: pin vX.Y.Z image digests" PR with the result. Compose
   keeps the tag (a `build:` service cannot carry a digest). The repository references
   `v1.5.0` by tag only: those images exist once the `v1.5.0` tag has been
   pushed and `release.yml` has run for it (it ran for the first time for
   `v1.3.0`; no image was ever published for `v1.2.0`, which was tagged
   before the release workflow existed), and the digests are pinned from
   that run's `release-manifest.json` afterwards.
4. Verify provenance before deploying:
   `gh attestation verify oci://ghcr.io/ashjha0/intraday-alpha-platform-python@sha256:<digest> --repo AshJha0/intraday-alpha-platform`.
5. Make the GHCR packages public (or grant the cluster a pull secret):
   packages are created private by default.

`release.yml` can only be exercised in full by pushing a tag, so a release is
its own test. Its `verify-ci` guard failed the v1.4.0 release once on an API
error although CI was green; since v1.5.0 the guard is
`tests/harness/verify_ci_green.py`, which retries an API error, polls a run that has not
finished yet and fails on a failed or missing run, and it can be run alone
against a given commit:
`gh workflow run release.yml --ref <branch> -f dry_run=true -f sha=<40 hex>`.

## 6. Operational settings the platform needs but the repository cannot hold

| Setting | Why it is outside the tree | What exists today |
|---|---|---|
| Off-host shipping of the audit JSONL (`risk_audit.jsonl`, `admin_audit.jsonl`, `config_audit.jsonl`, `decision_traces.jsonl`) | needs a log sink and credentials | **Nothing ships them.** The files are written to the state volume (`/data/state`, PVC `iap-platform-state`). GOVERNANCE.md section 3 requires daily off-host shipping; that is not implemented. |
| Alert delivery | receiver URLs are secrets | Alertmanager routes `page` and `ticket` alerts (v1.12.0); compose defaults to a local echo sink. Supply real URLs: compose `ALERT_PAGE_URL_FILE` / `ALERT_TICKET_URL_FILE`; k8s Secret `iap-alertmanager-webhook` with keys `page_url` and `ticket_url` (docs/runbooks/RUNBOOK_alerting.md §2). Point the `watchdog` receiver at a dead-man's-switch service to alarm when the heartbeat stops. |
| StorageClass | cluster specific | `storageClassName: standard` in `deployment/k8s/pvc.yaml` is a placeholder: replace it with a class that exists (`kubectl get storageclass`). |
| NetworkPolicy enforcement | needs a CNI that implements it | Policies are in `deployment/k8s/networkpolicy.yaml`; a CNI without NetworkPolicy support ignores them. |
| Image vulnerability scan (`trivy image`) | SECURITY.md asks for it before digest pinning | Not wired into any workflow. |

## 7. v2.0 stage 2: the sealed environment, the registered runner and the results branch

Required by U0 section 2 (properties 1, 2 and 5) and documented in
[RUNNER_THREAT_MODEL.md](RUNNER_THREAT_MODEL.md). None of this is applied by a file in the repository; the owner
sets it once with an admin `gh`. Each block ends with the `gh api` read that reproduces the check (CR1
acceptance: "a passing test or a documented settings check reproduced by `gh api`"). Status as of 2026-10-25:
**not yet set** (the owner has not run these; the branch `results` and the environment `sealed` did not exist
on GitHub as of 2026-10-25).

### 7.1 Code-owner review for the runner paths (property 2)

The development identity must not be able to change `.github/workflows/registered-run.yml`, `tools/runner/`,
`tools/sealed_service.py`, `tools/admin/` or `research/audit/` without a review it does not hold. `CODEOWNERS`
names `@AshJha0` for these paths; the `main` ruleset must require code-owner review for them to bind. Because a
pull-request author cannot approve their own pull request, this requires a second maintainer (D-25) or a
narrower ruleset that applies the requirement only when those paths change:

```bash
# a second ruleset on main, scoped by file path to the runner surfaces
gh api -X POST repos/AshJha0/intraday-alpha-platform/rulesets --input - <<'JSON'
{
  "name": "registered-runner surfaces",
  "target": "branch",
  "enforcement": "active",
  "conditions": {
    "ref_name": { "include": ["~DEFAULT_BRANCH"], "exclude": [] }
  },
  "rules": [
    { "type": "pull_request",
      "parameters": {
        "required_approving_review_count": 1,
        "dismiss_stale_reviews_on_push": true,
        "require_code_owner_review": true,
        "require_last_push_approval": true,
        "required_review_thread_resolution": true
      } },
    { "type": "file_path_restriction",
      "parameters": { "restricted_file_paths": [".github/workflows/registered-run.yml", "tools/runner/**", "tools/sealed_service.py", "tools/admin/**", "research/audit/**"] } }
  ]
}
JSON
# check
gh api repos/AshJha0/intraday-alpha-platform/rulesets --jq '.[] | {name, enforcement}'
```

Until a second maintainer exists, `require_code_owner_review` on a single-maintainer repository blocks every
merge touching these paths (including this PR's follow-ups); the owner decides between the second maintainer
(O-3, which also upgrades the evidence class) and the file-path restriction above, which stops a push that
changes these paths outside a pull request. Record the choice as a decision record.

### 7.2 The `sealed` environment (properties 1 and 2)

```bash
# create / update the environment: the owner is the required reviewer, no self-review bypass
gh api -X PUT repos/AshJha0/intraday-alpha-platform/environments/sealed --input - <<'JSON'
{ "wait_timer": 0,
  "prevent_self_review": false,
  "reviewers": [ { "type": "User", "id": OWNER_USER_ID } ],
  "deployment_branch_policy": { "protected_branches": true, "custom_branch_policies": false } }
JSON
# OWNER_USER_ID: gh api users/AshJha0 --jq .id
# check: required reviewer present, no secrets, no debug variables
gh api repos/AshJha0/intraday-alpha-platform/environments/sealed --jq '.protection_rules'
gh api repos/AshJha0/intraday-alpha-platform/environments/sealed/secrets --jq '.total_count'      # must be 0
gh api repos/AshJha0/intraday-alpha-platform/environments/sealed/variables --jq '.variables[].name' # must not list ACTIONS_STEP_DEBUG / ACTIONS_RUNNER_DEBUG
gh api repos/AshJha0/intraday-alpha-platform/actions/variables --jq '.variables[].name'            # same at repository level
gh api repos/AshJha0/intraday-alpha-platform/actions/secrets --jq '.secrets[].name'                # no ACTIONS_STEP_DEBUG
```

`prevent_self_review` is `false` because the owner both starts and approves runs while there is one
maintainer (custody: the class stays "mechanically isolated"); set it `true` when a second reviewer exists.

### 7.3 The `results` branch (property 5)

Created by the first registered run (orphan branch, committed by `iap-runner`). Protect it before that:

```bash
gh api -X POST repos/AshJha0/intraday-alpha-platform/rulesets --input - <<'JSON'
{
  "name": "results append-only",
  "target": "branch",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["refs/heads/results"], "exclude": [] } },
  "bypass_actors": [],
  "rules": [ { "type": "deletion" }, { "type": "non_fast_forward" } ]
}
JSON
# check
gh api repos/AshJha0/intraday-alpha-platform/rulesets --jq '.[] | select(.name=="results append-only") | .enforcement'
gh api repos/AshJha0/intraday-alpha-platform/branches/results --jq '.protected'
```

No pull-request requirement on `results`: the runner pushes one fast-forward commit per run with the job
token. A repository administrator can still bypass or delete a ruleset (D-51); the service's copy of each
result hash and the published checkpoints make that detectable.

### 7.4 Actions policy for the self-hosted runner (public repository)

```bash
# the default token stays read-only (section 4); fork pull requests need approval before any workflow runs
gh api repos/AshJha0/intraday-alpha-platform/actions/permissions --jq '{enabled, allowed_actions}'
# the registered runner and its labels (repository-level runner)
gh api repos/AshJha0/intraday-alpha-platform/actions/runners --jq '.runners[] | {name, status, labels: [.labels[].name]}'
```

The registered runner must show the labels `self-hosted`, `windows`, `iap-runner` and no other workflow may
target them (grep `.github/workflows/` for `iap-runner`: only `registered-run.yml`). "Require approval for all
outside collaborators" is set in the Actions settings page (no API field).

### 7.5 What stays unenforced

A ruleset binds everyone except whoever the ruleset's bypass list names and, in practice, the repository
administrator who can edit the ruleset. On a single-maintainer repository that administrator is the developer.
The register's D-25, D-50 and D-51 record this; the custody label (property 6) is the honest description.
