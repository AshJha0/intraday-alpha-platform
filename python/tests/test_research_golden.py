"""Golden reproduction of the ExperimentRunner (conventions §5;
tests/golden/expected_experiment_golden_frame.json).

The pinned spec (EQ03 at 5 s on instrument 1 of the golden equity vector,
``iap.research.golden``) is rebuilt and run here; the experiment id must
match exactly, every result float at 1e-9 abs/rel, everything else
exactly (``git_commit`` is the fixed provenance stand-in).  The golden's
own documents must validate against the research schemas, and the pinned
walk-forward metrics are cross-checked against ``validate_alpha`` called
directly, so the runner is proven to add no statistics of its own.
"""

from __future__ import annotations

import json
import math

import pytest
from conftest import CONFIGS_DIR, GOLDEN_DIR
from iap.alpha import build
from iap.backtest import Backtester, CostModel
from iap.contracts.types import ExperimentResult, ExperimentSpec
from iap.contracts.validate import validate, validate_typed
from iap.research import LOOKS_PER_EXPERIMENT, verify_experiment_id
from iap.research.golden import (
    GOLDEN_ALPHA,
    GOLDEN_COMMIT,
    GOLDEN_HORIZON,
    GOLDEN_INSTRUMENT,
    GOLDEN_VERSION,
    golden_document,
    golden_frames,
    golden_result,
    golden_spec,
    render_golden,
)
from iap.research.runner import document_drift, load_instrument_meta, restrict_frames
from iap.validation import validate_alpha
from iap.validation.ledger import ExperimentLedger
from iap.validation.methods import METHODS_V2, methods

TOL = 1e-9
GOLDEN = GOLDEN_DIR / "expected_experiment_golden_frame.json"


@pytest.fixture(scope="module")
def golden() -> dict:
    return json.loads(GOLDEN.read_text(encoding="ascii"))


@pytest.fixture(scope="module")
def frames():
    return golden_frames(GOLDEN_DIR, CONFIGS_DIR)


@pytest.fixture(scope="module")
def spec(frames) -> ExperimentSpec:
    return golden_spec(GOLDEN_DIR, frames)


@pytest.fixture(scope="module")
def result(spec, frames, tmp_path_factory) -> ExperimentResult:
    return golden_result(spec, frames, CONFIGS_DIR, tmp_path_factory.mktemp("golden"))


def _close(got: float, want: float) -> None:
    assert math.isfinite(got) and math.isfinite(want)
    assert abs(got - want) <= TOL + TOL * abs(want), f"{got} != {want}"


def test_golden_file_is_canonical_and_validates(golden):
    assert golden["x-version"] == GOLDEN_VERSION
    assert golden["source"]["alpha_id"] == GOLDEN_ALPHA
    assert golden["source"]["horizon"] == GOLDEN_HORIZON
    assert golden["source"]["instrument_id"] == GOLDEN_INSTRUMENT
    validate(golden["spec"], "research/experiment_spec.schema.json")
    validate(golden["result"], "research/experiment_result.schema.json")
    verify_experiment_id(ExperimentSpec.from_dict(golden["spec"]))
    # byte-canonical: re-rendering the parsed document reproduces the file
    assert render_golden(golden) == GOLDEN.read_text(encoding="ascii")


def test_golden_spec_reproduces(golden, spec):
    assert spec.experiment_id == golden["spec"]["experiment_id"]
    assert validate_typed(spec) == golden["spec"]


def test_golden_result_reproduces(golden, spec, result):
    want = golden["result"]
    got = validate_typed(result)
    assert got["git_commit"] == GOLDEN_COMMIT
    assert got["experiment_id"] == spec.experiment_id == want["experiment_id"]
    assert got["n_experiments_in_ledger"] == LOOKS_PER_EXPERIMENT
    assert set(got) == set(want)
    for key, expected in want.items():
        value = got[key]
        if isinstance(expected, bool) or not isinstance(expected, (int, float)):
            assert value == expected, key
        elif isinstance(expected, int):
            assert value == expected, key
        else:
            _close(value, expected)
    for key, expected in want["leakage_detail"].items():
        value = got["leakage_detail"][key]
        if isinstance(expected, float):
            _close(value, expected)
        else:
            assert value == expected, key


def test_golden_document_round_trips(golden, spec, result):
    """The regenerated document carries the golden's numbers: every float
    within 1e-9 (the last ulp of a BLAS reduction is CPU-dependent — the
    GitHub runner and a laptop differ there), everything else byte-equal.
    The file's own byte-canonical form is pinned separately by
    ``test_golden_file_is_canonical_and_validates``."""
    doc = golden_document(spec, result)
    assert document_drift(golden, doc) == []
    assert set(doc) == set(golden)

    # Structure and every non-float leaf are identical text after rendering
    # with floats masked.
    def _mask(node):
        if isinstance(node, bool):
            return node
        if isinstance(node, float):
            return "<float>"
        if isinstance(node, dict):
            return {k: _mask(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_mask(v) for v in node]
        return node

    assert render_golden(_mask(doc)) == render_golden(_mask(golden))


def test_golden_walk_forward_metrics_match_validate_alpha(golden, spec, frames):
    """The pinned IC / t / hit / turnover / folds are validate_alpha's own
    numbers for the same window and protocol — nothing is re-derived.

    "The same window" now means ``[train_period.start, test_period.start)``:
    the runner stopped handing the walk-forward the union of all three
    declared periods on 2026-09-20, because doing so trained the later folds
    inside the very holdout the result reports separately. Passing the full
    frames here would test the runner against a protocol it no longer uses.

    "The same protocol" is the spec's method bundle — ``v2``, the default
    since v1.5.0: the bundle's backtest and cost rules, its validation
    keywords, the spec's seed, and the ledger threshold of a fresh ledger
    (the Bonferroni |t| at this one experiment's looks).  The golden frames
    are in memory, so there are no normalized events and the recompute
    probe does not run — in the runner or here.  Under ``v2`` the result's
    ``ic`` / ``t_stat`` are the gate's (``gate_ic`` / ``gate_tstat``).
    """
    meta = load_instrument_meta(CONFIGS_DIR)
    cfg = spec.configuration
    assert cfg["methods"] == METHODS_V2
    bundle = methods(cfg["methods"])
    backtester = Backtester(
        bundle.cost_model(CostModel.load(CONFIGS_DIR / "execution" / "execution.json")),
        meta,
        bundle.backtest_config(
            latency_ns=cfg["latency_ns"],
            max_decision_age_ns=cfg["max_decision_age_ns"],
            flatten_at_session_end=cfg["flatten_at_session_end"],
        ),
    ).for_horizon(spec.horizon)
    ledger_t = ExperimentLedger.bonferroni_t_threshold_at(LOOKS_PER_EXPERIMENT)
    exec_cfg = json.loads((CONFIGS_DIR / "execution" / "execution.json").read_text())

    def factory():
        model = build(GOLDEN_ALPHA)
        model.horizon = GOLDEN_HORIZON
        return model

    window = restrict_frames(frames, spec.train_period.start_ts, spec.test_period.start_ts)
    report = validate_alpha(
        factory,
        window,
        backtester,
        meta,
        float(exec_cfg["defaults"]["max_participation"]),
        n_folds=cfg["n_folds"],
        embargo_ns=cfg["embargo_ns"],
        ledger_t_threshold=ledger_t,
        ledger_looks=LOOKS_PER_EXPERIMENT,
        seed=spec.seed,
        recompute=None,
        **bundle.validate_kwargs(),
    )
    want = golden["result"]
    _close(report["gate_ic"], want["ic"])
    _close(report["oos_rank_ic"], want["rank_ic"])
    _close(report["gate_tstat"], want["t_stat"])
    _close(report["oos_hit_rate"], want["hit_rate"])
    _close(report["turnover_flips_per_hour"], want["turnover"])
    _close(report["fold_sign_consistency"], want["fold_consistency"])
    assert report["nw_lags"] == want["nw_lags"]
    assert report["n_folds_run"] == want["n_folds"]
    assert report["verdict"] == want["verdict"]
    assert report["leakage"]["passed"] == want["leakage_passed"]
    assert document_drift(want["leakage_detail"], report["leakage"], tol=TOL) == []
    assert report["leakage"]["recompute_ok"] is None  # no events: the probe did not run
    assert report["gates"]["min_nw_tstat"] == max(3.0, ledger_t)
