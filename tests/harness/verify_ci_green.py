#!/usr/bin/env python3
"""Release guard: does a commit have a green ``ci`` run?

    python3 tests/harness/verify_ci_green.py --repo OWNER/NAME --sha <40 hex>

Used by the ``verify-ci`` job of ``.github/workflows/release.yml`` before any
image is built (GOVERNANCE promotion gate 10: an image is only ever built
from a tree whose full suite passed).  It asks the GitHub API for the runs of
``ci.yml`` at the commit and tells four situations apart, because they call
for four different reactions:

==========================  ================================================
what the API said           what the guard does
==========================  ================================================
nothing usable (a non-zero  retry with a growing backoff, up to
``gh`` exit, a 5xx, a body  ``--api-attempts`` consecutive failures (10, eleven
that is not the expected    minutes of waiting); then exit 2: "could not be
JSON)                       determined" is not "not green"
a completed, successful     exit 0
run exists
runs exist, none            exit 1 at once, naming every run and its
successful, none still      conclusion with its URL
running
a run is queued or in       poll every ``--poll-seconds`` until one succeeds,
progress                    all have finished, or ``--in-progress-budget``
                            seconds (40 min) have passed; then exit 1
no run at all               poll for ``--no-run-grace`` seconds (2 min: a run
                            is created a few seconds after the push), then
                            exit 1: an untested tree is not released
==========================  ================================================

Every attempt prints what it saw.  The first version of the guard was one
``gh run list ... --jq length`` whose empty output on an API 504 was compared
as a number: it failed the v1.4.0 release once although CI was green, and
passed unchanged on a re-run.

Standard library only (the job installs nothing); the clock, the sleep and
the query are parameters so the decision table is unit-tested without a
network (``tests/integration/test_release_guard.py``).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

WORKFLOW_FILE = "ci.yml"

EXIT_GREEN = 0
EXIT_NOT_GREEN = 1
EXIT_UNDETERMINED = 2

#: Backoff after the k-th consecutive API failure: 15 s, 30 s, ... capped at
#: 120 s.  Nine waits before the tenth attempt add up to 660 s.
BACKOFF_STEP_S = 15.0
BACKOFF_CAP_S = 120.0


class ApiError(Exception):
    """The API gave no usable answer (transport error, 5xx, malformed body)."""


@dataclass(frozen=True)
class Run:
    """One ``ci.yml`` run at the commit, as the API reports it."""

    run_id: int
    status: str
    conclusion: str | None
    event: str
    url: str

    @property
    def finished(self) -> bool:
        return self.status == "completed"

    @property
    def green(self) -> bool:
        return self.finished and self.conclusion == "success"

    def describe(self) -> str:
        outcome = self.conclusion if self.finished else self.status
        return f"run {self.run_id} ({self.event}): {outcome} {self.url}"


def parse_runs(body: str) -> list[Run]:
    """The runs of a ``GET .../workflows/ci.yml/runs`` response body.

    Anything that is not the documented shape is an :class:`ApiError`: a
    gateway error page or a truncated body must be retried, never read as
    "there are no runs"."""
    try:
        doc = json.loads(body)
    except ValueError as exc:
        raise ApiError(f"response is not JSON ({exc})") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("workflow_runs"), list):
        raise ApiError("response carries no workflow_runs list")
    runs: list[Run] = []
    for row in doc["workflow_runs"]:
        try:
            runs.append(
                Run(
                    run_id=int(row["id"]),
                    status=str(row["status"]),
                    conclusion=None if row.get("conclusion") is None else str(row["conclusion"]),
                    event=str(row.get("event", "")),
                    url=str(row.get("html_url", "")),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ApiError(f"malformed workflow run entry ({exc})") from exc
    return runs


def gh_query(repo: str, sha: str) -> str:
    """One API call through the ``gh`` CLI; :class:`ApiError` on any failure."""
    cmd = [
        "gh",
        "api",
        "-H",
        "Accept: application/vnd.github+json",
        f"repos/{repo}/actions/workflows/{WORKFLOW_FILE}/runs?head_sha={sha}&per_page=100",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ApiError(f"gh did not answer ({exc})") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise ApiError(f"gh exit {proc.returncode}: {detail[0] if detail else 'no output'}")
    return proc.stdout


def backoff_seconds(consecutive_failures: int) -> float:
    return min(BACKOFF_CAP_S, BACKOFF_STEP_S * consecutive_failures)


def verify(
    query: Callable[[], str],
    *,
    sha: str,
    api_attempts: int = 10,
    poll_seconds: float = 30.0,
    in_progress_budget: float = 2400.0,
    no_run_grace: float = 120.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    out: Callable[[str], None] = print,
) -> int:
    """Run the decision table of the module docs; returns the exit code."""
    started = clock()
    failures = 0
    attempt = 0
    while True:
        attempt += 1
        elapsed = clock() - started
        try:
            runs = parse_runs(query())
        except ApiError as exc:
            failures += 1
            out(f"attempt {attempt} (+{elapsed:.0f}s): API error {failures}/{api_attempts}: {exc}")
            if failures >= api_attempts:
                out(
                    f"::error::the GitHub API gave no usable answer {failures} times in a "
                    f"row; whether {sha} has a green ci run could not be determined — "
                    "re-run this job when the API is back"
                )
                return EXIT_UNDETERMINED
            sleep(backoff_seconds(failures))
            continue
        failures = 0
        out(f"attempt {attempt} (+{elapsed:.0f}s): {len(runs)} ci run(s) for {sha}")
        for run in runs:
            out(f"  {run.describe()}")
        green = [r for r in runs if r.green]
        if green:
            out(f"green: {green[0].describe()}")
            return EXIT_GREEN
        pending = [r for r in runs if not r.finished]
        if pending:
            if elapsed >= in_progress_budget:
                out(
                    f"::error::ci for {sha} is still in progress after {elapsed:.0f}s "
                    f"(budget {in_progress_budget:.0f}s): "
                    + "; ".join(r.describe() for r in pending)
                    + " — re-run this job once it has finished"
                )
                return EXIT_NOT_GREEN
            sleep(poll_seconds)
            continue
        if runs:
            out(
                f"::error::ci ran for {sha} and did not succeed: "
                + "; ".join(r.describe() for r in runs)
                + " — do not release an untested tree"
            )
            return EXIT_NOT_GREEN
        if elapsed >= no_run_grace:
            out(
                f"::error::no ci run exists for {sha} (waited {elapsed:.0f}s for one to "
                "appear); do not release an untested tree"
            )
            return EXIT_NOT_GREEN
        sleep(min(poll_seconds, max(1.0, no_run_grace - elapsed)))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True, help="OWNER/NAME")
    ap.add_argument("--sha", required=True, help="the commit to verify (40 hex)")
    ap.add_argument("--api-attempts", type=int, default=10)
    ap.add_argument("--poll-seconds", type=float, default=30.0)
    ap.add_argument("--in-progress-budget", type=float, default=2400.0)
    ap.add_argument("--no-run-grace", type=float, default=120.0)
    args = ap.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{40}", args.sha):
        print(f"::error::--sha must be a full 40-hex commit id, got {args.sha!r}")
        return EXIT_NOT_GREEN
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
        print(f"::error::--repo must be OWNER/NAME, got {args.repo!r}")
        return EXIT_NOT_GREEN
    if args.api_attempts < 1:
        print("::error::--api-attempts must be >= 1")
        return EXIT_NOT_GREEN
    return verify(
        lambda: gh_query(args.repo, args.sha),
        sha=args.sha,
        api_attempts=args.api_attempts,
        poll_seconds=args.poll_seconds,
        in_progress_budget=args.in_progress_budget,
        no_run_grace=args.no_run_grace,
        out=lambda line: print(line, flush=True),
    )


if __name__ == "__main__":
    sys.exit(main())
