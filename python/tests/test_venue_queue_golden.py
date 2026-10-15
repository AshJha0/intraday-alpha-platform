"""``tests/golden/expected_venue_queue.json`` (v1.17 B9): the Python
execution simulator — the rule-4 reference — reproduces the golden it
generated, so the Rust venue simulator's parity test
(``rust/venue/tests/golden_venue_queue.rs``) compares against the reference
and not against a stale file."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def generator(golden_dir: Path):
    tools = golden_dir.parents[1] / "python" / "tools"
    sys.path.insert(0, str(tools))
    try:
        import make_golden_venue_queue as gen
    finally:
        sys.path.remove(str(tools))
    return gen


def test_venue_queue_golden_is_what_the_reference_produces(golden_dir: Path, generator) -> None:
    committed = json.loads((golden_dir / "expected_venue_queue.json").read_text(encoding="utf-8"))
    assert committed["x-version"] == 1
    built = generator.build()
    assert built == committed
    names = [v["name"] for v in committed["vectors"]]
    assert len(names) == len(set(names)) >= 5
    for vec in committed["vectors"]:
        assert vec["expected_fills"], f"{vec['name']}: a vector without fills pins nothing"


def test_venue_queue_golden_covers_the_rule_4_paths(golden_dir: Path) -> None:
    """The pinned behaviours: pay-down with no fill, fills at and through the
    level, a sibling served from one print, an aggressive leg followed by a
    crossing fill at our limit, and a resting order left with a non-zero
    queue after cancels of orders behind it."""
    g = json.loads((golden_dir / "expected_venue_queue.json").read_text(encoding="utf-8"))
    by_name = {v["name"]: v for v in g["vectors"]}
    paydown = by_name["execute_paydown_at_and_through_our_level"]
    assert [(f["qty"], f["price_ticks"]) for f in paydown["expected_fills"]] == [
        (60, 2450),
        (40, 2450),
    ]
    cross = by_name["crossing_check_exemption_and_overlay"]
    assert [f["liquidity"] for f in cross["expected_fills"]] == ["taker", "maker", "maker"]
    assert [f["qty"] for f in cross["expected_fills"]] == [200, 60, 40]
    # one print is ONE pool: the earlier sibling is served first and the
    # later one only pays its (double-counted, as the reference pins) queue
    sibling = by_name["sibling_queue_one_print_served_in_order"]
    assert [(f["order_id"], f["qty"]) for f in sibling["expected_fills"]] == [(1, 100)]
    assert sibling["expected_final"][1] == {
        "order_id": 2,
        "remaining": 50,
        "resting": True,
        "ahead_qty": 400,
    }
    credit = by_name["cancel_and_modify_credit_only_from_orders_ahead"]
    assert [f["qty"] for f in credit["expected_fills"]] == [30, 10]
