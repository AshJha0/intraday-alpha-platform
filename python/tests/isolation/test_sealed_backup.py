"""Sealed-partition backup: the manifest the partition identity writes and the
check a restore must pass (``sealed_service.py backup-manifest`` /
``verify-backup``), and the refusals ``restore_sealed.ps1`` makes before it
changes anything.  Synthetic partitions in tmp_path; nothing under
``C:\\iap-sealed`` is touched and no script is run elevated."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys

import pytest
from conftest import REPO_ROOT
from sealed_fixtures import load_tool

ss = load_tool("sealed_service_backup", "tools/sealed_service.py")
RESTORE = REPO_ROOT / "tools" / "admin" / "restore_sealed.ps1"


def _partition(root):
    """A small partition: one dataset and a three-line audit chain."""
    (root / "datasets" / "d1").mkdir(parents=True)
    (root / "datasets" / "d1" / "x.csv").write_bytes(b"secret,1\n")
    (root / "datasets" / "d1" / "y.csv").write_bytes(b"secret,2\n")
    audit = ss.Audit(root / "service" / "audit.jsonl")
    for e in ("start", "ingested", "start"):
        audit.append({"event": e, "utc": "2026-10-26T00:00:00+00:00"})
    return root


def test_manifest_is_deterministic_and_holds_no_content(tmp_path):
    root = _partition(tmp_path / "b")
    one, two = ss.backup_manifest(root, "b"), ss.backup_manifest(root, "b")
    assert one == two
    assert json.dumps(one, sort_keys=True) == json.dumps(two, sort_keys=True)
    assert one["file_count"] == 3 and one["audit_problems"] == []
    assert one["chain_head"] == ss.Audit(root / "service" / "audit.jsonl").head()
    assert "secret" not in json.dumps(one)
    # the same tree copied elsewhere gives the same manifest (relative names)
    copy = tmp_path / "copy"
    shutil.copytree(root, copy)
    assert ss.backup_manifest(copy, "b") == one


def test_a_faithful_copy_verifies(tmp_path):
    root = _partition(tmp_path / "a")
    m = ss.backup_manifest(root, "a")
    copy = tmp_path / "restored"
    shutil.copytree(root, copy)
    assert ss.backup_problems(copy, m, "a") == []


def test_chain_head_mismatch_is_refused(tmp_path):
    root = _partition(tmp_path / "c")
    m = ss.backup_manifest(root, "c")
    # the service ran once more after the backup: chain intact, head moved
    ss.Audit(root / "service" / "audit.jsonl").append({"event": "start"})
    problems = ss.backup_problems(root, m, "c")
    assert any(p.startswith("chain head") for p in problems)
    # a manifest naming a different head is refused the same way
    root2 = _partition(tmp_path / "c2")
    forged = {**ss.backup_manifest(root2, "c"), "chain_head": "f" * 64}
    assert any(p.startswith("chain head") for p in ss.backup_problems(root2, forged, "c"))


def test_file_changes_and_a_broken_chain_are_refused(tmp_path):
    root = _partition(tmp_path / "b")
    m = ss.backup_manifest(root, "b")
    (root / "datasets" / "d1" / "x.csv").write_bytes(b"secret,9\n")
    (root / "datasets" / "d1" / "y.csv").unlink()
    (root / "datasets" / "d1" / "z.csv").write_bytes(b"new\n")
    problems = ss.backup_problems(root, m, "b")
    assert "sha256 differs: datasets/d1/x.csv" in problems
    assert "missing: datasets/d1/y.csv" in problems
    assert "unexpected: datasets/d1/z.csv" in problems
    assert not any("secret" in p for p in problems)

    root2 = _partition(tmp_path / "b2")
    audit = root2 / "service" / "audit.jsonl"
    lines = audit.read_bytes().split(b"\n")
    lines[1] = lines[1].replace(b"ingested", b"ingestex")
    audit.write_bytes(b"\n".join(lines))
    assert ss.backup_manifest(root2, "b")["audit_problems"]
    assert ss.backup_problems(root2, m, "b")  # never a clean pass


def test_wrong_partition_schema_and_bad_source_are_refused(tmp_path):
    root = _partition(tmp_path / "a")
    m = ss.backup_manifest(root, "a")
    assert "not 'b'" in ss.backup_problems(root, m, "b")[0]
    assert "schema" in ss.backup_problems(root, {**m, "schema": "x"}, "a")[0]
    assert "broken audit" in ss.backup_problems(root, {**m, "audit_problems": ["x"]}, "a")[0]


def test_runner_partition_without_audit_chain(tmp_path):
    root = tmp_path / "runner"
    (root / "cache").mkdir(parents=True)
    (root / "cache" / "f.bin").write_bytes(b"\0\1")
    m = ss.backup_manifest(root, "runner")
    assert m["chain_head"] is None and m["file_count"] == 1
    assert ss.backup_problems(root, m, "runner") == []


def test_cli_writes_manifest_outside_partition_and_verifies(tmp_path, capsys):
    root = _partition(tmp_path / "a")
    inside = root / "m.json"
    assert (
        ss.main(["backup-manifest", "--partition", "a", "--root", str(root), "--out", str(inside)])
        == 2
    )
    assert not inside.exists()
    out = tmp_path / "out" / "a.json"
    assert (
        ss.main(["backup-manifest", "--partition", "a", "--root", str(root), "--out", str(out)])
        == 0
    )
    printed = capsys.readouterr().out
    assert "secret" not in printed and '"files": 3' in printed
    assert (
        ss.main(["verify-backup", "--partition", "a", "--root", str(root), "--manifest", str(out)])
        == 0
    )
    ss.Audit(root / "service" / "audit.jsonl").append({"event": "start"})
    assert (
        ss.main(["verify-backup", "--partition", "a", "--root", str(root), "--manifest", str(out)])
        == 1
    )


# ----------------------------------------------- restore_sealed.ps1 refusals --
def _backup_set(tmp_path, partition="a"):
    """A synthetic backup set in the layout backup_sealed.ps1 writes."""
    src = _partition(tmp_path / "src")
    bset = tmp_path / "set"
    shutil.copytree(src, bset / partition)
    (bset / "manifests").mkdir()
    mfile = bset / "manifests" / f"{partition}.json"
    m = ss.backup_manifest(src, partition)
    mfile.write_text(json.dumps(m, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    top = {
        "schema": "iap.sealed_backup_set/v1",
        "stamp": "20261026T000000Z",
        "failed": [],
        "partitions": {
            partition: {
                "manifest_sha256": hashlib.sha256(mfile.read_bytes()).hexdigest(),
                "file_count": m["file_count"],
                "chain_head": m["chain_head"],
            }
        },
    }
    (bset / "backup_manifest.json").write_text(json.dumps(top), encoding="utf-8")
    return bset


def _restore(bset, sealed_root, partition="a"):
    ps = shutil.which("powershell") or shutil.which("pwsh")
    if sys.platform != "win32" or ps is None:
        pytest.skip("restore_sealed.ps1 is Windows PowerShell")
    return subprocess.run(
        [
            ps,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(RESTORE),
            "-BackupSet",
            str(bset),
            "-SealedRoot",
            str(sealed_root),
            "-Partitions",
            partition,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
    )


def test_restore_into_non_empty_target_is_refused(tmp_path):
    bset = _backup_set(tmp_path)
    target = tmp_path / "sealed" / "a"
    target.mkdir(parents=True)
    (target / "keep.txt").write_text("do not overwrite", encoding="utf-8")
    r = _restore(bset, tmp_path / "sealed")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "is not empty" in r.stdout
    assert (target / "keep.txt").read_text(encoding="utf-8") == "do not overwrite"
    assert sorted(p.name for p in target.iterdir()) == ["keep.txt"]


def test_restore_refuses_a_swapped_manifest(tmp_path):
    bset = _backup_set(tmp_path)
    mfile = bset / "manifests" / "a.json"
    doc = json.loads(mfile.read_text(encoding="utf-8"))
    doc["chain_head"] = "f" * 64
    mfile.write_text(json.dumps(doc), encoding="utf-8")
    r = _restore(bset, tmp_path / "sealed")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "does not match the sha256" in r.stdout
    assert not (tmp_path / "sealed").exists()


def test_backup_scripts_use_backup_semantics_and_never_print_content():
    """Static guard: robocopy in backup mode with full security, no content dump."""
    for name in ("backup_sealed.ps1", "restore_sealed.ps1"):
        text = (REPO_ROOT / "tools" / "admin" / name).read_text(encoding="utf-8")
        assert "/B /COPYALL" in text
        for line in text.splitlines():
            code = line.split("#", 1)[0]
            if "Get-Content" in code:
                assert "manifest" in code.lower() or "$mfile" in code or "$topPath" in code, line
            for bad in ("Format-Hex", "type ", " cat "):
                assert bad not in code, line
