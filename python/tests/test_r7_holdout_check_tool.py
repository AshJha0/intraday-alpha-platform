"""tools/r7_holdout_check.py: signed prereg required by default; draft opt-in; ambiguity fails."""

from __future__ import annotations

import hashlib
import importlib.util
import json

import pytest
from conftest import REPO_ROOT

_spec = importlib.util.spec_from_file_location(
    "r7_holdout_check", REPO_ROOT / "tools/r7_holdout_check.py"
)
hc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hc)

PAYLOAD = b"itch-bytes" * 100


def _setup(tmp_path, signed=True, draft=True, extra_signed=False, payload=PAYLOAD):
    repo = tmp_path / "repo"
    r7 = repo / "research" / "r7"
    r7.mkdir(parents=True)
    data = repo / "data" / "real"
    data.mkdir(parents=True)
    gz = data / "S112825-v50.txt.gz"
    gz.write_bytes(payload)
    files = {
        "files": [
            {"file": gz.name, "bytes": len(PAYLOAD), "sha256": hashlib.sha256(PAYLOAD).hexdigest()}
        ]
    }
    sig = {"signed": True, "signature": {"sig": "x"}, "holdout_files": files}
    if signed:
        (r7 / "r7_confirm_prereg.json").write_text(json.dumps(sig), encoding="utf-8")
    if extra_signed:
        (r7 / "r7_confirm_prereg_v2.json").write_text(json.dumps(sig), encoding="utf-8")
    if draft:
        (r7 / "r7_confirm_prereg_DRAFT.json").write_text(
            json.dumps({"signed": False, "holdout_files": files}), encoding="utf-8"
        )
    return gz, data


@pytest.fixture(autouse=True)
def _plenty_of_disk(monkeypatch):
    # never depend on the real machine's free space
    monkeypatch.setattr(hc, "free_gb", lambda _p: 1000.0)


def test_signed_ok(tmp_path, capsys):
    gz, data = _setup(tmp_path)
    assert hc.main([str(gz), str(data), "--allow-draft"]) == 0
    assert hc.main([str(gz), str(data)]) == 0
    out = capsys.readouterr()
    assert "PRECHECK OK" in out.out and "r7_confirm_prereg.json" in out.out
    assert "WARNING" not in out.err


def test_draft_refused_by_default(tmp_path, capsys):
    gz, data = _setup(tmp_path, signed=False)
    assert hc.main([str(gz), str(data)]) == 1
    assert "no signed" in capsys.readouterr().out


def test_draft_allowed_with_warning(tmp_path, capsys):
    gz, data = _setup(tmp_path, signed=False)
    assert hc.main([str(gz), str(data), "--allow-draft"]) == 0
    out = capsys.readouterr()
    assert "WARNING" in out.err and "DRAFT" in out.out


def test_unsigned_nondraft_file_is_not_signed(tmp_path, capsys):
    gz, data = _setup(tmp_path, signed=False, draft=False)
    r7 = data.parent.parent / "research" / "r7"
    (r7 / "r7_confirm_prereg.json").write_text(json.dumps({"signed": False}), encoding="utf-8")
    assert hc.main([str(gz), str(data)]) == 1


def test_several_signed_fail(tmp_path, capsys):
    gz, data = _setup(tmp_path, extra_signed=True)
    assert hc.main([str(gz), str(data)]) == 1
    assert "several signed" in capsys.readouterr().out


def test_hash_and_size_mismatch(tmp_path, capsys):
    gz, data = _setup(tmp_path, payload=PAYLOAD[:-1] + b"X")
    assert hc.main([str(gz), str(data)]) == 1
    assert "sha256 mismatch" in capsys.readouterr().out
    gz.write_bytes(b"short")
    assert hc.main([str(gz), str(data)]) == 1
    assert "size mismatch" in capsys.readouterr().out


def test_low_disk(tmp_path):
    gz, data = _setup(tmp_path)
    try:
        hc.check(gz, data, data.parent.parent, min_free_gb=1e12)
    except hc.PrecheckError as e:
        assert "free disk" in str(e)
    else:
        raise AssertionError("expected PrecheckError")


def test_low_disk_main_fails(tmp_path, monkeypatch, capsys):
    gz, data = _setup(tmp_path)
    monkeypatch.setattr(hc, "free_gb", lambda _p: 10.0)
    assert hc.main([str(gz), str(data)]) == 1
    assert "free disk 10.0 GB < 25" in capsys.readouterr().out
