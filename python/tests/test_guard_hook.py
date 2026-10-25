"""v2.0 stage 2, D1: one choke point for every data read.

* ``iap.core.guard`` fails closed with no hook; the holdout registry registers
  itself as the hook; ``iap`` bootstraps a lazy hook.
* ``iap.io`` is the only module that calls the raw readers (AST scan).
* a raw read of a file inside a registered (spent) dataset through ``iap.io``
  is refused before pandas / pyarrow / numpy open it; the former
  ``IAP_HOLDOUT_AUTHORISED`` override is gone.
* ``iap.core`` never names ``iap.research`` (D-11 import contract).

Synthetic datasets in tmp_path, decided against the committed registry
(2025-12-08 AAPL is spent, 2019-01-30 is in-sample); nothing under data/real is read.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path

import pytest
from conftest import REPO_ROOT
from iap import io as iap_io
from iap.core import guard
from iap.research import holdout_registry as hr

IAP_DIR = REPO_ROOT / "python" / "src" / "iap"
SPENT_DAY = "2025-12-08"
IN_DAY = "2019-01-30"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv(hr.LOG_ENV, str(tmp_path / "access.jsonl"))
    monkeypatch.setattr(hr, "_AUTHORISED_ROOTS", set())
    yield
    guard.register(hr.reader_guard)  # whatever a test did, the suite keeps the real hook


def make(root: Path, name: str, day: str, *, kind: str = "real") -> Path:
    d = root / name
    (d / "normalized").mkdir(parents=True)
    (d / "features").mkdir()
    body = f"events {name} {day}".encode()
    fname = f"eq_{day.replace('-', '')}.normalized.iap1"
    (d / "normalized" / fname).write_bytes(body)
    (d / "features" / "features_1.parquet").write_bytes(b"not a parquet file")
    (d / "features" / "cache.npy").write_bytes(b"not a numpy file")
    sha = hashlib.sha256(body).hexdigest()
    man = {
        "kind": kind,
        "dataset_version": hr.content_hash({fname: sha}),
        "normalized": {"files": {fname: sha}},
        "sessions": {day: {}},
        "universe": [{"symbol": "AAPL"}],
    }
    (d / "dataset.json").write_text(json.dumps(man), encoding="utf-8")
    return d


# ------------------------------------------------------------ the hook itself --
def test_holdout_registry_is_the_registered_hook():
    assert guard.registered() is hr.reader_guard


def test_without_a_hook_real_data_is_refused_and_synthetic_passes(tmp_path):
    guard.reset()
    real = make(tmp_path, "real", IN_DAY)  # even IN-SAMPLE real data: no hook, no read
    with pytest.raises(guard.GuardRefused, match="no data-access hook"):
        guard.check(real / "features" / "features_1.parquet")
    syn = make(tmp_path, "syn", SPENT_DAY, kind="synthetic")
    guard.check(syn / "features" / "features_1.parquet")  # passes
    loose = tmp_path / "loose.parquet"
    loose.write_bytes(b"x")
    guard.check(loose)  # passes: not a dataset, not under data/real


def test_without_a_hook_anything_under_data_real_is_refused(tmp_path):
    guard.reset()
    p = tmp_path / "data" / "real" / "copies" / "whatever.parquet"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"x")
    with pytest.raises(guard.GuardRefused):
        guard.check(p)


def test_package_root_bootstraps_a_lazy_hook_that_hands_over_to_the_registry(tmp_path):
    import iap

    guard.reset()
    iap._bootstrap_guard()
    assert guard.registered() is not None and guard.registered() is not hr.reader_guard
    d = make(tmp_path, "fresh", SPENT_DAY)
    with pytest.raises(hr.HoldoutRefused):
        guard.check(d / "features" / "features_1.parquet", "test")
    # the lazy hook did import the registry; a later import registers the real hook
    assert hr.reader_guard is not None


def test_bootstrap_does_not_replace_an_installed_hook():
    import iap

    calls = []
    guard.register(lambda p, purpose: calls.append((p, purpose)))
    iap._bootstrap_guard()
    guard.check("anything", "x")
    assert calls == [(Path("anything"), "x")]


def test_env_override_is_gone(tmp_path, monkeypatch):
    assert not hasattr(hr, "AUTH_ENV")
    d = make(tmp_path, "fresh", SPENT_DAY)
    monkeypatch.setenv("IAP_HOLDOUT_AUTHORISED", str(hr.dataset_root(d)))
    with pytest.raises(hr.HoldoutRefused):
        iap_io.read_parquet(d / "features" / "features_1.parquet")
    for p in IAP_DIR.rglob("*.py"):  # no code reads the variable (prose may name it)
        for line in p.read_text(encoding="utf-8").splitlines():
            assert not ("IAP_HOLDOUT_AUTHORISED" in line and "environ" in line), (p, line)


def test_authorisation_is_in_process_only(tmp_path):
    d = make(tmp_path, "ok", IN_DAY)
    hr.guard(d, purpose="entry point")
    assert str(hr.dataset_root(d)) in hr._authorised()
    assert "IAP_HOLDOUT_AUTHORISED" not in os.environ  # nothing exported to children


# -------------------------------------------------------- iap.io refuses raw reads --
@pytest.mark.parametrize(
    "reader, rel, lib, attr",
    [
        ("read_parquet", "features/features_1.parquet", "pandas", "read_parquet"),
        ("parquet_file", "features/features_1.parquet", "pyarrow.parquet", "ParquetFile"),
        ("read_table", "features/features_1.parquet", "pyarrow.parquet", "read_table"),
        ("np_load", "features/cache.npy", "numpy", "load"),
    ],
)
def test_raw_read_of_a_registered_path_fails_before_the_library_opens_it(
    tmp_path, monkeypatch, reader, rel, lib, attr
):
    import importlib

    mod = importlib.import_module(lib)
    monkeypatch.setattr(mod, attr, lambda *a, **k: pytest.fail(f"{lib}.{attr} was called"))
    d = make(tmp_path, "renamed_in_sample_looking", SPENT_DAY)
    with pytest.raises(hr.HoldoutRefused):
        getattr(iap_io, reader)(d / rel)
    log = json.loads((tmp_path / "access.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert log["decision"] == "refused"


def test_in_sample_read_through_iap_io_reaches_the_library(tmp_path, monkeypatch):
    import pandas as pd

    seen = []
    monkeypatch.setattr(pd, "read_parquet", lambda p, *a, **k: seen.append(Path(p)) or "frame")
    d = make(tmp_path, "ok", IN_DAY)
    assert iap_io.read_parquet(d / "features" / "features_1.parquet") == "frame"
    assert seen == [d / "features" / "features_1.parquet"]


# ------------------------------------------------------------- static contracts --
RAW_READERS = {
    ("pd", "read_parquet"),
    ("pandas", "read_parquet"),
    ("pq", "ParquetFile"),
    ("pq", "read_table"),
    ("np", "load"),
    ("numpy", "load"),
}


def raw_reader_calls(source: str) -> list[tuple[int, str]]:
    out = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            base = node.func.value
            if isinstance(base, ast.Name) and (base.id, node.func.attr) in RAW_READERS:
                out.append((node.lineno, f"{base.id}.{node.func.attr}"))
            if (
                isinstance(base, ast.Attribute)
                and isinstance(base.value, ast.Name)
                and base.value.id == "pyarrow"
                and ("pq", node.func.attr) in RAW_READERS
            ):
                out.append((node.lineno, f"pyarrow.parquet.{node.func.attr}"))
    return out


def test_no_raw_reader_outside_iap_io():
    """Zero open routes (D1 acceptance): every parquet / numpy read in ``iap`` goes
    through ``iap.io`` and so through the choke point."""
    found = []
    for path in sorted(IAP_DIR.rglob("*.py")):
        if path == IAP_DIR / "io.py":
            continue
        for line, what in raw_reader_calls(path.read_text(encoding="utf-8")):
            found.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{line}: {what}")
    assert found == [], "raw reader outside iap.io (route the read through iap.io):\n" + "\n".join(
        found
    )


def test_scanner_sees_the_raw_forms():
    src = "import pandas as pd\nimport numpy as np\nimport pyarrow.parquet as pq\n"
    src += "a = pd.read_parquet(p)\nb = np.load(p)\nc = pq.ParquetFile(p)\nd = pq.read_table(p)\n"
    assert [w for _, w in raw_reader_calls(src)] == [
        "pd.read_parquet",
        "np.load",
        "pq.ParquetFile",
        "pq.read_table",
    ]
    assert raw_reader_calls("x = iap_io.read_parquet(p)\ny = np.loads(b)\n") == []


def test_core_never_imports_research():
    """D-11 / D1 import contract: ``iap.core`` is independent of ``iap.research``
    (the guard hook is registered from outside)."""
    found = []
    for path in sorted((IAP_DIR / "core").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for n in names:
                if n.startswith("iap.research") or n.startswith("iap.agents"):
                    found.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno}: {n}")
    assert found == [], "\n".join(found)


def test_codec_readers_use_the_choke_point():
    src = (IAP_DIR / "core" / "codec.py").read_text(encoding="utf-8")
    assert src.count("guard.check(") == 2
    assert "holdout_registry" not in src
