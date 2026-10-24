"""F3 power diagnostic freeze checks: consistency, checkpoint integrity, no market data, non-gating reporting.

Synthetic only; no study is run (cells here use the dry-run seeds or a throw-away repo).
"""

from __future__ import annotations

import ast
import copy
import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from iap.agents import signing

REPO = Path(__file__).resolve().parents[2]
D = REPO / "research" / "v118" / "f3_power"
TOOL = REPO / "tools" / "prereg_bind_identity.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fp = _load("run_f3_power_freeze", D / "run_f3_power.py")
cf_ = _load("check_freeze_power_mod", D / "check_freeze.py")
pbi = _load("prereg_bind_identity_pf", TOOL)
pc, pm = fp.pc, fp.pm
DECL = fp.load_decl()


# ---------------------------------------------------------- 1. consistency --
def test_freeze_consistency_check_passes():
    res = cf_.run_checks()
    assert [(n, d) for n, ok, d in res if not ok] == [] and len(res) >= 20


def test_freeze_report_is_current():
    committed = (D / "FREEZE_CONSISTENCY.md").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert committed == cf_.render(cf_.run_checks())


def test_freeze_check_detects_a_drifted_protocol(tmp_path, monkeypatch):
    drift = (
        (D / "PROTOCOL.md")
        .read_text(encoding="utf-8")
        .replace('"n_per_cell": 10000', '"n_per_cell": 20000')
    )
    (tmp_path / "PROTOCOL.md").write_text(drift, encoding="utf-8")
    monkeypatch.setattr(cf_, "HERE", tmp_path)
    shutil.copy(D / "declaration.json", tmp_path / "declaration.json")
    for name in ("power_core.py", "power_math.py", "run_f3_power.py"):
        shutil.copy(D / name, tmp_path / name)
    names = {n for n, ok, _ in cf_.run_checks() if not ok}
    assert any("freeze-params" in n for n in names)


def test_freeze_check_detects_a_hard_coded_parameter(tmp_path, monkeypatch):
    monkeypatch.setattr(cf_, "HERE", tmp_path)
    for name in (
        "PROTOCOL.md",
        "declaration.json",
        "power_core.py",
        "power_math.py",
        "run_f3_power.py",
    ):
        shutil.copy(D / name, tmp_path / name)
    p = tmp_path / "power_math.py"
    p.write_text(p.read_text(encoding="utf-8") + "\nN = 10000\n", encoding="utf-8")
    assert any("hard-codes" in n for n, ok, _ in cf_.run_checks() if not ok)


def test_every_parameter_and_required_file_is_bound():
    args = fp.identity_args(DECL)
    assert args["thresholds"] == DECL and args["data_versions"] == ["synthetic-no-data"]
    assert cf_.REQUIRED_BOUND <= set(args["scripts"]) and cf_.REQUIRED_INPUTS <= set(args["inputs"])
    assert (
        "research/v118/f3_accept/core.py" in args["scripts"]
    )  # the imported generators are hash-bound


def test_seeds_are_disjoint_from_every_earlier_range():
    seeds = {pc.seed_for(DECL, r, g, dry) for r, g in fp.cells(DECL) for dry in (False, True)}
    seeds |= {pc.bootstrap_seed(DECL, r, g) for r, g in fp.cells(DECL)}
    assert len(seeds) == 27
    for lo, hi in cf_.FORBIDDEN_SEED_RANGES:
        assert not any(lo <= s <= hi for s in seeds)


# --------------------------------------------------- 2. checkpoint integrity --
def test_stream_is_deterministic_and_chunk_size_is_part_of_the_identity():
    a = pc.run_cell(DECL, "b1", 5, 8000, dry=True)
    assert a == pc.run_cell(DECL, "b1", 5, 8000, dry=True)
    d2 = copy.deepcopy(DECL)
    d2["chunk"] = 2000
    b = pc.run_cell(d2, "b1", 5, 8000, dry=True)
    assert (
        a["chunk_counts_sha256"] != b["chunk_counts_sha256"]
        and a["indicators_sha256"] != b["indicators_sha256"]
    )


def _git(root: Path, *a: str) -> None:
    subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True)


@pytest.fixture
def signed(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    (r / "python" / "src" / "iap").mkdir(parents=True)
    (r / "python" / "src" / "iap" / "lib.py").write_text("X = 1\n")
    for rel in [*fp.BOUND, *fp.INPUTS, DECL["protocol"]]:
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / rel, r / rel)
    priv, pub = signing.generate_keypair()
    (r / "research" / "agents").mkdir(parents=True)
    (r / "research" / "agents" / "trust_roots.json").write_text(
        json.dumps({"maintainers": {"owner": pub}})
    )
    for a in (("init", "-q"), ("config", "user.email", "t@t"), ("config", "user.name", "t"),
              ("config", "core.autocrlf", "false"), ("add", "-A"), ("commit", "-qm", "base")):  # fmt: skip
        _git(r, *a)
    key = tmp_path / "maintainer.key"
    key.write_text(json.dumps({"owner": priv}))
    import iap.agents.fingerprint as fpr

    monkeypatch.setattr(fpr, "check_registration_env", lambda **kw: True)
    dpath = r / "research" / "v118" / "f3_power" / "declaration.json"
    assert fp.main(["--root", str(r), "--decl", str(dpath), "bind"]) == 0
    argv = [
        "--root",
        str(r),
        "sign",
        "--prereg",
        DECL["prereg"],
        "--keyfile",
        str(key),
        "--maintainer",
        "owner",
    ]
    assert pbi.main(argv) == 0
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "signed")
    monkeypatch.setenv("IAP_RUN_ACTOR", "claude-queue-runner")
    return r, fp.verify(r / DECL["prereg"], DECL, r)


def test_resume_revalidates_the_complete_identity_and_the_stream(signed, tmp_path):
    r, rec = signed
    out = tmp_path / "out"
    out.mkdir()
    fp._cell_job(DECL, "a0", 5, str(out), rec)
    f = out / "cell_a0_G5.json"
    fp.validate_cell(DECL, rec, f)  # a finished cell of this very identity resumes
    cell = json.loads(f.read_text())
    assert cell["identity_sha256"] == rec["identity_sha256"] and cell["rng_final_state"] > 0
    assert rec["market_data_consumed"] is False
    for key, bad in (("identity_sha256", "0" * 64), ("prereg_sha256", "0" * 64), ("seed", 1), ("rng_final_state", 5),
                     ("chunk_counts_sha256", "x"), ("indicators_sha256", "x"), ("n_chunks", 99), ("n", 5),
                     ("chunk", 7)):  # fmt: skip
        f.write_text(json.dumps({**cell, key: bad}))
        with pytest.raises(SystemExit) as e:
            fp.validate_cell(DECL, rec, f)
        assert e.value.code == 2, key
    tampered = copy.deepcopy(cell)
    tampered["counts"]["K3"][4] += 1
    f.write_text(json.dumps(tampered))
    with pytest.raises(SystemExit):
        fp.validate_cell(DECL, rec, f)
    tampered = copy.deepcopy(cell)
    tampered["null_rejection_datasets"]["K16"][0] += 1
    f.write_text(json.dumps(tampered))
    with pytest.raises(SystemExit):
        fp.validate_cell(DECL, rec, f)


def test_identity_sha256_covers_the_whole_block(signed):
    r, rec = signed
    block = json.loads((r / DECL["prereg"]).read_text())["analysis_identity"]
    base = fp.identity_sha256(block)
    assert base == rec["identity_sha256"]
    for path in (("environment", "numpy"), ("commit",), ("interpreter",), ("thresholds", "chunk")):
        b = copy.deepcopy(block)
        t = b
        for k in path[:-1]:
            t = t[k]
        t[path[-1]] = "changed"
        assert fp.identity_sha256(b) != base, path


def test_resume_policy_finality_and_void_are_declared():
    assert (
        "cell granularity only" in DECL["resume"]["permitted"]
        and "one run only" in DECL["resume"]["failed_study"]
    )
    assert (
        "including VOID_INTEGRITY" in DECL["stop_rules"]["final_policy"]
        and "FRESH seeds" in DECL["integrity"]["on_failure"]
    )
    assert (
        "never resets or reseeds" in (D / "PROTOCOL.md").read_text(encoding="utf-8")
        or "chunking never resets" in DECL["stream"]["rule"]
    )
    proto = (D / "PROTOCOL.md").read_text(encoding="utf-8")
    assert (
        "A completed run is final" in proto and "Resume permitted only at cell granularity" in proto
    )


# ------------------------------------------------------- 3. no market data --
SOURCES = ("power_core.py", "power_math.py", "run_f3_power.py", "check_freeze.py")
FORBIDDEN = ("holdout_registry", "guard_or_exit", "data/real", "data\\\\real", "read_parquet", "read_csv",
             "dataset_identity", "iap.marketdata", "iap.ingest", "polars", "pyarrow", "pandas")  # fmt: skip


def test_sources_contain_no_market_data_path():
    for name in SOURCES:
        src = (D / name).read_text(encoding="utf-8")
        assert [t for t in FORBIDDEN if t in src] == [], name


def test_imports_are_the_declared_set():
    allowed_top = {"__future__", "argparse", "base64", "concurrent", "ctypes", "hashlib", "importlib", "json", "math", "os", "re", "sys",
                   "time", "collections", "pathlib", "typing", "numpy", "scipy", "power_core", "power_math", "run_f3_power", "iap",
                   "prereg_bind_identity"}  # fmt: skip
    allowed_iap = {
        "iap.core.rng",
        "iap.agents.analysis_identity",
        "iap.agents.fingerprint",
        "iap.research.known_null",
        "iap.research",
    }
    for name in SOURCES:
        for node in ast.walk(ast.parse((D / name).read_text(encoding="utf-8"))):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            for m in mods:
                assert m.split(".")[0] in allowed_top, (name, m)
                if m.startswith("iap"):
                    assert m in allowed_iap, (name, m)
    # known_null is imported only inside the dry-run self-check, never by the run path
    assert re.search(
        r"def _self_checks[\s\S]*?known_null", (D / "run_f3_power.py").read_text(encoding="utf-8")
    )
    for name in ("power_core.py", "power_math.py"):
        assert "known_null" not in (D / name).read_text(encoding="utf-8")


def test_the_only_file_the_core_reads_is_the_bound_acceptance_core():
    src = (D / "power_core.py").read_text(encoding="utf-8")
    assert (
        src.count("spec_from_file_location") == 1
        and 'ACC_CORE = ROOT / "research" / "v118" / "f3_accept" / "core.py"' in src
    )


def test_a_cell_and_the_analysis_perform_no_file_io(monkeypatch):
    res = {
        (r, g): pc.run_cell(DECL, r, g, 300, dry=True) for r, g in fp.cells(DECL)
    }  # warm up before trapping
    opened = []
    real_open = open

    def spy(file, *a, **k):
        opened.append(str(file))
        return real_open(file, *a, **k)

    import builtins

    monkeypatch.setattr(builtins, "open", spy)
    monkeypatch.setattr(
        Path, "open", lambda self, *a, **k: opened.append(str(self)) or real_open(self, *a, **k)
    )
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: opened.append(str(self)) or "")
    monkeypatch.setattr(Path, "read_bytes", lambda self, *a, **k: opened.append(str(self)) or b"")
    r = pc.run_cell(DECL, "b1", 10, 3000, dry=True)
    dec = fp.analyse_cells(DECL, res, tau=DECL["integrity"]["tau_dry"])
    fp.report(dec)
    assert r["n"] == 3000 and dec["status"] == "COMPLETE" and opened == []


def test_sentinel_is_documented_as_sentinel_only_and_the_identity_says_no_market_data():
    assert (
        DECL["market_data_consumed"] is False and "sentinel only" in DECL["data_versions_sentinel"]
    )
    assert "not a dataset identifier and not a holdout exemption" in DECL["data_versions_sentinel"]
    family = fp.identity_args(DECL)["family"]
    assert "NO market data" in family and "NON-GATING" in family
    proto = (D / "PROTOCOL.md").read_text(encoding="utf-8")
    assert "sentinel only" in proto and "holdout exemption" in proto


# ---------------------------------------------------- 4. non-gating report --
def test_the_study_has_no_pass_fail_criterion_and_says_so():
    assert "no pass/fail criterion and no power floor" in DECL["non_gating"]
    dec = fp.analyse_cells(DECL, {(r, g): pc.run_cell(DECL, r, g, 300, dry=True) for r, g in fp.cells(DECL)},
                           tau=DECL["integrity"]["tau_dry"])  # fmt: skip
    assert not any(k in dec for k in ("overall_pass", "pass", "verdict"))
    text = fp.report(dec)
    assert (
        "NON-GATING" in text
        and "PASS" not in text.replace("integrity band", "").split("## MDE80")[1]
    )
    for src in (D / "run_f3_power.py", D / "power_math.py"):
        assert "meets_ucb_rule" not in src.read_text(encoding="utf-8")


def test_effect_size_quantities_are_distinguished_and_boundaries_are_never_invented():
    spec = (REPO / "research/v118/F3_POWER_DIAGNOSTIC_SPEC.md").read_text(encoding="utf-8")
    assert "**d**" in spec and "Empirical power" in spec and "**MDE80**" in spec
    assert "MDE80 > 16" in spec and "no extrapolation" in spec
    dec = fp.analyse_cells(DECL, {(r, g): pc.run_cell(DECL, r, g, 300, dry=True) for r, g in fp.cells(DECL)},
                           tau=DECL["integrity"]["tau_dry"])  # fmt: skip
    for row in dec["mde80"]:
        v = row["mde80"]
        assert isinstance(v, float) or v.startswith(("> ", "<= ")), v
        if isinstance(v, float):
            assert DECL["grid"][0] <= v <= DECL["grid"][-1]
    assert np.isinf(pm.HIGH) and pm.LOW < 0
