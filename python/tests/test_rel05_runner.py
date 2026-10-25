"""research/execution/rel05/run_rel05.py: the REL-05 real-tape runner (synthetic repo, stub scorer).

No market data is read: refusals are exercised in a throw-away git repository holding a copy
of the runner and the declaration, with a test trust root; the scorer is replaced by a stub.
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from _dry_run_fixture import attach_planted_dry_run
from iap.agents import signing

REPO = Path(__file__).resolve().parents[2]
RUNNER = REPO / "research" / "execution" / "rel05" / "run_rel05.py"
TOOL = REPO / "tools" / "prereg_bind_identity.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rr = _load("run_rel05", RUNNER)
pbi = _load("prereg_bind_identity", TOOL)
DECL = rr.load_decl()


@pytest.fixture(autouse=True)
def _access_log(tmp_path, monkeypatch):
    monkeypatch.setenv("IAP_HOLDOUT_ACCESS_LOG", str(tmp_path / "holdout_access.jsonl"))


def _git(root: Path, *a: str) -> None:
    subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "python" / "src" / "iap").mkdir(parents=True)
    (r / "python" / "src" / "iap" / "lib.py").write_text("X = 1\n")
    d = r / "research" / "execution" / "rel05"
    d.mkdir(parents=True)
    shutil.copy(RUNNER, d / "run_rel05.py")
    shutil.copy(RUNNER.with_name("predeclaration.json"), d / "predeclaration.json")
    (d / "PREDECLARATION.md").write_text("# synthetic\n")
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
    return r / "research" / "execution" / "rel05" / "predeclaration.json"


def _ns(r: Path) -> argparse.Namespace:
    return argparse.Namespace(
        root=str(r), decl=_decl(r), prereg=None, data_root=r / "data", out=r / "out"
    )


def _bind(r: Path) -> None:
    assert rr.main(["--root", str(r), "--decl", str(_decl(r)), "bind"]) == 0


def _sign(r: Path, key: Path) -> None:
    attach_planted_dry_run(r, "research/preregs/rel05_prereg.json")  # R4: sign refuses without it
    argv = ["--root", str(r), "sign", "--prereg", "research/preregs/rel05_prereg.json"]
    assert pbi.main([*argv, "--keyfile", str(key), "--maintainer", "owner"]) == 0
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "signed prereg")


def _stub(calls: list[str]):
    def run_one(decl, data_root, sym):
        calls.append(sym)
        day = {"skill": 0.05, "brier": 0.1, "brier_naive": 0.105}
        return {
            "overall": day,
            "per_day": {d: dict(day) for d in decl["split"]["eval_days"]},
            "reliability": [{"n": 500, "observed_rate": 0.31, "mean_predicted": 0.30}],
            "n_eval": 1000,
            "n_fit": 2000,
        }

    return run_one


def _ok_env(monkeypatch):
    monkeypatch.setenv("IAP_RUN_ACTOR", "claude-queue-runner")
    monkeypatch.setattr(rr, "check_datasets", lambda decl, data_root, prereg_id: {})


# ----------------------------------------------------------------- the split --
def test_declared_split_is_valid_against_the_real_registry():
    rr.check_split(DECL)


def test_declared_split_touches_only_in_sample_sessions():
    reg = json.loads((REPO / "research/holdout_registry.json").read_text("utf-8"))
    for day in DECL["split"]["fit_days"] + DECL["split"]["eval_days"]:
        assert [r["state"] for r in reg["sessions"][day]] == ["in_sample"]


@pytest.mark.parametrize(
    "mutate, msg",
    [
        (lambda d: d["split"]["eval_days"].append("2019-01-30"), "both the fit and the eval"),
        (lambda d: d["split"]["eval_days"].__setitem__(0, "2019-05-01"), "not after"),
        (lambda d: d["split"].__setitem__("eval_days", []), "at least one"),
        (lambda d: d["split"]["fit_days"].append("2019-01-30"), "listed twice"),
        (lambda d: d["datasets"].pop("2020-01-30"), "exactly the fit and eval"),
    ],
)
def test_split_refusals(mutate, msg, capsys):
    d = copy.deepcopy(DECL)
    mutate(d)
    with pytest.raises(SystemExit) as e:
        rr.check_split(d)
    assert e.value.code == 2 and msg in capsys.readouterr().err


@pytest.mark.parametrize("day", ["2025-12-10", "2026-05-15", "2025-11-28", "2031-01-02"])
def test_split_refuses_spent_unread_or_unknown_sessions(day, capsys):
    d = copy.deepcopy(DECL)
    d["split"]["eval_days"][-1] = day
    d["datasets"][day] = d["datasets"].pop("2020-01-30")
    with pytest.raises(SystemExit) as e:
        rr.check_split(d)
    assert e.value.code == 2 and "holdout registry" in capsys.readouterr().err


# --------------------------------------------------------------- the guard --
def test_unregistered_dataset_dir_is_refused_by_the_guard(tmp_path, capsys):
    (tmp_path / "ds3_20190130").mkdir()
    with pytest.raises(SystemExit) as e:
        rr.check_datasets(DECL, tmp_path, "research/preregs/rel05_prereg.json")
    assert e.value.code and "dataset.json" in (capsys.readouterr().err + str(e.value.code))


def test_holdout_dataset_dir_is_refused_by_the_guard(tmp_path):
    from iap.research.holdout_registry import guard_or_exit

    d = tmp_path / "x"
    d.mkdir()
    (d / "dataset.json").write_text(
        json.dumps(
            {
                "kind": "real",
                "dataset_version": "v",
                "sessions": {"2025-12-10": {}},
                "normalized": {},
                "universe": [{"symbol": "AAPL"}],
            }
        )
    )
    with pytest.raises(SystemExit) as e:
        guard_or_exit(d, "rel05_tape", "research/preregs/rel05_prereg.json")
    assert e.value.code


def test_ram_floor():
    with pytest.raises(SystemExit):
        rr.check_ram(DECL, lambda: 1.0)
    assert rr.check_ram(DECL, lambda: 32.0) == 32.0


# ------------------------------------------------------------ run refusals --
def test_refuses_when_the_prereg_does_not_exist(repo, monkeypatch):
    r, _ = repo
    monkeypatch.setenv("IAP_RUN_ACTOR", "claude-queue-runner")
    calls: list[str] = []
    with pytest.raises(SystemExit) as e:
        rr.cmd_run(_ns(r), run_one=_stub(calls))
    assert e.value.code == 2 and calls == []


def test_refuses_an_unsigned_prereg(repo, monkeypatch, capsys):
    r, _ = repo
    _bind(r)
    monkeypatch.setenv("IAP_RUN_ACTOR", "claude-queue-runner")
    calls: list[str] = []
    with pytest.raises(SystemExit) as e:
        rr.cmd_run(_ns(r), run_one=_stub(calls))
    assert e.value.code == 2 and calls == []
    assert "not signed" in capsys.readouterr().err


def test_refuses_an_identity_mismatch(repo, monkeypatch, capsys):
    r, key = repo
    _bind(r)
    _sign(r, key)
    p = _decl(r)
    p.write_text(p.read_text().replace('"skill_min": 0.02', '"skill_min": 0.0'))
    _ok_env(monkeypatch)
    calls: list[str] = []
    with pytest.raises(SystemExit) as e:
        rr.cmd_run(_ns(r), run_one=_stub(calls))
    assert e.value.code == 2 and calls == []
    assert "mismatch" in capsys.readouterr().err


def test_refuses_the_signer_as_runner(repo, monkeypatch, capsys):
    r, key = repo
    _bind(r)
    _sign(r, key)
    _ok_env(monkeypatch)
    monkeypatch.setenv("IAP_RUN_ACTOR", "owner")
    calls: list[str] = []
    with pytest.raises(SystemExit) as e:
        rr.cmd_run(_ns(r), run_one=_stub(calls))
    assert e.value.code == 2 and calls == []
    assert "is the prereg signer" in capsys.readouterr().err


def test_refuses_signing_key_in_the_environment(repo, monkeypatch):
    r, key = repo
    _bind(r)
    _sign(r, key)
    _ok_env(monkeypatch)
    monkeypatch.setenv("IAP_SIGNING_KEY", "x")
    calls: list[str] = []
    with pytest.raises(SystemExit) as e:
        rr.cmd_run(_ns(r), run_one=_stub(calls))
    assert e.value.code == 2 and calls == []


def test_signed_run_scores_each_symbol_once_and_never_reruns(repo, monkeypatch):
    r, key = repo
    _bind(r)
    _sign(r, key)
    _ok_env(monkeypatch)
    monkeypatch.setattr(rr, "check_ram", lambda decl, f=None: 64.0)
    calls: list[str] = []
    assert rr.cmd_run(_ns(r), run_one=_stub(calls)) == 0
    assert calls == ["AAPL", "MSFT", "QQQ"]
    dec = json.loads((r / "out" / "DECISION.json").read_text())
    assert dec["model_supported_on_all_symbols"] is True and dec["level2"] == "not validated"
    assert "Level 2" in (r / "out" / "REPORT.md").read_text()
    with pytest.raises(SystemExit) as e:  # second launch: results exist
        rr.cmd_run(_ns(r), run_one=_stub(calls))
    assert e.value.code == 2 and calls == ["AAPL", "MSFT", "QQQ"]


# ------------------------------------------------------------ decision rule --
def _doc(skill, day_skills, gap=0.01):
    return {
        "overall": {"skill": skill, "brier": 0.1, "brier_naive": 0.11},
        "per_day": {f"d{i}": {"skill": s} for i, s in enumerate(day_skills)},
        "reliability": [
            {"n": 500, "observed_rate": 0.3 + gap, "mean_predicted": 0.3},
            {"n": 5, "observed_rate": 0.9, "mean_predicted": 0.1},  # underpowered: ignored
        ],
        "n_eval": 10,
        "n_fit": 20,
    }


@pytest.mark.parametrize(
    "doc, verdict",
    [
        (_doc(0.05, [0.04, 0.05, 0.06]), "supports"),
        (_doc(0.05, [0.04, -0.01, 0.06]), "inconclusive"),
        (_doc(0.01, [0.01, 0.01, 0.01]), "inconclusive"),
        (_doc(0.05, [0.05, 0.05, 0.05], gap=0.2), "inconclusive"),
        (_doc(-0.02, [0.01, -0.05, -0.03]), "contradicts"),
        (_doc(None, [None, None, None]), "not_estimable"),
    ],
)
def test_classify(doc, verdict):
    assert rr.classify(doc, DECL["decision_rule"])["verdict"] == verdict


def test_headline_needs_every_symbol():
    good, bad = _doc(0.05, [0.05] * 3), _doc(-0.1, [-0.1] * 3)
    assert rr.decide(DECL, {"AAPL": good, "MSFT": good, "QQQ": good})[
        "model_supported_on_all_symbols"
    ]
    assert not rr.decide(DECL, {"AAPL": good, "MSFT": good, "QQQ": bad})[
        "model_supported_on_all_symbols"
    ]


def test_decision_grid_is_the_declared_regular_session_grid():
    g = rr.decision_grid("2019-10-30", DECL["label"])
    assert len(g) == 6 * 3600 // 5 and g[1] - g[0] == 5 * rr.NS
    assert len(g) >= DECL["label"]["min_decisions_per_day_symbol"]


def test_declaration_is_unsigned_and_names_a_non_signer_actor():
    assert "UNSIGNED" in DECL["status"]
    cmd = (REPO / "research/execution/rel05/run_rel05.cmd").read_bytes()
    assert b"IAP_RUN_ACTOR=claude-queue-runner" in cmd
    attrs = (REPO / ".gitattributes").read_text(encoding="utf-8")
    assert "research/execution/rel05/*.cmd text eol=crlf" in attrs  # CRLF enforced at checkout


def test_run_symbol_fits_on_fit_days_and_scores_each_eval_day_on_synthetic_tape():
    from test_paper_sim_to_real import _day

    decl = copy.deepcopy(DECL)
    decl["split"] = {"fit_days": ["A1", "A2"], "eval_days": ["B1", "B2"]}
    decl["label"]["ttl_ns"] = 2 * 10**9
    seeds = {"A1": 1, "A2": 2, "B1": 3, "B2": 4}
    seen: list[str] = []

    def frame(d, root, day, sym):
        seen.append(day)
        return _day(seeds[day], day)

    doc = rr.run_symbol(decl, Path("."), "AAPL", frame=frame)
    assert sorted(seen) == ["A1", "A2", "B1", "B2"]
    assert doc["fit_days"] == ["A1", "A2"] and doc["eval_days"] == ["B1", "B2"]
    assert set(doc["per_day"]) == {"B1", "B2"} and doc["level2"] == "not validated"
    c = rr.classify(doc, decl["decision_rule"])
    assert c["verdict"] in {"supports", "inconclusive", "contradicts", "not_estimable"}
