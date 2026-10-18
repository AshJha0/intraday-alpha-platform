"""research/v118/rel10/run_rel10.py: the REL-10 runner's refusals (synthetic repo only).

No market data is read: every test builds a throw-away git repository holding a
copy of the runner, the committed declaration and a test trust root; the
session loader is replaced by the synthetic two-venue tape of
``test_v118_stale_quote``.
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
from test_v118_volume_clock_ofi import IID, session_events

REPO = Path(__file__).resolve().parents[2]
RUNNER = REPO / "research" / "v118" / "rel10" / "run_rel10.py"
DECL_SRC = REPO / "research" / "v118" / "rel10" / "predeclaration.json"
TOOL = REPO / "tools" / "prereg_bind_identity.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rr = _load("run_rel10", RUNNER)
pbi = _load("prereg_bind_identity", TOOL)


def _git(root: Path, *a: str) -> None:
    subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "python" / "src" / "iap").mkdir(parents=True)
    (r / "python" / "src" / "iap" / "lib.py").write_text("X = 1\n")
    d = r / "research" / "v118" / "rel10"
    d.mkdir(parents=True)
    shutil.copy(RUNNER, d / "run_rel10.py")
    shutil.copy(DECL_SRC, d / "predeclaration.json")
    (d / "PREDECLARATION.md").write_text("# synthetic declaration\n")
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
    return r / "research" / "v118" / "rel10" / "predeclaration.json"


def _bind(r: Path) -> None:
    assert rr.main(["--root", str(r), "--decl", str(_decl(r)), "bind"]) == 0


def _sign(r: Path, key: Path) -> None:
    argv = ["--root", str(r), "sign", "--prereg", "research/preregs/rel10_prereg.json"]
    assert pbi.main([*argv, "--keyfile", str(key), "--maintainer", "owner"]) == 0
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "signed prereg")


def _ns(r: Path) -> argparse.Namespace:
    return argparse.Namespace(
        root=str(r),
        decl=_decl(r),
        prereg=None,
        data_root=r / "data",
        out=r / "research" / "v118" / "rel10",
    )


def _loader(calls: list[tuple[str, str]]):
    def load(decl, data_root, sess, symbol):
        calls.append((sess["session"], symbol))
        return session_events(), IID, 0.01

    return load


def _no_data(monkeypatch):
    monkeypatch.setattr(rr, "check_datasets", lambda decl, data_root, prereg_id: {})


def _signed(repo, monkeypatch, actor="rel10-runner"):
    r, key = repo
    _bind(r)
    _sign(r, key)
    monkeypatch.setenv("IAP_RUN_ACTOR", actor)
    return r


# ------------------------------------------------------------- refusals --
def test_refuses_when_the_prereg_does_not_exist(repo, monkeypatch, capsys):
    r, _ = repo
    monkeypatch.setenv("IAP_RUN_ACTOR", "rel10-runner")
    calls: list = []
    with pytest.raises(SystemExit) as e:
        rr.cmd_run(_ns(r), loader=_loader(calls), find=lambda: [])
    assert e.value.code == 2 and calls == []
    assert "does not exist" in capsys.readouterr().err


def test_refuses_an_unsigned_prereg(repo, monkeypatch, capsys):
    r, _ = repo
    _bind(r)
    monkeypatch.setenv("IAP_RUN_ACTOR", "rel10-runner")
    calls: list = []
    with pytest.raises(SystemExit) as e:
        rr.cmd_run(_ns(r), loader=_loader(calls), find=lambda: [])
    assert e.value.code == 2 and calls == []
    assert "not signed" in capsys.readouterr().err


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: (r / "python/src/iap/lib.py").write_text("X = 2\n"),
        lambda r: _decl(r).write_text(
            _decl(r).read_text().replace('"threshold_quantile": 0.8', '"threshold_quantile": 0.9')
        ),
        lambda r: _decl(r).write_text(
            _decl(r).read_text().replace('"family_k": 24', '"family_k": 12')
        ),
    ],
)
def test_refuses_an_identity_mismatch(repo, monkeypatch, capsys, mutate):
    r = _signed(repo, monkeypatch)
    mutate(r)
    _no_data(monkeypatch)
    calls: list = []
    with pytest.raises(SystemExit) as e:
        rr.cmd_run(_ns(r), loader=_loader(calls), find=lambda: [])
    assert e.value.code == 2 and calls == []
    assert "mismatch" in capsys.readouterr().err


def test_refuses_the_signer_as_runner(repo, monkeypatch, capsys):
    r = _signed(repo, monkeypatch, actor="owner")
    _no_data(monkeypatch)
    with pytest.raises(SystemExit) as e:
        rr.cmd_run(_ns(r), loader=_loader([]), find=lambda: [])
    assert e.value.code == 2
    assert "is the prereg signer" in capsys.readouterr().err


@pytest.mark.parametrize("env", [{}, {"IAP_RUN_ACTOR": "rel10-runner", "IAP_KEYFILE": "k"}])
def test_refuses_an_undeclared_runner_or_one_holding_a_key(repo, monkeypatch, env):
    r = _signed(repo, monkeypatch)
    monkeypatch.delenv("IAP_RUN_ACTOR", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    _no_data(monkeypatch)
    with pytest.raises(SystemExit) as e:
        rr.cmd_run(_ns(r), loader=_loader([]), find=lambda: [])
    assert e.value.code == 2


def test_refuses_a_dataset_version_mismatch(repo, monkeypatch, capsys):
    r = _signed(repo, monkeypatch)
    import iap.research.holdout_registry as hr

    monkeypatch.setattr(hr, "dataset_identity", lambda d: {"dataset_version": "other"})
    monkeypatch.setattr(hr, "guard", lambda *a, **k: {})
    calls: list = []
    with pytest.raises(SystemExit) as e:
        rr.cmd_run(_ns(r), loader=_loader(calls), find=lambda: [])
    assert e.value.code == 2 and calls == []
    assert "!= declared" in capsys.readouterr().err


def test_holdout_guard_runs_on_every_dataset_and_its_refusal_stops_the_run(
    repo, monkeypatch, capsys
):
    r = _signed(repo, monkeypatch)
    import iap.research.holdout_registry as hr

    decl = rr.load_decl(_decl(r))
    by_path = {str(r / "data" / p): k for k, p in decl["dataset_paths"].items()}
    monkeypatch.setattr(
        hr, "dataset_identity", lambda d: {"dataset_version": decl["datasets"][by_path[str(d)]]}
    )
    seen: list[tuple[str, list[str], list[str], str]] = []

    def guard(d, *, purpose, prereg_id, sessions, symbols):
        seen.append((by_path[str(d)], sessions, symbols, prereg_id))
        if by_path[str(d)] == "iex_20260518":
            raise hr.HoldoutRefused("spent; prereg not listed in spent_by")
        return {}

    monkeypatch.setattr(hr, "guard", guard)
    calls: list = []
    with pytest.raises(SystemExit) as e:
        rr.cmd_run(_ns(r), loader=_loader(calls), find=lambda: [])
    assert e.value.code == 2 and calls == []
    assert "spent_by" in capsys.readouterr().err
    keys = [s[0] for s in seen]
    allk = sorted(decl["datasets"])
    assert keys == allk[: allk.index("iex_20260518") + 1]  # in order, stops at the refusal
    assert all(s[2] == ["AAPL", "MSFT"] and s[3] == "research/preregs/rel10_prereg.json"
               for s in seen)  # fmt: skip
    assert {k: s for k, s, _, _ in seen}["holdout2026"] == ["2026-05-15", "2026-05-18"]


# --------------------------------------------------------- the good path --
def test_signed_run_calibrates_then_evaluates_and_resumes_chain_b(repo, monkeypatch):
    r = _signed(repo, monkeypatch)
    _no_data(monkeypatch)
    calls: list[tuple[str, str]] = []
    events: list[tuple[str, int]] = []
    rc = rr.cmd_run(
        _ns(r),
        loader=_loader(calls),
        find=lambda: [4242],
        pause=lambda p: events.append(("suspend", p)) or True,
        unpause=lambda p: events.append(("resume", p)) or True,
    )
    assert rc == 0 and events == [("suspend", 4242), ("resume", 4242)]
    decl = rr.load_decl(_decl(r))
    cal = [s["session"] for s in decl["sessions"]["calibrate"]]
    ev = [s["session"] for s in decl["sessions"]["evaluate"]]
    want = [(s, y) for y in ("AAPL", "MSFT") for s in [*cal, *ev]]
    assert calls == want  # per symbol: calibrate first, then evaluate
    out = r / "research" / "v118" / "rel10"
    cals = json.loads((out / "calibration.json").read_text())
    assert cals["AAPL"]["n_shares"] % 100 == 0 and cals["AAPL"]["n_shares"] >= 100
    assert set(cals["AAPL"]["thresholds"]) == {
        f"{s}_{h}m" for s in decl["signals"] for h in decl["horizons_min"]
    }
    dec = json.loads((out / "DECISION.json").read_text())
    assert set(dec["uses"]) == {"taker", "maker"}
    for u in dec["uses"].values():
        assert u["verdict"] in {"candidate", "iterate", "reject", "underpowered"}
        assert len(u["cells"]) == 12
        for c in u["cells"].values():
            assert c["G"] == 4 and "missed_usd" in c and "fees_usd" in c
    assert "G = 4" in (out / "REPORT.md").read_text()
    before = (out / "DECISION.json").read_bytes()
    assert rr.cmd_analyse(_ns(r)) == 0
    assert (out / "DECISION.json").read_bytes() == before


def test_chain_b_is_resumed_when_the_run_fails(repo, monkeypatch):
    r = _signed(repo, monkeypatch)
    _no_data(monkeypatch)
    events: list[str] = []

    def boom(*a):
        raise RuntimeError("loader failed")

    with pytest.raises(RuntimeError):
        rr.cmd_run(
            _ns(r),
            loader=boom,
            find=lambda: [7],
            pause=lambda p: events.append("s") or True,
            unpause=lambda p: events.append("r") or True,
        )
    assert events == ["s", "r"]


# ------------------------------------------------- the committed declaration --
def test_committed_declaration_is_consistent():
    decl = rr.load_decl()
    cal = {s["session"] for s in decl["sessions"]["calibrate"]}
    ev = {s["session"] for s in decl["sessions"]["evaluate"]}
    assert not cal & ev and len(ev) == decl["statistics"]["g_evaluate"]
    assert set(rr.dataset_sessions(decl)) == set(decl["datasets"]) == set(decl["dataset_paths"])
    k = 2 * len(decl["signals"]) * len(decl["horizons_min"]) * len(decl["symbols"])
    assert decl["statistics"]["family_k"] == k
    args = rr.identity_args(decl)
    assert args["data_versions"] == sorted(decl["datasets"].values())
    assert f"K={k}" in args["family"]
    assert "OPEN" not in (REPO / decl["protocol"]).read_text(encoding="utf-8")


def test_bucket_rule_quantile_and_verdicts():
    decl = rr.load_decl()
    assert rr.n_shares_rule(decl, [10_000_000, 12_000_000]) == 28_200  # 28,205 per minute
    assert rr.n_shares_rule(decl, [10, 20]) == 100
    assert rr.quantile([1.0, 2.0, 3.0, 4.0, 5.0], 0.8) == pytest.approx(4.2)
    assert rr.classify({"ci_family_usd": [1, 2], "mde_family_usd": 9}, 20) == "candidate"
    assert rr.classify({"ci_family_usd": [-1, 1], "mde_family_usd": 10}, 20) == "reject"
    assert rr.classify({"ci_family_usd": [-1, 1], "mde_family_usd": 90}, 20) == "underpowered"
    c = {"verdict": "candidate"}
    u = {"verdict": "underpowered"}
    sy = ["AAPL", "MSFT"]
    assert rr.use_verdict({"ofi|1|AAPL": c, "ofi|1|MSFT": c}, sy) == "candidate"
    assert rr.use_verdict({"ofi|1|AAPL": c, "ofi|1|MSFT": u}, sy) == "iterate"
    assert rr.use_verdict({"ofi|1|AAPL": u, "ofi|1|MSFT": u}, sy) == "underpowered"
    assert rr.use_verdict({"ofi|1|AAPL": {"verdict": "reject"}, "ofi|1|MSFT": u}, sy) == "reject"


def test_calibration_reads_no_labels(monkeypatch):
    """Calibration builds rows only: the label and trip functions are never called."""
    import iap.research.volume_clock_ofi as vco

    def fail(*a, **k):
        raise AssertionError("calibration must not compute labels or trips")

    for name in ("label_row", "evaluate_taker", "evaluate_maker"):
        monkeypatch.setattr(vco, name, fail)
    decl = rr.load_decl()
    tape = vco.build_tape(session_events(), IID)
    cal = rr.calibrate(decl, [tape, tape], ["a", "b"])
    assert cal["calibrate_rows"] > 0
