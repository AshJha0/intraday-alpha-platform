"""research/v118/rel04_verify/run_rel04_verify.py: the REL-04 capture-ts verification runner.

Synthetic fixtures only: no data under data/real is read.  Refusals run in a throw-away git
repository holding a copy of the runner and the declaration, with a test trust root.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from iap.agents import signing

REPO = Path(__file__).resolve().parents[2]
RUNNER = REPO / "research" / "v118" / "rel04_verify" / "run_rel04_verify.py"
TOOL = REPO / "tools" / "prereg_bind_identity.py"
DAYS = ["2025-11-28", "2025-12-08", "2025-12-09", "2025-12-10", "2026-05-15", "2026-05-18"]
PID = "research/preregs/rel04_capts_verify_prereg.json"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rr = _load("run_rel04_verify", RUNNER)
pbi = _load("prereg_bind_identity", TOOL)
DECL = rr.load_decl()
# the committed declaration is pinned (PR pin step); the tests of the unpinned state use this copy
DRAFT = json.loads(json.dumps(DECL))
for _d in DRAFT["days"].values():
    _d["capts_dataset_version"] = None


def _pinned(decl=DECL):
    d = copy.deepcopy(decl)
    for i, day in enumerate(DAYS):
        d["days"][day]["capts_dataset_version"] = f"{i + 1:064x}"
    return d


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
    d = r / "research" / "v118" / "rel04_verify"
    d.mkdir(parents=True)
    shutil.copy(RUNNER, d / "run_rel04_verify.py")
    (d / "predeclaration.json").write_text(json.dumps(_pinned(), indent=2) + "\n")
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
    return r / "research" / "v118" / "rel04_verify" / "predeclaration.json"


def _ns(r: Path) -> argparse.Namespace:
    return argparse.Namespace(
        root=str(r), decl=_decl(r), prereg=None, data_root=r / "data", out=r / "out"
    )


def _bind(r: Path) -> None:
    assert rr.main(["--root", str(r), "--decl", str(_decl(r)), "bind"]) == 0


def _sign(r: Path, key: Path) -> None:
    argv = ["--root", str(r), "sign", "--prereg", PID]
    assert pbi.main([*argv, "--keyfile", str(key), "--maintainer", "owner"]) == 0
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "signed prereg")


def _ident(passes: bool = True):
    def f(decl, data_root, day):
        res = {
            "rows_reference": 10,
            "rows_candidate": 10,
            "row_count_difference": 0,
            "schema_equal": True,
            "compared": True,
            "mismatching_rows_excluding_receive_ts": 0 if passes else 3,
            "receive_ts": {"differing_rows": 9, "before_exchange_rows": 0},
        }
        return {"day": day, **res, "verdict": rr.day_verdict(res, "iex_capture")}

    return f


def _lat(decl, data_root, day):
    return {
        "day": day,
        "n": 5,
        "negative": 1,
        "negative_share": 0.2,
        "mean_ns": 10.0,
        "min_ns": -3,
        "max_ns": 99,
        "approx_p50_log2_bucket": 3,
        "approx_p99_log2_bucket": 6,
    }


def _feat(calls):
    def f(decl, data_root, day):
        calls.append(day)
        return {"day": day, "feature_files": {}}

    return f


def _ok(monkeypatch):
    monkeypatch.setenv("IAP_RUN_ACTOR", "claude-queue-runner")
    monkeypatch.setattr(rr, "check_registry", lambda decl, pid, registry=None: None)
    monkeypatch.setattr(rr, "check_datasets", lambda decl, data_root, pid: {})


def _run(r, **kw):
    kw.setdefault("identity", _ident())
    kw.setdefault("latency", _lat)
    kw.setdefault("free_gb", lambda: 64.0)
    kw.setdefault("disk_gb", lambda p: 500.0)
    return rr.cmd_run(_ns(r), **kw)


# ------------------------------------------------------------- declaration --
def test_declaration_is_an_unsigned_unpinned_draft():
    assert "UNSIGNED" in DECL["status"] and "NOT yet pinned" in DECL["status"]
    assert rr.unpinned(DRAFT) == DAYS
    assert rr.unpinned(DECL) == []
    assert not (REPO / DECL["prereg"]).exists()
    assert DECL["statistics"]["family_k"] == 0 and "none" in DECL["statistics"]["inference"]
    assert "iex.CAPTURE_TS_PROVENANCE" in " ".join(DECL["claims"]["cannot_claim"])


def test_declared_iex_versions_match_rel09_and_the_real_registry():
    rel09 = json.loads((REPO / "research/v118/rel09/predeclaration.json").read_text("utf-8"))
    reg = json.loads((REPO / "research/holdout_registry.json").read_text("utf-8"))
    for d in DAYS:
        row = DECL["days"][d]
        tag = d.replace("-", "")
        assert row["iex_dir"] == f"iex/iex_{tag}" and row["capts_dir"] == f"iex_capts/iex_{tag}"
        assert row["iex_dataset_version"] == rel09["datasets"][f"iex_{tag}"]
        assert reg["datasets"][row["iex_dataset_version"]]["sessions"] == [d]


def test_wrapper_names_a_non_signer_actor_and_is_crlf():
    cmd = (REPO / "research/v118/rel04_verify/run_rel04_verify.cmd").read_bytes()
    assert b"IAP_RUN_ACTOR=claude-queue-runner" in cmd and b"\r\n" in cmd
    assert b"\n" not in cmd.replace(b"\r\n", b"")
    attrs = (REPO / ".gitattributes").read_text(encoding="utf-8")
    assert "research/v118/rel04_verify/*.cmd text eol=crlf" in attrs


def test_bind_and_run_refuse_an_unpinned_declaration(capsys):
    with pytest.raises(SystemExit) as e:
        rr.identity_args(DRAFT)
    assert e.value.code == 2 and "not pinned" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        rr.check_days(DRAFT)


def test_check_days_refuses_a_swapped_directory(capsys):
    d = _pinned()
    d["days"][DAYS[0]]["capts_dir"] = "iex/iex_20251128"
    with pytest.raises(SystemExit):
        rr.check_days(d)
    assert "iex_capts/" in capsys.readouterr().err


# ---------------------------------------------------------------- registry --
def _real_registry():
    return json.loads((REPO / "research/holdout_registry.json").read_text("utf-8"))


def _idents(decl):
    return {
        d: {
            "dataset_version": decl["days"][d]["capts_dataset_version"],
            "content_hash": f"{i + 100:064x}",
            "sessions": [d],
            "symbols": ["AAPL", "MSFT", "QQQ"],
        }
        for i, d in enumerate(DAYS)
    }


def test_committed_registry_does_not_yet_authorise_this_prereg(capsys):
    reg = _real_registry()
    if all(
        PID in r.get("spent_by", [])
        for d in DAYS
        for r in reg["sessions"][d]
        if r["state"] == "spent"
    ):
        pytest.skip("the coordinator has applied the registry edit")
    with pytest.raises(SystemExit):
        rr.check_registry(_pinned(), PID, reg)
    assert "registry" in capsys.readouterr().err


def test_patched_registry_is_sealed_authorises_and_registers_capts_as_spent(tmp_path):
    from iap.research import holdout_registry as hr

    decl, reg = _pinned(), _real_registry()
    new = rr.patch_registry(decl, reg, _idents(decl))
    p = tmp_path / "reg.json"
    p.write_text(hr.render(new), encoding="utf-8")
    hr.load_registry(p)  # body hash valid
    rr.check_registry(decl, PID, new)
    for d in DAYS:
        assert all(PID in r["spent_by"] for r in new["sessions"][d] if r["state"] == "spent")
        e = new["datasets"][decl["days"][d]["capts_dataset_version"]]
        assert e["state"] == "spent" and e["label"].startswith("iex_capts_")
    changed = {k for k in reg["sessions"] if reg["sessions"][k] != new["sessions"][k]}
    assert changed <= set(DAYS)
    added = {k for k in new["datasets"] if k not in reg["datasets"]}
    assert added == {decl["days"][d]["capts_dataset_version"] for d in DAYS}
    assert all(new["datasets"][k] == v for k, v in reg["datasets"].items())


def test_patch_registry_is_idempotent_and_refuses_a_foreign_dataset():
    decl, reg = _pinned(), _real_registry()
    once = rr.patch_registry(decl, reg, _idents(decl))
    assert rr.patch_registry(decl, once, _idents(decl)) == once
    bad = _idents(decl)
    bad[DAYS[0]]["dataset_version"] = "f" * 64
    with pytest.raises(SystemExit):
        rr.patch_registry(decl, reg, bad)


def test_unregistered_capts_dataset_is_refused(capsys):
    decl, reg = _pinned(), _real_registry()
    new = rr.patch_registry(decl, reg, _idents(decl))
    del new["datasets"][decl["days"][DAYS[3]]["capts_dataset_version"]]
    with pytest.raises(SystemExit):
        rr.check_registry(decl, PID, new)
    assert "not registered" in capsys.readouterr().err


def test_capts_registered_in_sample_is_refused(capsys):
    decl, reg = _pinned(), _real_registry()
    new = rr.patch_registry(decl, reg, _idents(decl))
    new["datasets"][decl["days"][DAYS[0]]["capts_dataset_version"]]["state"] = "in_sample"
    with pytest.raises(SystemExit):
        rr.check_registry(decl, PID, new)
    assert "not registered as spent" in capsys.readouterr().err


# ------------------------------------------------------------------- guard --
def _write_dataset(path: Path, day: str, version: str, receive="iex_capture", lat=True):
    path.mkdir(parents=True, exist_ok=True)
    sess: dict = {
        "raw": {"capture_ts_before_exchange": 4, "capture_ts_absent": 0},
        "messages": {"iex": {}},
    }
    if lat:
        sess["messages"]["iex"]["feed_latency"] = {
            "n": 100,
            "negative": 2,
            "mean_ns": 5.5,
            "min_ns": -7,
            "max_ns": 900,
            "log2_histogram": {"3": 50, "5": 40, "9": 8},
        }
    doc = {
        "kind": "real",
        "dataset_version": version,
        "sessions": {day: sess},
        "source": {"receive_ts": receive} if receive else {},
        "normalized": {},
        "universe": [{"symbol": "AAPL"}],
    }
    (path / "dataset.json").write_text(json.dumps(doc))


def test_missing_day_directory_is_refused(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        rr.check_datasets(_pinned(), tmp_path, PID)
    assert e.value.code == 2 and "is missing" in capsys.readouterr().err


def test_a_prereg_id_missing_from_spent_by_is_refused_by_the_holdout_guard(tmp_path):
    reg = _real_registry()
    if all(PID in r.get("spent_by", []) for r in reg["sessions"][DAYS[0]] if r["state"] == "spent"):
        pytest.skip("the coordinator has applied the registry edit")
    decl = _pinned()
    row = decl["days"][DAYS[0]]
    _write_dataset(tmp_path / row["iex_dir"], DAYS[0], row["iex_dataset_version"])
    with pytest.raises(SystemExit) as e:
        rr.check_datasets(decl, tmp_path, PID)
    assert e.value.code


def test_dataset_version_mismatch_is_refused(tmp_path, monkeypatch, capsys):
    from iap.research import holdout_registry as hr

    decl = _pinned()
    row = decl["days"][DAYS[0]]
    _write_dataset(tmp_path / row["iex_dir"], DAYS[0], "e" * 64)
    monkeypatch.setattr(hr, "guard_or_exit", lambda *a, **k: {})
    with pytest.raises(SystemExit):
        rr.check_datasets(decl, tmp_path, PID)
    assert "dataset_version" in capsys.readouterr().err


# --------------------------------------------------------------- identity --
def _events(path: Path, n=50, *, receive=None, mutate=None, drop_col=None):
    import pyarrow as pa
    import pyarrow.parquet as pq

    ex = [1000 + 10 * i for i in range(n)]
    cols = {
        "event_id": list(range(1, n + 1)),
        "instrument_id": [1 + i % 2 for i in range(n)],
        "venue_id": [7] * n,
        "exchange_ts": ex,
        "receive_ts": ex if receive is None else receive(ex),
        "sequence": list(range(n)),
        "event_type": [1] * n,
        "side": [0] * n,
        "price_ticks": [10000 + i for i in range(n)],
        "qty": [100] * n,
        "order_id": list(range(n)),
        "trade_id": [0] * n,
    }
    if mutate:
        mutate(cols)
    if drop_col:
        del cols[drop_col]
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table({k: pa.array(v, type=pa.int64()) for k, v in cols.items()})
    pq.write_table(table, path, row_group_size=16)


def _cmp(tmp_path, **cand):
    ref, can = tmp_path / "ref.parquet", tmp_path / "cand.parquet"
    _events(ref)
    _events(can, **cand)
    res = rr.compare_events(ref, can, chunk_rows=7)  # chunks straddle the row groups
    return res, rr.day_verdict(res, "iex_capture")


def test_identity_passes_when_only_receive_ts_differs(tmp_path):
    res, v = _cmp(tmp_path, receive=lambda ex: [t + 25 for t in ex])
    assert v == {"pass": True, "failed_clauses": []}
    assert res["mismatching_rows_excluding_receive_ts"] == 0
    assert res["receive_ts"]["differing_rows"] == 50
    assert res["instrument_rows"] == {"1": 25, "2": 25}


def test_identity_reports_a_price_mismatch_with_examples(tmp_path):
    def mut(c):
        c["price_ticks"][41] += 1

    res, v = _cmp(tmp_path, receive=lambda ex: [t + 1 for t in ex], mutate=mut)
    assert not v["pass"] and "columns other than receive_ts differ" in v["failed_clauses"]
    assert res["mismatches"]["price_ticks"] == 1
    assert res["examples"]["price_ticks"][0] == {"row": 41, "reference": 10041, "candidate": 10042}


def test_identity_fails_on_schema_and_on_an_ineffective_capture_stamp(tmp_path):
    _, v = _cmp(tmp_path, receive=lambda ex: [t + 1 for t in ex], drop_col="trade_id")
    assert not v["pass"] and "schema differs" in v["failed_clauses"]
    _, v = _cmp(tmp_path)  # receive_ts == exchange_ts: the capture stamp had no effect
    assert not v["pass"] and any("identical" in c for c in v["failed_clauses"])


def test_identity_fails_on_capture_before_exchange_and_wrong_source(tmp_path):
    res, v = _cmp(
        tmp_path, receive=lambda ex: [t - 1 if i == 3 else t + 1 for i, t in enumerate(ex)]
    )
    assert not v["pass"] and "candidate receive_ts < exchange_ts" in v["failed_clauses"]
    assert res["receive_ts"]["before_exchange_rows"] == 1
    good, _ = _cmp(tmp_path, receive=lambda ex: [t + 1 for t in ex])
    bad = rr.day_verdict(good, None)
    assert not bad["pass"] and "source.receive_ts" in bad["failed_clauses"][0]


def test_identity_fails_on_different_row_counts(tmp_path):
    ref, can = tmp_path / "a.parquet", tmp_path / "b.parquet"
    _events(ref, 50)
    _events(can, 40, receive=lambda ex: [t + 1 for t in ex])
    res = rr.compare_events(ref, can)
    assert res["row_count_difference"] == -10 and res["compared"] is False
    assert not rr.day_verdict(res, "iex_capture")["pass"]


def test_identity_one_reads_the_declared_files(tmp_path):
    decl = _pinned()
    row = decl["days"][DAYS[0]]
    _write_dataset(tmp_path / row["capts_dir"], DAYS[0], "1" * 64)
    ref = tmp_path / row["iex_dir"] / "normalized" / "events.parquet"
    _events(ref)
    cand = tmp_path / row["capts_dir"] / "normalized" / "events.parquet"
    _events(cand, receive=lambda ex: [t + 3 for t in ex])
    out = rr.identity_one(decl, tmp_path, DAYS[0])
    assert out["verdict"]["pass"] and out["source_receive_ts"] == "iex_capture"
    ref.unlink()
    with pytest.raises(SystemExit):
        rr.identity_one(decl, tmp_path, DAYS[0])


# ----------------------------------------------------------------- latency --
def test_latency_summary_comes_from_dataset_json_only(tmp_path):
    decl = _pinned()
    row = decl["days"][DAYS[0]]
    _write_dataset(tmp_path / row["capts_dir"], DAYS[0], "1" * 64)
    out = rr.latency_one(decl, tmp_path, DAYS[0])
    assert (out["n"], out["negative"], out["negative_share"]) == (100, 2, 0.02)
    assert out["approx_p50_log2_bucket"] == 3 and out["approx_p99_log2_bucket"] == 9
    assert out["capture_ts_before_exchange"] == 4 and out["min_ns"] == -7


def test_latency_refuses_a_dataset_without_feed_latency(tmp_path):
    decl = _pinned()
    row = decl["days"][DAYS[0]]
    _write_dataset(tmp_path / row["capts_dir"], DAYS[0], "1" * 64, receive=None, lat=False)
    with pytest.raises(SystemExit):
        rr.latency_one(decl, tmp_path, DAYS[0])


# ---------------------------------------------------------------- features --
def test_features_command_is_the_process_iex_day_step(tmp_path):
    argv = rr.features_argv(_pinned(), tmp_path / "ds", "PY")
    assert argv[:3] == ["PY", "-m", "iap.features"]
    assert argv[argv.index("--engine") + 1] == "rust" and argv[argv.index("--workers") + 1] == "1"
    assert argv[argv.index("--configs") + 1].endswith("configs")


def test_features_step_runs_once_logs_and_refuses_failure(tmp_path):
    decl = _pinned()
    ds = tmp_path / decl["days"][DAYS[0]]["capts_dir"]
    ds.mkdir(parents=True)

    class P:
        returncode, stdout, stderr = 0, "out", "err"

    def run_ok(argv, **kw):
        (ds / "features").mkdir()
        (ds / "features" / "features_x.parquet").write_bytes(b"abc")
        return P()

    out = rr.features_one(decl, tmp_path, DAYS[0], run=run_ok, python="PY")
    assert out["feature_files"] == {"features_x.parquet": hashlib.sha256(b"abc").hexdigest()}
    assert (ds / "features.log").read_text() == "outerr"
    with pytest.raises(SystemExit):  # features exist: one run only
        rr.features_one(decl, tmp_path, DAYS[0], run=run_ok)
    shutil.rmtree(ds / "features")
    P.returncode = 3
    with pytest.raises(SystemExit):
        rr.features_one(decl, tmp_path, DAYS[0], run=lambda *a, **k: P())


# --------------------------------------------------------------------- pin --
def test_pin_writes_versions_and_refuses_a_non_capture_dataset(tmp_path):
    p = tmp_path / "predeclaration.json"
    p.write_text(json.dumps(DRAFT, indent=2) + "\n")
    data = tmp_path / "data"
    for i, d in enumerate(DAYS):
        _write_dataset(data / DECL["days"][d]["capts_dir"], d, f"{i + 1:064x}")
    assert rr.main(["--decl", str(p), "pin", "--data-root", str(data)]) == 0
    got = json.loads(p.read_text())
    assert [got["days"][d]["capts_dataset_version"] for d in DAYS] == [
        f"{i + 1:064x}" for i in range(6)
    ]
    _write_dataset(data / DECL["days"][DAYS[0]]["capts_dir"], DAYS[0], "9" * 64, receive=None)
    p.write_text(json.dumps(DRAFT, indent=2) + "\n")
    with pytest.raises(SystemExit):
        rr.main(["--decl", str(p), "pin", "--data-root", str(data)])


# ------------------------------------------------------------ run refusals --
def test_refuses_when_the_prereg_does_not_exist(repo, monkeypatch):
    r, _ = repo
    _ok(monkeypatch)
    calls: list[str] = []
    with pytest.raises(SystemExit) as e:
        _run(r, features=_feat(calls))
    assert e.value.code == 2 and calls == [] and not (r / "out").exists()


def test_refuses_an_unsigned_prereg(repo, monkeypatch, capsys):
    r, _ = repo
    _bind(r)
    _ok(monkeypatch)
    with pytest.raises(SystemExit) as e:
        _run(r)
    assert e.value.code == 2 and "not signed" in capsys.readouterr().err


def test_refuses_an_identity_mismatch(repo, monkeypatch, capsys):
    r, key = repo
    _bind(r)
    _sign(r, key)
    p = _decl(r)
    p.write_text(p.read_text().replace('"min_free_ram_gb": 8.0', '"min_free_ram_gb": 0.0'))
    _ok(monkeypatch)
    with pytest.raises(SystemExit) as e:
        _run(r)
    assert e.value.code == 2 and "mismatch" in capsys.readouterr().err


def test_refuses_the_signer_as_runner(repo, monkeypatch, capsys):
    r, key = repo
    _bind(r)
    _sign(r, key)
    _ok(monkeypatch)
    monkeypatch.setenv("IAP_RUN_ACTOR", "owner")
    with pytest.raises(SystemExit) as e:
        _run(r)
    assert e.value.code == 2 and "is the prereg signer" in capsys.readouterr().err


def test_refuses_a_signing_key_in_the_environment(repo, monkeypatch):
    r, key = repo
    _bind(r)
    _sign(r, key)
    _ok(monkeypatch)
    monkeypatch.setenv("IAP_SIGNING_KEY", "x")
    with pytest.raises(SystemExit) as e:
        _run(r)
    assert e.value.code == 2


def test_refuses_without_the_registry_edit(repo, monkeypatch, capsys):
    reg = _real_registry()
    if all(
        PID in r.get("spent_by", [])
        for d in DAYS
        for r in reg["sessions"][d]
        if r["state"] == "spent"
    ):
        pytest.skip("the coordinator has applied the registry edit")
    r, key = repo
    _bind(r)
    _sign(r, key)
    monkeypatch.setenv("IAP_RUN_ACTOR", "claude-queue-runner")
    with pytest.raises(SystemExit) as e:  # the real check_registry on the committed registry
        _run(r)
    assert e.value.code == 2 and "registry" in capsys.readouterr().err


def test_refuses_low_ram_or_disk(repo, monkeypatch):
    r, key = repo
    _bind(r)
    _sign(r, key)
    _ok(monkeypatch)
    with pytest.raises(SystemExit):
        _run(r, free_gb=lambda: 2.0)
    with pytest.raises(SystemExit):
        _run(r, disk_gb=lambda p: 1.0)


def test_signed_run_with_identity_pass_builds_features_once(repo, monkeypatch):
    r, key = repo
    _bind(r)
    _sign(r, key)
    _ok(monkeypatch)
    calls: list[str] = []
    assert _run(r, features=_feat(calls)) == 0
    assert calls == DAYS
    dec = json.loads((r / "out" / "DECISION.json").read_text())
    assert dec["identity"] == "pass" and dec["features_status"] == "built"
    report = (r / "out" / "REPORT.md").read_text()
    assert "Cannot claim" in report and "offset to IEX's send clock is unknown" in report
    with pytest.raises(SystemExit) as e:  # second launch: results exist
        _run(r, features=_feat(calls))
    assert e.value.code == 2 and calls == DAYS


def test_identity_failure_reports_and_builds_no_features(repo, monkeypatch):
    r, key = repo
    _bind(r)
    _sign(r, key)
    _ok(monkeypatch)
    calls: list[str] = []
    assert _run(r, identity=_ident(passes=False), features=_feat(calls)) == 0
    assert calls == [] and not list((r / "out").glob("features_*.json"))
    dec = json.loads((r / "out" / "DECISION.json").read_text())
    assert dec["identity"] == "fail" and dec["features_status"].startswith("not built")
    assert "NO" in (r / "out" / "REPORT.md").read_text()
