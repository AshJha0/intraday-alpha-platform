"""``iap.mvp.real``: the MVP loop over an ingested real-dataset directory
(v1.15 X-P1-2), on a tiny SYNTHETIC dataset written in the same on-disk
format as ``iap.marketdata.ingest`` (dataset.json + normalized/ +
configs/{instruments,venues}/)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from iap.core.codec import sha256_events_iap1, sha256_file, write_iap1, write_jsonl
from iap.marketdata.ingest import MANIFEST_VERSION
from iap.mvp.config import REPO_ROOT, load_config
from iap.mvp.feed import generate_feed
from iap.mvp.real import RUN_MANIFEST, main, run_symbol

TINY_CONFIG = REPO_ROOT / "configs" / "mvp" / "mvp_tiny.json"
PREREG = REPO_ROOT / "configs" / "prereg" / "v2.json"
DATE = "2026-08-24"


def _write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The tiny MVP feed, one venue, re-keyed to real ids (instrument 1,
    venue 101), written as an ingested dataset."""
    cfg = load_config(TINY_CONFIG)
    feed = generate_feed(cfg, tmp_path_factory.mktemp("gen"))
    events = [ev for ev in feed.events if ev.venue_id == 1]
    for i, ev in enumerate(events):
        ev.event_id = i + 1
        ev.instrument_id = 1
        ev.venue_id = 101
    root = tmp_path_factory.mktemp("ds") / "ds_tiny"
    norm = root / "normalized"
    norm.mkdir(parents=True)
    write_iap1(norm / "eq_tiny.normalized.iap1", events)
    write_jsonl(norm / "eq_tiny.normalized.jsonl", events)
    session = {"timezone": "UTC", "open": "13:30:00", "close": "13:38:00"}
    _write(
        root / "configs" / "instruments" / "instruments.json",
        {
            "x-version": 1,
            "calendar": {"trading_days": [DATE], "timezone": "UTC"},
            "sessions": {"EQUITY": session, "ETF": session},
            "instruments": [
                {
                    "symbol": "TINY",
                    "instrument_id": 1,
                    "asset_class": "EQUITY",
                    "currency": "USD",
                    "tick_size": 0.01,
                    "lot_size": 100,
                    "ref_price": 190.0,
                    "adv": 60000000,
                    "venues": ["XNAS"],
                }
            ],
        },
    )
    _write(
        root / "configs" / "venues" / "venues.json",
        {
            "x-version": 1,
            "venues": [
                {
                    "venue": "XNAS",
                    "venue_id": 101,
                    "asset_class": "EQUITY",
                    "taker_fee_per_share": 0.003,
                    "maker_rebate_per_share": 0.002,
                    "latency": {"mean_ns": 0, "jitter_ns": 0},
                    "supports": ["MBO", "AUCTION", "HALT"],
                }
            ],
        },
    )
    _write(
        root / "dataset.json",
        {
            "x-version": MANIFEST_VERSION,
            "kind": "real",
            "dataset_version": sha256_events_iap1(events),
            "normalized": {
                "files": {p.name: sha256_file(p) for p in sorted(norm.iterdir())},
                "qc_totals": {"events_in": len(events), "events_out": len(events)},
            },
            "sessions": {DATE: {"format": "synthetic-test"}},
        },
    )
    return root


def _run(dataset: Path, out: Path, **kw):
    return run_symbol(dataset, DATE, "TINY", out, prereg=PREREG, mvp_config=TINY_CONFIG, **kw)


def test_real_runner_is_deterministic_and_writes_a_manifest(dataset: Path, tmp_path: Path) -> None:
    a = _run(dataset, tmp_path / "a")
    b = _run(dataset, tmp_path / "b")
    assert a.result.trace_digest == b.result.trace_digest
    ma = (tmp_path / "a" / RUN_MANIFEST).read_bytes()
    assert ma == (tmp_path / "b" / RUN_MANIFEST).read_bytes()
    m = a.manifest
    assert m["dataset"]["legacy"] is False
    assert m["stream"]["n_events"] == a.result.engine.counters.events > 0
    # the streamed identity equals the platform's dataset definition
    assert (
        m["stream"]["data_version"]
        == json.loads((dataset / "dataset.json").read_text())["dataset_version"]
    )
    assert m["prereg"]["methods"] == "v2" and m["prereg"]["applied"] == []
    assert m["results"]["decisions"] > 0
    assert m["trace_digest"] == a.result.trace_digest


def test_limit_events_truncates_the_stream(dataset: Path, tmp_path: Path) -> None:
    run = _run(dataset, tmp_path / "lim", limit_events=500)
    assert run.manifest["limit_events"] == 500
    assert run.manifest["stream"]["n_events"] == 500
    assert run.result.engine.counters.events == 500


def test_cli_and_errors(dataset: Path, tmp_path: Path) -> None:
    args = [
        "--dataset",
        str(dataset),
        "--date",
        DATE,
        "--config",
        str(PREREG),
        "--mvp-config",
        str(TINY_CONFIG),
        "--out",
        str(tmp_path / "cli"),
        "--limit-events",
        "300",
    ]
    assert main([*args, "--symbols", "TINY"]) == 0
    assert (tmp_path / "cli" / "TINY" / RUN_MANIFEST).is_file()
    assert main([*args, "--symbols", "NOPE"]) == 2
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    doc = json.loads((dataset / "dataset.json").read_text())
    doc["x-version"] = 1
    _write(legacy / "dataset.json", doc)
    with pytest.raises(ValueError, match="allow-legacy-dataset"):
        _run(legacy, tmp_path / "x")
