"""Round 5 (b): a dry registration through the runner with a prereg generated
from a confirmation-profile template, on synthetic data only.

The world is the CR1 fixture (a throwaway repository, a synthetic sealed
partition, its service and an in-memory transport); the prereg is filled from
the committed PF-1 template by ``tools/prereg_from_template.py`` and signed
with the fixture's TEST key, never the owner's.  The committed templates
themselves are checked to be unsigned and to fix what their profile fixes.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import sys

import pytest
from conftest import REPO_ROOT
from runner_fixtures import IMAGE, World, git, redact, run
from sealed_fixtures import sign_document

TEMPLATES = REPO_ROOT / "research" / "v2" / "prereg_templates"


def _tool():
    spec = importlib.util.spec_from_file_location(
        "prereg_from_template", REPO_ROOT / "tools" / "prereg_from_template.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pft = _tool()


def _fill(world: World, **over) -> dict:
    fill = {
        "horizon_sessions": 3,
        "family": ["synthetic-h1", "synthetic-h2"],
        "T_min": 120,
        "capital_usd": 1_000_000,
        "hurdle_bps": 1.0,
        "margin": 0.25,
        "min_units_per_session": 1,
        "origin": "synthetic",
        "partition": world.partition,
        "dataset": "synthetic-dry-registration",
        "dataset_hash_id": world.dataset_hash_id,
        "dataset_files": dict(world.files),
        "image_digest": IMAGE,
        "command": [sys.executable, "-I", str(world.script)],
        "scripts": ["analysis.py"],
        "inputs": [],
    }
    fill.update(over)
    return fill


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    dest = w.root / "research" / "v2" / "prereg_templates"
    dest.mkdir(parents=True)
    shutil.copy(TEMPLATES / "PF-1.template.json", dest)
    (w.root / "protocol.md").write_text("synthetic dry-registration protocol\n")
    git(w.root, "add", "-A")
    git(w.root, "commit", "-q", "-m", "templates")
    w.commit = git(w.root, "rev-parse", "HEAD")
    return w


def _generate(world: World, **over) -> dict:
    return pft.build_prereg(
        world.root,
        "research/v2/prereg_templates/PF-1.template.json",
        _fill(world, **over),
        world.prereg_rel,
        protocol_rel="protocol.md",
        commit=world.commit,
        evidence_root=REPO_ROOT,
    )


def test_template_generated_prereg_runs_once_through_the_runner(world, monkeypatch):
    # the template binds the image digest; inside the pinned image the job sees it as IAP_IMAGE_DIGEST
    monkeypatch.setenv("IAP_IMAGE_DIGEST", IMAGE)
    doc = _generate(world)
    assert doc["signed"] is False and "signature" not in doc
    assert doc["protocol"] == "protocol.md" and "NO planted dry run" in doc["status"]
    assert doc["registration"]["procedure"] == "P-HAC-L5" and doc["registration"]["hac_lag"] == 5
    assert doc["analysis_identity"]["data_versions"] == [world.dataset_hash_id]
    assert doc["analysis_identity"]["image_digest"] == IMAGE
    world.write_prereg(sign_document(doc, world.priv, world.pub))  # the fixture's test key
    git(world.root, "add", "-A")
    git(world.root, "commit", "-q", "-m", "signed template-generated prereg")
    log = redact.RedactingLog([str(world.root)])
    rec = run.registered_run(
        world.root,
        world.prereg_rel,
        identity="iap-runner",
        transport=world.transport,
        cache_root=world.root.parent / "runner-cache",
        log=log,
    )
    assert rec["seq"] == 1 and rec["evidence_class"] == "mechanically isolated"
    assert world.ops() == ["read", "store_result", "publish"]
    assert git(world.root, "rev-parse", "results") == rec["result_commit"]
    assert str(world.root) not in log.text()
    with pytest.raises(run.RunRefused, match="already_registered"):
        run.registered_run(
            world.root,
            world.prereg_rel,
            identity="iap-runner",
            transport=world.transport,
            cache_root=world.root.parent / "runner-cache",
        )


def test_an_unsigned_template_prereg_is_refused_before_any_request(world):
    world.write_prereg(_generate(world))
    git(world.root, "add", "-A")
    git(world.root, "commit", "-q", "-m", "unsigned")
    with pytest.raises(run.RunRefused) as exc:
        run.registered_run(
            world.root,
            world.prereg_rel,
            identity="iap-runner",
            transport=world.transport,
            cache_root=world.root.parent / "runner-cache",
        )
    assert exc.value.code == 3 and "not signed" in str(exc.value)
    assert world.ops() == []


@pytest.mark.parametrize(
    "over, reason",
    [
        ({"horizon_sessions": 20}, "horizon outside the profile"),
        ({"T_min": 119}, "below the validated T"),
        ({"family": ["h", "h"]}, "empty or duplicated family"),
        ({"capital_usd": 0}, "economic screen missing"),
        ({"partition": "a"}, "is not 'b'"),
        ({"origin": "vendor"}, "data origin"),
        ({"min_units_per_session": 0}, "min_units_per_session"),
        ({"margin": -0.1}, "margin"),
    ],
)
def test_fills_the_profile_gate_would_refuse_are_refused(world, over, reason):
    with pytest.raises(pft.TemplateRefused, match=reason):
        _generate(world, **over)


def test_missing_or_unknown_fill_keys_are_refused(world):
    fill = _fill(world)
    del fill["T_min"]
    with pytest.raises(pft.TemplateRefused, match=r"missing \['T_min'\]"):
        pft.fill_template(
            json.loads((TEMPLATES / "PF-1.template.json").read_text()),
            fill,
            evidence_root=REPO_ROOT,
        )
    fill = _fill(world, procedure="P-CBB")
    with pytest.raises(pft.TemplateRefused, match=r"unknown \['procedure'\]"):
        pft.fill_template(
            json.loads((TEMPLATES / "PF-1.template.json").read_text()),
            fill,
            evidence_root=REPO_ROOT,
        )


def test_a_template_edited_to_a_procedure_without_domain_is_refused(world):
    t = json.loads((TEMPLATES / "PF-1.template.json").read_text())
    t = copy.deepcopy(t)
    t["registration"]["procedure"] = "P-CBB"
    with pytest.raises(
        pft.TemplateRefused, match="outside validated domain|without its development"
    ):
        pft.fill_template(t, _fill(world), evidence_root=REPO_ROOT)


def test_pf3_placeholder_refuses_to_fill(world):
    t = json.loads((TEMPLATES / "PF-3.placeholder.json").read_text())
    with pytest.raises(pft.TemplateRefused, match="PF-3 deferred"):
        pft.fill_template(t, {}, evidence_root=REPO_ROOT)


@pytest.mark.parametrize(
    "name", ["PF-1.template.json", "PF-2.template.json", "PF-3.placeholder.json"]
)
def test_committed_templates_are_unsigned(name):
    t = json.loads((TEMPLATES / name).read_text())
    assert t["kind"] == "prereg_template"
    assert "signature" not in t and t.get("signed") is not True


@pytest.mark.parametrize("pid, proc, lag", [("PF-1", "P-HAC-L5", 5), ("PF-2", "P-HAC-L20", 20)])
def test_templates_fix_what_the_profile_fixes(pid, proc, lag):
    from iap.validation import profiles

    t = json.loads((TEMPLATES / f"{pid}.template.json").read_text())
    reg = t["registration"]
    prof = profiles.PROFILES[pid]
    assert reg["profile"] == pid and reg["statistic"] == prof.statistic
    assert reg["procedure"] == proc and prof.procedures[proc]["lag"] == lag == reg["hac_lag"]
    assert reg["alpha_op"] == profiles.ALPHA_OP and reg["sample_size"]["cap"] == 1.25
    assert tuple(reg["refusal_conditions"]) == profiles.DIAGNOSTICS
    assert reg["registry"] == {"window_state": "fresh", "confirmation_dataset_looks": 0}
    dom = profiles.operating_domain(pid)
    assert dom[proc]["reference_passes"] and dom[proc]["generators"]
    assert f"at least {dom[proc]['T_min']}" in reg["sample_size"]["T_min"]["rule"]
    assert pft.slot_names(t) == {
        "horizon_sessions",
        "family",
        "T_min",
        "capital_usd",
        "hurdle_bps",
        "margin",
        "min_units_per_session",
        "origin",
        "partition",
        "dataset",
        "dataset_hash_id",
        "dataset_files",
        "image_digest",
        "command",
        "scripts",
        "inputs",
    }
