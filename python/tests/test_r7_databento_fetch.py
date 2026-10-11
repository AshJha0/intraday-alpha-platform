"""tools/r7_databento_fetch.py (v1.15): estimate first, download only with
approval within a cost ceiling, the API key never printed.  A fake client
stands in for the Databento SDK: no network in tests."""

from __future__ import annotations

import importlib.util
import json
import types

import pytest
from conftest import REPO_ROOT
from iap.marketdata.r7plan import load_candidates, render_batch

KEY = "db-SECRETSECRETSECRET"


def _tool():
    spec = importlib.util.spec_from_file_location(
        "r7_databento_fetch", REPO_ROOT / "tools" / "r7_databento_fetch.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeClient:
    def __init__(self, cost=1.25, fail=None):
        self.cost_calls, self.get_calls = [], []
        self.fail = fail

        def get_cost(**kw):
            if self.fail:
                raise RuntimeError(f"401 bad key {KEY}")
            self.cost_calls.append(kw)
            return cost

        def get_range(**kw):
            self.get_calls.append(kw)
            with open(kw["path"], "wb") as f:
                f.write(b"DBN-bytes-" + kw["start"].encode())

        self.metadata = types.SimpleNamespace(get_cost=get_cost)
        self.timeseries = types.SimpleNamespace(get_range=get_range)


@pytest.fixture
def env_key(monkeypatch):
    monkeypatch.setenv("DATABENTO_API_KEY", KEY)


def _run(tool, args, client):
    return tool.main(args, client_factory=lambda key: client)


def test_tier2_date_list_is_the_documented_forty_days():
    tool = _tool()
    doc = tool.load_dates(tool.DEFAULT_DATES)
    assert len(doc["dates"]) == 40 and doc["dates"][0] == "2025-01-15"
    assert "2026-07-29" in doc["dates"]
    plan = (REPO_ROOT / "docs" / "R7_DATA_PLAN.md").read_text(encoding="utf-8")
    assert all(d in plan for d in doc["dates"])


def test_window_is_the_new_york_calendar_day():
    tool = _tool()
    assert tool.window("2025-01-15") == ("2025-01-15T05:00:00+00:00", "2025-01-16T05:00:00+00:00")
    assert tool.window("2025-07-09")[0] == "2025-07-09T04:00:00+00:00"


def test_estimate_only_without_approval_or_ceiling(env_key, tmp_path, capsys):
    tool, client = _tool(), FakeClient()
    assert _run(tool, ["--vendor-dir", str(tmp_path)], client) == 0
    out = capsys.readouterr()
    report = json.loads(out.out)
    assert report["days"] == 40 and report["estimated_total_usd"] == 50.0
    assert client.get_calls == [] and "nothing downloaded" in out.err
    assert (
        client.cost_calls[0]["schema"] == "mbo" and client.cost_calls[0]["dataset"] == "XNAS.ITCH"
    )
    assert client.cost_calls[0]["symbols"] == ["AAPL", "MSFT", "QQQ"]
    # approval without a ceiling is still estimate-only
    assert _run(tool, ["--vendor-dir", str(tmp_path), "--i-have-approval"], client) == 0
    assert _run(tool, ["--vendor-dir", str(tmp_path), "--max-cost-usd", "100"], client) == 0
    assert client.get_calls == []
    assert KEY not in capsys.readouterr().out


def test_refuses_over_the_ceiling(env_key, tmp_path, capsys):
    tool, client = _tool(), FakeClient(cost=3.0)
    args = ["--vendor-dir", str(tmp_path), "--i-have-approval", "--max-cost-usd", "100"]
    assert _run(tool, args, client) == 3
    assert client.get_calls == [] and "exceeds" in capsys.readouterr().err


def test_downloads_within_ceiling_and_writes_a_candidate_manifest(env_key, tmp_path, capsys):
    tool, client = _tool(), FakeClient()
    vendor, man = tmp_path / "v", tmp_path / "cand.json"
    args = ["--vendor-dir", str(vendor), "--i-have-approval", "--max-cost-usd", "5",
            "--date", "2025-01-15", "--date", "2026-07-29", "--manifest-out", str(man)]  # fmt: skip
    assert _run(tool, args, client) == 0
    assert len(client.get_calls) == 2
    assert (vendor / "xnas-itch-20250115.mbo.dbn.zst").is_file()
    assert not list(vendor.glob("*.part"))
    doc = load_candidates(man)
    assert [d["format"] for d in doc["days"]] == ["dbn-mbo", "dbn-mbo"]
    assert all(len(d["sha256"]) == 64 for d in doc["days"])
    bat = render_batch(doc, manifest=str(man), python_dir="p", vendor_dir="V", out_root="O")
    assert "--format dbn-mbo" in bat and "--format itch50" not in bat
    assert _run(tool, args, client) == 0 and len(client.get_calls) == 2  # present: skipped
    assert KEY not in capsys.readouterr().out


def test_key_missing_and_key_never_printed_on_error(monkeypatch, capsys):
    tool = _tool()
    monkeypatch.delenv("DATABENTO_API_KEY", raising=False)
    assert _run(tool, [], FakeClient()) == 2
    monkeypatch.setenv("DATABENTO_API_KEY", KEY)
    assert _run(tool, [], FakeClient(fail=True)) == 1
    out = capsys.readouterr()
    assert KEY not in out.out + out.err and "***" in out.err


def test_candidate_manifest_rejects_an_unknown_format(tmp_path):
    m = tmp_path / "m.json"
    m.write_text(json.dumps({"days": [{"date": "2025-01-15", "file": "f", "bytes": 1,
                                       "role": "holdout", "format": "csv"}]}))  # fmt: skip
    with pytest.raises(ValueError, match="format"):
        load_candidates(m)
