"""REPO_SETTINGS.md describes the live ruleset, not a remembered one (v2.0 stage 11, S4).

``docs/governance/repo_settings_live.json`` is the snapshot ``tools/repo_settings_snapshot.py write``
takes with ``gh api``; the CI governance job warns when the live settings drift from it.  This
test pins the document to the snapshot: the required-check matrix in section 1 lists exactly the
snapshot's contexts, every context is a job (id or ``name:``) of a workflow in ``.github/workflows``,
and the matrix gives each a risk.  The v1.19 audit found 13 documented, 6 in the JSON block and
17 live; this makes that drift a failing test.
"""

from __future__ import annotations

import json
import re

import yaml
from conftest import REPO_ROOT

DOC = REPO_ROOT / "docs" / "governance" / "REPO_SETTINGS.md"
SNAPSHOT = REPO_ROOT / "docs" / "governance" / "repo_settings_live.json"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

#: Contexts whose check-run name is not a job of ci.yml / codeql.yml (the CodeQL workflow's
#: summary check) -- documented in the matrix all the same.
EXTERNAL_CONTEXTS = {"CodeQL"}


def _matrix_rows() -> list[dict[str, str]]:
    text = DOC.read_text(encoding="utf-8")
    start = text.index("<!-- required-check matrix: generated contexts -->")
    end = text.index("<!-- end required-check matrix -->", start)
    rows = []
    for line in text[start:end].splitlines():
        m = re.match(r"^\|\s*`([^`]+)`\s*\|(.*)\|\s*$", line)
        if m:
            cells = [c.strip() for c in m.group(2).split("|")]
            rows.append({"context": m.group(1), "cells": cells})
    return rows


def _workflow_job_names() -> set[str]:
    names: set[str] = set()
    for f in WORKFLOWS.glob("*.yml"):
        doc = yaml.safe_load(f.read_text(encoding="utf-8"))
        for job_id, job in (doc.get("jobs") or {}).items():
            names.add(job_id)
            name = (job or {}).get("name")
            if isinstance(name, str) and "${{" not in name:
                names.add(name)
            # a matrix job's name expression: record the literal alternatives and, for
            # `${{ matrix.<key> }}`, every value of that key in strategy.matrix.include
            if isinstance(name, str) and "${{" in name:
                names.update(re.findall(r"'([^']+)'", name))
                m = re.match(r"^(.*)\$\{\{\s*matrix\.([a-zA-Z_-]+)\s*\}\}(.*)$", name)
                matrix = ((job or {}).get("strategy") or {}).get("matrix") or {}
                if m:
                    values = [
                        e.get(m.group(2)) for e in matrix.get("include", []) if isinstance(e, dict)
                    ]
                    values += (
                        matrix.get(m.group(2), [])
                        if isinstance(matrix.get(m.group(2)), list)
                        else []
                    )
                    names.update(
                        f"{m.group(1)}{v}{m.group(3)}" for v in values if isinstance(v, str)
                    )
    return names


def test_snapshot_is_well_formed():
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert snap["x-version"] == 1
    assert snap["repo"] == "AshJha0/intraday-alpha-platform" and snap["branch"] == "main"
    ctx = snap["required_status_checks"]
    assert ctx == sorted(ctx) and len(ctx) == len(set(ctx)) and len(ctx) >= 17
    assert snap["strict_required_status_checks_policy"] is True
    assert "required_status_checks" in snap["rule_types"] and "pull_request" in snap["rule_types"]


def test_matrix_lists_exactly_the_live_required_contexts():
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    rows = _matrix_rows()
    documented = [r["context"] for r in rows]
    assert len(documented) == len(set(documented)), "duplicate rows"
    assert sorted(documented) == snap["required_status_checks"], (
        "REPO_SETTINGS.md section 1 and repo_settings_live.json disagree; "
        "re-run tools/repo_settings_snapshot.py write and update the matrix"
    )


def test_every_required_context_is_a_workflow_job_and_has_a_risk():
    names = _workflow_job_names()
    for r in _matrix_rows():
        ctx = r["context"]
        if ctx not in EXTERNAL_CONTEXTS:
            assert ctx in names, (
                f"required context {ctx!r} is not a job of any workflow: a merge could never satisfy it"
            )
        # columns: job (workflow) | change class | risk the check catches | per-PR cost
        assert len(r["cells"]) >= 4, r
        assert r["cells"][2], f"{ctx}: the matrix row gives no risk"


def test_document_states_the_snapshot_date_and_the_count():
    text = DOC.read_text(encoding="utf-8")
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert snap["fetched_at"][:10] in text, (
        "section 1 must name the date of the snapshot it was generated from"
    )
    assert f"{len(snap['required_status_checks'])} required" in text


def test_the_advisory_alias_exists_while_the_ruleset_names_it():
    """The job is `lint-and-secrets`; the ruleset still says `advisory`.  Until the owner swaps the
    context (REPO_SETTINGS.md owner action 1) ci.yml must carry the alias job, or no PR can merge."""
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    ci = yaml.safe_load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))
    jobs = ci["jobs"]
    assert "lint-and-secrets" in jobs
    if "advisory" in snap["required_status_checks"]:
        assert "advisory" in jobs and jobs["advisory"].get("needs") == "lint-and-secrets"
    else:
        assert "advisory" not in jobs, (
            "the ruleset no longer names `advisory`: delete the alias job"
        )
