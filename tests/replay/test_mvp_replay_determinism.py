"""Replay level: the MVP loop run twice from scratch produces identical bytes.

``python -m iap.mvp verify`` on the tiny configuration
(``configs/mvp/mvp_tiny.json``): two independent runs — seeded generation,
normalisation, books, features, alphas, portfolio, risk, execution, TCA,
traces — must agree on the event-stream sha256, the trace digest and every
number of the report (``iap.mvp.__main__.cmd_verify``).  A second check
re-runs the loop from the CAPTURED ``events.jsonl`` of one run (the incident
replay path, ``cmd_replay``) and asserts the same digest and the same
``traces.jsonl`` bytes.
"""
from __future__ import annotations

from iap.mvp.__main__ import cmd_replay, cmd_verify
from iap.mvp.config import load_config
from iap.mvp.feed import generate_feed
from iap.mvp.session import run_session

TINY = "configs/mvp/mvp_tiny.json"


def test_verify_two_runs_from_scratch_are_identical(repo_root):
    diffs = cmd_verify(repo_root / TINY, None, None)
    assert diffs == [], "\n".join(diffs)


def test_replay_from_captured_stream_is_byte_identical(repo_root, tmp_path):
    cfg = load_config(repo_root / TINY)
    out = tmp_path / "run"
    feed = generate_feed(cfg, out)
    first = run_session(cfg, feed, out)
    diffs = cmd_replay(out, tmp_path / "replay")
    assert diffs == [], "\n".join(diffs)
    for name in ("traces.jsonl", "report.json", "risk_audit.jsonl"):
        assert (tmp_path / "replay" / name).read_bytes() == (out / name).read_bytes(), name
    assert first.trace_digest == first.report["trace"]["digest"]
