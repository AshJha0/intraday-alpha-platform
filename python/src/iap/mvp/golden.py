"""The MVP golden document — shared by ``python/tools/make_golden_mvp.py``
(which writes ``tests/golden/expected_mvp.json``) and
``python/tests/test_mvp_golden.py`` (which reproduces it).

The document pins, for one run of ``configs/mvp/mvp.json``: the run and
session ids, the configuration hash, the event-stream sha256 / IAP1 sha256
/ ``data_version``, the event count and time bounds, the feature and model
versions, the trace digest with the first / last three trace ids, the
counts by risk rule / venue / algo, and the FULL report.  Ports reproduce
integers and hashes exactly and floats at abs/rel 1e-9.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from iap.mvp.session import RunResult

__all__ = ["GOLDEN_VERSION", "GOLDEN_CONFIG_PATH", "golden_document", "render"]

GOLDEN_VERSION = 1
GOLDEN_CONFIG_PATH = "configs/mvp/mvp.json"


def golden_document(res: RunResult) -> Dict[str, Any]:
    """The golden document for one run (pure function of the run)."""
    report = res.report
    return {
        "x-version": GOLDEN_VERSION,
        "description": (
            "MVP golden (python -m iap.mvp run on configs/mvp/mvp.json): the seeded "
            "feed, the full loop (book -> features -> EQ01/EQ03/EQ06 ensemble -> "
            "portfolio -> hard risk -> TWAP/POV/IS -> SOR -> simulator -> TCA -> "
            "attribution -> trace) and its report. Ports reproduce every value: "
            "integers and hashes exactly, floats at abs/rel 1e-9. Regenerate only on a "
            "deliberate, versioned change: PYTHONPATH=src python3 "
            "tools/make_golden_mvp.py --force."
        ),
        "config_path": GOLDEN_CONFIG_PATH,
        "run_id": res.config.run_id,
        "session_id": res.config.session_id,
        "seed": res.config.seed,
        "instrument": res.config.instrument,
        "instrument_id": res.feed.instrument_id,
        "config_version": report["run"]["config_version"],
        "data_version": res.feed.data_version,
        "events_sha256": res.feed.events_sha256,
        "events_iap1_sha256": res.feed.iap1_sha256,
        "n_events": len(res.feed.events),
        "first_ts": res.feed.first_ts,
        "last_ts": res.feed.last_ts,
        "feature_version": report["run"]["feature_version"],
        "model_version": report["run"]["model_version"],
        "trace_digest": res.trace_digest,
        "n_traces": len(res.engine.trace_ids),
        "first_trace_ids": res.engine.trace_ids[:3],
        "last_trace_ids": res.engine.trace_ids[-3:],
        "by_rule": report["risk"]["decisions_by_rule"],
        "by_venue": report["routing"]["venue_fill_qty"],
        "by_algo": {algo: row["n_orders"] for algo, row in report["execution"]["per_algo"].items()},
        "report": report,
    }


def render(doc: Dict[str, Any]) -> str:
    """The pinned on-disk rendering (2-space indent, sorted keys, ASCII, newline)."""
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n"
