"""v1.14 governance: trust roots and bound signatures (G-P1-4), the chained
multiple-testing ledger (G-P1-5)."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess

import pytest
from conftest import REPO_ROOT
from iap.agents import anchor, signing, trust
from iap.agents.blackboard import Blackboard
from iap.agents.broker import BrokerError, WriteBroker, sign
from iap.validation.ledger import (
    ExperimentLedger,
    LedgerIntegrityError,
    chain_head,
)

SEC = 10**9
PRIV, PUB = signing.generate_keypair()
TASK = {"title": "t", "spec": {}}


class Clock:
    def __init__(self) -> None:
        self.t = 1_000 * SEC

    def __call__(self) -> int:
        self.t += SEC
        return self.t


def _fp(alpha_id):
    return {"code_hash": "a" * 64, "features": ["f"], "feature_hash": "f" * 64}


def _broker(root, board=None, pubkeys=None):
    return WriteBroker(
        root, {"alice"}, clock=Clock(), fingerprinter=_fp, board=board, pubkeys=pubkeys
    )


def _harness(name):
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "tests" / "harness" / f"{name}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# -- G-P1-4: bound signatures ---------------------------------------------------


def test_bound_request_cannot_be_replayed_on_another_board_or_position(tmp_path):
    b = _broker(tmp_path, pubkeys={"alice": PUB})
    other = _broker(tmp_path, board=tmp_path / "other.jsonl", pubkeys={"alice": PUB})
    ctx = b.request_context()
    assert ctx["board"] == "research/agents/blackboard.jsonl"
    auth = sign(PRIV, "alice", "post_task", TASK, **ctx)
    assert auth["scheme"] == signing.ED25519_V2
    with pytest.raises(BrokerError, match="signed for board"):
        other.post_task("alice", "t", {}, auth=auth)
    b.post_task("alice", "t", {}, auth=auth)
    # same args, signed at the old head: refused once the head moved
    stale = sign(PRIV, "alice", "post_task", {"title": "u", "spec": {}}, **ctx)
    with pytest.raises(BrokerError, match="stale board head"):
        b.post_task("alice", "u", {}, auth=stale)
    # v1.10 unbound ed25519 is verify-only now
    with pytest.raises(BrokerError, match="scheme"):
        b.post_task("alice", "u", {}, auth=sign(PRIV, "alice", "post_task", TASK))
    bb = Blackboard(b.board.path)
    assert bb.verify_signatures({"alice": PUB}, board_id=ctx["board"]) == []
    assert bb.verify_signatures({"alice": PUB}, board_id="elsewhere.jsonl")


def test_verify_signatures_catches_a_bound_auth_moved_to_another_position(tmp_path):
    b = _broker(tmp_path, pubkeys={"alice": PUB})
    b.post_task(
        "alice", "t", {}, auth=sign(PRIV, "alice", "post_task", TASK, **b.request_context())
    )
    e = Blackboard(b.board.path).entries()[0]
    rebuilt = Blackboard(tmp_path / "copy.jsonl")
    rebuilt.append("task", "alice", 1, {"task_id": "x" * 16, "title": "pad", "spec": {}})
    rebuilt.append(e["kind"], "alice", e["ts"], e["body"], e["auth"])
    problems = rebuilt.verify_signatures({"alice": PUB})
    assert any("replayed" in p for p in problems)


# -- G-P1-4: maintainer-signed registry -------------------------------------------


def test_registry_must_be_signed_by_a_trust_root():
    mpriv, mpub = signing.generate_keypair()
    roots = {"maintainers": {"maint": mpub}}
    reg = {"alice": PUB}
    assert trust.verify_registry({}, None, None) == []  # empty: nothing to trust
    assert trust.verify_registry(reg, None, roots)  # unsigned
    assert trust.verify_registry(reg, None, {"maintainers": {}})
    doc = trust.sign_registry(mpriv, "maint", reg)
    assert trust.verify_registry(reg, doc, roots) == []
    _, evil = signing.generate_keypair()
    assert trust.verify_registry({**reg, "bob": evil}, doc, roots)  # registry edited
    other_priv, _ = signing.generate_keypair()
    forged = trust.sign_registry(other_priv, "maint", reg)
    assert trust.verify_registry(reg, forged, roots) == ["bad registry signature by 'maint'"]
    assert trust.verify_registry(reg, {**doc, "maintainer": "x"}, roots)


def test_cli_refuses_unsigned_prereg_by_a_keyed_agent(tmp_path, capsys):
    from iap.agents import cli

    keys = tmp_path.parent / f"{tmp_path.name}_keys"
    mkey = keys / "maint.json"
    root = tmp_path
    (root / "research" / "agents").mkdir(parents=True)
    assert (
        cli.main(
            ["--root", str(root), "maintainer-keygen", "--maintainer", "m", "--keyfile", str(mkey)]
        )
        == 0
    )
    assert (
        cli.main(
            [
                "--root",
                str(root),
                "agent-keygen",
                "--agent",
                "alice",
                "--keyfile",
                str(keys / "a.json"),
            ]
        )
        == 0
    )
    args = ["--root", str(root), "prereg", "--agent", "alice", "--alpha", "EQ01", "--horizon", "1s"]
    args += ["--hypothesis", "h", "--expected-sign", "1"]
    with pytest.raises(SystemExit, match="not trusted"):  # registry not signed yet
        cli.main(args)
    assert (
        cli.main(
            ["--root", str(root), "sign-registry", "--maintainer", "m", "--keyfile", str(mkey)]
        )
        == 0
    )
    assert trust.trusted_pubkeys(root).keys() == {"alice"}
    assert cli.main(args) == 1
    assert "registered public key" in capsys.readouterr().err


# -- G-P1-5: chained ledger ---------------------------------------------------------


def test_ledger_chain_detects_edits_and_is_recorded_on_prereg(tmp_path):
    b = _broker(tmp_path)
    b.preregister("alice", "EQ01", "1s", "h", 1)
    b.preregister("alice", "EQ02", "1s", "h", 1)
    path = tmp_path / "research" / "experiments.json"
    blob = json.loads(path.read_text())
    assert blob["chain_head"] == chain_head(blob["entries"]) and blob["chain_length"] == 2
    bodies = [e["body"] for e in b.board.entries()]
    assert bodies[1]["ledger_head"] == blob["chain_head"]
    assert bodies[0]["ledger"] == "research/experiments.json"
    assert bodies[0]["ledger_head"] == chain_head(blob["entries"][:1])
    mon = _harness("check_ledger_monotone")
    assert mon.check_prereg_heads(b.board.path.read_bytes(), blob) == []
    # drop the first look: the recorded head no longer matches
    tampered = dict(blob, entries=blob["entries"][1:], total_experiments=1)
    path.write_text(json.dumps(tampered))
    with pytest.raises(LedgerIntegrityError, match="chain_head"):
        ExperimentLedger(path)
    # a re-chained copy loads, but the prereg's head is no longer on its chain
    rechained = dict(tampered, chain_head=chain_head(tampered["entries"]), chain_length=1)
    path.write_text(json.dumps(rechained))
    ExperimentLedger(path)
    assert mon.check_prereg_heads(b.board.path.read_bytes(), rechained)
    # total must equal the looks
    path.write_text(json.dumps(dict(blob, total_experiments=99)))
    with pytest.raises(LedgerIntegrityError, match="total_experiments"):
        ExperimentLedger(path)


def test_monotone_check_requires_descent():
    mon = _harness("check_ledger_monotone")

    def doc(entries):
        return {
            "entries": entries,
            "total_experiments": sum(e["count"] for e in entries),
            "chain_head": chain_head(entries),
            "chain_length": len(entries),
        }

    e = [{"key": k, "alpha_id": "A", "kind": "k", "count": 1, "config": {}} for k in "abc"]
    assert mon.compare_ledger(doc(e[:2]), doc(e)) == []
    swapped = mon.compare_ledger(doc(e[:2]), doc([e[1], e[0], e[2]]))
    assert any("does not descend" in p for p in swapped)
    edited = [dict(e[0], count=2), e[1], e[2]]
    assert any("does not descend" in p for p in mon.compare_ledger(doc(e[:2]), doc(edited)))
    bad = dict(doc(e), chain_head="0" * 64)
    assert any("chain_head does not match" in p for p in mon.compare_ledger(doc(e[:2]), bad))
    unchained = {k: v for k, v in doc(e).items() if k != "chain_head"}
    assert any("no chain_head" in p for p in mon.compare_ledger(doc(e[:2]), unchained))


# -- G-P1-4: anchors against a ref ---------------------------------------------------


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_anchors_verified_against_a_ref(tmp_path):
    def git(*a):
        return subprocess.run(
            ["git", "-C", str(tmp_path), *a], check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    b = _broker(tmp_path)
    b.post_task("alice", "t", {})
    git("add", "-A")
    git("commit", "-qm", "board")
    board = b.board.path
    git("checkout", "-qb", "side")
    b.post_task("alice", "u", {})
    git("add", "-A")
    git("commit", "-qm", "side entry")
    af = tmp_path / "anchors.json"
    af.write_text(json.dumps(anchor.anchors(tmp_path, board)), encoding="ascii")
    assert anchor.verify(tmp_path, board, af)["ok"]
    rep = anchor.verify(tmp_path, board, af, ref="main")
    assert not rep["ok"] and any("is not on main" in p for p in rep["problems"])
    assert anchor.verify(tmp_path, board, af, ref="side")["ok"]
