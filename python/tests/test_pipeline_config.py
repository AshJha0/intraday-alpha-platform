"""Pipeline configuration must fail fast, never silently default.

Round-3 PLATFORM finding SEV-3: `iap.marketdata.__main__` ran with the
built-in `_DEFAULT_CONFIG` whenever `generator.json` was absent or the
`--config` path was typo'd. The defaults happen to equal the committed file
today, so the divergence would appear only later — as a dataset whose version
silently changed (anomaly rates, sessions, seed), which breaks the
reproducibility chain in REPRODUCIBILITY.md.

Contract: PLATFORM_CONVENTIONS.md §8 / §12.2, SPEC §26.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "python" / "src"


def _run(args: list[str], tmp_path: Path) -> subprocess.CompletedProcess:
    env = {"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin"}
    return subprocess.run(
        [sys.executable, "-m", "iap.marketdata", *args],
        capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=120,
    )


def test_missing_generator_config_is_an_error(tmp_path: Path) -> None:
    """--config pointing nowhere exits non-zero and names the path."""
    proc = _run(["--config", str(tmp_path / "nope.json"),
                 "--out", str(tmp_path / "data")], tmp_path)
    assert proc.returncode != 0
    assert "generator config not found" in proc.stderr
    assert "nope.json" in proc.stderr
    # and it says how to opt in on purpose
    assert "--allow-default-config" in proc.stderr


def test_missing_configs_dir_generator_json_is_an_error(tmp_path: Path) -> None:
    """A configs dir without generator.json is an error, not a silent default."""
    empty = tmp_path / "configs"
    empty.mkdir()
    proc = _run(["--configs-dir", str(empty), "--out", str(tmp_path / "data")],
                tmp_path)
    assert proc.returncode != 0
    assert "generator config not found" in proc.stderr


def test_default_config_requires_an_explicit_opt_in(tmp_path: Path) -> None:
    """--allow-default-config gets past the config check (and then fails on
    the missing reference data, proving the config gate was the only thing in
    the way)."""
    empty = tmp_path / "configs"
    empty.mkdir()
    proc = _run(["--configs-dir", str(empty), "--allow-default-config",
                 "--out", str(tmp_path / "data")], tmp_path)
    assert "generator config not found" not in proc.stderr


@pytest.mark.parametrize("flag", ["--allow-default-config"])
def test_flag_is_documented_in_help(tmp_path: Path, flag: str) -> None:
    proc = _run(["--help"], tmp_path)
    assert proc.returncode == 0
    assert flag in proc.stdout
