#!/usr/bin/env python3
"""Snapshot of the repository settings that live outside the tree (v2.0 stage 11, S4).

``docs/GOVERNANCE.md`` must describe the LIVE ruleset, not a remembered one
(the v1.19 audit found 13 documented, 6 in the JSON block and 17 live).  This tool reads the
settings with ``gh api`` and writes ``docs/governance/repo_settings_live.json``:

    python tools/repo_settings_snapshot.py write            # refresh the snapshot (owner / maintainer, with gh)
    python tools/repo_settings_snapshot.py check            # exit 1 when the live settings differ from the snapshot
    python tools/repo_settings_snapshot.py matrix           # print the required-check rows docs/GOVERNANCE.md must carry

What is read (all GET, no write):

* ``repos/{repo}/rules/branches/main``      the rules that bind main (required status checks, pull request, deletion, ...)
* ``repos/{repo}/rulesets``                 ruleset names, enforcement, update time
* ``repos/{repo}/actions/permissions/workflow``   default token permissions, PR approval by Actions
* ``repos/{repo}`` ``security_and_analysis``       secret scanning / push protection
* ``repos/{repo}/private-vulnerability-reporting``

``check`` runs in the CI governance job with ``continue-on-error`` (a warning, not a failure: the
default token may be refused on some endpoints and the live settings are the owner's to change);
``tests/test_repo_settings_doc.py`` fails when docs/GOVERNANCE.md's matrix and the snapshot disagree.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "docs" / "governance" / "repo_settings_live.json"
REPO = "AshJha0/intraday-alpha-platform"


def gh_api(path: str) -> object | None:
    """``gh api <path>`` as parsed JSON; None when the endpoint refuses (403/404) or gh is absent."""
    try:
        res = subprocess.run(["gh", "api", path], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None
    if res.returncode != 0:
        return None
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return None


def live() -> dict:
    rules = gh_api(f"repos/{REPO}/rules/branches/main") or []
    out: dict = {
        "x-version": 1,
        "repo": REPO,
        "branch": "main",
        "fetched_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rule_types": sorted({r["type"] for r in rules}),
        "required_status_checks": [],
        "strict_required_status_checks_policy": None,
        "pull_request": None,
        "rulesets": [],
        "actions_workflow_permissions": None,
        "security_and_analysis": None,
        "private_vulnerability_reporting": None,
    }
    for r in rules:
        if r["type"] == "required_status_checks":
            params = r.get("parameters", {})
            out["required_status_checks"] = sorted(
                c["context"] for c in params.get("required_status_checks", [])
            )
            out["strict_required_status_checks_policy"] = params.get(
                "strict_required_status_checks_policy"
            )
        elif r["type"] == "pull_request":
            params = dict(r.get("parameters", {}))
            params.pop("required_reviewers", None)
            out["pull_request"] = params
    rulesets = gh_api(f"repos/{REPO}/rulesets") or []
    out["rulesets"] = sorted(
        (
            {
                "name": s["name"],
                "target": s.get("target"),
                "enforcement": s.get("enforcement"),
                "updated_at": s.get("updated_at"),
            }
            for s in rulesets
        ),
        key=lambda s: s["name"],
    )
    out["actions_workflow_permissions"] = gh_api(f"repos/{REPO}/actions/permissions/workflow")
    repo = gh_api(f"repos/{REPO}")
    if isinstance(repo, dict):
        sa = repo.get("security_and_analysis") or {}
        out["security_and_analysis"] = {
            k: v.get("status") for k, v in sa.items() if isinstance(v, dict)
        }
    pvr = gh_api(f"repos/{REPO}/private-vulnerability-reporting")
    if isinstance(pvr, dict):
        out["private_vulnerability_reporting"] = pvr.get("enabled")
    return out


#: Fields compared by ``check`` (the volatile ``fetched_at`` and ruleset ``updated_at`` are not).
COMPARED = (
    "rule_types",
    "required_status_checks",
    "strict_required_status_checks_policy",
    "pull_request",
    "actions_workflow_permissions",
    "security_and_analysis",
    "private_vulnerability_reporting",
)


def load_snapshot(path: Path = SNAPSHOT) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def diff(snapshot: dict, current: dict) -> list[str]:
    out = []
    for key in COMPARED:
        if snapshot.get(key) != current.get(key):
            out.append(
                f"{key}: snapshot {json.dumps(snapshot.get(key), sort_keys=True)} != live {json.dumps(current.get(key), sort_keys=True)}"
            )
    snap_sets = {s["name"]: (s["target"], s["enforcement"]) for s in snapshot.get("rulesets", [])}
    live_sets = {s["name"]: (s["target"], s["enforcement"]) for s in current.get("rulesets", [])}
    if snap_sets != live_sets:
        out.append(f"rulesets: snapshot {snap_sets} != live {live_sets}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("cmd", choices=("write", "check", "matrix"))
    a = ap.parse_args(argv)
    if a.cmd == "matrix":
        snap = load_snapshot()
        for c in snap["required_status_checks"]:
            print(c)
        return 0
    current = live()
    if not current["required_status_checks"]:
        print(
            "could not read the branch rules (gh api refused or gh is absent); nothing written",
            file=sys.stderr,
        )
        return 3
    if a.cmd == "write":
        SNAPSHOT.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(
            f"wrote {SNAPSHOT.relative_to(ROOT)}: {len(current['required_status_checks'])} required contexts"
        )
        return 0
    problems = diff(load_snapshot(), current)
    if problems:
        print("LIVE SETTINGS DIFFER FROM docs/governance/repo_settings_live.json:")
        for p in problems:
            print("  " + p)
        print(
            "re-run `python tools/repo_settings_snapshot.py write` and update docs/GOVERNANCE.md section 1"
        )
        return 1
    print(
        f"live settings match the snapshot ({len(current['required_status_checks'])} required contexts)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
