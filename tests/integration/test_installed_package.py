"""Integration level: the *installed* package, outside the checkout.

The Docker images (``deployment/docker/Dockerfile.python``) ``pip install
./python`` non-editably and document ``python3 -m iap.mvp run --repo-root
/app``; until 2026-09-20 that command failed in the image because the
schemas were resolved relative to the checkout only (review finding 4).
This test reproduces the image's situation without Docker: a fresh venv, a
non-editable install, and ``python -m iap.mvp run`` on ``mvp_tiny.json``
from an unrelated working directory —

* with the wheel's packaged schemas alone (``iap/_schemas``, copied by
  ``python/setup.py`` at build time; no ``$IAP_SCHEMA_DIR``), asserting the
  packaged copy equals ``schemas/`` byte for byte;
* with ``$IAP_SCHEMA_DIR`` pointing at the checkout, as the image sets it;
* with ``$IAP_SCHEMA_DIR`` pointing nowhere, which must fail closed.

Skips cleanly only when a venv cannot be created or pip is unavailable in
it; the install uses ``--system-site-packages`` + ``--no-deps`` so it needs
no network (the research stack is already installed for the suite).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest

SCHEMA_SUFFIXES = (".json", ".sql", ".md")


def _schema_tree(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*"))
            if p.is_file() and p.suffix in SCHEMA_SUFFIXES and "__pycache__" not in p.parts}


@pytest.fixture(scope="module")
def installed(repo_root, tmp_path_factory) -> dict:
    """A non-editable install of ``python/`` in a fresh venv."""
    base = tmp_path_factory.mktemp("installed")
    venv_dir = base / "venv"
    try:
        venv.EnvBuilder(system_site_packages=True, with_pip=True, clear=True).create(venv_dir)
    except Exception as exc:  # noqa: BLE001 - ensurepip may be missing on some hosts
        pytest.skip(f"cannot create a venv here: {exc}")
    python = venv_dir / ("Scripts" if os.name == "nt" else "bin") / "python"
    probe = subprocess.run([str(python), "-m", "pip", "--version"],
                           capture_output=True, text=True, check=False)
    if probe.returncode != 0:
        pytest.skip(f"pip unavailable in the venv: {probe.stderr.strip()}")
    install = subprocess.run(
        [str(python), "-m", "pip", "install", "--no-deps", "--no-build-isolation",
         "--no-cache-dir", "--quiet", str(repo_root / "python")],
        capture_output=True, text=True, check=False, cwd=str(base))
    assert install.returncode == 0, install.stderr[-3000:]
    where = subprocess.run(
        [str(python), "-c", "import iap, pathlib; print(pathlib.Path(iap.__file__).parent)"],
        capture_output=True, text=True, check=True, cwd=str(base)).stdout.strip()
    site = Path(where)
    assert venv_dir in site.parents, f"iap resolved outside the venv: {site}"
    assert not (repo_root / "python" / "src") in site.parents
    return {"python": python, "site": site, "cwd": base}


def _run(installed: dict, args, env_extra=None, cwd=None):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONHASHSEED": "7"}
    env.update(env_extra or {})
    return subprocess.run([str(installed["python"]), "-m", "iap.mvp", *args],
                          cwd=str(cwd or installed["cwd"]), capture_output=True, text=True,
                          env=env, check=False)


def test_wheel_ships_the_repository_schemas_byte_for_byte(repo_root, installed):
    packaged = installed["site"] / "_schemas"
    assert packaged.is_dir(), "python/setup.py did not copy schemas/ into the wheel"
    assert _schema_tree(packaged) == _schema_tree(repo_root / "schemas")
    assert (packaged / "sql" / "iap_v1.sql").read_bytes() == \
        (repo_root / "schemas" / "sql" / "iap_v1.sql").read_bytes()
    resolved = subprocess.run(
        [str(installed["python"]), "-c",
         "from iap.contracts.versions import schema_dir; print(schema_dir())"],
        capture_output=True, text=True, check=True, cwd=str(installed["cwd"]),
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")}).stdout.strip()
    assert Path(resolved) == packaged


def test_mvp_runs_from_the_installed_package(repo_root, installed, tmp_path):
    cfg = repo_root / "configs" / "mvp" / "mvp_tiny.json"
    out_packaged = tmp_path / "packaged"
    run = _run(installed, ["run", "--repo-root", str(repo_root), "--config", str(cfg),
                           "--out", str(out_packaged)])
    assert run.returncode == 0, run.stderr[-3000:]
    assert run.stdout.startswith("mvp run ")
    for name in ("traces.jsonl", "iap.sqlite", "report.json", "paper_evidence.json"):
        assert (out_packaged / name).is_file(), name

    # The image sets IAP_SCHEMA_DIR=/app/schemas: same run, same bytes.
    out_env = tmp_path / "env"
    run_env = _run(installed, ["run", "--repo-root", str(repo_root), "--config", str(cfg),
                               "--out", str(out_env)],
                   env_extra={"IAP_SCHEMA_DIR": str(repo_root / "schemas")})
    assert run_env.returncode == 0, run_env.stderr[-3000:]
    a = json.loads((out_packaged / "report.json").read_text())
    b = json.loads((out_env / "report.json").read_text())
    assert a["trace"]["digest"] == b["trace"]["digest"]
    assert a["pnl"] == b["pnl"]
    assert (out_packaged / "traces.jsonl").read_bytes() == (out_env / "traces.jsonl").read_bytes()

    # An explicit override that does not exist fails closed, never falls through.
    bad = _run(installed, ["run", "--repo-root", str(repo_root), "--config", str(cfg),
                           "--out", str(tmp_path / "bad")],
               env_extra={"IAP_SCHEMA_DIR": str(tmp_path / "nowhere")})
    assert bad.returncode != 0
    assert "schema directory not found" in bad.stderr
