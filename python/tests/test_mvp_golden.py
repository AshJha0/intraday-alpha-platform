"""Golden: ``python -m iap.mvp run`` on ``configs/mvp/mvp.json`` reproduces
``tests/golden/expected_mvp.json`` (x-version 1) — the config hash, the
event-stream sha256 / data_version, the event count, the trace digest, the
first / last trace ids, the counts by rule / venue / algo and the full
report (floats at abs/rel 1e-9, everything else exact) — and the incident
replay from the captured ``events.jsonl`` reproduces the digest.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from iap.mvp.__main__ import cmd_replay
from iap.mvp.config import REPO_ROOT, load_config
from iap.mvp.feed import generate_feed
from iap.mvp.golden import GOLDEN_CONFIG_PATH, GOLDEN_VERSION, golden_document, render
from iap.mvp.session import RunResult, compare_runs, run_session

GOLDEN = REPO_ROOT / "tests" / "golden" / "expected_mvp.json"
TOL = 1e-9


@pytest.fixture(scope="module")
def golden() -> dict:
    assert GOLDEN.is_file(), f"missing {GOLDEN}"
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def golden_run(tmp_path_factory: pytest.TempPathFactory) -> RunResult:
    cfg = load_config()
    out = tmp_path_factory.mktemp("mvp-golden") / "run"
    feed = generate_feed(cfg, out)
    return run_session(cfg, feed, out)


def test_golden_file_is_canonical_and_versioned(golden: dict) -> None:
    assert golden["x-version"] == GOLDEN_VERSION
    assert render(golden) == GOLDEN.read_text(encoding="utf-8")
    assert golden["config_path"] == GOLDEN_CONFIG_PATH
    assert len(golden["trace_digest"]) == 64 and len(golden["config_version"]) == 64
    assert golden["report"]["x-version"] == 2


def test_run_reproduces_the_golden(golden: dict, golden_run: RunResult) -> None:
    actual = golden_document(golden_run)
    exact_keys = [k for k in golden if k != "report"]
    for key in exact_keys:
        assert actual[key] == golden[key], key
    diffs = compare_runs(golden["report"], actual["report"], tolerance=TOL)
    assert diffs == [], "\n".join(diffs)
    assert actual["n_events"] == golden["n_events"] == len(golden_run.feed.events)
    assert golden_run.trace_digest == golden["trace_digest"]


def test_config_in_force_matches_the_golden(golden: dict) -> None:
    cfg = load_config()
    assert cfg.run_id == golden["run_id"]
    assert cfg.config_version() == golden["config_version"]


def test_replay_from_captured_stream_reproduces_the_digest(golden: dict,
                                                          golden_run: RunResult) -> None:
    diffs = cmd_replay(golden_run.out_dir, golden_run.out_dir / "replay")
    assert diffs == [], "\n".join(diffs)
    replay_report = json.loads((golden_run.out_dir / "replay" / "report.json").read_text())
    assert replay_report["run"]["trace_digest"] == golden["trace_digest"]
    assert (golden_run.out_dir / "replay" / "traces.jsonl").read_bytes() == \
        (golden_run.out_dir / "traces.jsonl").read_bytes()


def test_golden_pins_the_honest_numbers(golden: dict) -> None:
    """The golden run trades, is fully traced and states its (cost-negative) result."""
    report = golden["report"]
    assert report["counts"]["n_parent_orders"] > 0 and report["counts"]["n_fills"] > 0
    assert set(golden["by_algo"]) == {"TWAP", "POV", "IS"}
    assert set(golden["by_venue"]) == {"XV1", "XV2", "XV3"}
    assert golden["by_rule"]["ALLOW"] == report["risk"]["allowed"]
    assert report["pnl"]["identity_abs_diff"] <= 1e-9
    assert report["alpha"]["cost_negative"] is True
    assert report["execution"]["n_orders_with_tca"] == report["counts"]["n_parent_orders"]
    assert Path(golden["config_path"]).name == "mvp.json"
    # IC audit (docs/MVP.md §7): every realized IC is the research label
    # definition, reported at the MVP horizon and at the fitted horizon with
    # the like-for-like gap; the shift-by-one IC of every alpha whose
    # realized IC is material collapses (|shifted| <= 0.25 |unshifted|) and
    # the cost-adjusted IC is far below the mid-to-mid IC for the alphas
    # the session traded on (the signal does not survive the spread).
    alpha = report["alpha"]
    assert alpha["ic_definition"].startswith("Pearson(expected_return, label)")
    for aid, row in alpha["per_alpha"].items():
        assert row["horizon"] == "1s" and row["at_research_horizon"]["horizon"] == \
            row["research_horizon"]
        assert row["ic_gap"] == pytest.approx(
            abs(row["at_research_horizon"]["realized_ic"] - row["research_ic"]), abs=1e-12)
        assert row["n_ic_samples"] <= row["n_signals"] <= report["counts"]["n_decisions"]
        if abs(row["realized_ic"]) >= 0.1:
            assert abs(row["realized_ic_shifted"]) <= 0.25 * abs(row["realized_ic"]), aid
    for aid in ("EQ01", "EQ03"):
        row = alpha["per_alpha"][aid]
        assert row["realized_ic"] > 0.2 and row["realized_ic_cost"] < 0.1
        assert row["realized_ic_cost"] < 0.5 * row["realized_ic"]
    ens = alpha["ensemble"]
    assert ens["realized_ic"] > 0.2 and abs(ens["realized_ic_shifted"]) < 0.05
