"""Integration level: the release workflow's ``verify-ci`` guard.

``tests/harness/verify_ci_green.py`` decides whether a commit may be
released. Its decision table has four rows — the API did not answer, a green
run exists, ci ran and did not succeed, ci is still running or never ran —
and the first version of the guard collapsed two of them: an API 504 read as
"no successful run" and failed the v1.4.0 release once. The script takes its
query, clock and sleep as parameters, so every row is driven here from a
scripted sequence of API answers, with no network and no waiting; the last
tests pin the workflow wiring (the manual trigger is a dry run and cannot
publish).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SHA = "b2dae0006996da25eb0ceb6d220c1c2626f5306b"


def _load():
    path = ROOT / "tests" / "harness" / "verify_ci_green.py"
    spec = importlib.util.spec_from_file_location("verify_ci_green", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module  # dataclasses resolve annotations through it
    spec.loader.exec_module(module)
    return module


guard = _load()


def body(*runs: tuple[int, str, str | None]) -> str:
    return json.dumps(
        {
            "total_count": len(runs),
            "workflow_runs": [
                {
                    "id": run_id,
                    "status": status,
                    "conclusion": conclusion,
                    "event": "push",
                    "html_url": f"https://github.com/o/r/actions/runs/{run_id}",
                }
                for run_id, status, conclusion in runs
            ],
        }
    )


class Harness:
    """A scripted API and a fake clock that only ``sleep`` advances."""

    def __init__(self, answers: list) -> None:
        self.answers = list(answers)
        self.calls = 0
        self.now = 0.0
        self.sleeps: list[float] = []
        self.lines: list[str] = []

    def query(self) -> str:
        self.calls += 1
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def run(self, **kwargs) -> int:
        return guard.verify(
            self.query,
            sha=SHA,
            sleep=self.sleep,
            clock=lambda: self.now,
            out=self.lines.append,
            **kwargs,
        )

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def test_a_green_run_passes_at_once() -> None:
    h = Harness([body((7, "completed", "success"))])
    assert h.run() == guard.EXIT_GREEN
    assert (h.calls, h.sleeps) == (1, [])
    assert "green: run 7" in h.text


def test_api_errors_are_retried_with_backoff_and_then_the_answer_counts() -> None:
    h = Harness(
        [
            guard.ApiError("gh exit 1: HTTP 504"),
            "<html>504 Gateway Time-out</html>",
            json.dumps({"message": "Server Error"}),
            body((7, "completed", "success")),
        ]
    )
    assert h.run() == guard.EXIT_GREEN
    assert h.calls == 4
    assert h.sleeps == [15.0, 30.0, 45.0]
    assert "API error 1/10" in h.text and "API error 3/10" in h.text
    assert "::error::" not in h.text


def test_an_api_that_never_answers_is_undetermined_not_red() -> None:
    h = Harness([guard.ApiError("gh exit 1: HTTP 504")])
    assert h.run() == guard.EXIT_UNDETERMINED
    assert h.calls == 10
    # nine waits before the tenth attempt: 15 + 30 + ... capped at 120 s
    assert h.sleeps == [15.0, 30.0, 45.0, 60.0, 75.0, 90.0, 105.0, 120.0, 120.0]
    assert sum(h.sleeps) == 660.0
    assert "could not be determined" in h.text


def test_a_failed_run_fails_immediately_with_its_url() -> None:
    h = Harness([body((7, "completed", "failure"), (8, "completed", "cancelled"))])
    assert h.run() == guard.EXIT_NOT_GREEN
    assert (h.calls, h.sleeps) == (1, [])
    assert "did not succeed" in h.text
    assert "https://github.com/o/r/actions/runs/7" in h.text
    assert "run 8 (push): cancelled" in h.text


def test_one_green_run_is_enough_beside_a_cancelled_one() -> None:
    h = Harness([body((7, "completed", "cancelled"), (8, "completed", "success"))])
    assert h.run() == guard.EXIT_GREEN


def test_a_run_in_progress_is_polled_until_it_succeeds() -> None:
    h = Harness(
        [
            body((7, "queued", None)),
            body((7, "in_progress", None)),
            body((7, "completed", "success")),
        ]
    )
    assert h.run() == guard.EXIT_GREEN
    assert h.sleeps == [30.0, 30.0]


def test_a_run_in_progress_is_polled_until_it_fails() -> None:
    h = Harness([body((7, "in_progress", None)), body((7, "completed", "failure"))])
    assert h.run() == guard.EXIT_NOT_GREEN
    assert h.sleeps == [30.0]
    assert "did not succeed" in h.text


def test_a_run_that_never_finishes_fails_after_the_budget() -> None:
    h = Harness([body((7, "in_progress", None))])
    assert h.run(in_progress_budget=120.0) == guard.EXIT_NOT_GREEN
    assert h.sleeps == [30.0] * 4
    assert "still in progress after 120s" in h.text
    assert "https://github.com/o/r/actions/runs/7" in h.text


def test_no_run_at_all_fails_after_the_grace_with_a_clear_message() -> None:
    h = Harness([body()])
    assert h.run() == guard.EXIT_NOT_GREEN
    assert sum(h.sleeps) == 120.0
    assert f"no ci run exists for {SHA}" in h.text


def test_a_run_that_appears_during_the_grace_is_seen() -> None:
    h = Harness([body(), body((7, "completed", "success"))])
    assert h.run() == guard.EXIT_GREEN


def test_api_failures_are_counted_consecutively() -> None:
    answers: list = []
    for _ in range(6):
        answers += [guard.ApiError("HTTP 504"), body((7, "in_progress", None))]
    answers.append(body((7, "completed", "success")))
    h = Harness(answers)
    assert h.run(api_attempts=2) == guard.EXIT_GREEN


@pytest.mark.parametrize(
    "text",
    ["", "not json", "[]", '{"workflow_runs": 3}', '{"workflow_runs": [{"id": "x"}]}'],
)
def test_malformed_bodies_are_api_errors_never_an_empty_run_list(text: str) -> None:
    with pytest.raises(guard.ApiError):
        guard.parse_runs(text)


def test_arguments_are_validated_before_any_query() -> None:
    assert guard.main(["--repo", "o/r", "--sha", "v1.4.0"]) == guard.EXIT_NOT_GREEN
    assert guard.main(["--repo", "o r; rm", "--sha", SHA]) == guard.EXIT_NOT_GREEN


def test_the_manual_trigger_is_a_dry_run_that_cannot_publish() -> None:
    doc = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text())
    triggers = doc.get(True, doc.get("on"))
    assert triggers["push"]["tags"] == ["v*"]
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert inputs["dry_run"]["default"] is True
    assert inputs["sha"]["required"] is True
    jobs = doc["jobs"]
    for name in ("images", "manifest"):
        assert jobs[name]["if"] == "github.event_name == 'push'"
    assert jobs["images"]["needs"] == "verify-ci"
    assert jobs["manifest"]["needs"] == "images"
    verify_steps = jobs["verify-ci"]["steps"]
    assert any("verify_ci_green.py" in str(s.get("run", "")) for s in verify_steps)
    # only the verify job may run on a manual trigger, and it writes nothing
    assert jobs["verify-ci"]["permissions"] == {"contents": "read", "actions": "read"}
