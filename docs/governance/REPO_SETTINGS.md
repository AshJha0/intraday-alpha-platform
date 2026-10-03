# Repository and operational settings (not enforced by files)

`GOVERNANCE.md` and `SECURITY.md` describe controls. Some of them are enforced
by a file in this repository (CI, `CODEOWNERS`, the structural checks in
`tests/harness/check_deployment.py`); the ones below are **repository or
operations settings that live outside the tree** and are **not yet configured**
as of 2026-10-03. Until they are, the corresponding sentence in the governance
documents is a requirement, not a fact.

Verified on 2026-10-03 with `gh api`: `main` has no branch protection
(`GET /branches/main/protection` returns 404), `GET /rulesets` returns `[]`,
secret scanning and push protection are enabled, Dependabot security updates
are disabled. Re-check before relying on any of this.

Everything below is run by the repository owner (`AshJha0`), once, with an
authenticated `gh` that has admin rights on the repository. Nothing in this
repository applies these settings for you, by design.

## 1. Branch ruleset for `main`

Requires a pull request, the seven CI jobs as status checks, and blocks force
pushes and branch deletion. The required check names are the job ids of
`.github/workflows/ci.yml`: `python`, `integration`, `cpp`, `rust`, `java`,
`golden`, `deployment`. Do **not** add `images` (it is skipped on pull requests
that do not touch image inputs, and a skipped required check blocks the merge)
or the non-blocking `advisory` job. `cpp-sanitizers` is blocking in CI and may be added to the required list.

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
          { "context": "java" },
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
   commit has a green `ci` run, builds the four images, pushes
   `ghcr.io/ashjha0/intraday-alpha-platform-{python,java,cpp,rust}:vX.Y.Z`,
   attests build provenance for each, writes `release-manifest.json` (image
   digests) and attaches it to the GitHub release (creating the release with
   generated notes if it does not exist; existing notes are left alone).
3. **Manual:** pin the deployment manifests by digest from
   `release-manifest.json` (`<name>:vX.Y.Z@sha256:...` in
   `deployment/docker/docker-compose.yml`,
   `deployment/k8s/java-platform.yaml`, `deployment/k8s/cronjob-data-pipeline.yaml`)
   and bump the version tag in the same files. The repository references
   `v1.2.0`, which was tagged before the release workflow existed, so no such
   image exists yet; the next release must bump these references.
4. Verify provenance before deploying:
   `gh attestation verify oci://ghcr.io/ashjha0/intraday-alpha-platform-java@sha256:<digest> --repo AshJha0/intraday-alpha-platform`.
5. Make the GHCR packages public (or grant the cluster a pull secret):
   packages are created private by default.

`release.yml` has not been run: it can only be exercised by pushing a tag, so
its first real run is its test.

## 6. Operational settings the platform needs but the repository cannot hold

| Setting | Why it is outside the tree | What exists today |
|---|---|---|
| Off-host shipping of the audit JSONL (`risk_audit.jsonl`, `admin_audit.jsonl`, `config_audit.jsonl`, `decision_traces.jsonl`) | needs a log sink and credentials | **Nothing ships them.** The files are written to the state volume (`/data/state`, PVC `iap-java-state`). GOVERNANCE.md section 3 requires daily off-host shipping; that is not implemented. |
| Alert delivery | webhook URL is a secret | Alertmanager is deployed and routes alerts; the receiver URL defaults to a placeholder that delivers nowhere. Supply it: compose `ALERT_WEBHOOK_URL_FILE=<file>`; k8s `kubectl -n intraday-alpha create secret generic iap-alertmanager-webhook --from-literal=url=<https url>`. Point the `watchdog` receiver at a dead-man's-switch service to alarm when the heartbeat stops. |
| StorageClass | cluster specific | `storageClassName: standard` in `deployment/k8s/pvc.yaml` is a placeholder: replace it with a class that exists (`kubectl get storageclass`). |
| NetworkPolicy enforcement | needs a CNI that implements it | Policies are in `deployment/k8s/networkpolicy.yaml`; a CNI without NetworkPolicy support ignores them. |
| Image vulnerability scan (`trivy image`) | SECURITY.md asks for it before digest pinning | Not wired into any workflow. |
