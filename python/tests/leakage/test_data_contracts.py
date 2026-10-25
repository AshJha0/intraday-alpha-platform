"""U2 data and feature contracts: declarations validated, enforced at import, lineage resolved."""

from __future__ import annotations

import importlib.util
import re
import sys
import textwrap

import pandas as pd
import pytest
from conftest import REPO_ROOT
from iap.data import contracts as C
from iap.data.contracts import (
    AvailabilityRule,
    ContractError,
    DatasetContract,
    FeatureContract,
    MissingContractError,
)

DATA_DIR = REPO_ROOT / "python" / "src" / "iap" / "data"


@pytest.fixture
def clean_registry():
    saved = (C.datasets(), C.features(), dict(C._FUNCTIONS))
    C.reset_registry()
    yield
    C.reset_registry()
    C._DATASETS.update(saved[0])
    C._FEATURES.update(saved[1])
    C._FUNCTIONS.update(saved[2])


def _ds(**kw) -> DatasetContract:
    base = dict(
        dataset_id="vendor.daily",
        source="vendor-x",
        availability=AvailabilityRule("period_end", 1),
        adjustment="split_dividend",
        missing_policy="ineligible",
        route="daily_pit",
        leakage_test_id="LK-CAUSALITY",
        fields=("open", "close"),
    )
    base.update(kw)
    return DatasetContract(**base)


def _fc(**kw) -> FeatureContract:
    base = dict(
        feature_id="rev.r1",
        inputs=("vendor.daily",),
        availability=AvailabilityRule("inputs", 0),
        adjustment="split_dividend",
        missing_policy="ineligible",
        route="daily_pit",
        leakage_test_id="LK-FUTURE-SHIFT",
        lookback_periods=1,
    )
    base.update(kw)
    return FeatureContract(**base)


def test_availability_rule_lower_bounds_by_basis():
    assert AvailabilityRule("receive_ts", 0).available_index(7) == 7
    assert AvailabilityRule("period_end", 1).available_index(7) == 8
    assert AvailabilityRule("publication_date", 2).available_index(7) == 9
    with pytest.raises(ContractError, match="lag 0 by definition"):
        AvailabilityRule("receive_ts", 1)
    for basis in ("period_end", "publication_date"):
        with pytest.raises(ContractError, match="lag_periods >= 1"):
            AvailabilityRule(basis, 0)
    with pytest.raises(ContractError):
        AvailabilityRule("period_end", -1)
    with pytest.raises(ContractError):
        AvailabilityRule("sometime", 1)


@pytest.mark.parametrize(
    "bad, match",
    [
        (dict(dataset_id="Bad Id"), "dataset_id"),
        (dict(source=""), "source"),
        (dict(availability=AvailabilityRule("inputs", 0)), "basis"),
        (dict(adjustment="magic"), "adjustment"),
        (dict(missing_policy="guess"), "missing_policy"),
        (dict(route="mixed"), "route"),
        (dict(leakage_test_id="LK-NONE"), "leakage_test_id"),
        (dict(missing_policy="forward_fill"), "missing_max_periods >= 1"),
        (dict(missing_max_periods=3), "forward_fill only"),
        (dict(fields=("a", "a")), "unique"),
        (dict(period="tick"), "period"),
    ],
)
def test_dataset_contract_refuses_malformed_declarations(bad, match):
    with pytest.raises(ContractError, match=match):
        _ds(**bad)


def test_dataset_contract_round_trips_to_a_document():
    d = _ds(missing_policy="forward_fill", missing_max_periods=2)
    doc = d.to_dict()
    assert doc["availability"] == {"basis": "period_end", "lag_periods": 1}
    assert doc["fields"] == ["open", "close"] and doc["missing_max_periods"] == 2


def test_feature_registration_resolves_lineage_and_refuses_unknown_inputs(clean_registry):
    with pytest.raises(
        ContractError, match="neither a registered dataset nor a registered feature"
    ):

        @C.feature(_fc())
        def feature_rev_r1(frame):
            return frame["close"]

    C.dataset(_ds())

    @C.feature(_fc())
    def feature_rev_r1(frame):  # noqa: F811
        return frame["close"].pct_change()

    c = C.contract_of(feature_rev_r1)
    assert c.source == "vendor-x" and c.total_lag_periods == 1  # dataset lag 1 + feature lag 0
    assert C.contract_of("rev.r1") is c and C.contract_of("vendor.daily") == _ds()

    @C.feature(
        _fc(feature_id="rev.r1_z", inputs=("rev.r1",), availability=AvailabilityRule("inputs", 1))
    )
    def feature_rev_r1_z(frame):
        return frame["close"]

    z = C.contract_of("rev.r1_z")
    assert z.total_lag_periods == 2 and z.source == "vendor-x"
    doc = C.lineage("rev.r1_z")
    assert doc["inputs"]["rev.r1"]["inputs"]["vendor.daily"]["source"] == "vendor-x"
    with pytest.raises(MissingContractError):
        C.contract_of("nothing.here")
    with pytest.raises(MissingContractError):
        C.contract_of(lambda frame: frame)


def test_feature_decorator_enforces_name_route_and_identity(clean_registry):
    C.dataset(_ds())
    C.dataset(
        _ds(dataset_id="iex.bars", route="iex_hist", availability=AvailabilityRule("receive_ts", 0))
    )
    with pytest.raises(ContractError, match="must be named 'feature_rev_r1'"):

        @C.feature(_fc())
        def compute_rev(frame):
            return frame

    with pytest.raises(ContractError, match="one route per experiment"):

        @C.feature(_fc(feature_id="mixed.x", inputs=("vendor.daily", "iex.bars"), route="iex_hist"))
        def feature_mixed_x(frame):
            return frame

    @C.feature(_fc())
    def feature_rev_r1(frame):
        return frame

    with pytest.raises(ContractError, match="already registered with a different contract"):

        @C.feature(_fc(lookback_periods=5))
        def feature_rev_r1(frame):  # noqa: F811
            return frame

    with pytest.raises(ContractError, match="already a feature id"):
        C.dataset(_ds(dataset_id="rev.r1"))
    with pytest.raises(ContractError, match="basis is 'inputs'"):
        _fc(availability=AvailabilityRule("period_end", 1))


def _import_source(tmp_path, name: str, body: str):
    path = tmp_path / f"{name}.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(name, None)
    return mod


def test_a_feature_module_without_a_contract_fails_import(tmp_path, clean_registry):
    with pytest.raises(MissingContractError, match="feature_naked"):
        _import_source(
            tmp_path,
            "naked_features",
            """
            from iap.data.contracts import enforce_module
            def feature_naked(frame):
                return frame["close"]
            enforce_module(__name__)
            """,
        )
    assert "naked_features" not in sys.modules
    # the same module with a contract imports, and enforce_module counts it
    mod = _import_source(
        tmp_path,
        "dressed_features",
        """
        from iap.data.contracts import AvailabilityRule, DatasetContract, FeatureContract, dataset, enforce_module, feature
        dataset(DatasetContract("t.ds", "synthetic", AvailabilityRule("receive_ts", 0), "none", "ineligible",
                                "synthetic", "LK-CAUSALITY", ("close",)))
        @feature(FeatureContract("t.f", ("t.ds",), AvailabilityRule("inputs", 0), "none", "ineligible",
                                 "synthetic", "LK-CAUSALITY"))
        def feature_t_f(frame):
            return frame["close"]
        N = enforce_module(__name__)
        """,
    )
    assert mod.N == 1


def test_every_feature_module_under_iap_data_calls_enforce_module():
    """Architecture test: the import-time enforcement cannot be forgotten."""
    defines = re.compile(r"^def feature_", re.M)
    for path in sorted(DATA_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if defines.search(text):
            assert re.search(r"^enforce_module\(__name__\)", text, re.M), (
                f"{path.name} defines feature functions but does not end with enforce_module(__name__)"
            )


def test_validate_feature_output_and_causality_probe_bounds(clean_registry):
    C.dataset(
        _ds(dataset_id="s.ds", route="synthetic", availability=AvailabilityRule("receive_ts", 0))
    )

    @C.feature(
        _fc(feature_id="s.bad_shape", inputs=("s.ds",), route="synthetic", lookback_periods=0)
    )
    def feature_s_bad_shape(frame):
        return frame["close"].iloc[:-1]

    frame = pd.DataFrame({"close": [1.0, 2.0, 3.0, 4.0]})
    with pytest.raises(ContractError, match="aligned"):
        C.check_causality(feature_s_bad_shape, frame)

    @C.feature(_fc(feature_id="s.ok", inputs=("s.ds",), route="synthetic", lookback_periods=0))
    def feature_s_ok(frame):
        return frame["close"] * 2

    assert C.check_causality(feature_s_ok, frame, probes=[0, 3])["probes"] == 2
    with pytest.raises(ContractError, match="outside the frame"):
        C.check_causality(feature_s_ok, frame, probes=[4])
