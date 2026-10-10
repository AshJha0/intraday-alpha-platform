"""v1.11 AI3: model registry, monitoring, shadow mode (synthetic data)."""

from __future__ import annotations

import json

import numpy as np
import pytest
from iap.agents.blackboard import Blackboard, digest
from iap.backtest.maker import MakerFilter
from iap.lifecycle.evidence import Evidence
from iap.mlops import (
    ModelRegistry,
    RegistryError,
    ShadowRunner,
    TamperError,
    calibration,
    monitor,
    promotion_decision,
)
from iap.mlops.__main__ import main as cli
from iap.models.zoo import load_registered, make_model

META = dict(
    dataset_version="synthetic-v1",
    date_range=("2026-01-02", "2026-03-31"),
    features=["f0", "f1", "f2"],
    seed=7,
    code_hash="c" * 64,
    feature_registry_hash="r" * 64,
)


def _data(n=1500, seed=0, shift=0.0):
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(n, 3)) + shift
    logit = 1.5 * X[:, 0] - X[:, 1]
    y = (rng.random(n) < 1 / (1 + np.exp(-logit))).astype(float)
    return X, y


def _prereg(tmp_path):
    bb = Blackboard(tmp_path / "bb.jsonl")
    pid = digest({"alpha": "EQ01", "horizon": "1m"})[:16]
    return bb.append("prereg", "research", 1, {"prereg_id": pid, "alpha": "EQ01"})


def _fit(seed=0):
    X, y = _data(seed=seed)
    return MakerFilter("meta_gbm", tau=0.5).fit(X, y)


def test_deterministic_id_and_roundtrip(tmp_path):
    entry = _prereg(tmp_path)
    r1 = _fit().register(tmp_path / "a", prereg=entry, **META)
    r2 = _fit().register(tmp_path / "b", prereg=entry, **META)
    assert r1.model_id == r2.model_id and len(r1.model_id) == 64
    assert r1.prereg_hash == entry["hash"] and not r1.exploratory
    # re-registering identical content is idempotent
    assert _fit().register(tmp_path / "a", prereg=entry, **META).model_id == r1.model_id
    # a different seed / dataset is a different id
    other = dict(META, seed=8)
    assert _fit().register(tmp_path / "a", prereg=entry, **other).model_id != r1.model_id
    f = MakerFilter.from_registry(tmp_path / "a", r1.model_id)
    X, _ = _data(seed=3)
    np.testing.assert_array_equal(f.score(X), _fit().score(X))
    assert f.model_id == r1.model_id and f.tau == 0.5


def test_refuses_without_prereg_unless_exploratory(tmp_path):
    with pytest.raises(RegistryError, match="pre-registration"):
        _fit().register(tmp_path, **META)
    rec = _fit().register(tmp_path, exploratory=True, **META)
    assert rec.exploratory and rec.prereg_hash is None


def test_immutable_on_conflicting_metadata(tmp_path):
    _fit().register(tmp_path, exploratory=True, metrics={"auc": 0.7}, **META)
    with pytest.raises(RegistryError, match="immutable"):
        _fit().register(tmp_path, exploratory=True, metrics={"auc": 0.9}, **META)


def test_tamper_detection(tmp_path):
    reg = ModelRegistry(tmp_path)
    rec = _fit().register(reg, exploratory=True, **META)
    reg.verify(rec.model_id)
    path = tmp_path / rec.model_id / "record.json"
    blob = json.loads(path.read_text())
    blob["metrics"] = {"auc": 0.99}
    path.write_text(json.dumps(blob))
    with pytest.raises(TamperError, match="record hash"):
        reg.load(rec.model_id)
    rec2 = _fit(seed=1).register(reg, exploratory=True, **META)
    art = tmp_path / rec2.model_id / "model.joblib"
    art.write_bytes(art.read_bytes() + b"x")
    with pytest.raises(TamperError, match="artefact"):
        reg.load(rec2.model_id)


def test_lineage_and_zoo_load(tmp_path):
    reg = ModelRegistry(tmp_path)
    X, y = _data()
    m0 = make_model("ridge").fit(X, y)
    r0 = reg.register(m0, name="ridge", kind="regressor", params={}, exploratory=True, **META)
    m1 = make_model("ridge").fit(X[:1000], y[:1000])
    r1 = reg.register(
        m1, name="ridge", kind="regressor", params={}, exploratory=True, parent=r0.model_id, **META
    )
    assert [r.model_id for r in reg.lineage(r1.model_id)] == [r1.model_id, r0.model_id]
    np.testing.assert_allclose(load_registered(tmp_path, r0.model_id).predict(X), m0.predict(X))
    assert {r.model_id for r in reg.list("ridge")} == {r0.model_id, r1.model_id}
    with pytest.raises(RegistryError, match="parent"):
        reg.register(
            m0, name="x", kind="regressor", params={}, exploratory=True, parent="nope", **META
        )


def _feats(X):
    return {f"f{i}": X[:, i] for i in range(X.shape[1])}


def test_drift_fires_on_shift_not_on_null():
    Xr, _ = _data(seed=1, n=3000)
    Xn, _ = _data(seed=2, n=3000)
    Xs, _ = _data(seed=2, n=3000, shift=0.6)
    null = monitor(kind="classifier", ref_features=_feats(Xr), cur_features=_feats(Xn))
    assert null["status"] == "OK", null["alerts"]
    shifted = monitor(kind="classifier", ref_features=_feats(Xr), cur_features=_feats(Xs))
    assert shifted["status"] == "ALERT"
    assert set(shifted["alerts"]) == {"feature_drift:f0", "feature_drift:f1", "feature_drift:f2"}


def test_calibration_ece():
    rng = np.random.default_rng(0)
    p = rng.random(20000)
    y = (rng.random(p.size) < p).astype(float)
    good = calibration(p, y)
    assert good["ece"] < 0.02
    assert abs(good["brier"] - np.mean((p - y) ** 2)) < 1e-12
    bad = calibration(np.clip(p + 0.3, 0, 1), y)
    assert bad["ece"] > 0.15
    assert sum(b["n"] for b in good["bins"]) == p.size
    rep = monitor(kind="classifier", ref_pred=p, ref_y=y, cur_pred=np.clip(p + 0.3, 0, 1), cur_y=y)
    assert "calibration:prediction" in rep["alerts"]


def test_ic_decay_regressor():
    rng = np.random.default_rng(0)
    y = rng.normal(size=2000)
    good = y + rng.normal(size=y.size)
    noise = rng.normal(size=y.size)
    rep = monitor(kind="regressor", ref_pred=good, ref_y=y, cur_pred=noise, cur_y=y)
    assert "ic_decay:prediction" in rep["alerts"]
    ok = monitor(kind="regressor", ref_pred=good, ref_y=y, cur_pred=good, cur_y=y)
    assert ok["status"] == "OK"


class _Boom:
    tau = 0.5

    def score(self, X):
        raise RuntimeError("candidate broke")


def test_shadow_never_changes_champion_decisions():
    champ = _fit(seed=0)
    cand = MakerFilter("meta_gbm", tau=0.3).fit(*_data(seed=5))
    frames = [_data(seed=10 + i, n=200)[0] for i in range(5)]
    alone = [champ.allow(X) for X in frames]
    for candidate in (cand, _Boom()):
        sr = ShadowRunner(champ, candidate)
        got = sr.run(frames)
        for a, b in zip(alone, got, strict=True):
            np.testing.assert_array_equal(a, b)
    ys = np.concatenate([_data(seed=10 + i, n=200)[1] for i in range(5)])
    assert sr.compare(ys).candidate_errors == 5
    sr = ShadowRunner(champ, cand)
    sr.run(frames)
    pnl = np.where(ys > 0, 1.0, -1.0)
    cmp = sr.compare(ys, pnl=pnl)
    assert cmp.n == 1000 and cmp.candidate["n_allowed"] > cmp.champion["n_allowed"]
    assert cmp.pnl_delta == pytest.approx(cmp.candidate["pnl"] - cmp.champion["pnl"])
    dec = promotion_decision(cmp, Evidence.empty())
    assert dec["promote"] is False and dec["gates_passed"] is False


def test_cli_monitor(tmp_path, capsys):
    Xr, _ = _data(seed=1, n=2000)
    Xs, _ = _data(seed=2, n=2000, shift=1.0)
    np.savez(tmp_path / "r.npz", **_feats(Xr))
    np.savez(tmp_path / "c.npz", **_feats(Xs))
    rc = cli(
        [
            "monitor",
            "--reference",
            str(tmp_path / "r.npz"),
            "--current",
            str(tmp_path / "c.npz"),
            "--kind",
            "classifier",
            "--out",
            str(tmp_path / "rep.json"),
        ]
    )
    assert rc == 1
    assert json.loads((tmp_path / "rep.json").read_text())["status"] == "ALERT"
    assert cli(["--registry", str(tmp_path / "reg"), "list"]) == 0
