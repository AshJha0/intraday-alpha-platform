"""F3 acceptance study freeze checks (reviewer conditions 1-4): consistency, checkpoint integrity, no market data, reporting.

Synthetic only; no acceptance study is run (cells here use the dry-run seeds or a throw-away repo).
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
from _dry_run_fixture import attach_planted_dry_run
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


fa = _load("run_f3_accept_freeze", D / "run_f3_accept.py")
cf_ = _load("check_freeze_mod", D / "check_freeze.py")
pbi = _load("prereg_bind_identity_f", TOOL)
core = fa.core
DECL = fa.load_decl()


# ---------------------------------------------------------- 1. consistency --
def test_freeze_consistency_check_passes():
    res = cf_.run_checks()
    failed = [(n, d) for n, ok, d in res if not ok]
    assert failed == []
    assert len(res) >= 15


def test_freeze_report_is_current():
    committed = (D / "FREEZE_CONSISTENCY.md").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert committed == cf_.render(cf_.run_checks())


def test_freeze_check_detects_a_drifted_protocol(tmp_path, monkeypatch):
    drift = (
        (D / "PROTOCOL.md").read_text(encoding="utf-8").replace('"chunk": 10000', '"chunk": 5000')
    )
    shifted = tmp_path / "PROTOCOL.md"
    shifted.write_text(drift, encoding="utf-8")
    monkeypatch.setattr(cf_, "HERE", tmp_path)
    for name in (
        "declaration.json",
        "core.py",
        "run_f3_accept.py",
        "joint_bound.py",
        "JOINT_BOUND.md",
    ):
        shutil.copy(D / name, tmp_path / name)
    names = {n for n, ok, _ in cf_.run_checks() if not ok}
    assert any("freeze-params" in n for n in names)


def test_every_parameter_and_required_file_is_bound():
    args = fa.identity_args(DECL)
    assert args["thresholds"] == DECL and args["data_versions"] == ["synthetic-no-data"]
    assert cf_.REQUIRED_BOUND <= set(args["scripts"]) and cf_.REQUIRED_INPUTS <= set(args["inputs"])


# --------------------------------------------------- 2. checkpoint integrity --
def test_one_stream_per_cell_never_reseeded(monkeypatch):
    made = []

    class Counting(core.SplitMix64):
        def __init__(self, seed):
            made.append(seed)
            super().__init__(seed)

    monkeypatch.setattr(core, "SplitMix64", Counting)
    r = core.run_cell(DECL, "b1", 7, 25000, dry=True)
    assert len(made) == 1 and made[0] == core.seed_for(DECL, "b1", 7, True) and r["n_chunks"] == 3


def test_stream_is_deterministic_and_chunk_size_is_part_of_the_identity():
    a = core.run_cell(DECL, "a1", 5, 20000, dry=True)
    assert a == core.run_cell(DECL, "a1", 5, 20000, dry=True)
    d2 = copy.deepcopy(DECL)
    d2["chunk"] = 5000
    # a0/b0 draw one array per chunk, so their stream is chunk-size invariant; a1/b1 interleave draws per chunk
    for reg in ("a0", "b0"):
        x, y = (
            core.run_cell(DECL, reg, 7, 20000, dry=True),
            core.run_cell(d2, reg, 7, 20000, dry=True),
        )
        assert x["rejections"] == y["rejections"] and x["rng_final_state"] == y["rng_final_state"]
    r1, r2 = core.SplitMix64(1), core.SplitMix64(1)
    one = core.draw_chunk(DECL, "a1", 5, 10, r1)
    two = np.concatenate(
        [core.draw_chunk(DECL, "a1", 5, 5, r2), core.draw_chunk(DECL, "a1", 5, 5, r2)]
    )
    assert not np.array_equal(one, two), (
        "a1 data depend on the chunk size, so it is frozen in the identity"
    )


def test_core_normals_equal_the_platform_stream():
    from iap.research import known_null as kn

    for seed in (1, 20291105, 2**63 + 5):
        x, y = core.SplitMix64(seed), kn.SplitMix64(seed)
        assert (
            np.array_equal(core.splitmix_normals(x, 1000), kn.splitmix_normals(y, 1000))
            and x.state == y.state
        )


def _git(root: Path, *a: str) -> None:
    subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True)


@pytest.fixture
def signed(tmp_path, monkeypatch):
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
    for a in (("init", "-q"), ("config", "user.email", "t@t"), ("config", "user.name", "t"),
              ("config", "core.autocrlf", "false"), ("add", "-A"), ("commit", "-qm", "base")):  # fmt: skip
        _git(r, *a)
    key = tmp_path / "maintainer.key"
    key.write_text(json.dumps({"owner": priv}))
    import iap.agents.fingerprint as fp

    monkeypatch.setattr(fp, "check_registration_env", lambda **kw: True)
    dpath = r / "research" / "v118" / "f3_accept" / "declaration.json"
    assert fa.main(["--root", str(r), "--decl", str(dpath), "bind"]) == 0
    attach_planted_dry_run(r, DECL["prereg"])  # R4: sign refuses without it
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
    rec = fa.verify(r / DECL["prereg"], DECL, r)
    return r, rec


def test_resume_revalidates_the_complete_identity_and_the_stream(signed, tmp_path):
    r, rec = signed
    out = tmp_path / "out"
    out.mkdir()
    fa._cell_job(DECL, "a0", 5, str(out), rec)
    f = out / "cell_a0_G5.json"
    fa.validate_cell(DECL, rec, f, True)  # a finished cell of this very identity resumes
    cell = json.loads(f.read_text())
    assert cell["identity_sha256"] == rec["identity_sha256"] and cell["rng_final_state"] > 0
    for key, bad in (("identity_sha256", "0" * 64), ("prereg_sha256", "0" * 64), ("seed", 1),
                     ("rng_final_state", 5), ("chunk_counts_sha256", "x"), ("n_chunks", 99)):  # fmt: skip
        f.write_text(json.dumps({**cell, key: bad}))
        with pytest.raises(SystemExit) as e:
            fa.validate_cell(DECL, rec, f, True)
        assert e.value.code == 2, key
    f.write_text(json.dumps({**cell, "rejections": {"K3": 0, "K6": 0, "K16": 0}}))
    with pytest.raises(SystemExit):
        fa.validate_cell(DECL, rec, f, True)


def test_identity_sha256_covers_the_whole_block(signed):
    r, rec = signed
    block = json.loads((r / DECL["prereg"]).read_text())["analysis_identity"]
    base = fa.identity_sha256(block)
    assert base == rec["identity_sha256"]
    for path in (("environment", "numpy"), ("commit",), ("interpreter",), ("thresholds", "chunk")):
        b = copy.deepcopy(block)
        t = b
        for k in path[:-1]:
            t = t[k]
        t[path[-1]] = "changed"
        assert fa.identity_sha256(b) != base, path


def test_resume_policy_and_failure_finality_are_declared():
    assert DECL["resume"]["permitted"] is True and DECL["resume"]["granularity"] == "cell"
    assert (
        "final" in DECL["resume"]["failed_study"]
        and DECL["stream"]["reseed_between_chunks"] is False
    )
    proto = (D / "PROTOCOL.md").read_text(encoding="utf-8")
    assert "Resume is permitted only as follows" in proto and "A failed study is final" in proto


# ------------------------------------------------------- 3. no market data --
SOURCES = ("core.py", "run_f3_accept.py", "joint_bound.py", "check_freeze.py")
FORBIDDEN = ("holdout_registry", "guard_or_exit", "data/real", "data\\\\real", "read_parquet", "read_csv",
             "dataset_identity", "iap.marketdata", "iap.ingest", "polars", "pyarrow", "pandas")  # fmt: skip


def test_sources_contain_no_market_data_path():
    for name in SOURCES:
        src = (D / name).read_text(encoding="utf-8")
        hits = [t for t in FORBIDDEN if t in src]
        assert hits == [], (name, hits)


def test_imports_are_the_declared_set():
    allowed_top = {"__future__", "argparse", "concurrent", "ctypes", "hashlib", "json", "math", "os", "re", "sys", "time",
                   "collections", "pathlib", "typing", "numpy", "scipy", "core", "joint_bound", "run_f3_accept", "iap", "generators", "prereg_bind_identity"}  # fmt: skip
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
    run_src = (D / "run_f3_accept.py").read_text(encoding="utf-8")
    assert re.search(r"def _check_against_pilot[\s\S]*?known_null", run_src)
    assert (
        "known_null"
        not in (D / "core.py")
        .read_text(encoding="utf-8")
        .split("def splitmix_uniforms")[0]
        .split("import numpy")[1]
    )


def test_a_cell_and_the_analysis_perform_no_file_io(monkeypatch, tmp_path):
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
    r = core.run_cell(DECL, "b1", 10, 3000, dry=True)
    res = {
        (reg, g): {"nan_p": 0, "rejections": {"K3": 1, "K6": 1, "K16": 1}}
        for reg, g, _ in fa.cells(DECL)
    }
    dec = fa.decide(DECL, res)
    fa.report(dec)
    assert r["n"] == 3000 and opened == []


def test_sentinel_is_documented_as_sentinel_only_and_the_identity_says_no_market_data():
    assert (
        DECL["market_data_consumed"] is False and "sentinel only" in DECL["data_versions_sentinel"]
    )
    assert "not a dataset identifier and not a holdout exemption" in DECL["data_versions_sentinel"]
    assert "NO market data" in fa.identity_args(DECL)["family"]
    proto = (D / "PROTOCOL.md").read_text(encoding="utf-8")
    assert "sentinel only" in proto and "holdout exemption" in proto


# ------------------------------------------------------- 4. result report --
def test_report_lists_27_and_9_separately_and_denies_automatic_closure():
    res = {
        (reg, g): {"nan_p": 0, "rejections": {"K3": 900, "K6": 950, "K16": 1000}}
        for reg, g, _ in fa.cells(DECL)
    }
    dec = fa.decide(DECL, res)
    assert len(dec["mandatory_evaluations"]) == 27 and len(dec["sensitivity_evaluations"]) == 9
    assert {e["regime"] for e in dec["sensitivity_evaluations"]} == {"a1"}
    assert {e["regime"] for e in dec["mandatory_evaluations"]} == {"a0", "b0", "b1"}
    text = fa.report(dec)
    assert "## Mandatory results (27)" in text and "## Sensitivity results" in text
    assert text.index("## Mandatory results (27)") < text.index("## Sensitivity results")
    assert "does NOT automatically close F3 or authorise real-data claims" in text
    assert (
        "NOT part of the verdict" in text and "never used to redefine the operating domain" in text
    )
    assert (
        text.count("| a1 |") == 9 and sum(text.count(f"| {x} |") for x in ("a0", "b0", "b1")) == 27
    )


def test_a_sensitivity_failure_does_not_change_the_verdict_and_a_mandatory_failure_does():
    km = core.k_max(50000, 0.025)
    res = {
        (reg, g): {"nan_p": 0, "rejections": {"K3": 10, "K6": 10, "K16": 10}}
        for reg, g, _ in fa.cells(DECL)
    }
    res[("a1", 7)]["rejections"]["K16"] = 40000
    d = fa.decide(DECL, res)
    assert d["overall_pass_all_27_mandatory"] and not all(
        e["meets_ucb_rule"] for e in d["sensitivity_evaluations"]
    )
    res[("a0", 5)]["rejections"]["K3"] = km + 1
    assert not fa.decide(DECL, res)["overall_pass_all_27_mandatory"]
    assert "FAIL (final for this prereg)" in fa.report(fa.decide(DECL, res))
