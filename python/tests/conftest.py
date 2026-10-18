"""Shared fixtures for the iap reference test suite."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parents[1]
GOLDEN_DIR = REPO_ROOT / "tests" / "golden"
CONFIGS_DIR = REPO_ROOT / "configs"

sys.path.insert(0, str(TESTS_DIR))  # for bruteforce_book

from iap.core.events import EventType, MarketEvent  # noqa: E402
from iap.reference.refdata import ReferenceData  # noqa: E402


@pytest.fixture(autouse=True)
def _alpha_universe_is_restored():
    """``iap.alpha.configure_universe`` sets process-global state: a test that
    leaves it pointed at another dataset changes every later alpha test in
    the same (xdist) worker.  Restore it and fail the leaking test."""
    from iap.alpha.base import _UNIVERSE, configure_universe

    before = dict(_UNIVERSE)
    yield
    after = dict(_UNIVERSE)
    if after != before:
        configure_universe(None)
        pytest.fail(f"test left the alpha universe changed: {after} (was {before})")


@pytest.fixture(scope="session")
def refdata() -> ReferenceData:
    return ReferenceData.load(CONFIGS_DIR)


@pytest.fixture(scope="session")
def golden_dir() -> Path:
    assert GOLDEN_DIR.is_dir(), f"missing {GOLDEN_DIR}"
    return GOLDEN_DIR


def mkev(
    seq: int,
    event_type: int,
    side: int = 0,
    price: int = 0,
    qty: int = 0,
    order_id: int = 0,
    trade_id: int = 0,
    instrument_id: int = 1,
    venue_id: int = 1,
    ts: int = 0,
) -> MarketEvent:
    """Convenience MarketEvent factory for unit tests."""
    ts = ts or 1_700_000_000_000_000_000 + seq * 1_000_000
    return MarketEvent(
        event_id=seq,
        instrument_id=instrument_id,
        venue_id=venue_id,
        exchange_ts=ts,
        receive_ts=ts + 150_000,
        sequence=seq,
        event_type=int(event_type),
        side=int(side),
        price_ticks=price,
        qty=qty,
        order_id=order_id,
        trade_id=trade_id,
    )


def add(seq, side, price, qty, oid, **kw):
    return mkev(seq, EventType.ADD, side, price, qty, oid, **kw)


#: modules that test the holdout registry itself, or rely on its refusals, run unpatched
_REAL_REGISTRY_MODULES = {"test_holdout_registry", "test_v116_lob_real", "test_v117_screen"}


@pytest.fixture(autouse=True)
def _fixture_datasets_are_not_registered_real_data(request, monkeypatch, tmp_path):
    """Test fixtures build throw-away dataset directories (often without a dataset.json)
    that the holdout registry cannot know.  Outside the registry's own tests an
    UNREGISTERED directory is treated as synthetic; a directory the registry does know
    (by version, content hash or session) is still judged by it."""
    if request.module.__name__.rsplit(".", 1)[-1] in _REAL_REGISTRY_MODULES:
        return
    from iap.research import holdout_registry as hr

    monkeypatch.setenv(hr.LOG_ENV, str(tmp_path / "holdout_access.jsonl"))
    real_decide = hr._decide

    def decide(*a, **k):
        try:
            return real_decide(*a, **k)
        except hr.HoldoutRefused as exc:
            unknown = (
                "no dataset.json" in str(exc)
                or "not in research/holdout_registry.json" in str(exc)
                or "no session / symbol selected" in str(exc)
            )
            if unknown and Path(a[0]).exists():
                return {"state": "synthetic", "reason": "test fixture: unregistered"}
            raise

    monkeypatch.setattr(hr, "_decide", decide)
