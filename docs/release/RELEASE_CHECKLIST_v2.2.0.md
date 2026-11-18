# Release checklist, v2.2.0

Written 2026-11-18 from the repository's own release procedure: `tools/release.py` (its module
docstring is the procedure as code), `.github/workflows/release.yml`, `.github/workflows/repro-image.yml`,
the `docs` job of `.github/workflows/ci.yml`, `tests/harness/check_deployment.py` (`image_pinning`), and
the two v2.1.0 release pull requests on `main`: #414 (commit `b73dfac1`, "changelog section, release
notes, status lines") and #415 (commits `13ede650` "pin v2.1.0 image digests" and `12090be1` "compose
image tags to v2.1.0"). `docs/RUNBOOKS.md` has no release section; this file and `tools/release.py flow`
are the procedure.

**The plain idea.** A release is two pull requests and one tag. The first pull request adds the words
(changelog, release notes, status lines). After it merges and CI is green on the merge commit, the tag
is pushed; the tag builds the images and records their digests. The second pull request writes those
digests into the Kubernetes manifests and moves the compose tags, because the digests do not exist
until the tag has been built.

Every command below runs from the repository root. Each step is marked **before merge** (can be done
on a branch) or **after merge** (needs the merged commit on `main`, the tag, or the built images).

## 0. Preconditions (before merge)

| # | check | command or file | status on 2026-11-18 |
|---|---|---|---|
| 0.1 | The round's work is on `main`: `round0-gates` merged by its own pull request | `git log --oneline main..round0-gates` prints nothing | not merged |
| 0.2 | Every `[[PENDING: ...]]` placeholder is replaced by a number copied from a committed artefact, or by a plain statement that the result does not exist | `git grep -n "\[\[PENDING:"` prints nothing | placeholders present (pooled result, three families, look counts, holdout outcome) |
| 0.3 | The coordinator's experiment-ledger entries for the round are committed; every look count quoted in a document then equals `research/experiments.json` | `python tests/harness/check_headline_numbers.py` (`experiment_ledger` row) and `python tests/harness/check_docs.py --release` (`release_truth`) | the ledger still reads 6,826 looks; the round's looks are not entered yet |
| 0.4 | The README parity table matches the tree. `itchbars` added Rust tests: the tree has 388 `#[test]` functions, the table says 376, so `check_headline_numbers.py` fails `parity_table_vs_source_tree` | update the README table and the prose counts in `docs/ARCHITECTURE.md` and `docs/BUILD_NOTES.md` from a `main` CI run of the merged round (CI derives them with `tests/harness/parity_counts.py`); do not type a count that no CI run printed | failing, pre-existing on `round0-gates` |
| 0.5 | No signed document, ledger entry or holdout registry change is made by the release pull request | `git diff --stat main...release/v2.2.0` lists only docs, `CHANGELOG.md`, `README.md`, `docs/index.html` | |

## 1. The release pull request (before merge)

Branch name `release/v2.2.0`. The name matters: on a `release/*` head the `docs` CI job also runs
`check_docs.py --release`, which fails on any undated status phrase and any look count that differs
from the ledger.

```powershell
git switch -c release/v2.2.0 origin/main
```

| # | file | change | v2.1.0 precedent |
|---|---|---|---|
| 1.1 | `CHANGELOG.md` | a `## v2.2.0 (YYYY-MM-DD): <title>` section at the top, negatives first. The heading must read exactly `## v2.2.0`: `release.py publish` takes the tag message and the release body from that section, and refuses an `## Unreleased` heading that still names v2.2 | `b73dfac1` |
| 1.2 | `docs/release/v2.2.0_RELEASE_NOTES.md` | the notes, "What this release is" first | `b73dfac1` |
| 1.3 | `README.md` line 3 | the status line: `Status, v2.2.0 (<date>)` | `b73dfac1` |
| 1.4 | `docs/index.html` | a new `<div class="honest new">` block for v2.2.0 above the v2.1.0 one, and the v2.1.0 block's class changed from `honest new` to `honest`. The `publish` site check needs the full string `v2.2.0` on the Pages site | `b73dfac1` |
| 1.5 | version strings | none in code. The v2.x releases did not bump `python/pyproject.toml` (`version = "1.18.0"`) or the Rust workspace (`version = "0.1.0"`); the version of record is the tag. Bumping them is an owner decision, not part of this procedure | none bumped at v2.1.0 |
| 1.6 | compose and Kubernetes image tags | **not in this pull request.** `check_deployment.py` `image_pinning` requires one version tag across every `ghcr.io/ashjha0/intraday-alpha-platform-*` reference in compose and in the Kubernetes manifests. The manifests can only move once the v2.2.0 digests exist (step 4), so compose moves with them | `12090be1` came after `13ede650` |

Local checks before pushing (no network):

```powershell
$env:PYTHONPATH = "python/src"; $env:PYTHONUTF8 = "1"
.venv-ci\Scripts\python tests/harness/check_docs.py --strict
.venv-ci\Scripts\python tests/harness/check_docs.py --release
.venv-ci\Scripts\python tests/harness/check_headline_numbers.py
.venv-ci\Scripts\python tests/harness/check_deployment.py
.venv-ci\Scripts\python tools/release.py flow
```

All must print 0 failures. `release.py flow` only prints the sequence.

**CI on the release pull request.** Required: the `ci` workflow green (including `docs`,
`headline numbers vs artefacts` and `deployment`), `codeql`, and `repro-image`. The round changed
`python/pyproject.toml` and `.github/workflows/ci.yml` but neither `python/requirements-ci.txt` nor
`deployment/docker/Dockerfile.repro`, so the repro image inputs hash should be unchanged and no
digest commit is expected. If `repro-image` does commit a digest to the branch, the bot's push starts
no workflow by itself; push an empty commit to re-trigger the checks (the v2.0 stage 3 note).

## 2. Merge, then CI on `main` (after merge)

The tag goes on the merge commit of the release pull request, and only after `ci` and every `codeql`
`analyze` job are green on that commit. `release.py publish` checks both; `release.yml` checks `ci`
again (`tests/harness/verify_ci_green.py`, which waits up to 40 minutes for a run in progress).

```powershell
git switch main; git pull --ff-only
git rev-parse HEAD                     # the merge commit of the release PR
gh run list --commit (git rev-parse HEAD) --workflow ci.yml
gh run list --commit (git rev-parse HEAD) --workflow codeql.yml
```

Optional guard rehearsal without a tag (runs only `verify-ci`, builds and pushes nothing):

```powershell
gh workflow run release.yml --ref main -f dry_run=true -f sha=<40-hex merge commit>
```

## 3. Tag and GitHub release (after merge)

```powershell
.venv-ci\Scripts\python tools/release.py publish --version 2.2.0              # dry run: prints every step and any BLOCKER
.venv-ci\Scripts\python tools/release.py --execute publish --version 2.2.0
```

`--execute publish` does, in order: requires green `ci` and `codeql` on the merge commit; creates the
annotated tag `v2.2.0` with the CHANGELOG section as its message and pushes it; runs
`gh release create v2.2.0 --verify-tag` with the same section; watches `release.yml` for the tag and
re-runs its failed jobs once if the `verify-ci` guard raced the `main` CI run; downloads
`release-manifest.json` and rewrites the three Kubernetes manifests (step 4); checks that the Pages
site shows `v2.2.0`.

`release.yml` on the tag builds the python, cpp and rust images, pushes them to GHCR as
`ghcr.io/ashjha0/intraday-alpha-platform-<lang>:v2.2.0`, attests build provenance and a CycloneDX SBOM
per image, writes SBOMs of the two Python lock files, and attaches `release-manifest.json` and the
SBOMs to the release.

## 4. The digest pull request (after the tag; a second pull request is needed)

`main` is protected, so the rewritten manifests go through a pull request, as #415 did for v2.1.0.

```powershell
git switch -c release/v2.2.0-pin-digests origin/main
# --execute publish left the three files rewritten; or redo it from the release asset:
gh release download v2.2.0 --pattern release-manifest.json --dir $env:TEMP
.venv-ci\Scripts\python tools/release.py --execute pin-digests --manifest $env:TEMP\release-manifest.json --version 2.2.0
```

| # | file | change |
|---|---|---|
| 4.1 | `deployment/k8s/platform-metrics.yaml`, `cronjob-data-pipeline.yaml`, `cronjob-state-backup.yaml` | `...-python:v2.2.0@sha256:<digest>`; commit "release: pin v2.2.0 image digests" |
| 4.2 | `deployment/docker/docker-compose.yml` | the six `image:` lines from `:v2.1.0` to `:v2.2.0` (tag only: a service with `build:` cannot carry a digest); commit "release: compose image tags to v2.2.0" |

Then `check_deployment.py` must pass (`image_pinning`: one tag across compose and Kubernetes), the
pull request's CI must be green, and the owner merges it. Nothing is re-tagged.

## 5. After both merges

| # | check | command |
|---|---|---|
| 5.1 | Provenance verifies for each image | `gh attestation verify oci://ghcr.io/ashjha0/intraday-alpha-platform-python@<digest> --repo AshJha0/intraday-alpha-platform` (and cpp, rust) |
| 5.2 | The Pages site shows v2.2.0 | the last step of `publish`; or open the site |
| 5.3 | The tag's commit is the release PR's merge commit | `git rev-list -n 1 v2.2.0` |

## Network, registry and money

| step | network or registry action | spends money? |
|---|---|---|
| CI on both pull requests and on `main` | GitHub Actions on GitHub-hosted runners | no: public repository |
| `repro-image` | pulls the image of record from GHCR; builds and pushes only if its inputs changed | no |
| tag push, `gh release create`, `gh release download` | GitHub API | no |
| `release.yml` | pushes three images to GHCR, attestations to the registry and Sigstore, downloads syft and `cyclonedx-bom` | no: GHCR and attestations are free for a public repository |
| lychee in the `docs` job | external link checks, advisory | no |
| site check in `publish` | one HTTPS GET of the Pages site | no |

No step calls a language-model API, and no step needs a payment method or a new account.
