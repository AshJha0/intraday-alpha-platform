"""tools/prereg_bind_identity.py: write and sign a prereg's analysis_identity (REL-02a)."""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
from iap.agents import analysis_identity as ai
from iap.agents import signing

TOOL = Path(__file__).resolve().parents[2] / "tools" / "prereg_bind_identity.py"
spec = importlib.util.spec_from_file_location("prereg_bind_identity", TOOL)
assert spec and spec.loader
bind = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bind)

GENERIC = [
    "--scripts",
    "tools/run.py",
    "--data-version",
    "v1",
    "--family",
    "F",
    "--metric",
    "m1",
    "m2",
    "--thresholds",
    '{"alpha": 0.05}',
]


def _git(root: Path, *a: str) -> None:
    subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "python" / "src" / "iap").mkdir(parents=True)
    (r / "python" / "src" / "iap" / "lib.py").write_text("X = 1\n")
    (r / "tools").mkdir()
    (r / "tools" / "run.py").write_text("print('analysis')\n")
    (r / "PROTOCOL.md").write_text("# protocol\n")
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "core.autocrlf", "false")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    return r


def _bind(r: Path, *extra: str) -> int:
    return bind.main(
        [
            "--root",
            str(r),
            "bind",
            "--protocol",
            "PROTOCOL.md",
            "--out",
            "preregs/p.json",
            *GENERIC,
            *extra,
        ]
    )


def test_bind_writes_an_unsigned_identity_that_verifies(repo):
    assert _bind(repo) == 0
    doc = json.loads((repo / "preregs/p.json").read_text())
    assert doc["signed"] is False and "signature" not in doc
    b = doc["analysis_identity"]
    assert b["commit"] == ai.head_commit(repo)
    assert set(b["scripts"]) == {"tools/run.py"}
    ai.verify_identity(
        repo, b, data_versions=["v1"], family="F", metrics=["m1", "m2"], thresholds={"alpha": 0.05}
    )
    # the prereg file itself may be uncommitted when binding again (still unsigned)
    assert _bind(repo) == 0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: (r / "tools" / "run.py").write_text("print('changed')\n"),  # modified script
        lambda r: (r / "notes.txt").write_text("x\n"),  # untracked file
        lambda r: (
            _ := (r / "PROTOCOL.md").write_text("# edited\n"),
            _git(r, "add", "PROTOCOL.md"),
        ),
    ],
)
def test_bind_refuses_a_dirty_tree(repo, mutate):
    mutate(repo)
    with pytest.raises(SystemExit) as e:
        _bind(repo)
    assert e.value.code == 2
    assert not (repo / "preregs/p.json").exists()


def test_bind_refuses_scripts_not_at_head(repo):
    (repo / "tools" / "new.py").write_text("x = 1\n")
    _git(repo, "add", "tools/new.py")
    _git(repo, "commit", "-qm", "add")
    _git(repo, "rm", "-q", "--cached", "tools/new.py")
    _git(repo, "commit", "-qm", "untrack")
    (repo / ".git" / "info" / "exclude").write_text("tools/new.py\n")
    with pytest.raises(SystemExit):
        bind.main(
            [
                "--root",
                str(repo),
                "bind",
                "--protocol",
                "PROTOCOL.md",
                "--out",
                "preregs/p.json",
                "--scripts",
                "tools/new.py",
                *GENERIC[2:],
            ]
        )


def _trust(repo: Path) -> tuple[Path, str]:
    priv, pub = signing.generate_keypair()
    (repo / "research" / "agents").mkdir(parents=True)
    (repo / "research" / "agents" / "trust_roots.json").write_text(
        json.dumps({"maintainers": {"owner": pub}})
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "trust root")
    key = repo.parent / "maintainer.key"
    key.write_text(json.dumps({"owner": priv}))
    return key, pub


def test_sign_then_verify_and_refuse_resign(repo):
    key, pub = _trust(repo)
    assert _bind(repo) == 0
    argv = [
        "--root",
        str(repo),
        "sign",
        "--prereg",
        "preregs/p.json",
        "--keyfile",
        str(key),
        "--maintainer",
        "owner",
    ]
    assert bind.main(argv) == 0
    doc = json.loads((repo / "preregs/p.json").read_text())
    assert ai.verify_signed_document(repo, doc) == pub
    with pytest.raises(SystemExit):
        bind.main(argv)  # already signed
    with pytest.raises(SystemExit):
        _bind(repo)  # re-binding a signed prereg would void it


def test_sign_refuses_when_a_script_changed_after_binding(repo):
    key, _ = _trust(repo)
    assert _bind(repo) == 0
    (repo / "tools" / "run.py").write_text("print('changed')\n")
    _git(repo, "commit", "-qam", "change script")
    with pytest.raises(SystemExit) as e:
        bind.main(
            [
                "--root",
                str(repo),
                "sign",
                "--prereg",
                "preregs/p.json",
                "--keyfile",
                str(key),
                "--maintainer",
                "owner",
            ]
        )
    assert e.value.code == 2
    assert json.loads((repo / "preregs/p.json").read_text())["signed"] is False


def test_study_config_supplies_the_rel06_identity():
    root = TOOL.parents[1]
    a = argparse.Namespace(study_config=root / "research/v118/known_null_config.json")
    from iap.research.known_null import identity_args

    cfg = json.loads((root / "research/v118/known_null_config.json").read_text(encoding="utf-8"))
    assert bind.identity_inputs(a, root) == identity_args(cfg)
