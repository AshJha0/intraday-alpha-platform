> **Retired 2026-10-25 (v2.0 docs audit):** this proposal was applied on 2026-10-18 and its outcome (C++ parity-only, 17 required checks) is recorded in docs/governance/GOVERNANCE.md section 0 and REPO_SETTINGS.md section 1; the file's own 'nothing applied' status line below is the pre-application text, kept as written (docs/DOC_AUDIT_v2.md).

# Proposal: D5(a), C++ demoted to parity-only (v1.18 REL-13), APPLIED 2026-10-18

Applied on 2026-10-18 with the owner's approval: the live ruleset `protect-main` no longer requires
`cpp-sanitizers` and now requires `docs` and `reproducible-build` (17 checks, read back from the API);
the bench guard step and the C++ image smoke run are `continue-on-error`. `coverage` is not yet
required (the owner adds it once the job has a run history on main).

Status: **proposal for the owner**. Nothing here has been applied: the
ruleset is the owner's to edit, and the CI changes below land only with the
owner's go-ahead. Decision D5 was answered (a) (IAP_Release_Programme_2026-10-17.md,
REL-13): C++ stays as a parity port (its parity and golden checks keep running),
but its sanitizer job, its image and its benchmark guard stop being merge gates.

## 1. Required checks: before and after

Read from the live ruleset `protect-main` (id 24455225) on 2026-10-18 with
`gh api repos/AshJha0/intraday-alpha-platform/rulesets/24455225`. Note that
docs/governance/REPO_SETTINGS.md §1 still documents six checks; the live list
has sixteen.

| check | today | proposed | why |
|---|---|---|---|
| python, python-312, python-slow, integration, rust, rust-pyo3, golden, deployment, governance, advisory, supply-chain, agent-evals, CodeQL, analyze (rust) | required | required | unchanged |
| `cpp` | required | required | the C++ build, ctest and the parity goldens stay a gate (parity-only still means parity is checked) |
| `cpp-sanitizers` | required | **not required** (job keeps running, reported) | D5(a) |
| `coverage` (new in this PR) | n/a | **required** | F6: the whole-suite floor is only "in force" when the job is a gate |

Count: 16 today, 16 after (minus `cpp-sanitizers`, plus `coverage`). REL-14a
(F6) separately asks for `docs` and `reproducible-build` to be required; that
would make 18, the figure REL-14a's gate names, and is listed in §3 as an
independent change.

## 2. CI changes that go with it (not in this PR)

1. **Bench guard.** The `benchmark regression guard (coarse)` step of the `cpp`
   job gates every merge today because `cpp` is required. Proposed: add
   `continue-on-error: true` to that step (it still runs and its artifact
   `bench-cpp` is still uploaded), so a noisy shared runner cannot block a
   merge through the C++ benchmark.
2. **C++ image.** The `images` job (not a required check) builds, scans and
   smoke-runs `iap/cpp`. Proposed: keep the build and the trivy scan, make the
   `the C++ replay container runs bench_all to completion` step
   `continue-on-error: true`; the image is no longer a release gate for the
   Python-first platform.
3. **cpp-sanitizers.** No file change: the job stays blocking as a job (red on
   failure), it just stops being a merge gate.

## 3. The ruleset edit (owner runs it)

Fetch the live ruleset, edit the list, PUT it back:

```bash
gh api repos/AshJha0/intraday-alpha-platform/rulesets/24455225 > /tmp/ruleset.json
```

```bash
python - <<'PY'
import json
r = json.load(open("/tmp/ruleset.json"))
for rule in r["rules"]:
    if rule["type"] == "required_status_checks":
        checks = rule["parameters"]["required_status_checks"]
        checks[:] = [c for c in checks if c["context"] != "cpp-sanitizers"]
        checks.append({"context": "coverage"})
        # REL-14a (F6), independent of D5(a): uncomment to also require them
        # checks += [{"context": "docs"}, {"context": "reproducible-build"}]
keep = {k: r[k] for k in ("name", "target", "enforcement", "conditions", "rules", "bypass_actors") if k in r}
json.dump(keep, open("/tmp/ruleset_new.json", "w"), indent=2)
PY
```

```bash
gh api -X PUT repos/AshJha0/intraday-alpha-platform/rulesets/24455225 --input /tmp/ruleset_new.json
```

Add `coverage` only after this PR's `coverage` job has been green on main once
(a required check that has never reported blocks every merge).

## 4. Measure (REL-13 gate)

REL-13's gate is "CI wall time and required-check list before and after".
Before: the sixteen checks above and the wall time of a main run before the
change (`gh run list --workflow ci --branch main --limit 5 --json databaseId,createdAt,updatedAt`).
After: the same two figures once the ruleset and the §2 edits are in.
