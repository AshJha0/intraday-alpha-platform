"""research/execution/b8v2/run_b8v2.py: the REL-04b runner's refusals (synthetic repo only).

No market data is read: every test builds a throw-away git repository holding a
copy of the runner, a synthetic declaration and a test trust root, and the
benchmark itself is replaced by a stub.
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
from iap.research import execution_metrics as exm

REPO = Path(__file__).resolve().parents[2]
RUNNER = REPO / "research" / "execution" / "b8v2" / "run_b8v2.py"
TOOL = REPO / "tools" / "prereg_bind_identity.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rb = _load("run_b8v2", RUNNER)
pbi = _load("prereg_bind_identity", TOOL)


def _git(root: Path, *a: str) -> None:
    subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "python" / "src" / "iap").mkdir(parents=True)
    (r / "python" / "src" / "iap" / "lib.py").write_text("X = 1\n")
    b8v2 = r / "research" / "execution" / "b8v2"
    b8v2.mkdir(parents=True)
    shutil.copy(RUNNER, b8v2 / "run_b8v2.py")
    (b8v2 / "PREDECLARATION.md").write_text("# synthetic declaration\n")
    decl = json.loads((REPO / "research/execution/b8v2/predeclaration.json").read_text("utf-8"))
    decl["headline"]["datasets"] = {"syn_a": "v_a", "syn_b": "v_b"}
    decl["headline"]["calibration_dir"] = "research/execution/b8v2/cal"
    decl["benchmark"]["venues"] = "research/execution/b8v2/venues.json"
    (b8v2 / "predeclaration.json").write_text(json.dumps(decl, indent=2))
    (b8v2 / "cal").mkdir()
    (b8v2 / "cal" / "venue_calibration_XNAS.json").write_text("{}\n")
    (b8v2 / "venues.json").write_text("{}\n")
    (b8v2 / "consolidated_adv.json").write_text(
        json.dumps({"_source": "synthetic", "AAPL": 5e7, "MSFT": 2e7})
    )
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
    return r / "research" / "execution" / "b8v2" / "predeclaration.json"


def _bind(r: Path) -> None:
    assert rb.main(["--root", str(r), "--decl", str(_decl(r)), "bind"]) == 0


def _sign(r: Path, key: Path) -> None:
    attach_planted_dry_run(r, "research/preregs/b8v2_prereg.json")  # R4: sign refuses without it
    argv = ["--root", str(r), "sign", "--prereg", "research/preregs/b8v2_prereg.json"]
    assert pbi.main([*argv, "--keyfile", str(key), "--maintainer", "owner"]) == 0
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "signed prereg")


def _ns(r: Path) -> argparse.Namespace:
    return argparse.Namespace(
        root=str(r),
        decl=_decl(r),
        prereg=None,
        data_root=r / "data",
        out=r / "research" / "execution" / "b8v2",
    )


def _paired(est: float, ci: list[float], fr_ci: list[float]) -> dict:
    def s(e, c):
        return {"estimate": e, "se": 1.0, "ci95": c, "ci_family": c, "mde_t": 0.5,
                "mde_t_family": 0.5, "n_clusters": 3}  # fmt: skip

    return {
        "n_pairs": 10,
        "trading_cost_per_filled_share_bps": s(est, ci),
        "cost_per_filled_share_bps": s(est, ci),
        "completion_charge_bps": s(0.0, [-1.0, 1.0]),
        "shortfall_per_target_share_bps": s(0.0, [-1.0, 1.0]),
        "fill_rate": s(0.0, fr_ci),
    }


def _stub(calls: list[str], seen: list[dict] | None = None):
    def run_one(**kw):
        calls.append(kw["symbols"][0])
        if seen is not None:
            seen.append(kw)
        base = kw["baseline_policy"]
        pv = {f"{p}-{base}": _paired(0.3, [-1.0, 2.0], [-0.1, 0.1])
              for p in kw["policies"] if p != base}  # fmt: skip
        cells = {f"{p}|IS|2": {"depth_exhausted": {"parent_share": 0.25}} for p in kw["policies"]}
        return {"paired_v2_vs_baseline": pv, "cells": cells}

    return run_one


def _no_data(monkeypatch):
    monkeypatch.setattr(rb, "check_datasets", lambda decl, data_root, prereg_id: {})


# ------------------------------------------------------------- refusals --
def test_refuses_when_the_prereg_does_not_exist(repo, monkeypatch):
    r, _ = repo
    monkeypatch.setenv("IAP_RUN_ACTOR", "b8v2-runner")
    calls: list[str] = []
    with pytest.raises(SystemExit) as e:
        rb.cmd_run(_ns(r), run_one=_stub(calls), find=lambda: [])
    assert e.value.code == 2 and calls == []


def test_refuses_an_unsigned_prereg(repo, monkeypatch, capsys):
    r, _ = repo
    _bind(r)
    monkeypatch.setenv("IAP_RUN_ACTOR", "b8v2-runner")
    calls: list[str] = []
    with pytest.raises(SystemExit) as e:
        rb.cmd_run(_ns(r), run_one=_stub(calls), find=lambda: [])
    assert e.value.code == 2 and calls == []
    assert "not signed" in capsys.readouterr().err


@pytest.mark.parametrize(
    "mutate, why",
    [
        (lambda r: (r / "python/src/iap/lib.py").write_text("X = 2\n"), "code tree"),
        (
            lambda r: (r / "research/execution/b8v2/cal/venue_calibration_XNAS.json").write_text(
                '{"changed": 1}\n'
            ),
            "changed since signing",
        ),
        (
            lambda r: _decl(r).write_text(
                _decl(r).read_text().replace('"family_k": 6', '"family_k": 3')
            ),
            "",
        ),
    ],
)
def test_refuses_an_identity_mismatch(repo, monkeypatch, capsys, mutate, why):
    r, key = repo
    _bind(r)
    _sign(r, key)
    mutate(r)
    monkeypatch.setenv("IAP_RUN_ACTOR", "b8v2-runner")
    _no_data(monkeypatch)
    calls: list[str] = []
    with pytest.raises(SystemExit) as e:
        rb.cmd_run(_ns(r), run_one=_stub(calls), find=lambda: [])
    assert e.value.code == 2 and calls == []
    err = capsys.readouterr().err
    assert "mismatch" in err and why in err


def test_refuses_the_signer_as_runner(repo, monkeypatch, capsys):
    r, key = repo
    _bind(r)
    _sign(r, key)
    monkeypatch.setenv("IAP_RUN_ACTOR", "owner")
    _no_data(monkeypatch)
    calls: list[str] = []
    with pytest.raises(SystemExit) as e:
        rb.cmd_run(_ns(r), run_one=_stub(calls), find=lambda: [])
    assert e.value.code == 2 and calls == []
    assert "is the prereg signer" in capsys.readouterr().err


@pytest.mark.parametrize("env", [{}, {"IAP_RUN_ACTOR": "b8v2-runner", "IAP_KEYFILE": "k"}])
def test_refuses_an_undeclared_runner_or_one_holding_a_key(repo, monkeypatch, env):
    r, key = repo
    _bind(r)
    _sign(r, key)
    monkeypatch.delenv("IAP_RUN_ACTOR", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    _no_data(monkeypatch)
    with pytest.raises(SystemExit) as e:
        rb.cmd_run(_ns(r), run_one=_stub([]), find=lambda: [])
    assert e.value.code == 2


def test_bind_refuses_while_consolidated_adv_is_null(repo):
    r, _ = repo
    p = r / "research/execution/b8v2/consolidated_adv.json"
    p.write_text(json.dumps({"_source": None, "AAPL": None, "MSFT": None}))
    _git(r, "commit", "-qam", "null adv")
    with pytest.raises(SystemExit) as e:
        _bind(r)
    assert e.value.code == 2
    assert not (r / "research/preregs/b8v2_prereg.json").exists()


def test_committed_consolidated_adv_is_filled_and_accepted():
    """The committed consolidated ADV file is filled (2026-10-18, sourced) and passes the check."""
    rb.check_consolidated(rb.load_decl(), REPO)


# --------------------------------------------------------- the good path --
def test_signed_run_suspends_chain_b_runs_aapl_then_msft_and_resumes(repo, monkeypatch):
    r, key = repo
    _bind(r)
    _sign(r, key)
    monkeypatch.setenv("IAP_RUN_ACTOR", "b8v2-runner")
    _no_data(monkeypatch)
    calls: list[str] = []
    seen: list[dict] = []
    events: list[tuple[str, int]] = []
    rc = rb.cmd_run(
        _ns(r),
        run_one=_stub(calls, seen),
        find=lambda: [4242],
        pause=lambda p: events.append(("suspend", p)) or True,
        unpause=lambda p: events.append(("resume", p)) or True,
    )
    assert rc == 0
    assert calls == ["AAPL", "MSFT"] * 4  # headline, s1, s2, s3
    # headline: the REL-04 capped child on every route; s3: the v1.17 child
    head, s3 = seen[0], seen[-1]
    assert head["policies"] == ("pin_nasdaq+capped", "pin_iex+capped", "sor+capped",
                                "sor_sweep+capped")  # fmt: skip
    assert head["baseline_policy"] == "pin_nasdaq+capped"
    ce = head["child_exec"]
    assert (ce.style, ce.max_levels, ce.depth_fraction, ce.repost_interval_ns, ce.max_reposts) == (
        "capped_ioc", 3, 1.0, 1_000_000_000, 20)  # fmt: skip
    assert s3["policies"] == ("pin_nasdaq", "pin_iex", "sor", "sor_sweep")
    assert s3["baseline_policy"] == "pin_nasdaq" and s3["child_exec"] is None
    assert events == [("suspend", 4242), ("resume", 4242)]
    out = r / "research/execution/b8v2"
    man = json.loads((out / "run_manifest.json").read_text())
    assert man["runner_actor"] == "b8v2-runner" and man["signer"] == "owner"
    assert man["chain_b_resumed"] == [4242]
    dec = json.loads((out / "DECISION.json").read_text())
    assert dec["family_k"] == 6 and dec["child_variant"] == "capped"
    assert set(dec["cells"]) == {f"{p}|{s}" for p in ("pin_iex", "sor", "sor_sweep")
                                 for s in ("AAPL", "MSFT")}  # fmt: skip
    assert dec["depth_exhausted_parent_share_by_cell"]["sor+capped|IS|2|MSFT"] == 0.25
    assert {c["verdict"] for c in dec["cells"].values()} == {"no_evidence"}
    assert not any(dec["better_than_baseline_on_both_symbols"].values())


def test_chain_b_is_resumed_when_a_run_fails(repo, monkeypatch):
    r, key = repo
    _bind(r)
    _sign(r, key)
    monkeypatch.setenv("IAP_RUN_ACTOR", "b8v2-runner")
    _no_data(monkeypatch)
    resumed: list[int] = []

    def boom(**kw):
        raise RuntimeError("replay failed")

    with pytest.raises(RuntimeError):
        rb.cmd_run(_ns(r), run_one=boom, find=lambda: [7], pause=lambda p: True,
                   unpause=lambda p: resumed.append(p) or True)  # fmt: skip
    assert resumed == [7]


# ------------------------------------------------------------ statistics --
ST = rb.load_decl()["statistics"]


@pytest.mark.parametrize(
    "ci, fr, verdict",
    [
        ([-3.0, -1.0], [-0.1, 0.1], "better"),
        ([-3.0, -1.0], [-0.2, -0.1], "worse"),  # cheaper because it fills less
        ([1.0, 3.0], [-0.1, 0.1], "worse"),
        ([-1.0, 3.0], [-0.1, 0.1], "no_evidence"),
    ],
)
def test_decision_rule(ci, fr, verdict):
    assert rb.classify(_paired(sum(ci) / 2, ci, fr), ST)["verdict"] == verdict


def test_ratio_diff_matches_a_hand_computation():
    rows = []
    for g, (na, da, nb, db) in enumerate([(1.0, 10.0, 2.0, 10.0), (3.0, 10.0, 2.0, 10.0),
                                          (2.0, 20.0, 1.0, 10.0)]):  # fmt: skip
        rows.append(("a", {"session": f"s{g}", "n": na, "d": da}))
        rows.append(("b", {"session": f"s{g}", "n": nb, "d": db}))
    a = [r for k, r in rows if k == "a"]
    b = [r for k, r in rows if k == "b"]
    s = exm.ratio_diff_stats(a, b, "n", "d", family_k=6)
    ra, rbb = 6.0 / 40.0, 5.0 / 30.0
    assert s["estimate"] == pytest.approx(ra - rbb)
    e = [(na - ra * da) / 40 - (nb - rbb * db) / 30 for na, da, nb, db in
         [(1, 10, 2, 10), (3, 10, 2, 10), (2, 20, 1, 10)]]  # fmt: skip
    se = (3 / 2 * sum(x * x for x in e)) ** 0.5
    assert s["se"] == pytest.approx(se)
    assert s["df"] == 2
    lo, hi = s["ci_family"]
    assert (hi - lo) / 2 == pytest.approx(10.885866619 * se, rel=1e-6)
    assert s["cluster_sums"]["s0"] == {"num_a": 1.0, "den_a": 10.0, "num_b": 2.0, "den_b": 10.0}
