"""v1.10 governance fixes G1-G4: look budget + code hash, reserve cap key,
external anchoring, authenticated agent identity."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from conftest import REPO_ROOT
from iap.agents import anchor, signing
from iap.agents.blackboard import Blackboard, digest, request_mac
from iap.agents.broker import BrokerError, WriteBroker, sign
from iap.agents.prereg_gate import PreregistrationError, require
from iap.agents.reserve import ReserveError, ReserveEvaluator
from iap.validation.ledger import ExperimentLedger

SEC = 10**9


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
    assert "iap.alpha.base" in fp["modules"] and set(fp["deps"]) == {"numpy", "pandas", "scipy"}


def _tree(root, files):
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))


def test_closure_follows_helpers_not_unrelated_modules(tmp_path):
    from iap.agents.fingerprint import closure_hash, module_closure

    files = {
        "iap/__init__.py": "",
        "iap/alpha/__init__.py": "",
        "iap/alpha/m.py": "from .h import f\ndef score():\n    from iap.util import g\n    return f()\n",
        "iap/alpha/h.py": "def f():\n    return 1\n",
        "iap/util/__init__.py": "def g():\n    return 2\n",
        "iap/other.py": "X = 1\n",
    }
    _tree(tmp_path, files)
    mods = module_closure(["iap.alpha.m"], tmp_path)
    assert mods == ["iap.alpha.h", "iap.alpha.m", "iap.util"]
    h0 = closure_hash(mods, tmp_path)
    _tree(tmp_path, {"iap/other.py": "X = 2\n"})
    assert closure_hash(module_closure(["iap.alpha.m"], tmp_path), tmp_path) == h0
    _tree(tmp_path, {k: v.replace("\n", "\r\n") for k, v in files.items()})
    assert closure_hash(module_closure(["iap.alpha.m"], tmp_path), tmp_path) == h0
    _tree(tmp_path, {"iap/alpha/h.py": "def f():\n    return 3\n"})
    assert closure_hash(module_closure(["iap.alpha.m"], tmp_path), tmp_path) != h0


def test_gate_refuses_after_a_helper_the_alpha_uses_changes(tmp_path):
    """Real EQ01 on a copy of the source tree: edit a module-level helper in
    iap/alpha/base.py -> refused; edit an unrelated module -> still allowed."""
    from iap.agents.fingerprint import SRC_ROOT, fingerprint

    src = tmp_path / "src"
    shutil.copytree(SRC_ROOT / "iap", src / "iap", ignore=shutil.ignore_patterns("__pycache__"))
    fp = lambda a: fingerprint(a, src)  # noqa: E731
    root = tmp_path / "repo"
    b = WriteBroker(root, {"alice"}, clock=Clock(), fingerprinter=fp)
    b.preregister("alice", "EQ01", "1s", "h", 1)
    unrelated = src / "iap" / "execution" / "__init__.py"
    unrelated.write_text(unrelated.read_text(encoding="utf-8") + "\n# edit\n", encoding="utf-8")
    require(root, "EQ01", "1s", fingerprinter=fp)
    base = src / "iap" / "alpha" / "base.py"
    text = base.read_text(encoding="utf-8")
    assert "\ndef col(" in text  # a module-level helper the alphas call
    base.write_text(
        text.replace("\ndef col(", "\ndef _unused():\n    pass\n\n\ndef col("), encoding="utf-8"
    )
    with pytest.raises(PreregistrationError, match="code_hash changed"):
        require(root, "EQ01", "1s", fingerprinter=fp)


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


PRIV, PUB = signing.generate_keypair()
TASK = {"title": "t", "spec": {}}


def test_ed25519_sign_verify_roundtrip():
    auth = signing.sign(PRIV, "alice", "post_task", TASK)
    assert auth["scheme"] == "ed25519" and signing.public_of(PRIV) == PUB
    rec = {"key_id": "alice", "op": "post_task", "args_digest": digest(TASK), **auth}
    assert signing.verify(rec, "alice", {"alice": PUB}) is None
    assert signing.verify({**rec, "op": "claim"}, "alice", {"alice": PUB}) == "bad signature"


def test_verifier_with_only_the_public_key_cannot_forge(tmp_path, code):
    """The broker holds only PUB.  Everything derivable from it - the public
    key used as a key, an HMAC under it, a random signature - is refused."""
    b = _broker(tmp_path, code, pubkeys={"alice": PUB})
    assert not hasattr(b, "keys") and b.pubkeys == {"alice": PUB}
    with pytest.raises(ValueError):  # a public key is not a private key
        signing.sign(PUB + "00", "alice", "post_task", TASK)
    nonce = "n" * 32
    forgeries = [
        {"scheme": "ed25519", "nonce": nonce, "sig": "00" * 64},
        {
            "scheme": "ed25519",
            "nonce": nonce,
            "sig": signing.sign(PUB, "alice", "post_task", TASK)["sig"],
        },
        {
            "scheme": "hmac-sha256",
            "nonce": nonce,
            "sig": "x",
            "mac": request_mac(bytes.fromhex(PUB), "alice", "post_task", TASK, nonce),
        },
    ]
    for auth in forgeries:
        with pytest.raises(BrokerError, match="signature|scheme"):
            b.post_task("alice", "t", {}, auth=auth)
    assert b.board.entries() == []


def test_authenticated_broker_refuses_unsigned_forged_and_replayed(tmp_path, code):
    b = _broker(tmp_path, code, pubkeys={"alice": PUB})
    with pytest.raises(BrokerError, match="unsigned"):
        b.post_task("alice", "t", {})
    other, _ = signing.generate_keypair()
    with pytest.raises(BrokerError, match="bad request signature"):  # someone else's key
        b.post_task("alice", "t", {}, auth=sign(other, "alice", "post_task", TASK))
    with pytest.raises(BrokerError, match="bad request signature"):  # signature for other args
        b.post_task("alice", "u", {}, auth=sign(PRIV, "alice", "post_task", TASK))
    auth = sign(PRIV, "alice", "post_task", TASK)
    tid = b.post_task("alice", "t", {}, auth=auth)
    with pytest.raises(BrokerError, match="replayed"):
        b.post_task("alice", "t", {}, auth=auth)
    with pytest.raises(BrokerError, match="bad request signature"):  # moved to another op
        b.claim("alice", tid, auth=auth)
    b.claim("alice", tid, auth=sign(PRIV, "alice", "claim", {"task_id": tid}))
    assert Blackboard(b.board.path).verify_signatures({"alice": PUB}) == []
    _, wrong_pub = signing.generate_keypair()
    assert Blackboard(b.board.path).verify_signatures({"alice": wrong_pub})
    with pytest.raises(ValueError, match="no public key"):
        WriteBroker(tmp_path, {"alice", "bob"}, pubkeys={"alice": PUB})


def test_new_hmac_requests_refused_but_old_hmac_entries_verify(tmp_path, code):
    """Mixed board: a legacy hmac-sha256 entry (first v1.10 draft, no
    ``scheme``) followed by ed25519 entries verifies; a new HMAC request is
    refused by the broker."""
    hkey = b"h" * 32
    board = Blackboard(tmp_path / "research" / "agents" / "blackboard.jsonl")
    nonce = "a" * 32
    legacy = {
        "key_id": "alice",
        "op": "post_task",
        "args_digest": digest({"title": "old", "spec": {}}),
        "nonce": nonce,
        "mac": request_mac(hkey, "alice", "post_task", {"title": "old", "spec": {}}, nonce),
    }
    board.append("task", "alice", 1, {"task_id": "x" * 16, "title": "old", "spec": {}}, legacy)
    b = _broker(tmp_path, code, pubkeys={"alice": PUB})
    with pytest.raises(BrokerError, match="scheme"):
        b.post_task("alice", "t", {}, auth={"scheme": "hmac-sha256", "nonce": "b", "sig": "c"})
    b.post_task("alice", "t", {}, auth=sign(PRIV, "alice", "post_task", TASK))
    assert board.verify() == 2
    assert board.verify_signatures({"alice": PUB}, {"alice": hkey}) == []
    assert any("no shared key" in p for p in board.verify_signatures({"alice": PUB}))
    assert board.verify_signatures({"alice": PUB}, {"alice": b"z" * 32})


def test_v19_reader_still_verifies_a_signed_board(tmp_path, code):
    """The on-disk format stays v1.9-readable: the v1.9 verify algorithm
    (hash over every field but ``hash``) accepts entries carrying ``auth``."""
    from iap.agents.blackboard import GENESIS

    b = _broker(tmp_path, code, pubkeys={"alice": PUB})
    b.post_task("alice", "t", {}, auth=sign(PRIV, "alice", "post_task", TASK))
    b.preregister(
        "alice",
        "EQ01",
        "1s",
        "h",
        1,
        auth=sign(
            PRIV,
            "alice",
            "preregister",
            {"alpha_id": "EQ01", "horizon": "1s", "hypothesis": "h", "expected_sign": 1},
        ),
    )
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
