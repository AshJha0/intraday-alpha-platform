"""v1.16: quoting v2 outcomes are derived from the pre-registered verdicts, not typed."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "qv2o", ROOT / "tools" / "quoting_v2_record_outcome.py"
)
qv2o = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qv2o)


def test_outcome_mapping():
    assert qv2o.outcome_of("EXISTS (in-sample; worth an out-of-sample test)") == "confirmed"
    assert qv2o.outcome_of("SKEW INERT (validity gate failed; no verdict)") == "inconclusive"
    assert qv2o.outcome_of("NO DEMONSTRATED QUOTING EDGE") == "rejected"


def test_committed_result_maps_every_registered_cell():
    rows = qv2o.plan(ROOT)
    assert len(rows) == 4
    assert {r[1] for r in rows} == {"rejected"}
    assert all(r[2].startswith("research/quoting_real/v2_summary.json#") for r in rows)
