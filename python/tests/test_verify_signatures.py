"""tools/verify_signatures.py: enumerates every signed document, reports a reason
per failure and exits non-zero (review finding 6 and the trust-anchor point).

The repository run checks the real signed documents on this checkout; the
adversarial cases use a throw-away git repository with a test trust root.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from iap.agents import signing
from iap.agents.blackboard import canonical

REPO = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location(
        "verify_signatures", REPO / "tools/verify_signatures.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vs = _load()


def test_every_signed_document_on_this_checkout_verifies():
    results = vs.run(REPO)
    bad = [r for r in results if not r["ok"]]
    assert bad == [], bad
    kinds = {r["kind"] for r in results}
    assert kinds == {"trust_root", "signed_document", "key_registry", "blackboard"}
    docs = {r["item"] for r in results if r["kind"] == "signed_document"}
    for rel in (
        "research/preregs/b8v2_prereg.json",
        "research/v118/null_protocol_prereg.json",
        "research/v118/rel08_rule_signed.json",
        "research/r7/r7_confirm_prereg.json",
    ):
        assert rel in docs


def test_pinned_fingerprint_is_the_sha256_of_the_trust_root_key():
    roots = json.loads((REPO / "research/agents/trust_roots.json").read_text(encoding="utf-8"))
    for name, pin in vs.PINNED["maintainers"].items():
        assert roots["maintainers"][name] == pin["public_key"]
        assert (
            hashlib.sha256(bytes.fromhex(pin["public_key"])).hexdigest()
            == pin["fingerprint_sha256"]
        )
    doc = (REPO / "docs/governance/TRUST_ROOT.md").read_text(encoding="utf-8")
    assert pin["fingerprint_sha256"] in doc and vs.PINNED["trust_roots_canonical_sha256"] in doc


# ------------------------------------------------------------- adversarial --
def _git(r: Path, *a: str) -> None:
    subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True)


def _sign(doc: dict, priv: str, pub: str, who: str = "owner") -> dict:
    doc = {**doc, "signed": True}
    msg = canonical(doc).encode("ascii")
    sig = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(priv)).sign(msg).hex()
    return {
        **doc,
        "signature": {
            "scheme": "ed25519",
            "maintainer": who,
            "public_key": pub,
            "message_sha256": hashlib.sha256(msg).hexdigest(),
            "sig": sig,
        },
    }


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    (r / "research/agents").mkdir(parents=True)
    priv, pub = signing.generate_keypair()
    roots = {"maintainers": {"owner": pub}}
    (r / "research/agents/trust_roots.json").write_text(json.dumps(roots), encoding="utf-8")
    pin = {
        "version": 1,
        "trust_roots_canonical_sha256": vs.canonical_sha256(roots),
        "maintainers": {"owner": {"public_key": pub, "fingerprint_sha256": vs.fingerprint(pub)}},
        "revoked": [],
    }
    monkeypatch.setattr(vs, "PINNED", pin)
    monkeypatch.setattr(vs, "check_registry", lambda root, ref: None)  # no agent registry here
    for name in ("a", "b"):
        doc = _sign({"kind": "prereg", "name": name}, priv, pub)
        (r / f"research/{name}.json").write_text(json.dumps(doc), encoding="utf-8")
    (r / "research/draft.json").write_text('{"signed": false, "signature": null}', encoding="utf-8")
    _git(r, "init", "-q")
    _git(r, "add", "-A")
    return r, priv, pub


def _fails(r: Path) -> dict[str, list[str]]:
    return {x["item"]: x["reasons"] for x in vs.run(r) if not x["ok"]}


def test_clean_repo_passes_and_drafts_are_not_signed_documents(repo):
    r, _, _ = repo
    assert _fails(r) == {}
    assert vs.main(["--root", str(r)]) == 0
    assert "research/draft.json" not in {x["item"] for x in vs.run(r)}


def test_edited_document_fails_with_its_reason_and_others_still_report(repo, capsys):
    r, _, _ = repo
    p = r / "research/a.json"
    doc = json.loads(p.read_text(encoding="utf-8"))
    doc["name"] = "changed after signing"
    p.write_text(json.dumps(doc), encoding="utf-8")
    fails = _fails(r)
    assert list(fails) == ["research/a.json"]
    assert "changed after signing" in fails["research/a.json"][0]
    assert vs.main(["--root", str(r)]) == 1
    out = capsys.readouterr().out
    assert (
        "FAIL" in out
        and "research/a.json" in out
        and "OK    signed_document  research/b.json" in out
    )


def test_forged_signature_and_foreign_key_and_unsigned_block_fail(repo):
    r, priv, pub = repo
    other_priv, other_pub = signing.generate_keypair()
    (r / "research/c.json").write_text(
        json.dumps(_sign({"name": "c"}, other_priv, other_pub)), encoding="utf-8"
    )
    d = _sign({"name": "d"}, priv, pub)
    d["signature"]["sig"] = "00" * 64
    (r / "research/d.json").write_text(json.dumps(d), encoding="utf-8")
    e = _sign({"name": "e"}, priv, pub)
    e["signed"] = False  # a signature block on a document that says it is unsigned
    (r / "research/e.json").write_text(json.dumps(e), encoding="utf-8")
    _git(r, "add", "-A")
    fails = _fails(r)
    assert "not the trust-root maintainer key" in fails["research/c.json"][0]
    assert "INVALID" in fails["research/d.json"][0]
    assert "not signed" in fails["research/e.json"][0]


def test_changed_trust_root_and_revoked_key_fail(repo, monkeypatch):
    r, _, pub = repo
    roots_p = r / "research/agents/trust_roots.json"
    _, extra = signing.generate_keypair()
    roots_p.write_text(
        json.dumps({"maintainers": {"owner": pub, "intruder": extra}}), encoding="utf-8"
    )
    reasons = _fails(r)["research/agents/trust_roots.json"]
    assert any("trust root changed" in x for x in reasons)
    assert any("intruder is in the trust root but not pinned" in x for x in reasons)
    roots_p.write_text(json.dumps({"maintainers": {"owner": pub}}), encoding="utf-8")
    monkeypatch.setitem(vs.PINNED, "revoked", [vs.fingerprint(pub)])
    fails = _fails(r)
    assert any("revoked" in x for x in fails["research/agents/trust_roots.json"])
    assert fails["research/a.json"] == ["signed with a revoked key"]


def test_tampered_fork_board_fails(tmp_path):
    import shutil

    r = tmp_path / "repo"
    src = REPO / "research/agents/forks/step2"
    dst = r / "research/agents/forks/step2"
    shutil.copytree(src, dst)
    board = dst / "blackboard.jsonl"
    fork = dst / "FORK.json"
    assert vs.check_fork_board(board, fork) == []
    lines = board.read_bytes().splitlines()
    board.write_bytes(b"\n".join(lines[:-1]) + b"\n")  # drop the last entry
    problems = vs.check_fork_board(board, fork)
    assert any("FORK.json" in p for p in problems)
