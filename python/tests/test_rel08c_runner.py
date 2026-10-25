"""research/auction/rel08c/run_rel08c.py: the REL-08c runner's refusals (synthetic repo only).

No market data is read: every test builds a throw-away git repository holding a
copy of the runner, the declaration and a test trust root; the extraction is a stub
that records whether it was reached.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from _dry_run_fixture import attach_planted_dry_run
from iap.agents import signing

REPO = Path(__file__).resolve().parents[2]
RUNNER = REPO / "research" / "auction" / "rel08c" / "run_rel08c.py"
TOOL = REPO / "tools" / "prereg_bind_identity.py"
SESSION = "2019-01-30"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rr = _load("run_rel08c", RUNNER)
pbi = _load("prereg_bind_identity", TOOL)


def _git(root: Path, *a: str) -> None:
    subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True)


def _registry(state: str) -> str:
    return json.dumps({"sessions": {SESSION: [{"state": state, "symbols": "*"}]}})


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "python" / "src" / "iap").mkdir(parents=True)
    (r / "python" / "src" / "iap" / "lib.py").write_text("X = 1\n")
    d = r / "research" / "auction" / "rel08c"
    d.mkdir(parents=True)
    shutil.copy(RUNNER, d / "run_rel08c.py")
    shutil.copy(RUNNER.parent / "predeclaration.json", d / "predeclaration.json")
    (d / "PREDECLARATION.md").write_text("# synthetic declaration\n")
    (r / "research" / "breadth").mkdir(parents=True)
    (r / "research" / "breadth" / "universe.json").write_text(
        json.dumps({"universe": [{"symbol": "AAA"}]})
    )
    (r / "research" / "holdout_registry.json").write_text(_registry("in_sample"))
    priv, pub = signing.generate_keypair()
    (r / "research" / "agents").mkdir(parents=True)
    (r / "research" / "agents" / "trust_roots.json").write_text(
        json.dumps({"maintainers": {"owner": pub}})
    )
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "core.autocrlf", "false")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    key = tmp_path / "maintainer.key"
    key.write_text(json.dumps({"owner": priv}))
    return r, key


def _decl(r: Path) -> Path:
    return r / "research" / "auction" / "rel08c" / "predeclaration.json"


def _bind(r: Path) -> None:
    assert rr.main(["--root", str(r), "--decl", str(_decl(r)), "bind"]) == 0


def _sign(r: Path, key: Path) -> None:
    attach_planted_dry_run(r, "research/preregs/rel08c_prereg.json")  # R4: sign refuses without it
    argv = ["--root", str(r), "sign", "--prereg", "research/preregs/rel08c_prereg.json"]
    assert pbi.main([*argv, "--keyfile", str(key), "--maintainer", "owner"]) == 0
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "signed prereg")


def _ns(r: Path) -> argparse.Namespace:
    return argparse.Namespace(
        root=str(r), decl=_decl(r), prereg=None, data_root=r / "data",
        out=r / "research" / "auction" / "rel08c",
    )  # fmt: skip


class Reached(Exception):
    pass


def _stub(calls: list[dict]):
    def extract(**kw):
        calls.append(kw)
        raise Reached

    return extract


def _run(r: Path, calls: list[dict], find=lambda: []):
    return rr.cmd_run(_ns(r), extract=_stub(calls), find=find)


def test_committed_declaration_has_no_open_parameter():
    decl = json.loads((RUNNER.parent / "predeclaration.json").read_text("utf-8"))
    assert all(v is not None for v in decl["study"].values())
    assert decl["input"]["sha256"].startswith("8c97b5b1") and len(decl["input"]["sha256"]) == 64
    text = (RUNNER.parent / "PREDECLARATION.md").read_text("utf-8")
    assert "[ ]" not in text and "screen only" in text and "underpowered" in text
    cfg = rr.study_config(decl)
    assert cfg.exit_tod_ns == (15 * 3600 + 54 * 60 + 55) * 10**9


def test_refuses_when_the_prereg_does_not_exist(repo, monkeypatch):
    r, _ = repo
    monkeypatch.setenv("IAP_RUN_ACTOR", "rel08c-runner")
    calls: list[dict] = []
    with pytest.raises(SystemExit) as e:
        _run(r, calls)
    assert e.value.code == 2 and calls == []


def test_refuses_an_unsigned_prereg(repo, monkeypatch, capsys):
    r, _ = repo
    _bind(r)
    monkeypatch.setenv("IAP_RUN_ACTOR", "rel08c-runner")
    calls: list[dict] = []
    with pytest.raises(SystemExit) as e:
        _run(r, calls)
    assert e.value.code == 2 and calls == []
    assert "not signed" in capsys.readouterr().err


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: (r / "python/src/iap/lib.py").write_text("X = 2\n"),
        lambda r: _decl(r).write_text(
            _decl(r).read_text().replace('"threshold": 0.2', '"threshold": 0.1')
        ),
        lambda r: (r / "research/breadth/universe.json").write_text('{"universe": []}'),
    ],
)
def test_refuses_an_identity_mismatch(repo, monkeypatch, capsys, mutate):
    r, key = repo
    _bind(r)
    _sign(r, key)
    mutate(r)
    monkeypatch.setenv("IAP_RUN_ACTOR", "rel08c-runner")
    calls: list[dict] = []
    with pytest.raises(SystemExit) as e:
        _run(r, calls)
    assert e.value.code == 2 and calls == []
    assert "mismatch" in capsys.readouterr().err


def test_refuses_the_signer_as_runner(repo, monkeypatch, capsys):
    r, key = repo
    _bind(r)
    _sign(r, key)
    monkeypatch.setenv("IAP_RUN_ACTOR", "owner")
    calls: list[dict] = []
    with pytest.raises(SystemExit) as e:
        _run(r, calls)
    assert e.value.code == 2 and calls == []
    assert "is the prereg signer" in capsys.readouterr().err


@pytest.mark.parametrize("env", [{}, {"IAP_RUN_ACTOR": "rel08c-runner", "IAP_KEYFILE": "k"}])
def test_refuses_an_undeclared_runner_or_one_holding_a_key(repo, monkeypatch, env):
    r, key = repo
    _bind(r)
    _sign(r, key)
    monkeypatch.delenv("IAP_RUN_ACTOR", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    calls: list[dict] = []
    with pytest.raises(SystemExit) as e:
        _run(r, calls)
    assert e.value.code == 2 and calls == []


def test_refuses_a_holdout_session_and_a_running_ingest(repo, monkeypatch, capsys):
    r, key = repo
    _bind(r)
    _sign(r, key)
    monkeypatch.setenv("IAP_RUN_ACTOR", "rel08c-runner")
    calls: list[dict] = []
    with pytest.raises(SystemExit) as e:
        _run(r, calls, find=lambda: [4242])
    assert e.value.code == 2 and calls == [] and "ingest is running" in capsys.readouterr().err
    (r / "research" / "holdout_registry.json").write_text(_registry("holdout"))
    with pytest.raises(SystemExit) as e:
        _run(r, calls)
    assert e.value.code == 2 and calls == [] and "holdout registry" in capsys.readouterr().err


def test_signed_run_reaches_extraction_with_the_declared_input(repo, monkeypatch):
    r, key = repo
    _bind(r)
    _sign(r, key)
    monkeypatch.setenv("IAP_RUN_ACTOR", "rel08c-runner")
    calls: list[dict] = []
    with pytest.raises(Reached):
        _run(r, calls)
    (kw,) = calls
    decl = json.loads(_decl(r).read_text())
    assert kw["expected_sha256"] == decl["input"]["sha256"]
    assert Path(kw["itch_path"]).as_posix().endswith("data/vendor/01302019.NASDAQ_ITCH50.gz")
    assert kw["symbols"] == ["AAA"] and kw["date"] == SESSION


def test_extractor_refuses_a_sha_mismatch_before_decoding(tmp_path):
    from iap.auction.rel08c_extract import extract_rel08c

    f = tmp_path / "x.itch"
    f.write_bytes(b"\x00\x01S")
    with pytest.raises(ValueError, match="sha256"):
        extract_rel08c(f, ["AAA"], tmp_path / "o", expected_sha256="0" * 64)
    assert not (tmp_path / "o").exists()
