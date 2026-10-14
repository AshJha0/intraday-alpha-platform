"""v1.16 item 17: MVP end-of-day flatten (``execution.eod_flatten_lead_ns``, opt-in)."""

from __future__ import annotations

from pathlib import Path

import pytest
from iap.mvp.config import MvpConfig
from iap.mvp.feed import generate_feed
from iap.mvp.session import run_session

pytestmark = pytest.mark.slow

REPO_ROOT = Path(__file__).resolve().parents[2]
TINY_CONFIG = REPO_ROOT / "configs" / "mvp" / "mvp_tiny.json"


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    cfg = MvpConfig.load(TINY_CONFIG, repo_root=REPO_ROOT)
    base = tmp_path_factory.mktemp("mvp-eod")
    feed = generate_feed(cfg, base / "feed")
    lead = cfg.session.length_ns // 3
    flat = cfg.with_overrides(eod_flatten_lead_ns=lead)
    return {
        "base": run_session(cfg, feed, base / "base"),
        "eod": run_session(flat, feed, base / "eod"),
    }


def test_default_is_off_and_unreported(runs):
    r = runs["base"]
    assert r.config.execution.eod_flatten_lead_ns is None
    assert "eod_flatten" not in r.report["counts"]


def test_flatten_window_targets_flat(runs):
    e = runs["eod"].report["counts"]["eod_flatten"]
    assert e["decisions"] > 0
    base_pos = abs(runs["base"].engine.account.position)
    assert abs(e["residual_position"]) <= base_pos
    if base_pos:
        assert e["parents"] >= 1


def test_config_refuses_non_positive_lead():
    cfg = MvpConfig.load(TINY_CONFIG, repo_root=REPO_ROOT)
    with pytest.raises(ValueError):
        cfg.with_overrides(eod_flatten_lead_ns=0)
