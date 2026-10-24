"""research/v118/f3_accept/run_f3_accept.py: the F3 acceptance-study runner (UNSIGNED DRAFT).

Synthetic only.  Refusals run in a throw-away git repository holding copies of the bound files and a test trust
root.  No acceptance study is run here.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from iap.agents import signing

REPO = Path(__file__).resolve().parents[2]
D = REPO / "research" / "v118" / "f3_accept"
TOOL = REPO / "tools" / "prereg_bind_identity.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fa = _load("run_f3_accept", D / "run_f3_accept.py")
pbi = _load("prereg_bind_identity", TOOL)
DECL = fa.load_decl()
PID = DECL["prereg"]


def _git(root: Path, *a: str) -> None:
    subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "python" / "src" / "iap").mkdir(parents=True)
    (r / "python" / "src" / "iap" / "lib.py").write_text("X = 1\n")
    for rel in [*fa.BOUND, *fa.INPUTS, DECL["protocol"]]:
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / rel, r / rel)
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
    return r / "research" / "v118" / "f3_accept" / "declaration.json"


def _ns(r: Path) -> argparse.Namespace:
    return argparse.Namespace(root=str(r), decl=_decl(r), prereg=None, out=r / "out")


def _bind(r: Path, monkeypatch) -> None:
    import iap.agents.fingerprint as fp

    monkeypatch.setattr(fp, "check_registration_env", lambda **kw: True)
    assert fa.main(["--root", str(r), "--decl", str(_decl(r)), "bind"]) == 0


def _sign(r: Path, key: Path) -> None:
    argv = [
        "--root",
        str(r),
        "sign",
        "--prereg",
        PID,
        "--keyfile",
        str(key),
        "--maintainer",
        "owner",
    ]
    assert pbi.main(argv) == 0
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "signed prereg")


def _ok(monkeypatch):
    monkeypatch.setenv("IAP_RUN_ACTOR", "claude-queue-runner")
    monkeypatch.setattr(fa, "available_ram_gb", lambda: 64.0)


def test_declaration_is_an_unsigned_draft_with_the_reviewed_parameters():
    assert "UNSIGNED" in DECL["status"]
    assert DECL["alpha_op"] == 0.02 and DECL["fwer_target"] == 0.025 and DECL["n_per_cell"] == 50000
    assert len(fa.cells(DECL)) == 12 and sum(m for _, _, m in fa.cells(DECL)) == 9
    assert not (REPO / PID).exists(), "the prereg must not exist before the owner signs"


def test_refuses_when_the_prereg_does_not_exist(repo, monkeypatch):
    r, _ = repo
    _ok(monkeypatch)
    with pytest.raises(SystemExit) as e:
        fa.cmd_run(_ns(r))
    assert e.value.code == 2 and not (r / "out").exists()


def test_refuses_an_unsigned_prereg(repo, monkeypatch, capsys):
    r, _ = repo
    _bind(r, monkeypatch)
    _ok(monkeypatch)
    with pytest.raises(SystemExit) as e:
        fa.cmd_run(_ns(r))
    assert e.value.code == 2 and "not signed" in capsys.readouterr().err


def test_refuses_an_identity_mismatch(repo, monkeypatch, capsys):
    r, key = repo
    _bind(r, monkeypatch)
    _sign(r, key)
    p = _decl(r)
    p.write_text(p.read_text().replace('"alpha_op": 0.02,', '"alpha_op": 0.03,'))
    _ok(monkeypatch)
    with pytest.raises(SystemExit) as e:
        fa.cmd_run(_ns(r))
    assert e.value.code == 2 and "mismatch" in capsys.readouterr().err


def test_refuses_the_signer_as_runner(repo, monkeypatch, capsys):
    r, key = repo
    _bind(r, monkeypatch)
    _sign(r, key)
    _ok(monkeypatch)
    monkeypatch.setenv("IAP_RUN_ACTOR", "owner")
    with pytest.raises(SystemExit) as e:
        fa.cmd_run(_ns(r))
    assert e.value.code == 2 and "is the prereg signer" in capsys.readouterr().err


def test_refuses_a_signing_key_in_the_environment(repo, monkeypatch):
    r, key = repo
    _bind(r, monkeypatch)
    _sign(r, key)
    _ok(monkeypatch)
    monkeypatch.setenv("IAP_SIGNING_KEY", "x")
    with pytest.raises(SystemExit) as e:
        fa.cmd_run(_ns(r))
    assert e.value.code == 2


def test_refuses_when_results_already_exist(repo, monkeypatch, capsys):
    r, key = repo
    _bind(r, monkeypatch)
    _sign(r, key)
    _ok(monkeypatch)
    (r / "out").mkdir()
    (r / "out" / "DECISION.json").write_text("{}")
    with pytest.raises(SystemExit) as e:
        fa.cmd_run(_ns(r))
    assert e.value.code == 2 and "one run only" in capsys.readouterr().err


def test_decide_applies_the_clopper_pearson_rule_to_all_27_mandatory_evaluations():
    km = fa.core.k_max(50000, 0.025)
    assert km == 1192 and fa.core.cp_upper(km, 50000) <= 0.025 < fa.core.cp_upper(km + 1, 50000)

    def res(count):
        return {
            (r, g): {"nan_p": 0, "rejections": {"K3": count, "K6": count, "K16": count}}
            for r, g, _ in fa.cells(DECL)
        }

    ok = fa.decide(DECL, res(1000))
    assert (
        ok["overall_pass_all_27_mandatory"]
        and len(ok["mandatory_evaluations"]) == 27
        and len(ok["sensitivity_evaluations"]) == 9
    )
    bad = res(1000)
    bad[("b1", 7)]["rejections"]["K6"] = km + 1
    assert not fa.decide(DECL, bad)["overall_pass_all_27_mandatory"]
    sens = res(1000)
    sens[("a1", 5)]["rejections"]["K3"] = 5000  # sensitivity cell: reported, not a criterion
    assert fa.decide(DECL, sens)["overall_pass_all_27_mandatory"]


def test_joint_bound_file_matches_its_script():
    jb = (D / "JOINT_BOUND.md").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert jb == fa.joint_bound.render(fa.joint_bound.table(DECL))
