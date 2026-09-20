# tools/github — epics and issues from one YAML

`issues.yaml` is the single source of truth for the repository's GitHub
labels, milestones, epics and issues. `create_issues.py` validates it,
prints it, renders it to `docs/EPICS.md`, and pushes it to GitHub
idempotently with the `gh` CLI. Dependencies: the Python standard library
and PyYAML (`pip install --break-system-packages pyyaml` if missing).

## Usage

```bash
# validate + print the plan as a table (default; no network)
python3 tools/github/create_issues.py
python3 tools/github/create_issues.py --dry-run --only epic:E11

# render / check the human-readable copy (the integration test runs --check-md)
python3 tools/github/create_issues.py --render-md docs/EPICS.md
python3 tools/github/create_issues.py --check-md docs/EPICS.md

# push to GitHub (needs gh, authenticated: gh auth login)
python3 tools/github/create_issues.py --apply
python3 tools/github/create_issues.py --apply --repo AshJha0/intraday-alpha-platform --only epic:E15
```

Exit codes: `0` ok · `1` usage · `2` the YAML failed validation (every
problem listed) · `3` `gh` missing or not authenticated · `4` a `gh`
command failed (command and stderr printed) · `5` `--check-md` found the
rendering stale.

## What `--apply` does, in order

1. `gh label create --force` for every label (create or update colour and
   description).
2. `gh api repos/{owner}/{repo}/milestones` — list (all states) and create
   any milestone whose title is missing.
3. `gh issue list --state all --json title,number,state --limit 500` once;
   for a title not in that list, an authoritative
   `gh issue list --search 'in:title "<title>"'` before creating. **Exact
   title is the idempotency key** — rerunning never duplicates.
4. Epics first: create (or, if found, sync labels and milestone).
5. Issues: create with `Part of #<epic>` in the body (or sync labels and
   milestone if found). An issue whose `status` is `done` is closed with a
   comment listing its evidence; a done issue found open is closed the same
   way.
6. Finally every epic body is rewritten with its task list
   `- [x] #N` / `- [ ] #N` (done / not done).

Bodies carry a `<!-- managed-by: tools/github/issues.yaml key=… -->` marker.
Existing bodies of *issues* are not overwritten (comments and edits on
GitHub survive); epic bodies are, because the task list is derived.

## Plan schema (`issues.yaml`)

```yaml
repo: owner/name
labels:      [{name, color, description}]        # <group>:<value>; groups area/lang/type/priority/status/phase
milestones:  [{title, description}]
epics:       [{key, title, labels, milestone, objective, scope[], acceptance[], out_of_scope[]}]
issues:      [{key, epic, title, labels, milestone, status, estimate_days, context, acceptance[], evidence[]}]
```

Validation (`validate()`): unique keys and titles across epics and issues;
every issue references an existing epic; every label and milestone is
defined; one `type:*` and one `priority:*` label each; epics carry
`type:epic`, issues do not; `status` is `done` / `in-progress` / `backlog`
and any `status:*` label agrees with it (the script adds the status label
itself); `estimate_days` positive; `acceptance` and `evidence` non-empty;
no epic without issues. An epic's status is derived from its issues
(`done`, `in-progress`, `partial` = some done, rest backlog, `backlog`) and
carried as a `status:*` label on the epic.

Status is a statement about the repository: `done` cites the files and
tests that prove it (the integration test checks that at least one cited
path exists on disk, and that each of the eight modules of the 2026-09-19/20
release — contracts, risk, execution, lifecycle, trace, store, research, mvp
— is named by a done issue), `in-progress` lists the planned paths of the
current release (0 issues as of 2026-09-20: the release closed all 15),
`backlog` says what would prove it done. Current counts: 24 epics, 123
issues — 96 done, 0 in progress, 27 backlog (`docs/EPICS.md` summary;
`docs/ROADMAP.md` maps them to the phases).

## Keeping it in sync

- Edit the YAML, regenerate `docs/EPICS.md`, commit both.
  `tests/integration/test_github_issue_plan.py` validates the YAML with the
  script's own validator, runs `--dry-run` as a subprocess, and fails when
  `docs/EPICS.md` differs from the rendering.
- When a PR closes an issue, update that issue's `status` and `evidence`.
- Re-run `--apply` at any time; it only creates what is missing, syncs
  labels/milestones, closes newly-done issues and refreshes epic task lists.
