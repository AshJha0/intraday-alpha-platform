"""CR1 governance checks that need no runner: the workflow file (environment,
labels, trigger, debug modes, permissions, artifact), CODEOWNERS coverage
(property 2), the settings page naming the protections the owner must set,
the threat model covering the six properties, and the custody wording
(property 6)."""

from __future__ import annotations

import re

import yaml
from conftest import REPO_ROOT

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "registered-run.yml"
CODEOWNERS = REPO_ROOT / "CODEOWNERS"
GOV = REPO_ROOT / "docs" / "governance"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_workflow_is_manual_only_in_the_sealed_environment_on_the_runner_identity():
    wf = _workflow()
    on = wf[True] if True in wf else wf["on"]  # PyYAML reads the bare `on:` key as True
    assert list(on) == ["workflow_dispatch"], "only workflow_dispatch may start a registered run"
    inputs = on["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"prereg", "commit", "mode", "year", "stooq_sha256"}  # DR-16
    assert inputs["mode"]["options"] == ["registered", "dd1", "dd1-probe", "dd1-dev"]
    assert inputs["mode"]["default"] == "registered"
    assert wf["permissions"] == {"contents": "write"}
    assert wf["concurrency"] == {"group": "registered-run", "cancel-in-progress": False}
    jobs = wf["jobs"]
    assert list(jobs) == ["registered-run"]
    job = jobs["registered-run"]
    assert job["environment"] == "sealed"
    assert set(job["runs-on"]) >= {"self-hosted", "iap-runner"}
    assert job["env"]["ACTIONS_STEP_DEBUG"] == "false"
    assert job["env"]["ACTIONS_RUNNER_DEBUG"] == "false"
    steps = job["steps"]
    # the bound commit is resolved to its full SHA first (a short SHA is a branch name to
    # actions/checkout; owner run 40215808482), then checked out from that output and
    # nothing else runs before the checkout
    resolve = steps[0]
    assert (
        resolve.get("id") == "resolve"
        and "repos/${{ github.repository }}/commits/" in resolve["run"]
    )
    checkout = steps[1]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"]["ref"] == "${{ steps.resolve.outputs.sha }}"
    assert any("whoami" in (s.get("run") or "") for s in steps)
    assert any("tools/runner/run.py run" in (s.get("run") or "") for s in steps)
    # DD1 (stage 2): the ingestion step runs only in mode dd1, the analysis only otherwise,
    # under the same environment approval and runner identity, with the same single artifact
    dd1 = [s for s in steps if "tools/ingest/dd1_daily.py run" in (s.get("run") or "")]
    assert (
        len(dd1) == 1
        and dd1[0]["if"]
        == "${{ inputs.mode == 'dd1' || inputs.mode == 'dd1-probe' || inputs.mode == 'dd1-dev' }}"
    )
    assert '-eq "dd1-probe") { $zip += "--probe" }' in dd1[0]["run"]
    assert '-eq "dd1-dev") { $zip += "--dev-only" }' in dd1[0]["run"]
    analysis = [s for s in steps if "tools/runner/run.py run" in (s.get("run") or "")]
    assert analysis[0]["if"] == "${{ inputs.mode == 'registered' }}"  # a new mode never runs it
    assert "--log results-log.redacted.txt" in dd1[0]["run"]
    uploads = [s for s in steps if (s.get("uses") or "").startswith("actions/upload-artifact@")]
    assert len(uploads) == 1 and uploads[0]["with"]["path"] == "results-log.redacted.txt"
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "secrets." not in text, "the workflow reads no secret"
    assert "pull_request" not in text.split("\non:")[1].split("\npermissions:")[0]


def test_every_action_is_pinned_to_a_commit_sha():
    for line in WORKFLOW.read_text(encoding="utf-8").splitlines():
        m = re.search(r"uses:\s*([^\s@]+)@([0-9a-f]+)", line)
        if "uses:" in line:
            assert m and len(m.group(2)) == 40, line


def test_codeowners_cover_the_workflow_and_the_runner():
    rules = [
        line.split()
        for line in CODEOWNERS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    patterns = {r[0]: r[1:] for r in rules}
    for path in (
        "/.github/workflows/registered-run.yml",
        "/tools/runner/",
        "/tools/sealed_service.py",
        "/tools/admin/",
        "/research/audit/",
    ):
        assert path in patterns, f"CODEOWNERS lacks {path}"
        assert patterns[path] == ["@AshJha0"]


def test_repo_settings_list_the_protections_the_owner_must_set():
    text = (GOV / "REPO_SETTINGS.md").read_text(encoding="utf-8")
    for needle in (
        "sealed",
        "required reviewer",
        "results",
        "ACTIONS_STEP_DEBUG",
        "registered-run.yml",
        "tools/runner/",
        "require_code_owner_review",
        "self-hosted",
    ):
        assert needle in text, needle


def test_threat_model_covers_the_six_properties_and_the_custody_label():
    text = (GOV / "RUNNER_THREAT_MODEL.md").read_text(encoding="utf-8")
    for n in range(1, 7):
        assert re.search(rf"(^|\n)#+ .*\b{n}\b", text) or f"Property {n}" in text, f"property {n}"
    assert "mechanically isolated" in text
    assert "independently confirmed" in text
    for needle in (
        "direct read",
        "enumeration",
        "cache",
        "debug",
        "CODEOWNERS",
        "ledger",
        "append-only",
        "custody",
    ):
        assert needle.lower() in text.lower(), needle


def test_sealed_data_doc_states_the_single_machine_limit():
    text = (GOV / "SEALED_DATA.md").read_text(encoding="utf-8")
    assert "single" in text.lower() and "machine" in text.lower()
    assert "administrator" in text.lower()
    assert "mechanically isolated" in text


def test_runner_setup_installs_nothing_from_the_repository():
    text = (GOV / "RUNNER_SETUP.md").read_text(encoding="utf-8")
    assert "iap-runner" in text and "config.cmd" in text and "svc" in text.lower()
    assert "sealed" in text


def test_custody_label_is_written_into_every_result():
    src = (REPO_ROOT / "tools" / "runner" / "run.py").read_text(encoding="utf-8")
    assert '"evidence_class": "mechanically isolated"' in src
