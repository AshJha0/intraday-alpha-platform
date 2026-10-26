"""U14 / Z1: the release evidence manifest builder (tools/release/evidence_manifest.py).

Fixtures: a small tree with one claim per evidence class: a signed prereg and
the runner's ``hashes.json`` and redacted log for the registered classes, plain
artefacts for the development classes, nothing for ``pending``.  Signing uses a
throwaway test key.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from conftest import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "python" / "tests" / "isolation"))
from sealed_fixtures import keypair, sign_document  # noqa: E402


def _tool():
    spec = importlib.util.spec_from_file_location(
        "evidence_manifest", REPO_ROOT / "tools" / "release" / "evidence_manifest.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


em = _tool()


def _write(root: Path, rel: str, text: str) -> str:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return rel


@pytest.fixture
def tree(tmp_path):
    priv, pub = keypair()
    prereg = sign_document(
        {"kind": "analysis_prereg", "prereg_id": "research/preregs/x.json"}, priv, pub
    )
    _write(tmp_path, "research/preregs/x.json", json.dumps(prereg, indent=1))
    rec = {
        "prereg_id": "research/preregs/x.json",
        "prereg_sha256": em.prereg_hash(prereg),
        "seq": 1,
        "result_sha256": "ab" * 32,
        "evidence_class": "mechanically isolated",
    }
    _write(tmp_path, "results/x/0001/hashes.json", json.dumps(rec))
    _write(tmp_path, "logs/x.redacted.txt", "verified prereg ...\ndone\n")
    _write(tmp_path, "research/notes/dev.md", "development diagnostics\n")
    _write(tmp_path, "research/notes/coverage.md", "order-level data: none on the free route\n")
    reg = {
        "prereg": "research/preregs/x.json",
        "runner_log": "logs/x.redacted.txt",
        "result_hashes": "results/x/0001/hashes.json",
    }
    claims = {
        "release": "v2.0.0-fixture",
        "claims": [
            {
                "id": "C1",
                "claim": "D1 confirmed on Track B",
                "evidence_class": "mechanically isolated",
                **reg,
            },
            {
                "id": "C2",
                "claim": "D2 earns its hurdle",
                "evidence_class": "development-supported",
                "artefacts": ["research/notes/dev.md"],
            },
            {
                "id": "C3",
                "claim": "H4 order-flow family",
                "evidence_class": "data-ineligible",
                "reason": "no order-level data on the free route",
                "artefacts": ["research/notes/coverage.md"],
            },
            {
                "id": "C4",
                "claim": "Track C combined book",
                "evidence_class": "pending",
                "reason": "PF-3 deferred",
            },
            {
                "id": "C5",
                "claim": "D3 registered and rejected",
                "evidence_class": "rejected",
                **reg,
            },
            {
                "id": "C6",
                "claim": "H2 looked promising in development",
                "evidence_class": "exploratory",
                "artefacts": ["research/notes/dev.md"],
            },
        ],
    }
    return tmp_path, claims


def test_manifest_carries_class_hashes_prereg_and_runner_record(tree):
    root, claims = tree
    m = em.build(root, claims)
    assert [c["id"] for c in m["claims"]] == ["C5", "C3", "C4", "C6", "C2", "C1"]  # negatives first
    assert (
        m["classes"]["mechanically isolated"] == 1 and m["classes"]["independently confirmed"] == 0
    )
    c1 = next(c for c in m["claims"] if c["id"] == "C1")
    assert c1["prereg"]["sha256"] == em.file_sha256(root / "research/preregs/x.json")
    assert (
        c1["prereg"]["canonical_sha256"]
        == json.loads((root / "results/x/0001/hashes.json").read_text())["prereg_sha256"]
    )
    assert c1["runner_log"]["path"] == "logs/x.redacted.txt" and c1["runner_record"]["seq"] == 1
    c4 = next(c for c in m["claims"] if c["id"] == "C4")
    assert set(c4) == {"id", "claim", "evidence_class", "reason"}
    assert em.check(root, m) == []
    assert em.build(root, claims) == m  # deterministic


def test_check_reports_a_changed_or_missing_file(tree):
    root, claims = tree
    m = em.build(root, claims)
    (root / "research/notes/dev.md").write_text("edited\n")
    (root / "logs/x.redacted.txt").unlink()
    drift = em.check(root, m)
    assert any("dev.md changed" in d for d in drift) and any(
        "x.redacted.txt is missing" in d for d in drift
    )


def test_line_endings_do_not_change_a_text_hash(tmp_path):
    (tmp_path / "a").write_bytes(b"x\r\ny\r\n")
    (tmp_path / "b").write_bytes(b"x\ny\n")
    assert em.file_sha256(tmp_path / "a") == em.file_sha256(tmp_path / "b")


def _one(claims, cid):
    return next(c for c in claims["claims"] if c["id"] == cid)


@pytest.mark.parametrize(
    "cid, change, reason",
    [
        ("C1", lambda c: c.pop("runner_log"), "needs runner_log"),
        ("C1", lambda c: c.update(evidence_class="confirmed"), "not one of"),
        (
            "C1",
            lambda c: c.update(evidence_class="independently confirmed"),
            "approver other than the developer",
        ),
        (
            "C1",
            lambda c: c.update(
                evidence_class="independently confirmed",
                custody={"approver": "owner", "developer": "owner"},
            ),
            "approver other",
        ),
        ("C2", lambda c: c.pop("artefacts"), "at least one artefact"),
        ("C3", lambda c: c.pop("reason"), "needs a reason"),
        ("C4", lambda c: c.update(artefacts=["research/notes/dev.md"]), "cites no evidence"),
        ("C2", lambda c: c.update(artefacts=["nope.md"]), "does not exist"),
        ("C2", lambda c: c.update(claim=" "), "empty claim"),
    ],
)
def test_a_claim_short_of_its_class_is_refused(tree, cid, change, reason):
    root, claims = tree
    claims = copy.deepcopy(claims)
    change(_one(claims, cid))
    with pytest.raises(em.ManifestRefused, match=reason):
        em.build(root, claims)


def test_an_unsigned_prereg_or_a_foreign_runner_record_is_refused(tree):
    root, claims = tree
    doc = json.loads((root / "research/preregs/x.json").read_text())
    doc.pop("signature")
    doc["signed"] = False
    (root / "research/preregs/y.json").write_text(json.dumps(doc))
    c = copy.deepcopy(claims)
    _one(c, "C1")["prereg"] = "research/preregs/y.json"
    with pytest.raises(em.ManifestRefused, match="is not signed"):
        em.build(root, c)
    rec = json.loads((root / "results/x/0001/hashes.json").read_text())
    rec["prereg_sha256"] = "00" * 32
    (root / "results/x/0001/hashes.json").write_text(json.dumps(rec))
    with pytest.raises(em.ManifestRefused, match="names another prereg"):
        em.build(root, claims)


def test_a_runner_record_claiming_a_higher_class_is_refused(tree):
    root, claims = tree
    rec = json.loads((root / "results/x/0001/hashes.json").read_text())
    rec["evidence_class"] = "independently confirmed"
    (root / "results/x/0001/hashes.json").write_text(json.dumps(rec))
    with pytest.raises(em.ManifestRefused, match="not 'mechanically isolated'"):
        em.build(root, claims)


def test_independently_confirmed_needs_separate_custody(tree):
    root, claims = tree
    c = copy.deepcopy(claims)
    _one(c, "C1").update(
        evidence_class="independently confirmed",
        custody={"approver": "second-maintainer", "developer": "owner"},
    )
    m = em.build(root, c)
    assert m["claims"][-1]["custody"] == {"approver": "second-maintainer", "developer": "owner"}


def test_duplicate_ids_and_empty_files_are_refused(tree):
    root, claims = tree
    c = copy.deepcopy(claims)
    _one(c, "C2")["id"] = "C1"
    with pytest.raises(em.ManifestRefused, match="duplicate claim ids"):
        em.build(root, c)
    with pytest.raises(em.ManifestRefused, match="lists no claims"):
        em.build(root, {"claims": []})


def test_notes_table_is_negatives_first_and_names_each_class(tree):
    root, claims = tree
    text = em.notes(em.build(root, claims))
    rows = [r for r in text.splitlines()[2:]]
    assert [r.split("|")[1].strip() for r in rows] == ["C5", "C3", "C4", "C6", "C2", "C1"]
    assert all(any(k in r for k in em.CLASSES) for r in rows)
    assert "(PF-3 deferred)" in text


def test_cli_build_check_and_refusal(tree, capsys):
    root, claims = tree
    (root / "claims.json").write_text(json.dumps(claims))
    out = root / "manifest.json"
    assert (
        em.main(
            ["--root", str(root), "build", "--claims", str(root / "claims.json"), "--out", str(out)]
        )
        == 0
    )
    assert em.main(["--root", str(root), "check", "--manifest", str(out)]) == 0
    _one(claims, "C2").pop("artefacts")
    (root / "claims.json").write_text(json.dumps(claims))
    assert em.main(["--root", str(root), "build", "--claims", str(root / "claims.json")]) == 2
    assert "REFUSED" in capsys.readouterr().err
