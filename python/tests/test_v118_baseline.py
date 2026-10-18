"""v1.18 REL-01: the baseline tool recomputes the published numbers from raw JSON.

Pins the tool's day-cluster statistic against ``cluster_mean_stats`` (the one the B8 report used),
checks that the committed ``research/v118/baseline_metrics.json`` is current, and that the ledger
chain recomputes.
"""

from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path

import pytest
from iap.research.execution_benchmark import cluster_mean_stats

ROOT = Path(__file__).resolve().parents[2]


def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "v118_baseline", ROOT / "tools" / "v118_baseline.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def tool():
    return _load_tool()


def test_cluster_mean_matches_the_projects_statistic(tool):
    rows = [("a", 1.0), ("a", 3.0), ("b", -2.0), ("b", 0.5), ("c", 4.0), ("c", 2.5)]
    mine = tool.cluster_mean(rows)
    ref = cluster_mean_stats([v for _, v in rows], [g for g, _ in rows])
    assert mine["mean"] == pytest.approx(ref["mean"], abs=1e-12)
    assert mine["se"] == pytest.approx(ref["se"], abs=1e-12)
    assert mine["ci95"][0] == pytest.approx(ref["ci95"][0], abs=1e-3)
    assert mine["ci95"][1] == pytest.approx(ref["ci95"][1], abs=1e-3)
    assert mine["n_clusters"] == ref["n_clusters"] == 3


def test_single_cluster_has_no_se(tool):
    out = tool.cluster_mean([("a", 1.0), ("a", 2.0)])
    assert math.isnan(out["se"])


def test_frozen_baseline_recomputes_where_its_inputs_are_unchanged(tool):
    """The baseline is frozen history (REL-01): later PRs may change ci.yml, the lock
    file or a research artefact. Each section is recomputed only while the input file
    it was built from still has the recorded sha256; a changed input means the
    section describes an earlier tree and is skipped, never silently rewritten."""
    committed = json.loads(
        (ROOT / "research/v118/baseline_metrics.json").read_text(encoding="utf-8")
    )
    fresh = json.loads(json.dumps(tool.build()))
    checked = 0
    for section in ("ledger", "r7_confirm", "alpha_registry"):
        if committed[section]["sha256"] == fresh[section]["sha256"]:
            assert committed[section] == fresh[section], section
            checked += 1
    for sym, block in committed["b8_oos3"].items():
        if block["sha256"] == fresh["b8_oos3"][sym]["sha256"]:
            assert block == fresh["b8_oos3"][sym], sym
            checked += 1
    assert checked >= 1, "every baseline input has changed; regenerate a new baseline"


def test_ledger_chain_recomputes_and_counts_agree():
    doc = json.loads((ROOT / "research/v118/baseline_metrics.json").read_text(encoding="utf-8"))
    led = doc["ledger"]
    assert led["chain_ok"] is True
    assert led["total_looks"] == led["sum_of_entry_counts"]
    assert sum(x["looks"] for x in led["document_datasets_summary"]) == led["total_looks"]


def test_b8_headline_numbers_reproduce_the_report():
    doc = json.loads((ROOT / "research/v118/baseline_metrics.json").read_text(encoding="utf-8"))
    aapl = doc["b8_oos3"]["AAPL"]["pairs"]["sor-pin_nasdaq"]["shortfall_bps"]
    assert aapl["mean"] == pytest.approx(2.20, abs=0.005)
    assert aapl["se"] == pytest.approx(0.71, abs=0.005)
    msft = doc["b8_oos3"]["MSFT"]["pairs"]["sor_sweep-pin_nasdaq"]["shortfall_bps"]
    assert msft["mean"] == pytest.approx(1.22, abs=0.005)
