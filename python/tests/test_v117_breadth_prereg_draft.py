"""v1.17 C8: the breadth-book pre-registration DRAFT is well formed and the
registration path's screen gate would refuse it (a dry run: nothing is
registered, nothing is signed, no data is read)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from iap.research.breadth_book import SCORE_CANDIDATE, screen_check
from iap.research.screen import ScreenRefusal, require_screened

REPO = Path(__file__).resolve().parents[2]
DRAFT = REPO / "research" / "preregs" / "breadth_book_prereg_DRAFT.json"
C6_DRAFT = REPO / "research" / "preregs" / "net_of_cost_screen_prereg_DRAFT.json"
SCREEN = REPO / "research" / "breadth" / "screen" / "screen_multi7.json"
HOLDOUT_DAYS = [
    "2025-11-28",
    "2025-12-08",
    "2025-12-09",
    "2025-12-10",
    "2025-12-11",
    "2025-12-12",
    "2026-05-15",
    "2026-05-18",
    "2026-06-12",
]


@pytest.fixture(scope="module")
def draft() -> dict:
    return json.loads(DRAFT.read_text(encoding="utf-8"))


def test_draft_is_unsigned_with_option_a_chosen(draft: dict) -> None:
    assert draft["schema"] == json.loads(C6_DRAFT.read_text(encoding="utf-8"))["schema"]
    assert draft["status"].startswith("DRAFT")
    assert draft["signed"] is False and draft["signature"] is None
    assert draft["on_blackboard"] is False
    # the owner chose option (a) on 2026-10-16; signing is still the owner's
    assert draft["decision_required"]["chosen"]["option"] == "a"
    assert set(draft["decision_required"]["options"]) == {"a", "b", "c"}


def test_shared_prereg_keys(draft: dict) -> None:
    c6 = json.loads(C6_DRAFT.read_text(encoding="utf-8"))
    shared = {"schema", "kind", "status", "signed", "signature", "on_blackboard", "plan_item"}
    shared |= {"drafted", "drafted_by", "hypothesis", "statistic", "ordering", "open_for_owner"}
    assert shared <= set(c6) and shared <= set(draft)


def test_screen_dependency_matches_artefact(draft: dict) -> None:
    dep = draft["screen_dependency"]
    assert hashlib.sha256(SCREEN.read_bytes()).hexdigest() == dep["sha256"]
    summary = json.loads(SCREEN.read_text(encoding="utf-8"))["summary"]
    s = summary
    quoted = f"{s['n_pass']} pass, {s['n_fail']} fail, {s['n_not_screened']} not screened"
    assert dep["status"] == quoted
    assert draft["decision_required"]["screen_result"].startswith(quoted)
    assert s["n_candidates"] == 23 and s["passed"] == []


def test_holdout_days_and_no_breadth_dataset_yet(draft: dict) -> None:
    h = draft["holdout"]
    assert h["days"] == HOLDOUT_DAYS
    assert h["breadth_dataset_versions"] == []
    assert (
        "ca5d079831be4e63aa76e350bb840a29a6964c0152f4bbbcce5010269a193854"
        in (h["existing_dataset_versions_3_names_spent"])
    )


def test_option_b_candidate_numbers_match_screen(draft: dict) -> None:
    b = draft["decision_required"]["options"]["b"]
    doc = json.loads(SCREEN.read_text(encoding="utf-8"))
    by_id = {c["id"]: c for c in doc["candidates"]}
    for cid, nums in b["screen_numbers"].items():
        c = by_id[cid]
        assert not c["passed"]
        assert round(c["net"]["tstat_day_cluster"], 2) == nums["net_t_day"]
        assert round(c["net"]["ic"], 4) == nums["net_ic"]
    # the named candidate is the best net day-clustered t among the book's scores
    book_ids = [i for i in by_id if i.split("/")[0] in {a for a, _ in SCORE_CANDIDATE.values()}]
    scored = [i for i in book_ids if by_id[i].get("net")]
    best = max(scored, key=lambda i: by_id[i]["net"]["tstat_day_cluster"])
    assert b["candidate"].startswith(best)


@pytest.mark.parametrize(
    "candidate", ["EQ11/15m/linear_z_v1", "EQ10/1s/linear_z_v1", "EQ10/1s/linear_z_v2"]
)
def test_registration_dry_run_refuses_every_book_candidate(candidate: str) -> None:
    """Option (a) in practice: the screen gate the registration and the
    confirmatory run share refuses each book candidate before any read."""
    alpha, horizon, model = candidate.split("/")
    with pytest.raises(ScreenRefusal):
        require_screened(SCREEN, alpha, horizon, model)
    block = screen_check(SCREEN, True, candidate, "eq11")
    assert block is not None and "refused" in block
