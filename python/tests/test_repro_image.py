"""v2.0 stage 3 P1: the pinned reproduction image and what it reproduces.

* ``analysis_identity`` binds the image digest (``image_digest``) and checks it
  against ``IAP_IMAGE_DIGEST``; a block without it verifies as before; the
  same block verifies at a relocated POSIX root (the container's ``/work``).
* Runners write relative paths (``execution_benchmark.portable_path``).
* The committed B8 v2 decision and report re-derive byte for byte from the
  committed benchmark JSONs; the committed REL-08 scorecard re-renders byte for
  byte from the committed screen JSON with the renderer at its own commit.
* The image entrypoint (``deployment/docker/repro_entrypoint.py``) resolves the
  runner from the identity, refuses on mismatch under ``--require-identity``,
  and ``--code-at`` runs the tree of an earlier commit.
* The image-inputs hash follows the Dockerfile; the comparison tool's outcomes.

No market data is read; no docker is needed (the image itself is built and
exercised by ``.github/workflows/repro-image.yml``).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import REPO_ROOT
from iap.agents import analysis_identity as ai
from iap.research.execution_benchmark import portable_path

DIGEST = "sha256:" + "ab" * 32
OTHER = "sha256:" + "cd" * 32
KW = dict(data_versions=["v1"], family="F", metrics=["m"], thresholds={"t": 1.0})
ENTRYPOINT = REPO_ROOT / "deployment" / "docker" / "repro_entrypoint.py"
TOOLS = REPO_ROOT / "tools" / "repro"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(root: Path, *a: str) -> str:
    p = subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True, text=True)
    return p.stdout.strip()


def _w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))


@pytest.fixture
def repo(tmp_path):
    """A small checkout with the REAL ``iap`` package (the entrypoint imports
    it from the mounted checkout) and one bound runner ``run_study.py``."""
    r = tmp_path / "repo"
    shutil.copytree(
        REPO_ROOT / "python" / "src" / "iap",
        r / "python" / "src" / "iap",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    _w(r / "configs/reference/fee_tiers.json", '{"taker": 0.3}\n')
    _w(r / "data/reference/feature_registry.json", '{"ofi": 1}\n')
    _w(r / "python/requirements-ci.txt", "pytest==8.0.0 \\\n    --hash=sha256:00\n")
    _w(
        r / "research/study/run_study.py",
        "import sys\nprint('ran', ' '.join(sys.argv[1:]))\n"
        "from pathlib import Path\n"
        "Path(sys.argv[sys.argv.index('--out') + 1]).mkdir(parents=True, exist_ok=True)\n"
        "(Path(sys.argv[sys.argv.index('--out') + 1]) / 'DECISION.json').write_text('{}')\n",
    )
    _w(r / "tools/other.py", "print('other')\n")
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    return r


def _prereg(r: Path, **extra) -> Path:
    block = ai.build_identity(r, scripts=["research/study/run_study.py"], **KW, **extra)
    doc = {"x-version": 1, "kind": "analysis_prereg", "signed": False, "analysis_identity": block}
    p = r / "research/preregs/study.json"
    _w(p, json.dumps(doc, indent=2, sort_keys=True) + "\n")
    return p


# ------------------------------------------------------------ identity --
def test_image_digest_is_bound_and_checked_against_the_running_image(repo):
    b = ai.build_identity(repo, scripts=["research/study/run_study.py"], image_digest=DIGEST, **KW)
    assert b["image_digest"] == DIGEST and b["identity_version"] == 2
    bad = ai.environment_mismatches(repo, b, env={})
    assert len(bad) == 1 and "IAP_IMAGE_DIGEST" in bad[0] and "not running inside" in bad[0]
    bad = ai.environment_mismatches(repo, b, env={"IAP_IMAGE_DIGEST": OTHER})
    assert len(bad) == 1 and "is not the bound" in bad[0]
    assert ai.environment_mismatches(repo, b, env={"IAP_IMAGE_DIGEST": DIGEST}) == []
    with pytest.raises(ai.IdentityError, match="reproduction image"):
        ai.verify_identity(repo, b, env={}, **KW)
    assert (
        ai.verify_identity(repo, b, env={"IAP_IMAGE_DIGEST": DIGEST}, **KW)["identity_version"] == 2
    )


def test_block_without_image_digest_verifies_as_before(repo):
    b = ai.build_identity(repo, scripts=["research/study/run_study.py"], **KW)
    assert "image_digest" not in b
    assert ai.environment_mismatches(repo, b, env={}) == []
    assert ai.environment_mismatches(repo, b, env={"IAP_IMAGE_DIGEST": OTHER}) == []


def test_malformed_image_digest_is_refused(repo):
    with pytest.raises(ai.IdentityError, match="sha256:<64 hex>"):
        ai.build_identity(
            repo, scripts=["research/study/run_study.py"], image_digest="latest", **KW
        )
    assert ai.read_image_digest(repo) is None
    _w(repo / ai.IMAGE_DIGEST_REL, DIGEST + "\n")
    assert ai.read_image_digest(repo) == DIGEST
    _w(repo / ai.IMAGE_DIGEST_REL, "sha256:zz\n")
    with pytest.raises(ai.IdentityError, match="one line"):
        ai.read_image_digest(repo)


def test_identity_verifies_at_a_relocated_posix_root(repo, tmp_path):
    """The container mounts the checkout at /work: every path in the block is
    repo-relative, so the block built at one root verifies at another."""
    b = ai.build_identity(repo, scripts=["research/study/run_study.py"], **KW)
    clone = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(repo), str(clone)], check=True)
    posix_root = Path(clone.as_posix())
    assert ai.verify_identity(posix_root, b, **KW)["commit"] == b["commit"]
    assert ai.environment_mismatches(posix_root, b, env={}) == []


def test_binder_takes_the_digest_from_the_checkout(repo, monkeypatch):
    """``tools/prereg_bind_identity.py bind`` binds deployment/docker/IMAGE_DIGEST
    by default, nothing with --no-image-digest, the flag's value otherwise."""
    pbi = _load(REPO_ROOT / "tools" / "prereg_bind_identity.py", "pbi_repro")
    _w(repo / "research/study/PROTOCOL.md", "# protocol\n")
    _w(repo / ai.IMAGE_DIGEST_REL, DIGEST + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "digest")
    base = [
        "--root", str(repo), "bind", "--protocol", "research/study/PROTOCOL.md",
        "--out", "research/preregs/p.json", "--scripts", "research/study/run_study.py",
        "--data-version", "v1", "--family", "F", "--metric", "m", "--thresholds", "{}",
        "--dry-run",
    ]  # fmt: skip
    out = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a))))
    assert pbi.main(base) == 0
    assert json.loads(out[-1])["analysis_identity"]["image_digest"] == DIGEST
    out.clear()
    assert pbi.main([*base, "--no-image-digest"]) == 0
    assert "image_digest" not in json.loads(out[-1])["analysis_identity"]
    out.clear()
    assert pbi.main([*base, "--image-digest", OTHER]) == 0
    assert json.loads(out[-1])["analysis_identity"]["image_digest"] == OTHER


# ------------------------------------------------------- relative paths --
def test_runners_write_portable_paths(tmp_path, monkeypatch):
    assert portable_path(REPO_ROOT / "research" / "execution" / "b8") == "research/execution/b8"
    monkeypatch.chdir(REPO_ROOT)  # a relative input resolves against the working directory
    assert portable_path("research/execution/b8/calibration_train3") == (
        "research/execution/b8/calibration_train3"
    )
    monkeypatch.chdir(tmp_path)
    assert portable_path(tmp_path / "data" / "real" / "x") == "data/real/x"
    outside = portable_path(Path(tmp_path.anchor) / "elsewhere" / "y")
    assert "\\" not in outside and outside.endswith("elsewhere/y")


# --------------------------------------------------------- re-derivation --
def test_b8v2_decision_and_report_rederive_byte_for_byte(tmp_path):
    """Tier A of the F7 guide, in the suite: `analyse` on the two committed
    headline JSONs reproduces DECISION.json and REPORT.md exactly."""
    b8 = REPO_ROOT / "research" / "execution" / "b8v2"
    out = tmp_path / "b8v2"
    out.mkdir()
    for sym in ("AAPL", "MSFT"):
        shutil.copy(b8 / f"benchmark_v2_oos3_{sym}.json", out)
    runner = _load(b8 / "run_b8v2.py", "run_b8v2_repro")
    assert runner.main(["analyse", "--out", str(out)]) == 0
    for name in ("DECISION.json", "REPORT.md"):
        assert (out / name).read_bytes() == (b8 / name).read_bytes(), name


def test_rel08_scorecard_rerenders_byte_for_byte_at_its_own_commit(tmp_path):
    """The committed scorecard was written by the renderer at the commit that
    committed it; a later commit changed the scorecard's units and precision,
    so HEAD's renderer presents the same numbers differently.  The
    reproduction renders with the renderer at the scorecard's commit
    (iap-repro --code-at), which must be byte-identical."""
    rel08 = REPO_ROOT / "research" / "v118" / "rel08"
    md = rel08 / "SCORECARD_ds_20190130.md"
    commit = _git(REPO_ROOT, "log", "-1", "--format=%H", "--", str(md.relative_to(REPO_ROOT)))
    if not commit:
        pytest.skip("shallow checkout: the scorecard's commit is not available")
    export = tmp_path / "at"
    export.mkdir()
    tar = tmp_path / "code.tar"
    with tar.open("wb") as fh:
        subprocess.run(
            ["git", "-C", str(REPO_ROOT), "archive", "--format=tar", commit, "python/src"],
            stdout=fh,
            check=True,
        )
    import tarfile

    with tarfile.open(tar) as tf:
        tf.extractall(export, filter="data")
    env = {**os.environ, "PYTHONPATH": str(export / "python" / "src"), "PYTHONUTF8": "1"}
    out = tmp_path / "rel08"
    subprocess.run(
        [
            sys.executable,
            str(TOOLS / "render_rel08_scorecard.py"),
            "--screen",
            str(rel08 / "screen_ds_20190130.json"),
            "--out",
            str(out),
        ],
        check=True,
        env=env,
        capture_output=True,
    )
    assert (out / "SCORECARD_ds_20190130.md").read_bytes() == md.read_bytes()


# -------------------------------------------------------------- entrypoint --
def _ep(root: Path, *args: str, record: Path | None = None, env: dict | None = None):
    e = {k: v for k, v in os.environ.items() if k != "IAP_IMAGE_DIGEST"}
    e.update(env or {})
    if record is not None:
        e["IAP_REPRO_RECORD"] = str(record)
    return subprocess.run(
        [sys.executable, str(ENTRYPOINT), "--root", str(root), *args],
        capture_output=True,
        text=True,
        env=e,
    )


def test_entrypoint_resolves_the_bound_runner_and_runs_it(repo, tmp_path):
    _prereg(repo)
    rec = tmp_path / "rec.json"
    p = _ep(repo, "research/preregs/study.json", "--", "--out", str(tmp_path / "o"), record=rec)
    assert p.returncode == 0, p.stderr
    assert "ran --out" in p.stdout and (tmp_path / "o" / "DECISION.json").is_file()
    r = json.loads(rec.read_text())
    assert r["runner"] == "research/study/run_study.py" and r["runner_bound"] is True
    assert r["mismatches"] == [] and r["actor"] == "iap-repro" and r["code_at"] is None


def test_entrypoint_refuses_identity_mismatch_only_when_required(repo, tmp_path):
    _prereg(repo)
    _w(repo / "python/src/iap/_planted.py", "X = 2\n")  # the code tree changed after binding
    rec = tmp_path / "rec.json"
    p = _ep(repo, "--list", "research/preregs/study.json", record=rec)
    assert p.returncode == 0 and "identity mismatch: python/src/iap code tree" in p.stdout
    assert json.loads(rec.read_text())["mismatches"]
    p = _ep(repo, "--list", "--require-identity", "research/preregs/study.json")
    assert p.returncode == 2 and "REFUSED" in p.stderr


def test_entrypoint_checks_the_image_digest_and_refuses_signing_material(repo, tmp_path):
    _prereg(repo, image_digest=DIGEST)
    p = _ep(repo, "--list", "--require-identity", "research/preregs/study.json")
    assert p.returncode == 2 and "IAP_IMAGE_DIGEST" in p.stdout
    p = _ep(
        repo, "--list", "--require-identity", "research/preregs/study.json",
        env={"IAP_IMAGE_DIGEST": DIGEST},
    )  # fmt: skip
    assert p.returncode == 0, p.stdout + p.stderr
    p = _ep(repo, "--list", "research/preregs/study.json", env={"IAP_SIGNING_KEY": "00"})
    assert p.returncode == 2 and "signing key material" in p.stderr


def test_entrypoint_needs_a_runner_it_can_name(repo, tmp_path):
    _w(repo / "research/study/run_other.py", "print('two')\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "two runners")
    block = ai.build_identity(
        repo, scripts=["research/study/run_study.py", "research/study/run_other.py"], **KW
    )
    _w(
        repo / "research/preregs/two.json",
        json.dumps({"signed": False, "analysis_identity": block}) + "\n",
    )
    p = _ep(repo, "--list", "research/preregs/two.json")
    assert p.returncode == 2 and "pass --runner" in p.stderr
    rec = tmp_path / "rec.json"
    p = _ep(repo, "--runner", "tools/other.py", "research/preregs/two.json", record=rec)
    assert p.returncode == 0 and "other" in p.stdout
    assert json.loads(rec.read_text())["runner_bound"] is False


def test_entrypoint_code_at_runs_the_tree_of_an_earlier_commit(repo, tmp_path):
    _prereg(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "prereg")
    bound = _git(repo, "rev-parse", "HEAD")
    _w(repo / "research/study/run_study.py", "print('changed runner')\n")
    _w(repo / "python/src/iap/_planted.py", "X = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "later")
    p = _ep(repo, "--list", "--require-identity", "research/preregs/study.json")
    assert p.returncode == 2
    rec = tmp_path / "rec.json"
    p = _ep(
        repo, "--code-at", bound, "--require-identity", "research/preregs/study.json",
        "--", "--out", str(tmp_path / "o"), record=rec,
    )  # fmt: skip
    assert p.returncode == 0, p.stdout + p.stderr
    assert "ran --out" in p.stdout and "changed runner" not in p.stdout
    assert json.loads(rec.read_text())["code_at"] == bound
    p = _ep(repo, "--code-at", "0" * 40, "--list", "research/preregs/study.json")
    assert p.returncode == 2 and "not a commit" in p.stderr


# ------------------------------------------------------------ image inputs --
def test_image_inputs_follow_the_dockerfile(tmp_path):
    ii = _load(TOOLS / "image_inputs.py", "image_inputs_repro")
    files = ii.input_files()
    assert "deployment/docker/Dockerfile.repro" in files
    assert "python/requirements-ci.txt" in files
    assert "deployment/docker/repro_entrypoint.py" in files
    for rel in files:
        assert (REPO_ROOT / rel).is_file(), rel
    h = ii.inputs_hash()
    for rel in files:
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO_ROOT / rel, dst)
    assert ii.inputs_hash(tmp_path) == h
    (tmp_path / "python/requirements-ci.txt").write_bytes(b"numpy==1.0\n")
    assert ii.inputs_hash(tmp_path) != h
    df = REPO_ROOT / "deployment/docker/Dockerfile.repro"
    text = df.read_text(encoding="utf-8")
    assert re.search(r"^FROM python:3\.12\.10-slim-bookworm@sha256:[0-9a-f]{64}$", text, re.M)
    assert "USER iap" in text and "--network none" in text


def test_image_digest_of_record_is_one_line():
    p = REPO_ROOT / ai.IMAGE_DIGEST_REL
    if not p.is_file():
        pytest.skip("deployment/docker/IMAGE_DIGEST not recorded yet (repro-image.yml writes it)")
    assert ai.read_image_digest(REPO_ROOT) is not None
    assert p.read_bytes().count(b"\n") == 1


# -------------------------------------------------------------- comparison --
def test_comparison_outcomes(tmp_path):
    cr = _load(TOOLS / "compare_reproduction.py", "compare_repro")
    tol = {"bps": 0.01, "fraction": 0.0001, "counts": 0}
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text('{"cost_bps": 1.0, "fill_rate": 0.5, "n": 3}\n')
    b.write_text('{"cost_bps": 1.0, "fill_rate": 0.5, "n": 3}\n')
    assert cr.compare(a, b, tol)["outcome"] == "byte-identical"
    b.write_text('{"cost_bps": 1.005, "fill_rate": 0.50005, "n": 3}\n')
    row = cr.compare(a, b, tol)
    assert row["outcome"] == "within-tolerance" and len(row["differences"]) == 2
    b.write_text('{"cost_bps": 1.05, "fill_rate": 0.5, "n": 3}\n')
    assert cr.compare(a, b, tol)["outcome"] == "differs"
    b.write_text('{"cost_bps": 1.0, "fill_rate": 0.5, "n": 4}\n')
    assert "counts exact" in cr.compare(a, b, tol)["differences"][0]
    m1 = tmp_path / "x.md"
    m2 = tmp_path / "y.md"
    m1.write_text("a\nb\n")
    m2.write_text("a\nc\n")
    assert cr.compare(m1, m2, tol)["outcome"] == "differs"
    rc = cr.main([f"{a}={a}", "--out", str(tmp_path / "rep"), "--informational", f"{m1}={m2}"])
    assert rc == 0
    rep = json.loads((tmp_path / "rep" / "reproduction_report.json").read_text())
    assert rep["status"] == "reproduced in the pinned image by CI"
    assert rep["informational"][0]["outcome"] == "differs"
    assert cr.main([f"{m1}={m2}", "--out", str(tmp_path / "rep2")]) == 1
