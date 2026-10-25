"""Result artefacts over 1 MB live as release assets, not tree files (v2.0 stage 11, S1).

``research/v118/RESULT_ARTEFACTS.json`` is the manifest; ``tools/result_artefacts.py``
checks and fetches.  Pins: the manifest is well formed; a local copy must match
its sha256 (a corrupted copy fails ``check``); and no NEW tracked file over 1 MB
appears under ``research/`` -- the pre-existing ones are listed here by name so
that the next large result goes to a release asset with its hash in the
manifest instead of into the pack.
"""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import REPO_ROOT

_SPEC = importlib.util.spec_from_file_location(
    "result_artefacts", REPO_ROOT / "tools" / "result_artefacts.py"
)
ra = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(ra)

ONE_MB = 1 << 20

#: Tracked files over 1 MB that predate the rule (v1.17-v1.18 execution benchmarks, the
#: AAPL benchmark slice, REL-10 trips, the power report).  Reproduction records cite them
#: in place; they are grandfathered, not endorsed.  Anything else over 1 MB under research/
#: must be a release asset listed in the manifest.
GRANDFATHERED = frozenset(
    {
        "research/execution/rel04c/benchmark/benchmark_rel04c_L1.json",
        "research/execution/rel04c/benchmark/benchmark_rel04c_L2.json",
        "research/execution/rel04c/benchmark/benchmark_rel04c_L3.json",
        "research/execution/b8v2/benchmark_v2_oos3_s2_child_impact_AAPL.json",
        "research/execution/b8v2/benchmark_v2_oos3_AAPL.json",
        "research/execution/b8v2/benchmark_v2_oos3_s1_no_hidden_AAPL.json",
        "research/execution/b8v2/benchmark_v2_oos3_s3_v117_child_AAPL.json",
        "research/execution/b8v2/benchmark_v2_oos3_s2_child_impact_MSFT.json",
        "research/execution/b8v2/benchmark_v2_oos3_MSFT.json",
        "research/execution/b8v2/benchmark_v2_oos3_s1_no_hidden_MSFT.json",
        "research/execution/b8v2/benchmark_v2_oos3_s3_v117_child_MSFT.json",
        "research/v118/rel10/trips_evaluate.json",
        "research/power/extended/POWER_REPORT.json",
        "research/execution/benchmark/benchmark_multi7_signal.json",
    }
)


def test_manifest_is_well_formed():
    entries = ra.load()
    paths = [e["path"] for e in entries]
    assert len(paths) == len(set(paths))
    assert "research/v118/known_null_result.json" in paths
    assert "research/v118/known_null_v2_result.json" in paths
    for e in entries:
        assert e["release"].startswith("v")
        assert e["bytes"] > ONE_MB
        # the result document that quotes the hash exists and names the file
        doc = REPO_ROOT / e["result_document"]
        assert doc.is_file(), e["result_document"]
        assert Path(e["path"]).name in doc.read_text(encoding="utf-8")


def test_manifest_rejects_a_bad_hash(tmp_path):
    bad = tmp_path / "m.json"
    bad.write_text(
        '{"x-version": 1, "artefacts": [{"path": "x.json", "sha256": "abc", "bytes": 5, "release": "v1", "asset": "x.json"}]}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="64 lowercase hex"):
        ra.load(bad)


def test_check_passes_a_matching_copy_and_fails_a_corrupted_one(tmp_path, capsys):
    data = b"result" * 1000
    (tmp_path / "research" / "v118").mkdir(parents=True)
    f = tmp_path / "research" / "v118" / "r.json"
    f.write_bytes(data)
    entry = {
        "path": "research/v118/r.json",
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "release": "v0",
        "asset": "r.json",
    }
    assert ra.check([entry], tmp_path) == 0
    assert "OK" in capsys.readouterr().out
    f.write_bytes(data + b"!")
    assert ra.check([entry], tmp_path) == 1
    assert "MISMATCH" in capsys.readouterr().out
    # an absent copy is reported, not a failure (the file is a release asset)
    f.unlink()
    assert ra.check([entry], tmp_path) == 0
    assert "absent" in capsys.readouterr().out


def test_the_moved_blobs_are_not_tracked_and_are_ignored():
    tracked = subprocess.run(
        [
            "git",
            "ls-files",
            "research/v118/known_null_result.json",
            "research/v118/known_null_v2_result.json",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert tracked == []
    ignored = subprocess.run(
        ["git", "check-ignore", "research/v118/known_null_result.json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert ignored.returncode == 0, "a rerun must not re-commit the blob"


@pytest.mark.skipif(
    sys.platform == "win32" and not (REPO_ROOT / ".git").exists(), reason="needs the checkout"
)
def test_no_new_tracked_file_over_1mb_under_research():
    out = subprocess.run(
        ["git", "ls-files", "-z", "research"], cwd=REPO_ROOT, capture_output=True
    ).stdout
    big = []
    for rel in out.decode("utf-8").split("\0"):
        if not rel:
            continue
        p = REPO_ROOT / rel
        if p.is_file() and p.stat().st_size > ONE_MB:
            big.append(rel)
    manifest_paths = {e["path"] for e in ra.load()}
    offenders = sorted(set(big) - GRANDFATHERED - manifest_paths)
    assert offenders == [], (
        "new result file(s) over 1 MB in the tree; publish as a release asset and list in "
        f"research/v118/RESULT_ARTEFACTS.json: {offenders}"
    )
