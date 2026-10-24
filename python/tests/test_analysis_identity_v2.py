"""Analysis identity v2 (review finding 1): every input that can change a result
is bound, v1 preregs stay verifiable, and no supported runner skips the check.

Adversarial: each test changes ONE input after binding and expects a refusal
that names it.  No market data is read.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest
from iap.agents import analysis_identity as ai

REPO = Path(__file__).resolve().parents[2]
KW = dict(data_versions=["v1"], family="F", metrics=["m"], thresholds={"t": 1.0})


def _git(root: Path, *a: str) -> None:
    subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True)


def _w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    _w(r / "python/src/iap/lib.py", "X = 1\n")
    _w(r / "python/src/iap/features/spec_data.json", '{"f": 1}\n')
    _w(r / "python/src/iap/labels/labels.py", "H = 10\n")
    _w(r / "configs/reference/fee_tiers.json", '{"taker": 0.3}\n')
    _w(r / "configs/venues/venues.json", '{"XNAS": {}}\n')
    _w(r / "data/reference/feature_registry.json", '{"ofi": 1}\n')
    _w(r / "research/study/calibration.json", '{"k": 2}\n')
    _w(r / "python/requirements-ci.txt", "pytest==8.0.0 \\\n    --hash=sha256:00\n")
    _w(r / "tools/run.py", "print('analysis')\n")
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    return r


def _bound(r: Path) -> dict:
    return ai.build_identity(
        r, scripts=["tools/run.py"], inputs=["research/study/calibration.json"], **KW
    )


def test_v2_block_binds_every_input_class(repo):
    b = _bound(repo)
    assert b["identity_version"] == 2
    for key in (
        "code_tree_sha256",
        "configs_tree_sha256",
        "reference_tree_sha256",
        "deps_lock_sha256",
        "environment",
        "interpreter",
    ):
        assert b[key], key
    assert list(b["inputs"]) == ["research/study/calibration.json"]
    assert "pytest" in b["environment"]
    out = ai.verify_identity(repo, b, **KW)
    assert out["identity_version"] == 2


@pytest.mark.parametrize(
    "rel,text,needle",
    [
        ("tools/run.py", "print('x')\n", "script tools/run.py changed"),
        ("python/src/iap/lib.py", "X = 2\n", "code tree differs"),
        ("python/src/iap/features/spec_data.json", '{"f": 2}\n', "code tree differs"),
        ("python/src/iap/labels/labels.py", "H = 20\n", "code tree differs"),
        ("python/src/iap/new_helper.py", "Y = 1\n", "code tree differs"),
        ("configs/reference/fee_tiers.json", '{"taker": 0.0}\n', "configs/ tree"),
        ("configs/venues/venues.json", '{"IEXG": {}}\n', "configs/ tree"),
        ("configs/extra/new.json", "{}\n", "configs/ tree"),
        ("data/reference/feature_registry.json", '{"ofi": 2}\n', "data/reference/ tree"),
        ("research/study/calibration.json", '{"k": 3}\n', "input research/study/calibration"),
        ("python/requirements-ci.txt", "pytest==7.0.0\n", "dependency lock"),
    ],
)
def test_changed_input_cannot_reuse_the_registration(repo, rel, text, needle):
    b = _bound(repo)
    _w(repo / rel, text)
    with pytest.raises(ai.IdentityError, match=re.escape(needle)):
        ai.verify_identity(repo, b, **KW)


def test_crlf_checkout_is_not_a_change(repo):
    b = _bound(repo)
    p = repo / "configs/venues/venues.json"
    p.write_bytes(p.read_bytes().replace(b"\n", b"\r\n"))
    ai.verify_identity(repo, b, **KW)


def test_interpreter_and_installed_packages_are_bound(repo, monkeypatch):
    b = _bound(repo)
    monkeypatch.setattr(ai, "interpreter", lambda: "cpython-3.99.0")
    with pytest.raises(ai.IdentityError, match="interpreter differs"):
        ai.verify_identity(repo, b, **KW)
    monkeypatch.undo()
    drifted = {**b["environment"], "pytest": "0.0.1"}
    with pytest.raises(ai.IdentityError, match="installed packages differ"):
        ai.verify_identity(repo, {**b, "environment": drifted}, **KW)


def test_dataset_change_is_a_new_version_and_tampering_is_refused(tmp_path):
    from iap.research.holdout_registry import content_hash

    d = tmp_path / "ds"
    _w(d / "normalized/a.iap1", "events-1")
    import hashlib

    sha = hashlib.sha256(b"events-1").hexdigest()
    man = {"kind": "real", "dataset_version": content_hash({"a.iap1": sha}),
           "normalized": {"files": {"a.iap1": sha}}}  # fmt: skip
    _w(d / "dataset.json", json.dumps(man))
    v = ai.dataset_version_of(d)
    (d / "normalized/a.iap1").write_text("events-2")  # same manifest, changed bytes
    with pytest.raises(ai.IdentityError, match="content differs"):
        ai.dataset_version_of(d)
    assert v == man["dataset_version"]


def test_unknown_identity_version_is_refused(repo):
    with pytest.raises(ai.IdentityError, match="unknown analysis identity version"):
        ai.verify_identity(repo, {**_bound(repo), "identity_version": 9}, **KW)


def test_v1_block_still_verifies_under_v1_rules(repo):
    """A v1 block (no identity_version) is checked exactly as when it was signed:
    configs / lock / interpreter are not part of it, so changing them is not a
    v1 mismatch, while a script or .py change still is."""
    v1 = {
        "scripts": ai.script_fingerprints(repo, ["tools/run.py"]),
        "code_tree_sha256": ai.code_tree_sha256(repo),
        "commit": ai.head_commit(repo),
        **{k: v for k, v in KW.items()},
    }
    v1["data_versions"] = sorted(v1["data_versions"])
    v1["metrics"] = sorted(v1["metrics"])
    _w(repo / "configs/venues/venues.json", "{}\n")
    assert ai.verify_identity(repo, v1, **KW)["identity_version"] == 1
    _w(repo / "python/src/iap/lib.py", "X = 3\n")
    with pytest.raises(ai.IdentityError, match="code tree differs"):
        ai.verify_identity(repo, v1, **KW)


SIGNED_V1 = [
    "research/preregs/b8v2_prereg.json",
    "research/v118/null_protocol_prereg.json",
]


@pytest.mark.parametrize("rel", SIGNED_V1)
def test_signed_v1_preregs_on_main_keep_verifying(rel):
    doc = json.loads((REPO / rel).read_text(encoding="utf-8"))
    ai.verify_signed_document(REPO, doc)  # signature untouched by v2
    assert ai.identity_version(doc["analysis_identity"]) == 1


def test_rel08_rule_resigned_under_identity_v2():
    doc = json.loads((REPO / "research/v118/rel08_rule_signed.json").read_text(encoding="utf-8"))
    ai.verify_signed_document(REPO, doc)
    assert ai.identity_version(doc["analysis_identity"]) == 2


# --------------------------------------------------------------- runners --
def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _force_mismatch(monkeypatch):
    monkeypatch.setattr(ai, "environment_mismatches", lambda root, bound: ["FORCED mismatch"])
    monkeypatch.setenv(ai.ACTOR_ENV, "test-runner")
    for k in ai.SIGNING_ENV:
        monkeypatch.delenv(k, raising=False)


def _doc(rel: str) -> dict:
    return json.loads((REPO / rel).read_text(encoding="utf-8"))


def test_r7_confirm_refuses_on_mismatch(monkeypatch):
    r7 = _load("r7_confirm_t", REPO / "tools/r7_confirm.py")
    _force_mismatch(monkeypatch)
    doc = {**_doc(SIGNED_V1[0]), "signature": {"maintainer": "owner"}}
    with pytest.raises(r7.CheckError, match="FORCED"):
        r7.verify_analysis_identity(REPO, doc)


def test_rel06_known_null_refuses_on_mismatch(monkeypatch):
    kn = _load("rel06_t", REPO / "tools/rel06_known_null.py")
    _force_mismatch(monkeypatch)
    cfg = json.loads((REPO / "research/v118/known_null_config.json").read_text(encoding="utf-8"))
    with pytest.raises(ai.IdentityError, match="FORCED"):
        kn.verify_prereg(REPO / SIGNED_V1[1], cfg)


def test_b8v2_runner_refuses_on_mismatch(monkeypatch, capsys):
    rb = _load("run_b8v2_t", REPO / "research/execution/b8v2/run_b8v2.py")
    _force_mismatch(monkeypatch)
    with pytest.raises(rb.Refused):
        rb.verify(REPO / SIGNED_V1[0], rb.load_decl(), root=REPO, env={ai.ACTOR_ENV: "t"})
    assert "FORCED" in capsys.readouterr().err


def test_rel05_runner_refuses_on_mismatch(monkeypatch, capsys):
    rr = _load("run_rel05_t", REPO / "research/execution/rel05/run_rel05.py")
    _force_mismatch(monkeypatch)
    with pytest.raises(rr.Refused):
        rr.verify(REPO / SIGNED_V1[0], rr.load_decl(), root=REPO, env={ai.ACTOR_ENV: "t"})
    assert "FORCED" in capsys.readouterr().err


def test_rel08_screen_refuses_on_mismatch(monkeypatch):
    from iap.research import rel08_rule

    _force_mismatch(monkeypatch)
    rule = rel08_rule.load_signed_rule(REPO / "research/v118/rel08_rule_signed.json", REPO)
    want = str(rule.doc["dataset"]["dataset_version"])
    with pytest.raises(rel08_rule.RuleRefused, match="FORCED"):
        rel08_rule.check_identity(rule, REPO, want)


def test_holdout_guard_refuses_a_prereg_whose_identity_does_not_match(monkeypatch, tmp_path):
    """The chokepoint: an entry point that skips its own identity check still
    cannot read a restricted scope under an identity-bound prereg."""
    from iap.research import holdout_registry as hr

    monkeypatch.setattr(ai, "environment_mismatches", lambda root, bound: ["FORCED mismatch"])
    with pytest.raises(hr.HoldoutRefused, match="FORCED"):
        hr.check_prereg_identity(SIGNED_V1[0], "anything", REPO)
    monkeypatch.setattr(ai, "environment_mismatches", lambda root, bound: [])
    with pytest.raises(hr.HoldoutRefused, match="not bound by the prereg"):
        hr.check_prereg_identity(SIGNED_V1[0], "not-a-bound-version", REPO)
    bound = _doc(SIGNED_V1[0])["analysis_identity"]["data_versions"][0]
    hr.check_prereg_identity(SIGNED_V1[0], bound, REPO)  # matching: allowed
    hr.check_prereg_identity("research/r7/r7_confirm_prereg.json", "x", REPO)  # legacy: no block


# The supported confirmatory runners and the identity check each one calls.
# rel08c (auction/rel08c_extract.py) and REL-09 / REL-10 (research/stale_quote.py,
# research/volume_clock_ofi.py) have no confirmatory runner on main yet: no signed
# prereg exists for them.  When one is added it consumes a signed document, so
# test_no_prereg_consumer_skips_the_identity_check below forces it into this list.
RUNNERS = {
    "tools/r7_confirm.py": "verify_identity",
    "tools/rel06_known_null.py": "verify_identity",
    "python/src/iap/research/known_null_v3.py": "verify_identity",
    "research/execution/b8v2/run_b8v2.py": "verify_identity",
    "research/execution/rel05/run_rel05.py": "verify_identity",
    "research/v118/rel04_verify/run_rel04_verify.py": "verify_identity",
    "research/v118/f3_accept/run_f3_accept.py": "verify_identity",
    "research/v118/f3_power/run_f3_power.py": "verify_identity",
    "python/src/iap/research/rel08_screen.py": "check_identity",
    "python/src/iap/research/rel08_rule.py": "verify_identity",
    "python/src/iap/agents/prereg_gate.py": "verify_identity",
    "python/src/iap/research/holdout_registry.py": "environment_mismatches",
    "research/auction/rel08c/run_rel08c.py": "verify_identity",
    "research/v118/rel09/run_rel09.py": "verify_identity",
    "research/v118/rel10/run_rel10.py": "verify_identity",
    "python/src/iap/auction/rel08c_study.py": "environment_mismatches",
}
# Consume a signed document but run no analysis: binders / signers / the
# identity module itself / tools that only verify signatures.
NOT_RUNNERS = {
    "python/src/iap/agents/analysis_identity.py",
    "tools/prereg_bind_identity.py",
    "tools/rel08_rule_sign.py",
    "tools/verify_signatures.py",
    # F3 acceptance: the pre-execution verification script (verifies the signed prereg, runs no analysis)
    "research/v118/f3_accept/execution_preflight.py",
    # F3 power study: the freeze-consistency check reads the bound hashes of the signed acceptance prereg (runs no analysis)
    "research/v118/f3_power/check_freeze.py",
}


def _prereg_consumers() -> set[str]:
    pat = re.compile(r"verify_signed_document|load_signed_rule|analysis_identity|prereg_is_signed")
    out = set()
    for base in ("tools", "research", "python/src/iap"):
        for p in (REPO / base).rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            if pat.search(p.read_text(encoding="utf-8", errors="replace")):
                out.add(p.relative_to(REPO).as_posix())
    return out


def test_no_prereg_consumer_skips_the_identity_check():
    """Every file that consumes a signed prereg / rule is a registered runner
    whose identity call is present, or an explicitly listed non-runner."""
    unknown = sorted(_prereg_consumers() - set(RUNNERS) - NOT_RUNNERS)
    # files that only mention the block name in docs / pass it through
    passthrough = {
        "python/src/iap/agents/broker.py",  # writes the block into a prereg body
        "python/src/iap/research/__main__.py",  # declares identity_actual -> prereg_gate
        "python/src/iap/research/known_null.py",  # identity_args helper
        "python/src/iap/research/rel08_screen.py",
    }
    assert sorted(set(unknown) - passthrough) == [], (
        "a new file consumes signed preregs: add it to RUNNERS with its identity check"
    )
    for rel, call in RUNNERS.items():
        assert call in (REPO / rel).read_text(encoding="utf-8"), f"{rel} lost its {call}"
