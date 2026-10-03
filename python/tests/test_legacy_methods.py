"""The ``legacy_v1`` method bundle reproduces v1.4.0.

v1.5.0 made the corrected research methods the defaults and kept every old
rule selectable by name.  "Selectable" is a claim about numbers, so it is
tested against artefacts pinned from the v1.4.0 release itself:

* ``tests/golden/alpha_report_EQ03_v1.4.0.json`` and
  ``alpha_report_FX01_v1.4.0.json`` are ``research/alpha_reports/EQ03.json``
  and ``FX01.json`` exactly as tagged ``v1.4.0`` (byte for byte).  The
  pipeline's own entry points (``run_all.research_backtester`` /
  ``run_all.alpha_report``) under ``methods("legacy_v1")`` must return every
  field of those documents on the bundled dataset — the same dataset
  v1.4.0 was computed on (``data_version`` unchanged).
* The default bundle must NOT: the same alpha under ``v2`` differs in the
  gate statistic, the threshold and the backtest, and the test names where.

Fields the v1.5.0 report adds are allowed beside the pinned ones; nothing the
v1.4.0 document holds may be missing or different.  Floats are compared at
1e-9 relative (the cross-platform tolerance of every data-derived pin in this
suite); everything else exactly.
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys

import pytest
from conftest import GOLDEN_DIR, REPO_ROOT
from iap.alpha.data import load_features
from iap.experiment.tracker import data_version
from iap.validation.methods import METHODS_LEGACY, METHODS_V2, methods

FEATURES_DIR = REPO_ROOT / "data" / "features"
#: ``data_version`` of the dataset v1.4.0 was computed on (CHANGELOG v1.4.0).
V140_DATA_VERSION_PREFIX = "116b7787"
#: Rank ICs are computed from ranks of float features: a last-ulp difference
#: between platforms can swap two near-tied ranks, which moves a rank IC by
#: ~1e-5 without touching any other statistic.
RANK_IC_ABS_TOL = 1e-4

pytestmark = pytest.mark.skipif(
    not FEATURES_DIR.is_dir() or not list(FEATURES_DIR.glob("features_1*.parquet")),
    reason="data/features missing — regenerate via python3 -m iap.features",
)


def _run_all():
    name = "research_alpha_reports_run_all"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "research" / "alpha_reports" / "run_all.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pipeline():
    run_all = _run_all()
    if not data_version(REPO_ROOT).startswith(V140_DATA_VERSION_PREFIX):
        pytest.skip("the bundled dataset is not the one v1.4.0 was computed on")
    return run_all, load_features(FEATURES_DIR), run_all._load_meta()


def _flatten(doc, prefix=""):
    if isinstance(doc, dict):
        for key, value in doc.items():
            yield from _flatten(value, f"{prefix}{key}.")
    elif isinstance(doc, list):
        yield f"{prefix}#", len(doc)
        for i, value in enumerate(doc):
            yield from _flatten(value, f"{prefix}{i}.")
    else:
        yield prefix[:-1], doc


def _report(pipeline, alpha_id: str, bundle_name: str, **kw) -> dict:
    run_all, frames, meta = pipeline
    bundle = methods(bundle_name)
    report = run_all.alpha_report(
        alpha_id, frames, run_all.research_backtester(bundle, meta), meta, bundle, **kw
    )
    # through JSON, as the pipeline writes it (NaN / numpy scalars normalised)
    return json.loads(json.dumps(report, sort_keys=True))


@pytest.mark.parametrize("alpha_id", ["EQ03", "FX01"])
def test_legacy_v1_reproduces_the_v140_alpha_report(pipeline, alpha_id):
    pinned = json.loads((GOLDEN_DIR / f"alpha_report_{alpha_id}_v1.4.0.json").read_text())
    got = dict(_flatten(_report(pipeline, alpha_id, METHODS_LEGACY)))
    want = dict(_flatten(pinned))
    assert len(want) > 150, "the pinned report is the full v1.4.0 document"
    missing = sorted(set(want) - set(got))
    assert not missing, (
        f"fields of the v1.4.0 report the legacy bundle no longer returns: {missing}"
    )
    wrong = []
    for key, expected in want.items():
        actual = got[key]
        if isinstance(expected, float) and isinstance(actual, (int, float)):
            tol = RANK_IC_ABS_TOL if key.endswith("rank_ic") else 0.0
            if not math.isclose(actual, expected, rel_tol=1e-9, abs_tol=max(tol, 1e-12)):
                wrong.append((key, expected, actual))
        elif actual != expected:
            wrong.append((key, expected, actual))
    assert not wrong, f"legacy_v1 differs from v1.4.0 in {len(wrong)} fields: {wrong[:8]}"
    # the headline of the pinned document, spelled out, and the rules the
    # report says it was computed under — each one the named legacy value
    assert got["verdict"] == pinned["verdict"]
    assert _report(pipeline, alpha_id, METHODS_LEGACY)["methods"] == {
        "block_rows": None,
        "cap_fills_at_l1": False,
        "capacity": "participation",
        "fold_diagnostics": False,
        "ic_rows": "valid_only",
        "impact_model": "linear",
        "position_policy": "sign",
        "recompute_probe": False,
        "significance": "within_bucket",
        "stress_version": 1,
        "tstat_threshold": "fixed",
    }


def test_the_default_bundle_is_not_the_legacy_one(pipeline):
    """Guard against the reproduction above passing because the bundle is
    ignored: under ``v2`` the same alpha is judged by other rules."""
    pinned = json.loads((GOLDEN_DIR / "alpha_report_EQ03_v1.4.0.json").read_text())
    legacy = _report(pipeline, "EQ03", METHODS_LEGACY)
    # the ledger policy needs the threshold its caller derived (any value
    # serves here: the statistics below do not depend on it)
    default = _report(pipeline, "EQ03", METHODS_V2, ledger_t=4.0, gate_looks=1000)
    assert default["methods"] != legacy["methods"]
    assert not set(default["methods"].values()) & {"sign", "linear", "within_bucket", "fixed"}
    # the gate reads another statistic: v1.4.0 gated on the within-bucket
    # NW t of the uncrossed book, v2 on the HAC t of the pooled slope ...
    assert legacy["gate_tstat"] == pytest.approx(pinned["nw_tstat_uncrossed"], rel=1e-9)
    assert abs(default["gate_tstat"] - pinned["nw_tstat_uncrossed"]) > 0.1
    # ... on other rows (BLACKOUT rows scored at their reopen return) ...
    assert legacy["gate_ic"] == pytest.approx(pinned["gate_ic"], rel=1e-9)
    assert default["gate_ic"] != pytest.approx(pinned["gate_ic"], rel=1e-4)
    assert default["gate_ic_valid_only"] == pytest.approx(pinned["gate_ic"], rel=1e-9)
    # ... and the backtest trades by another rule: the legacy sign policy
    # trades EQ03 thousands of times at a loss; under the cost-aware default
    # its forecast never clears the round-trip cost and it makes no trade
    assert pinned["net_pnl_1x_cost"] < 0.0
    assert legacy["trade_count_1x_cost"] > 1000
    assert legacy["net_pnl_1x_cost"] == pytest.approx(pinned["net_pnl_1x_cost"], rel=1e-9)
    assert default["trade_count_1x_cost"] < legacy["trade_count_1x_cost"]
    assert default["net_pnl_1x_cost"] != pytest.approx(pinned["net_pnl_1x_cost"], rel=1e-3)
