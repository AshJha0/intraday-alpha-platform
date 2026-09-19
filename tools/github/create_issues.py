#!/usr/bin/env python3
"""Create the platform's GitHub labels, milestones, epics and issues from
tools/github/issues.yaml — the single source of truth — and render it to
docs/EPICS.md.

Dependencies: the Python standard library and PyYAML (``pip install
--break-system-packages pyyaml`` if missing). Applying needs the ``gh`` CLI,
authenticated (``gh auth login``).

Modes
-----
  --dry-run (default)   validate the plan and print it as a table; no network
  --apply               create/update on GitHub, idempotently (see README.md)
  --render-md PATH      validate and write the human-readable rendering
  --check-md PATH       validate and exit 5 if PATH differs from the rendering

Exit codes
----------
  0  success
  1  usage error
  2  plan validation failed (every problem is listed)
  3  gh is missing or not authenticated
  4  a gh command failed
  5  --check-md: the rendered file is out of sync with the YAML
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import yaml
except ImportError:  # pragma: no cover - reported to the user, not tested
    sys.stderr.write(
        "error: PyYAML is required: pip install --break-system-packages pyyaml\n"
    )
    sys.exit(1)

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
DEFAULT_PLAN = HERE / "issues.yaml"

STATUSES = ("done", "in-progress", "backlog")
STATUS_ORDER = {s: i for i, s in enumerate(STATUSES)}
MANAGED_MARK = "<!-- managed-by: tools/github/issues.yaml key={key} -->"

EXIT_OK, EXIT_USAGE, EXIT_INVALID, EXIT_NO_GH, EXIT_GH_FAILED, EXIT_STALE = 0, 1, 2, 3, 4, 5


class PlanError(Exception):
    """The YAML violates the plan schema; ``problems`` lists every violation."""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = list(problems)


class GhError(Exception):
    """A ``gh`` invocation failed; the message carries the command and stderr."""


# --------------------------------------------------------------------------
# loading + validation
# --------------------------------------------------------------------------

def load_plan(path: Path = DEFAULT_PLAN) -> dict:
    """Load and validate the plan; raise PlanError listing every problem."""
    with open(path, "r", encoding="utf-8") as fh:
        plan = yaml.safe_load(fh)
    problems = validate(plan)
    if problems:
        raise PlanError(problems)
    return plan


def _is_str_list(v, non_empty: bool = True) -> bool:
    return isinstance(v, list) and (not non_empty or len(v) > 0) and all(
        isinstance(x, str) and x.strip() for x in v)


def validate(plan) -> List[str]:
    """Return a list of human-readable problems (empty means valid)."""
    p: List[str] = []
    if not isinstance(plan, dict):
        return ["top level must be a mapping"]
    for section in ("labels", "milestones", "epics", "issues"):
        if not isinstance(plan.get(section), list) or not plan[section]:
            p.append(f"`{section}:` must be a non-empty list")
    if p:
        return p
    if not isinstance(plan.get("repo"), str) or "/" not in plan["repo"]:
        p.append("`repo:` must be 'owner/name'")

    # labels
    label_names: List[str] = []
    for i, lab in enumerate(plan["labels"]):
        if not isinstance(lab, dict) or not {"name", "color", "description"} <= set(lab):
            p.append(f"labels[{i}]: needs name, color, description")
            continue
        label_names.append(lab["name"])
        if not re.fullmatch(r"[0-9A-Fa-f]{6}", str(lab["color"])):
            p.append(f"label {lab['name']!r}: color must be 6 hex digits, got {lab['color']!r}")
        if not re.fullmatch(r"(area|lang|type|priority|status|phase):[a-z0-9-]+", lab["name"]):
            p.append(f"label {lab['name']!r}: must be <group>:<value> with group in area/lang/type/priority/status/phase")
    for name, n in Counter(label_names).items():
        if n > 1:
            p.append(f"duplicate label {name!r}")
    labels = set(label_names)
    for st in STATUSES + ("partial",):
        if f"status:{st}" not in labels:
            p.append(f"missing label status:{st}")
    if "type:epic" not in labels:
        p.append("missing label type:epic")

    # milestones
    ms_titles: List[str] = []
    for i, ms in enumerate(plan["milestones"]):
        if not isinstance(ms, dict) or not {"title", "description"} <= set(ms):
            p.append(f"milestones[{i}]: needs title, description")
            continue
        ms_titles.append(ms["title"])
    for name, n in Counter(ms_titles).items():
        if n > 1:
            p.append(f"duplicate milestone {name!r}")
    milestones = set(ms_titles)

    # epics
    keys: Counter = Counter()
    titles: Counter = Counter()
    epic_keys: set = set()
    for i, e in enumerate(plan["epics"]):
        where = f"epics[{i}]"
        if not isinstance(e, dict):
            p.append(f"{where}: must be a mapping")
            continue
        key = e.get("key")
        where = f"epic {key!r}" if key else where
        for field in ("key", "title", "labels", "milestone", "objective", "scope", "acceptance", "out_of_scope"):
            if field not in e:
                p.append(f"{where}: missing `{field}`")
        if not key:
            continue
        keys[key] += 1
        epic_keys.add(key)
        if e.get("title"):
            titles[e["title"]] += 1
        _check_labels(where, e.get("labels"), labels, p, epic=True)
        if e.get("milestone") not in milestones:
            p.append(f"{where}: unknown milestone {e.get('milestone')!r}")
        for field in ("scope", "acceptance", "out_of_scope"):
            if field in e and not _is_str_list(e[field]):
                p.append(f"{where}: `{field}` must be a non-empty list of strings")
        if "objective" in e and not (isinstance(e["objective"], str) and e["objective"].strip()):
            p.append(f"{where}: `objective` must be non-empty text")

    # issues
    for i, it in enumerate(plan["issues"]):
        where = f"issues[{i}]"
        if not isinstance(it, dict):
            p.append(f"{where}: must be a mapping")
            continue
        key = it.get("key")
        where = f"issue {key!r}" if key else where
        for field in ("key", "epic", "title", "labels", "milestone", "status", "estimate_days", "context", "acceptance", "evidence"):
            if field not in it:
                p.append(f"{where}: missing `{field}`")
        if not key:
            continue
        keys[key] += 1
        if it.get("title"):
            titles[it["title"]] += 1
        if it.get("epic") not in epic_keys:
            p.append(f"{where}: references unknown epic {it.get('epic')!r}")
        if it.get("status") not in STATUSES:
            p.append(f"{where}: status must be one of {STATUSES}, got {it.get('status')!r}")
        _check_labels(where, it.get("labels"), labels, p, epic=False, status=it.get("status"))
        if it.get("milestone") not in milestones:
            p.append(f"{where}: unknown milestone {it.get('milestone')!r}")
        est = it.get("estimate_days")
        if not isinstance(est, (int, float)) or isinstance(est, bool) or est <= 0:
            p.append(f"{where}: estimate_days must be a positive number")
        if "acceptance" in it and not _is_str_list(it["acceptance"]):
            p.append(f"{where}: `acceptance` must be a non-empty list of acceptance criteria")
        if "evidence" in it and not _is_str_list(it["evidence"]):
            p.append(f"{where}: `evidence` must be a non-empty list (files/tests for done, planned paths otherwise)")
        if "context" in it and not (isinstance(it["context"], str) and it["context"].strip()):
            p.append(f"{where}: `context` must be non-empty text")

    for k, n in keys.items():
        if n > 1:
            p.append(f"duplicate key {k!r} ({n} times across epics + issues)")
    for t, n in titles.items():
        if n > 1:
            p.append(f"duplicate title {t!r} ({n} times) — titles are the idempotency key on GitHub")
    for t in titles:
        if len(t) > 256:
            p.append(f"title longer than GitHub's 256-character limit: {t[:60]!r}...")

    used_epics = {it.get("epic") for it in plan["issues"] if isinstance(it, dict)}
    for e in epic_keys - used_epics:
        p.append(f"epic {e!r} has no issues")
    return p


def _check_labels(where: str, labs, defined: set, p: List[str], *, epic: bool, status: Optional[str] = None) -> None:
    if not _is_str_list(labs):
        p.append(f"{where}: `labels` must be a non-empty list")
        return
    for lab in labs:
        if lab not in defined:
            p.append(f"{where}: undefined label {lab!r}")
    types = [l for l in labs if l.startswith("type:")]
    if epic:
        if "type:epic" not in labs:
            p.append(f"{where}: epic labels must include type:epic")
    else:
        if not types:
            p.append(f"{where}: needs a type:* label")
        if "type:epic" in labs:
            p.append(f"{where}: an issue may not carry type:epic")
        for l in labs:
            if l.startswith("status:") and l != f"status:{status}":
                p.append(f"{where}: label {l} contradicts status: {status}")
    if not any(l.startswith("priority:") for l in labs):
        p.append(f"{where}: needs a priority:* label")


# --------------------------------------------------------------------------
# derived views
# --------------------------------------------------------------------------

def issue_labels(it: dict) -> List[str]:
    """Labels as they go to GitHub: the YAML labels plus the derived status label."""
    labs = list(it["labels"])
    st = f"status:{it['status']}"
    if st not in labs:
        labs.append(st)
    return labs


def epic_status(children: Sequence[dict]) -> str:
    """Derived, never hand-set: `done` when every child is done; `in-progress`
    when any child is in progress; `partial` when some children are done and
    the rest are backlog with nothing in flight; `backlog` when no child is
    done. `partial` is an epic-only label (status:partial)."""
    sts = {c["status"] for c in children}
    if sts == {"done"}:
        return "done"
    if "in-progress" in sts:
        return "in-progress"
    if "done" in sts:
        return "partial"
    return "backlog"


def epic_labels(e: dict, children: Sequence[dict]) -> List[str]:
    labs = list(e["labels"])
    st = f"status:{epic_status(children)}"
    if st not in labs:
        labs.append(st)
    return labs


def children_of(plan: dict, epic_key: str) -> List[dict]:
    return [it for it in plan["issues"] if it["epic"] == epic_key]


def select(plan: dict, only: Optional[str]) -> Tuple[List[dict], List[dict]]:
    """Apply the --only filter; returns (epics, issues)."""
    if not only:
        return list(plan["epics"]), list(plan["issues"])
    if not only.startswith("epic:"):
        raise PlanError([f"--only expects epic:<key>, got {only!r}"])
    key = only[len("epic:"):]
    epics = [e for e in plan["epics"] if e["key"] == key]
    if not epics:
        raise PlanError([f"--only: no epic with key {key!r}"])
    return epics, children_of(plan, key)


def status_counts(items: Iterable[dict]) -> Dict[str, int]:
    c = Counter(it["status"] for it in items)
    return {s: c.get(s, 0) for s in STATUSES}


# --------------------------------------------------------------------------
# bodies
# --------------------------------------------------------------------------

def _box(checked: bool) -> str:
    return "- [x]" if checked else "- [ ]"


def epic_body(e: dict, children: Sequence[dict], numbers: Optional[Dict[str, int]] = None) -> str:
    """The epic's GitHub body. With ``numbers`` (key -> issue number) the
    children appear as a task list of ``#N`` references; without, as keys."""
    st = epic_status(children)
    lines = [
        MANAGED_MARK.format(key=e["key"]),
        f"**Key:** `{e['key']}` · **Milestone:** {e['milestone']} · **Status:** {st}",
        "",
        "## Objective",
        e["objective"].strip(),
        "",
        "## Scope",
    ]
    lines += [f"- {s}" for s in e["scope"]]
    lines += ["", "## Acceptance criteria"]
    lines += [f"{_box(st == 'done')} {a}" for a in e["acceptance"]]
    lines += ["", "## Out of scope"]
    lines += [f"- {s}" for s in e["out_of_scope"]]
    lines += ["", "## Issues"]
    for c in children:
        ref = f"#{numbers[c['key']]}" if numbers and c["key"] in numbers else f"`{c['key']}`"
        suffix = "" if numbers and c["key"] in numbers else f" — {c['title']}"
        lines.append(f"{_box(c['status'] == 'done')} {ref}{suffix} ({c['status']})")
    return "\n".join(lines) + "\n"


def issue_body(it: dict, epic: dict, epic_number: Optional[int] = None) -> str:
    done = it["status"] == "done"
    part_of = f"Part of #{epic_number}" if epic_number else f"Part of epic `{epic['key']}` ({epic['title']})"
    evidence_heading = {
        "done": "## Evidence (files and tests that prove it)",
        "in-progress": "## Planned paths",
        "backlog": "## Evidence / pointers",
    }[it["status"]]
    lines = [
        MANAGED_MARK.format(key=it["key"]),
        part_of,
        "",
        f"**Key:** `{it['key']}` · **Status:** {it['status']} · **Milestone:** {it['milestone']} · **Estimate:** {_fmt_days(it['estimate_days'])}",
        "",
        "## Context",
        it["context"].strip(),
        "",
        "## Acceptance criteria",
    ]
    lines += [f"{_box(done)} {a}" for a in it["acceptance"]]
    lines += ["", evidence_heading]
    lines += [f"- `{ev}`" for ev in it["evidence"]]
    return "\n".join(lines) + "\n"


def _fmt_days(d) -> str:
    return f"{d:g} day" + ("" if d == 1 else "s")


# --------------------------------------------------------------------------
# dry-run table
# --------------------------------------------------------------------------

def render_table(plan: dict, only: Optional[str] = None) -> str:
    epics, issues = select(plan, only)
    out: List[str] = []
    out.append(f"repo: {plan['repo']}")
    out.append(f"labels: {len(plan['labels'])}   milestones: {len(plan['milestones'])}   "
               f"epics: {len(epics)}   issues: {len(issues)}")
    sc = status_counts(issues)
    out.append("issues by status: " + "  ".join(f"{s}={sc[s]}" for s in STATUSES))
    out.append(f"estimate (days): done={_sum_days(issues, 'done'):g}  "
               f"in-progress={_sum_days(issues, 'in-progress'):g}  backlog={_sum_days(issues, 'backlog'):g}")
    out.append("")
    widths = (6, 12, 9, 5, 70)
    header = _row(("key", "status", "milestone", "days", "title"), widths)
    for e in epics:
        kids = children_of(plan, e["key"]) if not only else [i for i in issues if i["epic"] == e["key"]]
        out.append(f"{e['key']}  {e['title']}  [{e['milestone']}; {epic_status(kids)}; "
                   f"{len(kids)} issues; {_sum_days(kids):g} days]")
        out.append(header)
        out.append(_row(("-" * w for w in widths), widths))
        for c in kids:
            out.append(_row((c["key"], c["status"], c["milestone"], f"{c['estimate_days']:g}", c["title"]), widths))
        out.append("")
    return "\n".join(out)


def _sum_days(items: Iterable[dict], status: Optional[str] = None) -> float:
    return float(sum(i["estimate_days"] for i in items if status is None or i["status"] == status))


def _row(cells: Iterable[str], widths: Sequence[int]) -> str:
    parts = []
    for cell, w in zip(cells, widths):
        cell = str(cell)
        if len(cell) > w:
            cell = cell[: w - 1] + "…"
        parts.append(cell.ljust(w))
    return "  ".join(parts).rstrip()


# --------------------------------------------------------------------------
# markdown rendering (docs/EPICS.md)
# --------------------------------------------------------------------------

def render_md(plan: dict) -> str:
    epics, issues = plan["epics"], plan["issues"]
    sc = status_counts(issues)
    ms_order = [m["title"] for m in plan["milestones"]]
    ms_desc = {m["title"]: m["description"] for m in plan["milestones"]}
    by_ms: "OrderedDict[str, List[dict]]" = OrderedDict((m, []) for m in ms_order)
    for e in epics:
        by_ms[e["milestone"]].append(e)

    L: List[str] = []
    L.append("# Epics and issues")
    L.append("")
    L.append("<!-- GENERATED FILE — do not edit. Source: tools/github/issues.yaml; regenerate with")
    L.append("     python3 tools/github/create_issues.py --render-md docs/EPICS.md")
    L.append("     tests/integration/test_github_issue_plan.py fails when this file is stale. -->")
    L.append("")
    L.append("The platform build plan as GitHub epics and issues, generated from")
    L.append("[`tools/github/issues.yaml`](../tools/github/issues.yaml) — the single source of")
    L.append("truth that `tools/github/create_issues.py --apply` pushes to")
    L.append(f"`{plan['repo']}`. Status is checked against the repository, not the plan:")
    L.append("**done** cites the files and tests that prove it, **in-progress** lists the")
    L.append("planned paths of the current release, **backlog** says what would prove it done.")
    L.append("")
    L.append("## Summary")
    L.append("")
    L.append("| | count | estimate (days) |")
    L.append("|---|---:|---:|")
    L.append(f"| epics | {len(epics)} | |")
    L.append(f"| issues | {len(issues)} | {_sum_days(issues):g} |")
    for s in STATUSES:
        L.append(f"| issues `{s}` | {sc[s]} | {_sum_days(issues, s):g} |")
    L.append("")
    L.append("### By milestone")
    L.append("")
    L.append("| milestone | epics | issues | done | in-progress | backlog |")
    L.append("|---|---:|---:|---:|---:|---:|")
    for m in ms_order:
        ms_issues = [i for i in issues if i["milestone"] == m]
        c = status_counts(ms_issues)
        L.append(f"| {m} | {len(by_ms[m])} | {len(ms_issues)} | {c['done']} | {c['in-progress']} | {c['backlog']} |")
    L.append("")
    L.append("Issues are listed under their epic; an issue's own milestone can differ from")
    L.append("the epic's (a backlog item under a finished epic sits in **Backlog**).")
    L.append("")
    L.append("## Epics")
    L.append("")
    for e in epics:
        kids = children_of(plan, e["key"])
        L.append(f"- [{e['key']} — {_strip_epic(e['title'])}](#{_anchor(e)}) · {e['milestone']} · "
                 f"{epic_status(kids)} · {len(kids)} issues")
    L.append("")

    for m in ms_order:
        if not by_ms[m]:
            continue
        L.append(f"## {m}")
        L.append("")
        L.append(ms_desc[m].strip())
        L.append("")
        for e in by_ms[m]:
            kids = children_of(plan, e["key"])
            c = status_counts(kids)
            L.append(f"### {e['key']} — {_strip_epic(e['title'])}")
            L.append("")
            L.append(f"**Status:** {epic_status(kids)} · **Milestone:** {e['milestone']} · "
                     f"**Issues:** {len(kids)} (done {c['done']}, in-progress {c['in-progress']}, "
                     f"backlog {c['backlog']}) · **Estimate:** {_sum_days(kids):g} days · "
                     f"**Labels:** " + ", ".join(f"`{l}`" for l in epic_labels(e, kids)))
            L.append("")
            L.append(e["objective"].strip())
            L.append("")
            L.append("**Scope:** " + "; ".join(e["scope"]) + ".")
            L.append("")
            L.append("**Acceptance criteria:**")
            L.append("")
            for a in e["acceptance"]:
                L.append(f"{_box(epic_status(kids) == 'done')} {a}")
            L.append("")
            L.append("**Out of scope:** " + "; ".join(e["out_of_scope"]) + ".")
            L.append("")
            L.append("| key | title | status | est. (d) | milestone | evidence |")
            L.append("|---|---|---|---:|---|---|")
            for it in kids:
                ev = "<br>".join(f"`{_md_escape(x)}`" for x in it["evidence"])
                L.append(f"| {it['key']} | {_md_escape(it['title'])} | {it['status']} | "
                         f"{it['estimate_days']:g} | {it['milestone']} | {ev} |")
            L.append("")
    L.append("## Labels")
    L.append("")
    L.append("| label | description |")
    L.append("|---|---|")
    for lab in plan["labels"]:
        L.append(f"| `{lab['name']}` | {lab['description']} |")
    L.append("")
    return "\n".join(L)


def _strip_epic(title: str) -> str:
    return title[len("Epic: "):] if title.startswith("Epic: ") else title


def _anchor(e: dict) -> str:
    text = f"{e['key']} — {_strip_epic(e['title'])}".lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s+", "-", text.strip())


def _md_escape(s: str) -> str:
    return s.replace("|", "\\|")


# --------------------------------------------------------------------------
# gh
# --------------------------------------------------------------------------

class Gh:
    """Thin wrapper over the gh CLI with idempotent helpers."""

    def __init__(self, repo: str, verbose: bool = True) -> None:
        self.repo = repo
        self.verbose = verbose

    # -- plumbing ---------------------------------------------------------
    @staticmethod
    def ensure_available() -> None:
        if shutil.which("gh") is None:
            raise GhError("the `gh` CLI is not installed — https://cli.github.com/ (then `gh auth login`)")
        r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
        if r.returncode != 0:
            raise GhError("gh is installed but not authenticated — run `gh auth login`\n" + (r.stderr or r.stdout).strip())

    def run(self, args: Sequence[str], input_text: Optional[str] = None) -> str:
        cmd = ["gh", *args]
        if self.verbose:
            print("  $ " + " ".join(_q(a) for a in cmd))
        r = subprocess.run(cmd, capture_output=True, text=True, input=input_text)
        if r.returncode != 0:
            raise GhError(f"command failed ({r.returncode}): {' '.join(_q(a) for a in cmd)}\n{r.stderr.strip()}")
        return r.stdout

    def api_json(self, path: str, method: str = "GET", fields: Optional[Dict[str, str]] = None, paginate: bool = False):
        args = ["api", path, "-X", method]
        if paginate:
            args.append("--paginate")
        for k, v in (fields or {}).items():
            args += ["-f", f"{k}={v}"]
        out = self.run(args)
        if not out.strip():
            return None
        # --paginate concatenates JSON arrays; join them
        if paginate:
            merged: list = []
            for chunk in _split_json_arrays(out):
                merged.extend(chunk)
            return merged
        return json.loads(out)

    # -- labels -----------------------------------------------------------
    def label_create(self, name: str, color: str, description: str) -> None:
        self.run(["label", "create", name, "--repo", self.repo, "--color", color,
                  "--description", description, "--force"])

    # -- milestones -------------------------------------------------------
    def milestones(self) -> Dict[str, int]:
        owner, name = self.repo.split("/", 1)
        rows = self.api_json(f"repos/{owner}/{name}/milestones?state=all&per_page=100", paginate=True) or []
        return {m["title"]: m["number"] for m in rows}

    def milestone_ensure(self, title: str, description: str, existing: Dict[str, int]) -> int:
        if title in existing:
            return existing[title]
        owner, name = self.repo.split("/", 1)
        row = self.api_json(f"repos/{owner}/{name}/milestones", "POST",
                            {"title": title, "description": description})
        existing[title] = row["number"]
        return row["number"]

    # -- issues -----------------------------------------------------------
    def issues_by_title(self) -> Dict[str, dict]:
        out = self.run(["issue", "list", "--repo", self.repo, "--state", "all",
                        "--json", "title,number,state", "--limit", "500"])
        return {row["title"]: row for row in json.loads(out or "[]")}

    def issue_find(self, title: str) -> Optional[dict]:
        """Exact-title lookup via search (authoritative even past the 500-row list)."""
        out = self.run(["issue", "list", "--repo", self.repo, "--state", "all",
                        "--search", f'in:title "{title}"', "--json", "title,number,state", "--limit", "500"])
        for row in json.loads(out or "[]"):
            if row["title"] == title:
                return row
        return None

    def issue_create(self, title: str, body: str, labels: Sequence[str], milestone: str) -> int:
        with _body_file(body) as bf:
            out = self.run(["issue", "create", "--repo", self.repo, "--title", title,
                            "--body-file", bf, "--label", ",".join(labels), "--milestone", milestone])
        m = re.search(r"/issues/(\d+)\s*$", out.strip())
        if not m:
            raise GhError(f"could not parse the issue number from gh output: {out!r}")
        return int(m.group(1))

    def issue_edit(self, number: int, *, body: Optional[str] = None, labels: Optional[Sequence[str]] = None,
                   milestone: Optional[str] = None) -> None:
        args = ["issue", "edit", str(number), "--repo", self.repo]
        if labels:
            args += ["--add-label", ",".join(labels)]
        if milestone:
            args += ["--milestone", milestone]
        if body is not None:
            with _body_file(body) as bf:
                self.run(args + ["--body-file", bf])
            return
        if len(args) > 4:
            self.run(args)

    def issue_close(self, number: int, comment: str) -> None:
        self.run(["issue", "close", str(number), "--repo", self.repo, "--comment", comment])

    def issue_reopen(self, number: int) -> None:
        self.run(["issue", "reopen", str(number), "--repo", self.repo])


def _q(a: str) -> str:
    return a if re.fullmatch(r"[\w./:=@,-]+", a) else "'" + a.replace("'", "'\\''") + "'"


def _split_json_arrays(text: str) -> Iterable[list]:
    dec = json.JSONDecoder()
    idx = 0
    text = text.strip()
    while idx < len(text):
        obj, end = dec.raw_decode(text, idx)
        yield obj if isinstance(obj, list) else [obj]
        idx = end
        while idx < len(text) and text[idx].isspace():
            idx += 1


class _body_file:
    def __init__(self, body: str) -> None:
        self.body = body
        self.path = ""

    def __enter__(self) -> str:
        fd, self.path = tempfile.mkstemp(prefix="iap-issue-", suffix=".md")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(self.body)
        return self.path

    def __exit__(self, *exc) -> None:
        try:
            os.unlink(self.path)
        except OSError:
            pass


# --------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------

def apply(plan: dict, repo: str, only: Optional[str] = None) -> None:
    Gh.ensure_available()
    gh = Gh(repo)
    epics, issues = select(plan, only)
    epic_by_key = {e["key"]: e for e in plan["epics"]}

    print(f"== labels ({len(plan['labels'])})")
    for lab in plan["labels"]:
        gh.label_create(lab["name"], lab["color"], lab["description"])

    print(f"== milestones ({len(plan['milestones'])})")
    existing_ms = gh.milestones()
    for ms in plan["milestones"]:
        gh.milestone_ensure(ms["title"], ms["description"], existing_ms)

    print("== existing issues")
    known = gh.issues_by_title()
    numbers: Dict[str, int] = {}
    created = updated = closed = 0

    def find(title: str) -> Optional[dict]:
        row = known.get(title)
        if row is None:
            row = gh.issue_find(title)
            if row:
                known[title] = row
        return row

    print(f"== epics ({len(epics)})")
    for e in epics:
        kids = children_of(plan, e["key"])
        labels = epic_labels(e, kids)
        row = find(e["title"])
        if row:
            numbers[e["key"]] = row["number"]
            gh.issue_edit(row["number"], labels=labels, milestone=e["milestone"])
            updated += 1
        else:
            numbers[e["key"]] = gh.issue_create(e["title"], epic_body(e, kids), labels, e["milestone"])
            created += 1
        print(f"  {e['key']} -> #{numbers[e['key']]}")

    print(f"== issues ({len(issues)})")
    for it in issues:
        e = epic_by_key[it["epic"]]
        epic_no = numbers.get(e["key"])
        labels = issue_labels(it)
        body = issue_body(it, e, epic_no)
        row = find(it["title"])
        if row:
            numbers[it["key"]] = row["number"]
            gh.issue_edit(row["number"], labels=labels, milestone=it["milestone"])
            updated += 1
            state = row.get("state", "OPEN").upper()
        else:
            numbers[it["key"]] = gh.issue_create(it["title"], body, labels, it["milestone"])
            created += 1
            state = "OPEN"
        if it["status"] == "done" and state != "CLOSED":
            gh.issue_close(numbers[it["key"]], _close_comment(it))
            closed += 1
        print(f"  {it['key']} -> #{numbers[it['key']]} ({it['status']})")

    print("== epic task lists")
    for e in epics:
        kids = children_of(plan, e["key"])
        gh.issue_edit(numbers[e["key"]], body=epic_body(e, kids, numbers))

    print(f"done: created {created}, updated {updated}, closed {closed}")


def _close_comment(it: dict) -> str:
    ev = "\n".join(f"- `{x}`" for x in it["evidence"])
    return ("Closing as **done**: this exists in the repository. Evidence (from "
            "tools/github/issues.yaml):\n\n" + ev)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", type=Path, default=DEFAULT_PLAN, help="path to issues.yaml")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="validate and print the plan (default)")
    mode.add_argument("--apply", action="store_true", help="create/update labels, milestones, epics, issues with gh")
    mode.add_argument("--render-md", type=Path, metavar="PATH", help="write the markdown rendering to PATH")
    mode.add_argument("--check-md", type=Path, metavar="PATH", help="exit 5 if PATH is not the current rendering")
    ap.add_argument("--repo", help="owner/name override (default: `repo:` in the YAML)")
    ap.add_argument("--only", metavar="epic:<key>", help="restrict to one epic and its issues")
    args = ap.parse_args(argv)

    try:
        plan = load_plan(args.plan)
    except FileNotFoundError:
        sys.stderr.write(f"error: plan not found: {args.plan}\n")
        return EXIT_USAGE
    except yaml.YAMLError as exc:
        sys.stderr.write(f"error: {args.plan} is not valid YAML: {exc}\n")
        return EXIT_INVALID
    except PlanError as exc:
        sys.stderr.write(f"error: {args.plan} failed validation ({len(exc.problems)} problems):\n")
        for pr in exc.problems:
            sys.stderr.write(f"  - {pr}\n")
        return EXIT_INVALID

    repo = args.repo or plan["repo"]
    try:
        if args.render_md:
            text = render_md(plan)
            args.render_md.write_text(text, encoding="utf-8")
            print(f"wrote {args.render_md} ({len(text.splitlines())} lines, "
                  f"{len(plan['epics'])} epics, {len(plan['issues'])} issues)")
            return EXIT_OK
        if args.check_md:
            current = args.check_md.read_text(encoding="utf-8") if args.check_md.exists() else ""
            if current != render_md(plan):
                sys.stderr.write(f"error: {args.check_md} is out of sync with {args.plan}; regenerate with\n"
                                 f"  python3 {Path(__file__).relative_to(REPO_ROOT) if Path(__file__).is_relative_to(REPO_ROOT) else __file__} --render-md {args.check_md}\n")
                return EXIT_STALE
            print(f"{args.check_md} is in sync with {args.plan}")
            return EXIT_OK
        if args.apply:
            print(f"applying {args.plan} to {repo}")
            apply(plan, repo, args.only)
            return EXIT_OK
        print(render_table(plan, args.only))
        print("(dry run — nothing was sent to GitHub; add --apply to create)")
        return EXIT_OK
    except PlanError as exc:
        sys.stderr.write("error: " + "\n".join(exc.problems) + "\n")
        return EXIT_INVALID
    except GhError as exc:
        msg = str(exc)
        sys.stderr.write(f"error: {msg}\n")
        return EXIT_NO_GH if ("not installed" in msg or "not authenticated" in msg) else EXIT_GH_FAILED


if __name__ == "__main__":
    sys.exit(main())
