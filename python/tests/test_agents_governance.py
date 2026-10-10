"""v1.10 governance fixes G1-G4: look budget + code hash, reserve cap key,
external anchoring, authenticated agent identity."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from conftest import REPO_ROOT
from iap.agents import anchor
from iap.agents.blackboard import Blackboard
from iap.agents.broker import BrokerError, WriteBroker, sign
from iap.agents.prereg_gate import PreregistrationError, require
from iap.agents.reserve import ReserveError, ReserveEvaluator
from iap.validation.ledger import ExperimentLedger

SEC = 10**9
KEY = b"k" * 32


class Clock:
    def __init__(self) -> None:
        self.t = 1_000 * SEC

    def __call__(self) -> int:
        self.t += SEC
        return self.t


class Code:
    """A switchable fake fingerprint."""

    def __init__(self) -> None:
        self.v = "a"

    def __call__(self, alpha_id: str) -> dict:
        return {"code_hash": self.v * 64, "features": ["f"], "feature_hash": "f" * 64}


@pytest.fixture
def code():
    return Code()


def _broker(tmp_path, code, **kw):
    return WriteBroker(tmp_path, {"alice"}, clock=Clock(), fingerprinter=code, **kw)


# -- G1 ----------------------------------------------------------------------


def test_prereg_debits_a_look_and_carries_the_code_hash(tmp_path, code):
    b = _broker(tmp_path, code)
    b.preregister("alice", "EQ01", "1s", "h", 1)
    led = ExperimentLedger(tmp_path / "research" / "experiments.json")
    assert led.total_experiments == 1 and led.entries[0]["kind"] == "prereg"
    body = b.board.entries()[0]["body"]
    assert body["format"] == 2 and body["code"]["code_hash"] == "a" * 64
    assert body["ledger_total"] == 1


def test_gate_refuses_after_code_change_until_superseded(tmp_path, code):
    b = _broker(tmp_path, code)
    b.preregister("alice", "EQ01", "1s", "h", 1)
    assert require(tmp_path, "EQ01", "1s", fingerprinter=code)["seq"] == 1
    code.v = "b"
    with pytest.raises(PreregistrationError, match="code_hash changed"):
        require(tmp_path, "EQ01", "1s", fingerprinter=code)
    with pytest.raises(BrokerError):
        b.preregister("alice", "EQ01", "1s", "h2", 1)  # no silent re-registration
    b.preregister("alice", "EQ01", "1s", "h2", 1, supersede=True)
    assert require(tmp_path, "EQ01", "1s", fingerprinter=code)["seq"] == 2
    with pytest.raises(BrokerError, match="unchanged"):
        b.preregister("alice", "EQ01", "1s", "h3", 1, supersede=True)
    assert ExperimentLedger(tmp_path / "research" / "experiments.json").total_experiments == 2


def test_legacy_v19_preregs_still_pass_the_gate():
    """The committed 2026 holdout preregs (no fingerprint) verify and gate as before."""
    board = REPO_ROOT / "research" / "agents" / "blackboard.jsonl"
    assert Blackboard(board).verify() >= 6
    assert require(REPO_ROOT, "EQ01", "1s")["body"].get("format") is None


def test_real_fingerprint_is_stable_and_covers_features():
    from iap.agents.fingerprint import fingerprint

    fp = fingerprint("EQ01")
    assert fp == fingerprint("EQ01") and len(fp["code_hash"]) == 64 and fp["features"]
    assert fingerprint("COMB_EQ") is None


# -- G2 ----------------------------------------------------------------------


def test_reserve_cap_ignores_dummy_fields(tmp_path, code):
    b = WriteBroker(tmp_path, {"alice"}, trusted={"evaluator"}, clock=Clock(), fingerprinter=code)
    b.preregister("alice", "EQ01", "1s", "h", 1)
    ev = ReserveEvaluator(b"s" * 32, b, lambda s, c: False, max_attempts=2)
    cand = {"alpha_id": "EQ01", "horizon": "1s", "expected_sign": 1}
    ev.evaluate("alice", cand)
    ev.evaluate("alice", dict(reversed(list(cand.items()))))
    with pytest.raises(ReserveError, match="exhausted"):
        ev.evaluate("alice", cand)
    with pytest.raises(ReserveError, match="unknown candidate fields"):
        ev.evaluate("alice", {**cand, "dummy": 1})
    code.v = "b"  # new code: refused until re-registered
    with pytest.raises(ReserveError, match="code changed"):
        ev.evaluate("alice", cand)


# -- G3 ----------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_anchor_detects_rechaining(tmp_path, code):
    def git(*a):
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    b = _broker(tmp_path, code)
    b.post_task("alice", "t", {})
    b.post_task("alice", "u", {})
    git("add", "-A")
    git("commit", "-qm", "board")
    b.post_task("alice", "v", {})
    board = b.board.path
    doc = anchor.anchors(tmp_path, board)
    assert [r["seq"] for r in doc["entries"]] == [1, 2]
    af = tmp_path / "anchors.json"
    af.write_text(json.dumps(doc), encoding="ascii")
    rep = anchor.verify(tmp_path, board, af)
    assert rep["ok"] and rep["anchored"] == 2 and rep["unanchored"] == 1
    # re-chain: edit entry 1 and recompute every hash -> internally valid
    entries = Blackboard(board).entries()
    board.unlink()
    rebuilt = Blackboard(board)
    for e in entries:
        body = dict(e["body"], title=e["body"]["title"] + "!")
        rebuilt.append(e["kind"], e["agent"], e["ts"], body)
    assert rebuilt.verify() == 3
    rep = anchor.verify(tmp_path, board, af)
    assert not rep["ok"] and any("re-chained" in p for p in rep["problems"])


# -- G4 ----------------------------------------------------------------------


def test_authenticated_broker_refuses_unsigned_forged_and_replayed(tmp_path, code):
    b = _broker(tmp_path, code, keys={"alice": KEY})
    with pytest.raises(BrokerError, match="unsigned"):
        b.post_task("alice", "t", {})
    args = {"title": "t", "spec": {}}
    with pytest.raises(BrokerError, match="bad request signature"):
        b.post_task("alice", "t", {}, auth=sign(b"x" * 32, "alice", "post_task", args))
    with pytest.raises(BrokerError, match="bad request signature"):  # signature for other args
        b.post_task("alice", "u", {}, auth=sign(KEY, "alice", "post_task", args))
    auth = sign(KEY, "alice", "post_task", args)
    tid = b.post_task("alice", "t", {}, auth=auth)
    with pytest.raises(BrokerError, match="replayed"):
        b.post_task("alice", "t", {}, auth=auth)
    with pytest.raises(BrokerError, match="bad request signature"):  # moved to another op
        b.claim("alice", tid, auth=auth)
    b.claim("alice", tid, auth=sign(KEY, "alice", "claim", {"task_id": tid}))
    assert Blackboard(b.board.path).verify_signatures({"alice": KEY}) == []
    assert Blackboard(b.board.path).verify_signatures({"alice": b"y" * 32})
    with pytest.raises(ValueError, match="no key"):
        WriteBroker(tmp_path, {"alice", "bob"}, keys={"alice": KEY})


def test_v19_reader_still_verifies_a_signed_board(tmp_path, code):
    """The on-disk format stays v1.9-readable: the v1.9 verify algorithm
    (hash over every field but ``hash``) accepts entries carrying ``auth``."""
    from iap.agents.blackboard import GENESIS, digest

    b = _broker(tmp_path, code, keys={"alice": KEY})
    b.post_task("alice", "t", {}, auth=sign(KEY, "alice", "post_task", {"title": "t", "spec": {}}))
    prev = GENESIS
    for e in Blackboard(b.board.path).entries():
        assert e["prev"] == prev and e["hash"] == digest(
            {k: v for k, v in e.items() if k != "hash"}
        )
        prev = e["hash"]


def test_no_prereg_results_are_not_gate_eligible():
    from iap.research.runner import EXPLORATORY_REASON
    from iap.research.specs import gate_eligibility

    class Spec:
        configuration: dict = {}

    import iap.research.specs as specs

    orig = specs._configuration_violations
    specs._configuration_violations = lambda cfg: []
    try:
        e = gate_eligibility(Spec(), run_reasons=[EXPLORATORY_REASON])
    finally:
        specs._configuration_violations = orig
    assert not e.eligible and EXPLORATORY_REASON in e.reasons
