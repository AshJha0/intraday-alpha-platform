"""v1.15: a FEED-derived latency table used as the ORDER-ENTRY leg warns."""

from __future__ import annotations

import warnings

import pytest
from iap.execution import ExecutionSimulator
from iap.execution.calibration import (
    FEED_LATENCY_SOURCE,
    SCHEMA,
    VERSION,
    ExecCalibration,
    FeedLatencyAsOrderLegWarning,
)
from test_execution_rules import VEN, make_config


def _cal(source: str) -> ExecCalibration:
    row = {"probs": [0.0, 1.0], "quantiles_ns": [100, 200], "source": source}
    return ExecCalibration.from_dict(
        {"schema": SCHEMA, "version": VERSION, "latency": {str(VEN): row}}
    )


def test_feed_derived_table_warns():
    cal = _cal(FEED_LATENCY_SOURCE)
    assert cal.feed_derived_latency_venues() == (VEN,)
    with pytest.warns(FeedLatencyAsOrderLegWarning, match="ORDER-ENTRY"):
        ExecutionSimulator(make_config(), cal)


def test_parametric_table_and_default_do_not_warn():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        ExecutionSimulator(make_config(), _cal("parametric_tail"))
        ExecutionSimulator(make_config())
