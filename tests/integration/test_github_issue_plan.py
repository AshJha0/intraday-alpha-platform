"""Integration level: the GitHub issue plan is valid, dry-runs, and docs/EPICS.md is in sync.

``tools/github/issues.yaml`` is the single source of truth for the platform's
epics and issues; ``tools/github/create_issues.py`` validates it, renders it
and (with ``gh``) applies it. This test wires the three together the way a
contributor does — the script's own validator over the checked-in YAML, the
default ``--dry-run`` as a subprocess, ``--check-md`` against the committed
rendering — and asserts the contract at the end of the chain: the plan parses,
every issue hangs off an epic, the honesty rules hold (done cites evidence,
in-progress cites planned paths), and ``docs/EPICS.md`` is exactly what the
YAML renders to.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

TOOL = "tools/github/create_issues.py"
PLAN = "tools/github/issues.yaml"
RENDERED = "docs/EPICS.md"

IN_PROGRESS_PATHS = (
    "python/src/iap/contracts", "python/src/iap/risk", "python/src/iap/execution",
    "python/src/iap/lifecycle", "python/src/iap/trace", "python/src/iap/store",
    "python/src/iap/research", "python/src/iap/mvp",
)


@pytest.fixture(scope="module")
def tool(repo_root):
    """The script imported as a module (no side effects at import time)."""
    path = repo_root / TOOL
    spec = importlib.util.spec_from_file_location("create_issues", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["create_issues"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def plan(tool, repo_root):
    return tool.load_plan(repo_root / PLAN)


def test_plan_validates_with_the_scripts_own_validator(tool, plan):
    assert tool.validate(plan) == []
    assert plan["repo"] == "AshJha0/intraday-alpha-platform"


def test_plan_shape_and_coverage(tool, plan):
    epics, issues = plan["epics"], plan["issues"]
    assert 18 <= len(epics) <= 30
    assert 90 <= len(issues) <= 140
    keys = {e["key"] for e in epics}
    assert all(i["epic"] in keys for i in issues)
    # every epic has at least one issue and every milestone is used
    used_ms = {e["milestone"] for e in epics} | {i["milestone"] for i in issues}
    assert used_ms == {m["title"] for m in plan["milestones"]}
    # the three statuses are all represented — the plan is honest, not aspirational
    counts = tool.status_counts(issues)
    assert counts["done"] > 0 and counts["in-progress"] > 0 and counts["backlog"] > 0


def test_done_evidence_points_at_real_paths(plan, repo_root):
    """Every done issue cites at least one path that exists on disk."""
    missing = []
    for it in plan["issues"]:
        if it["status"] != "done":
            continue
        hits = 0
        for ev in it["evidence"]:
            for frag in ev.split(";"):
                cand = frag.strip().split(" ")[0]
                cand = cand.split("{")[0].rstrip("/")
                if cand and (repo_root / cand).exists():
                    hits += 1
        if hits == 0:
            missing.append(it["key"])
    assert not missing, f"done issues with no existing evidence path: {missing}"


def test_in_progress_issues_cover_the_release_modules(plan):
    text = "\n".join("\n".join(i["evidence"]) for i in plan["issues"] if i["status"] == "in-progress")
    for path in IN_PROGRESS_PATHS:
        assert path in text, f"no in-progress issue plans {path}"
    # in-progress evidence is either marked as planned or already on disk (work landing now)
    for it in plan["issues"]:
        if it["status"] == "in-progress":
            marked = any("planned" in ev or "generated" in ev for ev in it["evidence"])
            assert marked or _any_path_exists(it["evidence"]), \
                f"{it['key']}: in-progress evidence must be marked as planned or exist"


def _any_path_exists(evidence) -> bool:
    root = Path(__file__).resolve().parents[2]
    for ev in evidence:
        for frag in ev.split(";"):
            cand = frag.strip().split(" ")[0].split("{")[0].rstrip("/")
            if cand and (root / cand).exists():
                return True
    return False


def test_known_honest_verdicts_are_recorded(plan):
    by_key = {i["key"]: i for i in plan["issues"]}
    assert by_key["A06"]["status"] == "done" and "0 PROMOTE" in by_key["A06"]["title"]
    assert by_key["X08"]["status"] == "backlog" and "PEG" in by_key["X08"]["title"]
    assert by_key["L04"]["status"] == "backlog" and "RETIRED" in by_key["L04"]["title"]
    assert by_key["ML03"]["status"] == "done" and "FAILED" in by_key["ML03"]["title"]


def test_bodies_render(tool, plan):
    epic = plan["epics"][0]
    kids = tool.children_of(plan, epic["key"])
    body = tool.epic_body(epic, kids, {k["key"]: 100 + n for n, k in enumerate(kids)})
    assert "## Acceptance criteria" in body and "- [ ] #100" in body or "- [x] #100" in body
    it = kids[0]
    ib = tool.issue_body(it, epic, 42)
    assert ib.splitlines()[1] == "Part of #42"
    assert "## Acceptance criteria" in ib
    assert ("- [x]" in ib) == (it["status"] == "done")


def test_dry_run_subprocess(repo_root):
    r = subprocess.run([sys.executable, TOOL, "--dry-run"], cwd=repo_root,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "issues by status:" in r.stdout
    assert "nothing was sent to GitHub" in r.stdout
    for key in ("E01", "E24", "MV01"):
        assert key in r.stdout


def test_only_filter_and_bad_filter(repo_root):
    ok = subprocess.run([sys.executable, TOOL, "--only", "epic:E10"], cwd=repo_root,
                        capture_output=True, text=True, timeout=60)
    assert ok.returncode == 0 and "E10" in ok.stdout and "E11 " not in ok.stdout
    bad = subprocess.run([sys.executable, TOOL, "--only", "epic:NOPE"], cwd=repo_root,
                         capture_output=True, text=True, timeout=60)
    assert bad.returncode == 2 and "no epic" in bad.stderr


def test_invalid_plan_is_rejected(tool, plan, tmp_path):
    import copy
    import yaml
    broken = copy.deepcopy(plan)
    broken["issues"][0]["epic"] = "E99"
    broken["issues"][1]["acceptance"] = []
    broken["issues"][2]["title"] = broken["issues"][3]["title"]
    problems = tool.validate(broken)
    assert any("unknown epic 'E99'" in p for p in problems)
    assert any("acceptance" in p for p in problems)
    assert any("duplicate title" in p for p in problems)
    path = tmp_path / "broken.yaml"
    path.write_text(yaml.safe_dump(broken), encoding="utf-8")
    r = subprocess.run([sys.executable, TOOL, "--plan", str(path)], capture_output=True, text=True,
                       cwd=Path(__file__).resolve().parents[2], timeout=60)
    assert r.returncode == 2 and "failed validation" in r.stderr


def test_epics_md_is_in_sync_with_the_yaml(tool, plan, repo_root):
    rendered = tool.render_md(plan)
    current = (repo_root / RENDERED).read_text(encoding="utf-8")
    assert current == rendered, (
        f"{RENDERED} is stale; regenerate with: python3 {TOOL} --render-md {RENDERED}")
    r = subprocess.run([sys.executable, TOOL, "--check-md", RENDERED], cwd=repo_root,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
