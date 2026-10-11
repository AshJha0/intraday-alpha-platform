"""v1.15 ML governance: feature drift monitor and model cards at promotion."""

from __future__ import annotations

import json

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from iap.backtest.maker import MakerFilter
from iap.mlops.__main__ import main as mlops_main
from iap.mlops.model_card import CARD, card_hash
from iap.mlops.registry import PROMOTION, ModelRegistry, RegistryError
from iap.research import drift
from test_mlops import META, _data

# ------------------------------------------------------------------ drift


def _chunk(rng, n, shift=0.0, iid=1):
    return {
        "instrument_id": np.full(n, iid, dtype=np.uint32),
        "f_a": rng.normal(size=n) + shift,
        "f_b": rng.exponential(size=n),
        "label_mid_1s": rng.normal(size=n),
    }


def _store(path, chunks):
    path.mkdir(parents=True, exist_ok=True)
    by_iid: dict[int, list] = {}
    for c in chunks:
        by_iid.setdefault(int(c["instrument_id"][0]), []).append(c)
    for iid, cs in by_iid.items():
        cols = {k: np.concatenate([c[k] for c in cs]) for k in cs[0]}
        cols["exchange_ts"] = np.arange(len(cols["f_a"]), dtype=np.int64)
        cols["validity_bits"] = [b"\x03"] * len(cols["f_a"])
        pq.write_table(pa.table(cols), path / f"features_{iid}.parquet")
    return path


def test_no_drift_small_psi():
    rng = np.random.default_rng(0)
    ref = [_chunk(rng, 5000), _chunk(rng, 5000, iid=2)]
    tgt = [_chunk(rng, 5000), _chunk(rng, 5000, iid=2)]
    rep = drift.compute_drift(ref, tgt)
    assert rep["descriptive_only"] is True
    for g in ("pooled", "1", "2"):
        for f in ("f_a", "f_b"):
            row = rep["groups"][g][f]
            assert row["psi"] < 0.02 and row["ks"] < 0.05, (g, f, row)
            assert row["status"] == "ok"
        assert "label_mid_1s" not in rep["groups"][g]


def test_planted_shift_flagged_only_where_planted():
    rng = np.random.default_rng(1)
    ref = [_chunk(rng, 5000), _chunk(rng, 5000, iid=2)]
    tgt = [_chunk(rng, 5000, shift=1.0), _chunk(rng, 5000, iid=2)]
    rep = drift.compute_drift(ref, tgt)
    a1 = rep["groups"]["1"]["f_a"]
    assert a1["status"] == "drift"
    assert {"psi", "ks", "mean_shift"} <= set(a1["flags"])
    assert a1["mean_shift_std"] == pytest.approx(1.0, abs=0.1)
    assert rep["groups"]["2"]["f_a"]["status"] == "ok"
    assert rep["groups"]["1"]["f_b"]["status"] == "ok"
    assert rep["summary"]["1"]["drifted"] == ["f_a"]


def test_chunked_equals_in_memory(tmp_path):
    rng = np.random.default_rng(2)
    ref = [_chunk(rng, 3000), _chunk(rng, 2000, iid=2)]
    tgt = [_chunk(rng, 2500, shift=0.3), _chunk(rng, 1500, iid=2)]
    rdir, tdir = _store(tmp_path / "ref", ref), _store(tmp_path / "tgt", tgt)
    mem = drift.compute_drift(
        lambda: drift.iter_store([rdir], 10**9), lambda: drift.iter_store([tdir], 10**9)
    )
    chk = drift.compute_drift(
        lambda: drift.iter_store([rdir], 137), lambda: drift.iter_store([tdir], 137)
    )
    assert mem["summary"] == chk["summary"]
    for g, rows in mem["groups"].items():
        for f, r in rows.items():
            c = chk["groups"][g][f]
            for k, v in r.items():
                if isinstance(v, float):
                    assert c[k] == pytest.approx(v, rel=1e-9, abs=1e-12), (g, f, k)
                else:
                    assert c[k] == v, (g, f, k)


def test_cli_writes_reports(tmp_path, capsys):
    rng = np.random.default_rng(3)
    rdir = _store(tmp_path / "ref", [_chunk(rng, 2000)])
    tdir = _store(tmp_path / "tgt", [_chunk(rng, 2000, shift=2.0)])
    out = tmp_path / "out"
    assert drift.main(["--ref", str(rdir), "--target", str(tdir), "--out", str(out)]) == 0
    rep = json.loads((out / "drift_report.json").read_text())
    assert rep["groups"]["pooled"]["f_a"]["status"] == "drift"
    assert rep["ref"] == [str(rdir)]
    md = (out / "drift_report.md").read_text()
    assert "Descriptive only" in md and "| f_a | drift |" in md


def test_insufficient_rows():
    rng = np.random.default_rng(4)
    rep = drift.compute_drift([_chunk(rng, 50)], [_chunk(rng, 50)])
    assert rep["groups"]["pooled"]["f_a"]["status"] == "insufficient"


# ------------------------------------------------------------------ model cards

CARD_KW = dict(
    metrics_net_of_costs={"sharpe_net": 0.4, "pnl_net_bps": 1.2},
    known_limitations=["fit on 7 sessions"],
    intended_use="maker fill filter research",
    owner_signoff="PR #999",
    sessions=["2026-01-02"],
    calibration={"ece": 0.02},
)


def _registered(tmp_path):
    X, y = _data(seed=0)
    rec = MakerFilter("meta_gbm", tau=0.5).fit(X, y).register(tmp_path, exploratory=True, **META)
    return ModelRegistry(tmp_path), rec.model_id


def test_promote_requires_card(tmp_path):
    reg, mid = _registered(tmp_path)
    with pytest.raises(RegistryError, match="no model card"):
        reg.promote(mid)
    assert not (tmp_path / mid / PROMOTION).exists()
    assert mlops_main(["--registry", str(tmp_path), "promote", mid]) == 2


def test_promote_with_card(tmp_path):
    reg, mid = _registered(tmp_path)
    card = reg.write_card(mid, **CARD_KW)
    assert card["prereg"] == {"id": None, "hash": None} and card["exploratory"] is True
    doc = reg.promote(mid)
    assert doc["card_hash"] == card["card_hash"]
    assert reg.check_promotions() == []


@pytest.mark.parametrize(
    "mutate,msg",
    [
        (lambda c: c.pop("intended_use"), "missing field 'intended_use'"),
        (lambda c: c.update(known_limitations=[]), "known_limitations"),
        (lambda c: c.update(metrics_net_of_costs={}), "metrics_net_of_costs"),
        (lambda c: c.update(owner_signoff=" "), "owner_signoff"),
        (lambda c: c.update(record_hash="0" * 64), "record_hash"),
        (lambda c: c.update(prereg={"id": "x", "hash": "y"}), "prereg"),
    ],
)
def test_invalid_card_blocks_promotion(tmp_path, mutate, msg):
    reg, mid = _registered(tmp_path)
    card = reg.write_card(mid, **CARD_KW)
    mutate(card)
    card["card_hash"] = card_hash(card)
    (tmp_path / mid / CARD).write_text(json.dumps(card))
    with pytest.raises(RegistryError, match="invalid model card") as ei:
        reg.promote(mid)
    assert msg in str(ei.value)


def test_edited_card_hash_mismatch_and_check_promotions(tmp_path):
    reg, mid = _registered(tmp_path)
    reg.write_card(mid, **CARD_KW)
    reg.promote(mid)
    card = json.loads((tmp_path / mid / CARD).read_text())
    card["intended_use"] = "production trading"  # edited without re-hashing
    (tmp_path / mid / CARD).write_text(json.dumps(card))
    assert any("card_hash mismatch" in p for p in reg.check_promotions())
    (tmp_path / mid / CARD).unlink()
    assert any("no model card" in p for p in reg.check_promotions())


def test_write_card_rejects_empty_human_fields(tmp_path):
    reg, mid = _registered(tmp_path)
    with pytest.raises(RegistryError, match="intended_use"):
        reg.write_card(mid, **{**CARD_KW, "intended_use": ""})


def test_cli_card(tmp_path):
    reg, mid = _registered(tmp_path)
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(CARD_KW))
    assert mlops_main(["--registry", str(tmp_path), "card", mid, "--spec", str(spec)]) == 0
    assert mlops_main(["--registry", str(tmp_path), "promote", mid]) == 0
