# Repository and operational settings (not enforced by files)

`GOVERNANCE.md` and `SECURITY.md` describe controls. Some of them are enforced
by a file in this repository (CI, `CODEOWNERS`, the structural checks in
`tests/harness/check_deployment.py`); the ones below are **repository or
operations settings that live outside the tree**. Until one is configured, the
corresponding sentence in the governance documents is a requirement, not a fact.

Verified on 2026-10-14 with `gh api`: the ruleset `protect-main` is active on
`main` and requires the checks `python`, `integration`, `cpp`, `rust`,
`golden`, `deployment`, `CodeQL`, `governance`, `advisory`, `rust-pyo3`,
`python-slow`, `supply-chain` and `analyze (rust)` (`java` was dropped on
2026-10-11). The rest of this page (secret scanning, Dependabot and the
operational settings) was last checked on 2026-10-03. Re-check before relying
on any of this.

Everything below is run by the repository owner (`AshJha0`), once, with an
authenticated `gh` that has admin rights on the repository. Nothing in this
repository applies these settings for you, by design.

## 1. Branch ruleset for `main`

Requires a pull request, the six CI jobs as status checks, and blocks force
pushes and branch deletion. The required check names are the job ids of
`.github/workflows/ci.yml`: `python`, `integration`, `cpp`, `rust`,
`golden`, `deployment`. (v1.15: the `java` job was removed when the Java
engine was archived to `archive/java/`; drop `java` from an existing ruleset.) Do **not** add `images` (it is skipped on pull requests
that do not touch image inputs, and a skipped required check blocks the merge)
or, without a decision, the `advisory` job (ruff + gitleaks, both blocking). Since v1.14 the dependency audits run in the blocking `supply-chain` job, and CodeQL also analyses Rust (check `analyze (rust)`); both may be added to the required list. `cpp-sanitizers` is blocking in CI and may be added to the required list. Since v1.16 `agent-evals` is blocking in CI (no `continue-on-error`) and may be added to the required list.

`required_approving_review_count` is 0 below because GitHub does not let an
author approve their own pull request, and this repository currently has one
maintainer: a count of 1 would make every merge impossible. The documented
policy (GOVERNANCE.md section 1: 1 to 2 reviewers by change class) needs a
second maintainer; when there is one, change the count to 1 (or 2 for the
risk-engine and contracts classes), set `require_code_owner_review` to true and
give the second maintainer a line in `CODEOWNERS`.

```bash
gh api -X POST repos/AshJha0/intraday-alpha-platform/rulesets --input - <<'JSON'
{
  "name": "main",
  "target": "branch",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["~DEFAULT_BRANCH"], "exclude": [] } },
  "rules": [
    { "type": "deletion" },
    { "type": "non_fast_forward" },
    { "type": "pull_request",
      "parameters": {
        "required_approving_review_count": 0,
        "dismiss_stale_reviews_on_push": true,
        "require_code_owner_review": false,
        "require_last_push_approval": false,
        "required_review_thread_resolution": true
      } },
    { "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": true,
        "required_status_checks": [
          { "context": "python" },
          { "context": "integration" },
          { "context": "cpp" },
          { "context": "rust" },
          { "context": "golden" },
          { "context": "deployment" }
        ]
      } }
  ]
}
JSON
```

Check it: `gh api repos/AshJha0/intraday-alpha-platform/rulesets --jq '.[].name'`.
To change it later, `gh api -X PUT repos/AshJha0/intraday-alpha-platform/rulesets/<id> --input ...`.

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
token. A repository administrator can still bypass or delete a ruleset (D-48); the service's copy of each
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
The register's D-25, D-47 and D-48 record this; the custody label (property 6) is the honest description.
