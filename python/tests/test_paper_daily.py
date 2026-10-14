"""v1.16 item 9: the forward paper-trade driver (``iap.paper``) on the tiny
SYNTHETIC dataset of test_mvp_real (same on-disk format as an ingest), plus
deterministic checks of the TCA comparison and the monitors."""

from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from iap.backtest.costs import CostModel
from iap.core.codec import sha256_events_iap1, sha256_file, write_iap1, write_jsonl
from iap.marketdata.ingest import MANIFEST_VERSION
from iap.mvp.config import REPO_ROOT, load_config
from iap.mvp.feed import generate_feed
from iap.paper import daily, monitors, tca_compare

TINY_CONFIG = REPO_ROOT / "configs" / "mvp" / "mvp_tiny.json"
DATE = "2026-08-24"


def _write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The tiny MVP feed, one venue, re-keyed to real ids, as an ingested
    dataset (the fixture of test_mvp_real)."""
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


def _row(**kw: object) -> tca_compare.ParentCostRow:
    base: dict = dict(
        parent_order_id=1,
        algo="IS",
        side=1,
        qty=100,
        filled_qty=100,
        decision_mid=100.0,
        half_spread=0.01,
        decision_ts=0,
        sim_is_bps=2.0,
        sim_spread_bps=1.0,
        sim_fee_bps=0.3,
        sim_impact_bps=0.5,
    )
    base.update(kw)
    return tca_compare.ParentCostRow(**base)


MODEL = CostModel(
    impact_coeff_bps_per_pct_adv=2.0,
    equity_taker_fee_per_share=0.003,
    fx_commission_per_million=0.0,
    impact_model="linear",
)
EQ = dict(asset_class="EQUITY", adv=1_000_000, lot_size=1)


def test_expected_bps_by_hand() -> None:
    exp = tca_compare.expected_bps(_row(), MODEL, "EQUITY", 1_000_000, 1)
    # spread 100 * 0.01 / (100 * 100) = 1 bp; fee 0.3 / 10000 = 0.3 bp;
    # impact 2 bps per %ADV * 0.01 %ADV = 0.02 bp
    assert exp["spread"] == pytest.approx(1.0)
    assert exp["fee"] == pytest.approx(0.3)
    assert exp["impact"] == pytest.approx(0.02)
    assert exp["is"] == pytest.approx(1.32)


def test_compare_weights_skips_and_alerts() -> None:
    rows = [
        _row(),
        _row(
            parent_order_id=2,
            algo="TWAP",
            filled_qty=300,
            sim_is_bps=1.36,
            sim_spread_bps=1.0,
            sim_fee_bps=0.3,
            sim_impact_bps=0.06,
        ),
        _row(parent_order_id=3, filled_qty=0),
        _row(parent_order_id=4, decision_mid=float("nan")),
    ]
    rep = tca_compare.compare(rows, MODEL, is_gap_alert_bps=0.1, **EQ)
    assert rep["n_parents_compared"] == 2
    assert rep["skipped"] == {"no_fill": 1, "no_market": 1}
    # IS gaps: parent 1 = 2 - 1.32 = 0.68, parent 2 = 1.36 - 1.36 = 0 (impact 0.06 bp at 0.03 %ADV); weights 100 / 300
    assert rep["aggregate"]["is"]["gap_bps"] == pytest.approx(0.68 / 4)
    assert rep["per_algo"]["TWAP"]["is_gap_bps"] == pytest.approx(0.0)
    assert len(rep["alerts"]) == 1
    quiet = tca_compare.compare(rows, MODEL, is_gap_alert_bps=1.0, **EQ)
    assert quiet["alerts"] == []
    assert tca_compare.compare(rows, MODEL, is_gap_alert_bps=0.1, **EQ) == rep
    merged = tca_compare.merge({"A": rep, "B": quiet}, 0.1)
    assert merged["n_parents_compared"] == 4
    assert merged["aggregate"]["is"]["gap_bps"] == pytest.approx(0.68 / 4)


def test_ic_check_thresholds() -> None:
    kw: dict = dict(min_ratio=0.25, rolling_window=3, min_rolling_days=3)
    ok = monitors.ic_check("EQ10", 0.08, 1000, 0.0838, [0.07, 0.09], **kw)
    assert ok["alerts"] == [] and ok["rolling"]["days"] == 3
    assert ok["rolling"]["mean_ic"] == pytest.approx(0.08)
    flip = monitors.ic_check("EQ10", -0.01, 1000, 0.0838, [], **kw)
    assert any("opposite sign" in a for a in flip["alerts"])
    low = monitors.ic_check("EQ05", 0.005, 1000, 0.0409, [], **kw)
    assert any("x holdout" in a for a in low["alerts"])
    assert low["rolling"]["ratio_to_holdout"] is None  # too few days
    decay = monitors.ic_check("EQ05", 0.03, 1000, 0.0409, [0.0, 0.0, 0.0], **kw)
    assert decay["ratio_to_holdout"] > 0.25
    assert any("alpha decay" in a for a in decay["alerts"])
    none = monitors.ic_check("EQ05", None, 0, 0.0409, [0.04], **kw)
    assert any("no realised IC" in a for a in none["alerts"])


def _store(d: Path, x: np.ndarray) -> Path:
    d.mkdir(parents=True)
    pd.DataFrame(
        {
            "instrument_id": np.ones(len(x), dtype=np.int64),
            "exchange_ts": np.arange(len(x), dtype=np.int64),
            "f_a": x,
            "f_b": np.linspace(0.0, 1.0, len(x)),
        }
    ).to_parquet(d / "features_1.parquet")
    return d


def test_feature_drift_flags_a_shifted_feature(tmp_path: Path) -> None:
    rng = np.random.default_rng(7)
    ref = _store(tmp_path / "ref", rng.normal(size=5000))
    same = _store(tmp_path / "same", rng.normal(size=5000))
    moved = _store(tmp_path / "moved", rng.normal(loc=2.0, size=5000))
    assert monitors.feature_drift([ref], same)["alerts"] == []
    rep = monitors.feature_drift([ref], moved)
    assert rep["alerts"]
    assert all("f_a" in a and "f_b" not in a for a in rep["alerts"])
    assert monitors.feature_drift([ref], moved) == rep


def _paper_config(tmp_path: Path) -> Path:
    doc = json.loads((REPO_ROOT / "configs" / "paper" / "paper_trade.json").read_text("utf-8"))
    doc.update(
        mvp_config="configs/mvp/mvp_tiny.json",
        symbols=["TINY", "NOPE"],
        eod_flatten_lead_ns=60_000_000_000,
    )
    doc["drill"]["max_events_after_kill"] = 200
    p = tmp_path / "paper.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


@pytest.mark.slow
def test_daily_driver_end_to_end_idempotent(dataset: Path, tmp_path: Path) -> None:
    cfg = _paper_config(tmp_path)
    out_root = tmp_path / "paper"
    kw: dict = dict(
        config_path=cfg, dataset=dataset, data_root=tmp_path, out_root=out_root, log=lambda m: None
    )
    code, rep = daily.run_day(DATE, **kw)
    assert rep is not None and code in (0, daily.EXIT_ALERTS)
    out = out_root / DATE
    for name in ("report.json", "report.md", "tca_compare.json", "monitors.json", "drill.json"):
        assert (out / name).is_file(), name
    assert not (out / "drill_stage").exists()
    assert rep["symbols"] == ["TINY"] and rep["symbols_not_in_dataset"] == ["NOPE"]
    man = json.loads((out / "mvp" / "TINY" / "run_manifest.json").read_text("utf-8"))
    assert man["mvp"]["eod_flatten_lead_ns"] == 60_000_000_000
    assert man["prereg"]["path"] == "configs/prereg/v3.json"
    assert rep["tca"]["n_parents_compared"] > 0
    assert math.isfinite(rep["tca"]["aggregate"]["is"]["expected_bps"])
    assert rep["monitors"]["drift"].startswith("no feature store")
    assert rep["drill"]["dry_run"] is True
    assert rep["drill"]["status"] in ("pass", "inconclusive")
    first = (out / "report.json").read_bytes()
    # a rerun of a completed date is a no-op
    code2, rep2 = daily.run_day(DATE, **kw)
    assert code2 == 0 and rep2 is None
    assert (out / "report.json").read_bytes() == first
    # --force rebuilds it, deterministically
    _, rep3 = daily.run_day(DATE, force=True, **kw)
    assert (out / "report.json").read_bytes() == first
    assert rep3 == rep


def test_daily_driver_refuses_holdout_and_missing(dataset: Path, tmp_path: Path) -> None:
    cfg = _paper_config(tmp_path)
    hold = tmp_path / "r7_holdout"
    shutil.copytree(dataset, hold)
    with pytest.raises(ValueError, match="forbidden"):
        daily.run_day(
            DATE, config_path=cfg, dataset=hold, out_root=tmp_path / "p", log=lambda m: None
        )
    # auto-locate skips the holdout copy and finds nothing else
    with pytest.raises(ValueError, match="no ingested dataset"):
        daily.run_day(
            DATE, config_path=cfg, data_root=tmp_path, out_root=tmp_path / "p", log=lambda m: None
        )
    args = ["--date", DATE, "--config", str(cfg), "--data-root", str(tmp_path)]
    assert daily.main([*args, "--out-root", str(tmp_path / "p")]) == 2
    assert not (tmp_path / "p" / DATE).exists()
    assert daily.is_forbidden("r7_20251210", ["r7_2025*"])
    assert not daily.is_forbidden("paper_20261015", ["r7_2025*", "holdout2026"])


def test_pending_dates(tmp_path: Path) -> None:
    def ds(name: str, dates: list[str]) -> None:
        _write(tmp_path / name / "dataset.json", {"sessions": {d: {} for d in dates}})

    ds("paper_20261015", ["2026-10-15"])
    ds("paper_20261016", ["2026-10-16"])
    ds("r7_holdout", ["2025-12-10"])  # not a paper_* dataset: never pending
    out = tmp_path / "paper"
    _write(out / "2026-10-15" / "report.json", {"status": "complete"})
    _write(out / "2026-10-16" / "report.json", {"status": "running"})
    assert daily.pending_dates(tmp_path, out) == ["2026-10-16"]
