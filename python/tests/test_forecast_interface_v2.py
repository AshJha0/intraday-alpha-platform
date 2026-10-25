"""EN1 (v2.0 stage 5): forecast records round-trip, incomplete records are refused, the registry is content-addressed."""

from __future__ import annotations

import json

import pytest
from iap.alpha.forecast import (
    FIELDS,
    ForecastError,
    ForecastRecord,
    consume,
    horizon_seconds,
    parse_horizon,
    validate_horizon,
)
from iap.alpha.registry import ForecastRegistry, RegistryEntry, RegistryError
from iap.contracts.versions import content_hash

FEATURE_HASH = content_hash({"feature": "canary.signal"})


def _entry(**kw) -> RegistryEntry:
    base = dict(
        family_id="h1",
        horizon="15m",
        model_class="pooled_linear_reversal",
        parameters={"rho": 0.42, "shrink": "james_stein", "weights": [1, 0]},
        feature_set_hash=FEATURE_HASH,
        training_window=("2024-01-02", "2024-12-31"),
        dataset_identity="daily_pit@abc123",
        software_version="v2.0.0-dev+443f9329",
        selection_history={"fits_before_this": 0},
    )
    base.update(kw)
    return RegistryEntry(**base)


def _record(rid: str, **kw) -> ForecastRecord:
    base = dict(
        family_id="h1",
        name="AAPL",
        available_ts=1_700_000_000_000_000_000,
        horizon="15m",
        mu_hat_bps=3.25,
        uncertainty_bps=1.5,
        registry_id=rid,
    )
    base.update(kw)
    return ForecastRecord(**base)


def test_record_round_trips_through_canonical_json_and_hashes_stably():
    rid = _entry().registry_id
    r = _record(rid)
    doc = r.to_dict()
    assert tuple(doc) == FIELDS
    again = ForecastRecord.from_dict(json.loads(r.canonical()))
    assert again == r and again.record_hash() == r.record_hash()
    assert _record(rid, mu_hat_bps=3.26).record_hash() != r.record_hash()
    assert r.key() == ("h1", "15m", "AAPL")


@pytest.mark.parametrize("missing", FIELDS)
def test_a_record_missing_any_field_is_refused(missing):
    doc = _record(_entry().registry_id).to_dict()
    del doc[missing]
    with pytest.raises(ForecastError, match=f"missing \\['{missing}'\\]"):
        ForecastRecord.from_dict(doc)
    doc = _record(_entry().registry_id).to_dict()
    doc[missing] = None
    with pytest.raises(ForecastError, match="null fields"):
        ForecastRecord.from_dict(doc)


@pytest.mark.parametrize(
    "bad, match",
    [
        (dict(family_id="H1"), "family_id"),
        (dict(name=""), "name"),
        (dict(available_ts=1.5), "available_ts"),
        (dict(available_ts=True), "available_ts"),
        (dict(available_ts=-1), "i64"),
        (dict(horizon="15 minutes"), "horizon"),
        (dict(horizon="0m"), "horizon"),
        (dict(horizon=15), "horizon"),
        (dict(mu_hat_bps=float("nan")), "finite"),
        (dict(mu_hat_bps="3"), "finite"),
        (dict(uncertainty_bps=-0.1), ">= 0"),
        (dict(uncertainty_bps=float("inf")), "finite"),
        (dict(registry_id="deadbeef"), "registry_id"),
    ],
)
def test_malformed_fields_are_refused(bad, match):
    with pytest.raises(ForecastError, match=match):
        _record(_entry().registry_id, **bad)
    doc = _record(_entry().registry_id).to_dict()
    doc["extra"] = 1
    with pytest.raises(ForecastError, match="unexpected \\['extra'\\]"):
        ForecastRecord.from_dict(doc)


def test_horizon_vocabulary():
    assert validate_horizon("c2o") == "c2o" and parse_horizon("5d") == (5, "d")
    assert (
        horizon_seconds("15m") == 900
        and horizon_seconds("2h") == 7200
        and horizon_seconds("1d") == 86_400
    )
    with pytest.raises(ForecastError, match="without a fixed length"):
        horizon_seconds("c2o")
    with pytest.raises(ForecastError):
        validate_horizon("1w")


def test_consume_refuses_unavailable_duplicate_and_unregistered_records():
    reg = ForecastRegistry()
    rid = reg.register(_entry())
    r = _record(rid)
    t = r.available_ts
    assert consume([r], t_decision=t, registry=reg) == (r,)
    assert (
        consume([r, _record(rid, name="MSFT", horizon="30m")], t_decision=t + 1, registry=reg)[
            1
        ].name
        == "MSFT"
    )
    with pytest.raises(ForecastError, match="available at"):
        consume([r], t_decision=t - 1, registry=reg)
    with pytest.raises(ForecastError, match="duplicate"):
        consume([r, _record(rid, mu_hat_bps=1.0)], t_decision=t, registry=reg)
    other = _entry(parameters={"rho": 0.5}).registry_id
    with pytest.raises(ForecastError, match="unknown registry id"):
        consume([_record(other)], t_decision=t, registry=reg)
    with pytest.raises(ForecastError, match="not a ForecastRecord"):
        consume([r.to_dict()], t_decision=t)  # type: ignore[list-item]
    with pytest.raises(ForecastError, match="t_decision"):
        consume([r], t_decision=1.0)  # type: ignore[arg-type]


def test_registry_is_content_addressed_append_only_and_verifiable(tmp_path):
    reg = ForecastRegistry()
    e = _entry()
    rid = reg.register(e)
    assert rid == content_hash(e.to_dict()) == e.registry_id
    assert (
        reg.register(_entry()) == rid and len(reg) == 1
    )  # identical declaration: same id, no new entry
    rid2 = reg.register(
        _entry(parameters={"rho": 0.43, "shrink": "james_stein", "weights": [1, 0]})
    )
    assert rid2 != rid and len(reg) == 2
    assert reg.register(_entry(horizon="30m")) not in (rid, rid2)
    assert [x.horizon for x in reg.by_family("h1", "30m")] == ["30m"] and len(
        reg.by_family("h1")
    ) == 3
    assert reg.by_family("d1") == []
    assert reg.contains(rid) and not reg.contains("0" * 64)
    with pytest.raises(RegistryError, match="unknown registry id"):
        reg.get("0" * 64)
    reg.verify()

    path = tmp_path / "registry.json"
    reg.save(path)
    loaded = ForecastRegistry.load(path)
    assert loaded.ids() == reg.ids() and loaded.digest == reg.digest and loaded.get(rid) == e
    reg.save(tmp_path / "again.json")
    assert (tmp_path / "again.json").read_bytes() == path.read_bytes()  # canonical bytes

    # tamper: change a parameter in the file without re-hashing -> refused on load
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["entries"][0]["entry"]["parameters"]["rho"] = 0.99
    (tmp_path / "tampered.json").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(RegistryError, match="hashes to"):
        ForecastRegistry.load(tmp_path / "tampered.json")
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["entries"].pop()
    (tmp_path / "dropped.json").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(RegistryError, match="digest"):
        ForecastRegistry.load(tmp_path / "dropped.json")
    (tmp_path / "other.json").write_text('{"schema": "x"}', encoding="utf-8")
    with pytest.raises(RegistryError, match="not a"):
        ForecastRegistry.load(tmp_path / "other.json")


@pytest.mark.parametrize(
    "bad, match",
    [
        (dict(family_id="H1"), "family_id"),
        (dict(horizon="15"), "horizon"),
        (dict(model_class=" "), "model_class"),
        (dict(parameters={"nan": float("nan")}), "non-finite"),
        (dict(parameters={1: 2}), "non-string key"),
        (dict(parameters={"obj": object()}), "not a JSON value"),
        (dict(feature_set_hash="abc"), "feature_set_hash"),
        (dict(training_window=("2024-12-31", "2024-01-01")), "after its end"),
        (dict(training_window=("2024-01-01",)), "training_window"),
        (dict(dataset_identity=""), "dataset_identity"),
        (dict(software_version=""), "software_version"),
        (dict(approval="blessed"), "approval"),
        (dict(notes=3), "notes"),
    ],
)
def test_malformed_registry_entries_are_refused(bad, match):
    with pytest.raises(RegistryError, match=match):
        _entry(**bad)
    with pytest.raises(RegistryError, match="unexpected"):
        RegistryEntry.from_dict({**_entry().to_dict(), "surprise": 1})
    with pytest.raises(RegistryError, match="takes a RegistryEntry"):
        ForecastRegistry().register(_entry().to_dict())  # type: ignore[arg-type]
