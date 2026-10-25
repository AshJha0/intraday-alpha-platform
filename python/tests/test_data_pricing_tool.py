"""tools/data_pricing.py (v2.0 DP1): metadata and cost endpoints only, keys never printed, no download possible."""

from __future__ import annotations

import importlib.util
import json
import types

import pytest
from conftest import REPO_ROOT

KEY = "db-SECRETSECRETSECRET"


def _tool():
    spec = importlib.util.spec_from_file_location(
        "data_pricing", REPO_ROOT / "tools" / "data_pricing.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeDatabento:
    """Looks like ``databento.Historical``: metadata endpoints answer, anything else would be a download."""

    def __init__(self, cost=12.5, fail=False):
        self.cost_calls = []
        self.fail = fail

        def get_cost(**kw):
            if fail:
                raise RuntimeError(f"401 unauthorised for key {KEY}")
            self.cost_calls.append(kw)
            return cost

        def get_dataset_range(dataset):
            return {"start": "2018-05-01T00:00:00+00:00", "end": "2026-10-23T00:00:00+00:00"}

        def list_unit_prices(dataset):
            return [
                {"mode": "live", "unit_prices": {"mbo": 0.5}},
                {
                    "mode": "historical",
                    "unit_prices": {"mbo": 0.4, "ohlcv-1m": 0.1, "bbo-1m": 0.2, "ohlcv-1d": 0.05},
                },
            ]

        self.metadata = types.SimpleNamespace(
            get_cost=get_cost,
            get_dataset_range=get_dataset_range,
            list_unit_prices=list_unit_prices,
        )
        self.timeseries = types.SimpleNamespace(
            get_range=lambda **kw: pytest.fail("download attempted")
        )
        self.batch = types.SimpleNamespace(
            submit_job=lambda **kw: pytest.fail("download attempted")
        )


@pytest.fixture
def no_keys(monkeypatch):
    for v in ("DATABENTO_API_KEY", "POLYGON_API_KEY", "NASDAQ_DATA_LINK_API_KEY"):
        monkeypatch.delenv(v, raising=False)


def test_without_keys_every_cost_column_is_awaiting_key(no_keys, capsys, tmp_path):
    tool = _tool()
    rows = tool.run(["--as-of", "2026-10-24", "--json", str(tmp_path / "p.json")])
    out = capsys.readouterr().out
    assert "downloads 0, purchases 0" in out
    db = [r for r in rows if r["vendor"] == "databento"]
    assert len(db) == len(tool.DATABENTO_PLAN) + 1 and all(
        r["cost_usd"] == "awaiting key" for r in db
    )
    assert {r["item"] for r in rows} >= {"i", "ii", "iii"}
    assert (
        db[0]["window"] == "2006-01-01..2026-10-01"
    )  # daily window ends at the last complete month
    assert [r for r in db if r["item"] == "ii"][0][
        "window"
    ] == "2023-10-01..2026-10-01"  # 36 months
    tier2 = [r for r in db if r["item"] == "iii"][0]
    assert tier2["window"] == "2025-01-15..2026-09-22" and tier2["symbols"] == "AAPL,MSFT,QQQ"
    poly = [r for r in rows if r["vendor"].startswith("polygon")]
    assert (
        len(poly) == 1
        and "awaiting key" in poly[0]["notes"]
        and "$199/month" in poly[0]["cost_usd"]
    )
    ndl = [r for r in rows if r["vendor"].startswith("nasdaq")]
    assert len(ndl) == 1 and "awaiting key" in ndl[0]["notes"]
    doc = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))
    assert (
        doc["downloads"] == 0
        and doc["purchases"] == 0
        and doc["keys_present"]
        == {
            "databento": False,
            "polygon": False,
            "ndl": False,
        }
    )


def test_with_a_databento_key_costs_fill_through_metadata_only(no_keys, monkeypatch, capsys):
    monkeypatch.setenv("DATABENTO_API_KEY", KEY)
    tool = _tool()
    fake = FakeDatabento(cost=12.5)
    rows = tool.run(["--as-of", "2026-10-24"], databento_client=tool.MetadataOnly(fake))
    out = capsys.readouterr().out
    assert KEY not in out
    db = [r for r in rows if r["vendor"] == "databento"]
    priced = [r for r in db if r["item"] in ("i", "ii")]
    assert all(r["cost_usd"] == 12.5 for r in priced)
    assert all(
        r["coverage"] == "2018-05-01T00:00:00+00:00..2026-10-23T00:00:00+00:00" for r in priced
    )
    # the daily request starts at the dataset's own start, never before
    daily = [r for r in db if r["item"] == "i"][0]
    assert (
        daily["priced_window"] == "2018-05-01..2026-10-01"
        and daily["unit_price_usd_per_gb"] == 0.05
    )
    assert all(
        kw["symbols"] == "ALL_SYMBOLS" and kw["stype_in"] == "raw_symbol"
        for kw in fake.cost_calls[:6]
    )
    tier2 = [r for r in db if r["item"] == "iii"][0]
    assert (
        tier2["cost_usd"] == pytest.approx(40 * 12.5) and tier2["coverage"] == "40 sessions priced"
    )
    assert tier2["unit_price_usd_per_gb"] == 0.4
    assert len(fake.cost_calls) == len(tool.DATABENTO_PLAN) + 40


def test_metadata_only_wrapper_makes_a_download_impossible():
    tool = _tool()
    wrapped = tool.MetadataOnly(FakeDatabento())
    assert wrapped.metadata.get_dataset_range("X")["start"].startswith("2018")
    with pytest.raises(AttributeError, match="metadata endpoints only"):
        wrapped.timeseries.get_range(dataset="X")
    with pytest.raises(AttributeError, match="metadata endpoints only"):
        wrapped.batch.submit_job(dataset="X")


def test_errors_are_reported_per_row_with_the_key_scrubbed(no_keys, monkeypatch, capsys):
    monkeypatch.setenv("DATABENTO_API_KEY", KEY)
    tool = _tool()
    rows = tool.run(
        ["--as-of", "2026-10-24", "--vendor", "databento"],
        databento_client=tool.MetadataOnly(FakeDatabento(fail=True)),
    )
    out = capsys.readouterr().out
    assert KEY not in out and "<redacted>" in out
    assert all(str(r["cost_usd"]).startswith("error: RuntimeError") for r in rows)


def test_symbols_file_replaces_all_symbols_and_is_bounded(no_keys, monkeypatch, tmp_path):
    monkeypatch.setenv("DATABENTO_API_KEY", KEY)
    tool = _tool()
    f = tmp_path / "u.txt"
    f.write_text("# universe\naapl\nMSFT\n\n", encoding="utf-8")
    fake = FakeDatabento()
    rows = tool.run(
        ["--as-of", "2026-10-24", "--vendor", "databento", "--symbols-file", str(f)],
        databento_client=tool.MetadataOnly(fake),
    )
    assert (
        fake.cost_calls[0]["symbols"] == ["AAPL", "MSFT"]
        and rows[0]["symbols"] == "2 symbols from file"
    )
    f.write_text("\n".join(f"S{i}" for i in range(2001)), encoding="utf-8")
    with pytest.raises(SystemExit, match="2,000 per request"):
        tool.run(["--as-of", "2026-10-24", "--symbols-file", str(f)])
    (tmp_path / "e.txt").write_text("# nothing\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="no symbols"):
        tool.run(["--as-of", "2026-10-24", "--symbols-file", str(tmp_path / "e.txt")])


def test_probe_rows_are_metadata_requests_and_report_entitlement(no_keys, monkeypatch, capsys):
    monkeypatch.setenv("POLYGON_API_KEY", "poly-SECRET")
    monkeypatch.setenv("NASDAQ_DATA_LINK_API_KEY", "ndl-SECRET")
    tool = _tool()
    seen = []

    class Resp:
        def __init__(self, status, body):
            self.status, self._body = status, body

        def read(self, n):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(req, timeout):
        seen.append(req.full_url)
        if "polygon" in req.full_url:
            assert req.get_header("Authorization") == "Bearer poly-SECRET"
            return Resp(200 if "tickers" in req.full_url else 403, b"{}")
        return Resp(
            200,
            json.dumps({"datatable": {"columns": [{"name": "ticker"}, {"name": "date"}]}}).encode(),
        )

    rows = tool.run(["--as-of", "2026-10-24", "--vendor", "polygon", "--probe"], opener=opener)
    assert [r["coverage"] for r in rows[1:]] == [
        "entitled",
        "HTTP 403",
        "HTTP 403",
        "HTTP 403",
        "HTTP 403",
    ]
    assert all("limit=1" in u for u in seen)
    rows = tool.run(["--as-of", "2026-10-24", "--vendor", "ndl", "--probe"], opener=opener)
    assert all(r["coverage"] == "entitled, 2 columns" for r in rows[1:]) and len(rows) == 1 + len(
        tool.NDL_TABLES
    )
    assert all(u.endswith("metadata.json?api_key=ndl-SECRET") for u in seen[5:])
    out = capsys.readouterr().out
    assert "ndl-SECRET" not in out and "poly-SECRET" not in out
    # without --probe no request is made
    seen.clear()
    tool.run(["--as-of", "2026-10-24", "--vendor", "ndl"], opener=opener)
    assert seen == []
