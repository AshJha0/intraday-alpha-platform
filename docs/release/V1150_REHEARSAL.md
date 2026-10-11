# v1.15.0 release rehearsal (dry run, 2026-10-11)

`tools/release.py` (v1.15 E-P1-1) was run in its default dry-run mode against
`main` @ `1700acd` for a hypothetical v1.15.0. `--execute` was never passed:
nothing was tagged, pushed, dispatched or released. The only commands that ran
were read-only queries (`git rev-parse`, `git status`, `git show`,
`git fetch origin main --tags`, `gh run list`).

## 1. `python tools/release.py flow`

```
release flow (tools/release.py, dry-run by default; --execute to act):
 1. merge the working branches into release/vX.Y.Z (one PR the owner merges)
 2. python tools/release.py prepare-regenerate --branch release/vX.Y.Z
      reset research/experiments.json to origin/main, drop the 5 runner dirs,
      commit + push, gh workflow run ci.yml -f regenerate=true -f regenerate_allow_rerun=true
 3. python tools/release.py apply-bundle --run-id <regenerate run id>
      gh run download -n regenerated-artefacts; tar --force-local -xzf,
      skipping CRLF-only expected_features*; review the diff, commit, push
 4. owner merges the release PR (merge commit)
 5. python tools/release.py publish --version X.Y.Z
      main CI green -> annotated tag from CHANGELOG -> gh release --verify-tag
      -> release.yml (rerun if verify-ci raced) -> site shows vX.Y.Z
      (needs a '## vX.Y.Z' CHANGELOG heading: rename 'Unreleased' first)
```

(Before this PR the header read "v1.14 release flow" and step 5 ended at
"site check"; both were updated.)

## 2. `python tools/release.py prepare-regenerate --branch rehearsal/v1.15.0-throwaway`

Exit 0. The branch name is a throwaway: the branch check only applies with
`--execute`.

```
[dry-run] research/experiments.json already equals origin/main
[dry-run] git rm -r -q research/experiments/00ebeb2b537b5155
[dry-run] git rm -r -q research/experiments/20f1b9093e7d0d04
[dry-run] git rm -r -q research/experiments/4a2900e4a6705542
[dry-run] git rm -r -q research/experiments/695e7b1e2bd2253e
[dry-run] git rm -r -q research/experiments/838e0c2d75de4db6
[dry-run] git add research/experiments.json
[dry-run] git commit -m 'release: reset experiments ledger to origin/main and drop the 5 runner experiment dirs for CI regeneration'
[dry-run] git push origin rehearsal/v1.15.0-throwaway
[dry-run] gh workflow run ci.yml --ref rehearsal/v1.15.0-throwaway -f regenerate=true -f regenerate_allow_rerun=true
```

All five pinned (alpha, horizon) runner dirs were found (the list equals
`tools/regenerate_dataset_artifacts.py RUNNER_EXPERIMENTS`, checked by a test).

## 3. `python tools/release.py publish --version 1.15.0`

Exit 1 (as it should be today). After the fix in this PR the dry run lists
every blocker and still prints the plan:

```
[dry-run] BLOCKER: CHANGELOG.md has no '## v1.15.0' section
[dry-run] BLOCKER: no green ci run on main for 1700acd0093c7144675af9fa5d7c9c09c0bee036: tag only after main CI is green
[dry-run] release notes from CHANGELOG (1 lines): <tmp>/v1.15.0.md
[dry-run] git tag -a v1.15.0 1700acd0093c7144675af9fa5d7c9c09c0bee036 -F <tmp>/v1.15.0.md
[dry-run] git push origin v1.15.0
[dry-run] gh release create v1.15.0 --verify-tag --title v1.15.0 --notes-file <tmp>/v1.15.0.md
[dry-run] then: watch release.yml for v1.15.0; rerun once if verify-ci raced; site check
error: dry run found blockers: CHANGELOG.md has no '## v1.15.0' section; no green ci run on main for 1700acd...: tag only after main CI is green
```

Both blockers are expected: the main CI run for `1700acd` was still in
progress, and the CHANGELOG section is `## Unreleased (v1.15)`.
`apply-bundle` was not rehearsed (it needs a regenerate run id).

## Findings

### Fixed in this PR (with mocked git/gh tests in `python/tests/test_release_tool.py`)

1. **Site check was vacuous.** `site_mentions` looked for `vX.Y` (`v1.15`).
   The live site already says `v1.15` three times (roadmap), so the check
   would pass before the release appeared; `v1.1` would also match `v1.14`.
   It now requires the full `v1.15.0`, not followed by a digit or `.digit`.
2. **Dry run stopped at the first blocker.** `publish` raised on the CI gate
   before it read the CHANGELOG, so a rehearsal hid the missing section and
   never showed the tag/release commands. In dry-run mode blockers (tag
   exists, CHANGELOG section missing, main CI not green) are now collected,
   the plan is printed and the command exits 1. With `--execute` the first
   blocker still raises before anything is tagged.
3. `flow` text no longer says "v1.14 release flow" and names the CHANGELOG
   rename and the full-version site check.

### Checked, no change needed

- **Java removed:** `release.py` has no java checks or artefacts. Java
  survives only in `release.yml` (`images` matrix builds
  `intraday-alpha-platform-java` from `Dockerfile.java`, which copies
  `archive/java`), in `ci.yml` `images` (builds and trivy-scans four
  images) and in the `deployment` job's JDK. That matches the v1.15
  CHANGELOG ("compose and k8s are unchanged").
- **Ledger baseline:** prepare-regenerate resets `research/experiments.json`
  byte-for-byte to `origin/main` (it was already equal). The v1.14 chained
  ledger (G-P1-5) and v1.15 agent-proposal debits (A-P1-2) are entries in that
  file, so the reset keeps main's chain intact; the regenerate job appends to
  it. Model cards / `promotion.json` and the trust-root registry live outside
  the ledger and the runner dirs, so the reset does not touch them.
- **CI gate vs required checks:** `publish` (like `release.yml`'s
  `verify-ci` guard, `tests/harness/verify_ci_green.py`) requires a green
  `ci.yml` push run on the merge commit. `protect-main` requires 13 checks:
  python, integration, cpp, rust, golden, deployment, CodeQL, governance,
  advisory, rust-pyo3, python-slow, supply-chain, analyze (rust). All but
  `CodeQL` and `analyze (rust)` are `ci.yml` jobs; those two come from
  `codeql.yml` and are enforced at merge time by the ruleset, not by the tag
  gate.

### Needs a decision

1. **Ship the Java image in v1.15.0?** `release.yml` will still build, attest
   and publish `ghcr.io/ashjha0/intraday-alpha-platform-java:v1.15.0` from
   the frozen archive, and the manifest job still installs a JDK. Keep (k8s
   `java-platform.yaml` uses it) or drop the `java` matrix entry and the k8s
   manifest.
2. **Should the tag gate also require `codeql.yml`?** Today a main commit
   with a failed post-merge CodeQL run can still be tagged.
3. **CHANGELOG:** rename `## Unreleased (v1.15)` to `## v1.15.0 — <date>`
   in the release PR; the Unreleased section does not yet list #92, #94,
   #85, #93 or #96 (plan progress lists them as merged).
