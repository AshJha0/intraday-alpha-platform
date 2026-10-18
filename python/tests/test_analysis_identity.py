"""Analysis identity bound by a signed prereg (v1.18 REL-02a)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from iap.agents import analysis_identity as ai
from iap.agents.prereg_gate import PreregistrationError, require

KW = dict(
    data_versions=["v1"],
    family="B8-v2",
    metrics=["shortfall_bps", "fill_rate"],
    thresholds={"mde_bps": 3.0},
)


def _git(root: Path, *a: str) -> None:
    subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "python" / "src" / "iap").mkdir(parents=True)
    (r / "python" / "src" / "iap" / "lib.py").write_text("X = 1\n")
    (r / "tools").mkdir()
    (r / "tools" / "run.py").write_text("print('analysis')\n")
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    return r


def _bound(r):
    return ai.build_identity(r, scripts=["tools/run.py"], **KW)


def test_matching_identity_passes_and_crlf_is_normalised(repo):
    b = _bound(repo)
    out = ai.verify_identity(repo, b, **KW)
    assert out["legacy"] is False and out["scripts"] == ["tools/run.py"]
    (repo / "tools" / "run.py").write_bytes(b"print('analysis')\r\n")  # checkout line endings
    ai.verify_identity(repo, b, **KW)


@pytest.mark.parametrize(
    "mutate,needle",
    [
        (lambda r: (r / "tools" / "run.py").write_text("print('altered')\n"), "run.py changed"),
        (lambda r: (r / "python/src/iap/lib.py").write_text("X = 2\n"), "code tree differs"),
    ],
)
def test_altered_script_or_library_is_refused(repo, mutate, needle):
    b = _bound(repo)
    mutate(repo)
    with pytest.raises(ai.IdentityError, match=needle):
        ai.verify_identity(repo, b, **KW)


@pytest.mark.parametrize(
    "override,needle",
    [
        ({"data_versions": ["v2"]}, "data versions"),
        ({"family": "other"}, "family"),
        ({"metrics": ["shortfall_bps"]}, "metrics"),
        ({"thresholds": {"mde_bps": 9.0}}, "thresholds"),
    ],
)
def test_changed_declaration_is_refused(repo, override, needle):
    b = _bound(repo)
    with pytest.raises(ai.IdentityError, match=needle):
        ai.verify_identity(repo, b, **{**KW, **override})


def test_unrelated_commit_is_refused_and_descendant_commit_is_accepted(repo):
    b = _bound(repo)
    (repo / "docs.txt").write_text("a prereg document lands later\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "prereg doc")  # HEAD moves, bound commit is an ancestor
    ai.verify_identity(repo, b, **KW)
    b2 = {**b, "commit": "0" * 40}
    with pytest.raises(ai.IdentityError, match="not HEAD or an ancestor"):
        ai.verify_identity(repo, b2, **KW)


def test_missing_block_refused_unless_legacy(repo):
    with pytest.raises(ai.IdentityError, match="binds no analysis_identity"):
        ai.verify_identity(repo, None, **KW)
    assert ai.verify_identity(repo, None, legacy_ok=True, **KW)["legacy"] is True


def test_signer_is_not_the_runner():
    doc = {"signature": {"maintainer": "ashjha0"}}
    with pytest.raises(ai.IdentityError, match="not set"):
        ai.require_independent_runner(doc, {})
    with pytest.raises(ai.IdentityError, match="is the prereg signer"):
        ai.require_independent_runner(doc, {ai.ACTOR_ENV: "ashjha0"})
    with pytest.raises(ai.IdentityError, match="key material"):
        ai.require_independent_runner(doc, {ai.ACTOR_ENV: "runner", "IAP_SIGNING_KEY": "00"})
    assert ai.require_independent_runner(doc, {ai.ACTOR_ENV: "runner-1"}) == "runner-1"


def test_prereg_gate_refuses_a_run_that_does_not_match_the_bound_identity(repo, tmp_path):
    """The prereg-consuming path (prereg_gate.require) enforces the signed block."""
    board = tmp_path / "board.jsonl"
    # write a prereg entry carrying the block through the broker's body shape
    from iap.agents import blackboard as bb

    b = bb.Blackboard(board)
    pid = bb.digest({"alpha": "EQ10", "horizon": "1s"})[:16]
    body = {
        "alpha_id": "EQ10",
        "horizon": "1s",
        "hypothesis": "h",
        "expected_sign": 1,
        "prereg_id": pid,
        "format": 2,
        "analysis_identity": _bound(repo),
    }
    b.append("prereg", "owner", 1, body)
    ok = dict(KW)
    require(repo, "EQ10", "1s", board, agents=set(), identity_actual=ok)
    with pytest.raises(PreregistrationError, match="must declare"):
        require(repo, "EQ10", "1s", board, agents=set())  # declares nothing
    (repo / "tools" / "run.py").write_text("print('changed')\n")
    with pytest.raises(PreregistrationError, match="run.py changed"):
        require(repo, "EQ10", "1s", board, agents=set(), identity_actual=ok)
