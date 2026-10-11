"""R7 data preparation (v1.13): candidate manifests, the resumable batch
wrapper, download verification, recorded sampling and the gated downloader."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json

import pytest
from conftest import REPO_ROOT
from iap.marketdata import r7plan
from iap.marketdata.__main__ import main as md_main
from iap.validation.sessions import day_tags

CANDIDATES = REPO_ROOT / "research" / "r7" / "r7_candidates.json"


def _manifest(tmp_path, days, symbols="AAPL,MSFT,QQQ"):
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"symbols": symbols, "days": days}))
    return p


def _day(date="2025-12-08", role="holdout", data=b"x" * 10, **kw):
    return {
        "date": date,
        "file": f"f{date}.gz",
        "url": f"https://emi.nasdaq.com/ITCH/f{date}.gz",
        "bytes": len(data),
        "sha256": None,
        "role": role,
        **kw,
    }


def test_committed_candidates_are_valid_unseen_and_tagged():
    doc = r7plan.load_candidates(CANDIDATES)
    dates = [d["date"] for d in doc["days"]]
    assert len(dates) == 10 and not set(dates) & r7plan.SPENT_DAYS
    assert round(sum(d["bytes"] for d in doc["days"]) / 1e9, 2) == 88.75
    assert "fomc" in day_tags("2025-12-10") and "holiday_thin" in day_tags("2025-11-28")


@pytest.mark.parametrize(
    "bad,msg",
    [
        (_day(date="2025-12-06"), "weekend"),
        (_day(role="oos"), "role"),
        (_day(date="2026-05-18"), "SPENT"),
        (_day(url="https://evil.example/x.gz"), "allowlist"),
        (_day(url="http://emi.nasdaq.com/x.gz"), "allowlist"),
        (_day(file="../x.gz"), "bare file"),
        (_day(bytes=0), "positive"),
    ],
)
def test_manifest_validation_fails_closed(tmp_path, bad, msg):
    with pytest.raises(ValueError, match=msg):
        r7plan.load_candidates(_manifest(tmp_path, [bad]))


def test_spent_day_may_be_insample_but_duplicates_are_rejected(tmp_path):
    r7plan.load_candidates(_manifest(tmp_path, [_day(date="2026-05-18", role="insample")]))
    with pytest.raises(ValueError, match="duplicate"):
        r7plan.load_candidates(_manifest(tmp_path, [_day(), _day()]))


def test_verify_file_size_and_sha256(tmp_path):
    f = tmp_path / "a.gz"
    f.write_bytes(b"abc")
    sha = hashlib.sha256(b"abc").hexdigest()
    assert r7plan.verify_file(f, 3, sha)["ok"]
    assert r7plan.verify_file(f, 4)["reason"] == "size mismatch"
    assert r7plan.verify_file(f, 3, "0" * 64)["reason"] == "sha256 mismatch"
    assert r7plan.verify_file(tmp_path / "no.gz", 3)["reason"] == "missing"


def test_verify_download_cli_exit_codes(tmp_path, capsys):
    m = _manifest(tmp_path, [_day()])
    vendor = tmp_path / "v"
    vendor.mkdir()
    assert md_main(["verify-download", "--manifest", str(m), "--vendor-dir", str(vendor)]) == 1
    (vendor / "f2025-12-08.gz").write_bytes(b"x" * 10)
    assert md_main(["verify-download", "--manifest", str(m), "--vendor-dir", str(vendor)]) == 0
    assert '"ok": true' in capsys.readouterr().out


def test_render_batch_is_resumable_and_never_downloads(tmp_path):
    doc = r7plan.load_candidates(
        _manifest(
            tmp_path, [_day(), _day(date="2025-12-09"), _day(date="2021-07-13", role="insample")]
        )
    )
    text = r7plan.render_batch(
        doc, manifest="m.json", python_dir="py", vendor_dir="V", out_root="O", workers=3
    )
    assert text.count("goto skip_") == 3 + 2 * 2  # 3 days, merge + features per role
    assert 'merge "%OUT%\\ds_20251208" "%OUT%\\ds_20251209" --out "%OUT%\\r7_holdout"' in text
    assert 'xcopy /e /i /q "%OUT%\\ds_20210713" "%OUT%\\r7_insample"' in text
    assert "--workers 3" in text and "record-sampling" in text
    low = text.lower()
    assert "http" not in low and "del " not in low and "curl" not in low
    assert text.endswith("exit /b 1\r\n")


def test_plan_ingest_dry_run_and_write(tmp_path, capsys):
    m = _manifest(tmp_path, [_day(), _day(date="2025-12-09")])
    base = ["plan-ingest", "--manifest", str(m), "--vendor-dir", str(tmp_path), "--out-root", "O"]
    assert md_main([*base, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert '"present": false' in out and "@echo off" in out
    cmd = tmp_path / "run.cmd"
    assert md_main([*base, "--cmd", str(cmd)]) == 0
    assert cmd.read_bytes().startswith(b"@echo off\r\n")


def test_estimate_uses_measured_rates():
    e = r7plan.estimate([{"bytes": 16_150_000_000}])
    assert e["ingest_hours"] == pytest.approx(59 / 60, abs=0.05)
    assert e["dataset_gb"] == pytest.approx(16.15 * 1.7, abs=0.1)
    assert r7plan.estimate([{"bytes": 1}], n_symbols=15)["features_gb"] == 3.0


def test_record_sampling_cli_writes_dataset_json(tmp_path, capsys):
    ds = tmp_path / "ds"
    ds.mkdir()
    sessions = {d: {} for d in ("2025-12-08", "2025-12-09", "2025-12-10", "2026-06-12")}
    (ds / "dataset.json").write_text(
        json.dumps({"x-version": 2, "kind": "real", "dataset_version": "v", "sessions": sessions})
    )
    rc = md_main(["record-sampling", "--dataset-dir", str(ds), "--n", "3", "--seed", "7"])
    assert rc == 0
    m = json.loads((ds / "dataset.json").read_text())
    assert m["dataset_version"] == "v" and len(m["sampling"]["selected"]) == 3
    assert m["sampling"]["seed"] == 7 and m["sampling"]["strata"] == "month"


def _downloader():
    spec = importlib.util.spec_from_file_location(
        "r7_download", REPO_ROOT / "tools" / "r7_download.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def test_downloader_refuses_without_approval_and_checks_size(tmp_path):
    dl = _downloader()
    m = _manifest(tmp_path, [_day()])
    vendor = tmp_path / "v"
    calls = []

    def opener(url):
        calls.append(url)
        return _Resp(b"x" * 10)

    assert dl.main(["--manifest", str(m), "--vendor-dir", str(vendor)], opener=opener) == 2
    assert calls == []
    args = ["--manifest", str(m), "--vendor-dir", str(vendor), "--i-have-approval"]
    assert dl.main(args, opener=opener) == 0
    assert (vendor / "f2025-12-08.gz").stat().st_size == 10 and len(calls) == 1
    assert dl.main(args, opener=opener) == 0 and len(calls) == 1  # present: skipped
    short = _manifest(tmp_path, [_day(date="2025-12-09")])
    bad = ["--manifest", str(short), "--vendor-dir", str(vendor), "--i-have-approval"]
    assert dl.main(bad, opener=lambda u: _Resp(b"x" * 9)) == 1
    assert not (vendor / "f2025-12-09.gz").exists()
