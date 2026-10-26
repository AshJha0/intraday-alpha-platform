"""The append-only ``results`` branch (property 5).

Every registered run adds exactly one commit to the protected branch
``results`` from the ``iap-runner`` git identity, in a separate worktree so
the checked-out bound commit is never touched: ``results/<prereg slug>/
<seq>/result.json`` and ``hashes.json``, plus one line appended to
``research/audit/CHECKPOINTS.jsonl`` (the repository anchor of the sealed
service's audit chain).  History is preserved by the branch protection the
owner sets (``docs/GOVERNANCE.md`` section 7); the service
keeps its own copy of the result hash, so a rewritten result is detectable
(``tools/runner/run.py verify-result``).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

RESULTS_BRANCH = "results"
RUNNER_IDENTITY = ("iap-runner", "iap-runner@localhost")
CHECKPOINTS_REL = Path("research") / "audit" / "CHECKPOINTS.jsonl"


def slug(prereg_id: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", prereg_id.removesuffix(".json"))
    return s.strip("_") or "prereg"


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env_identity = [
        "-c",
        f"user.name={RUNNER_IDENTITY[0]}",
        "-c",
        f"user.email={RUNNER_IDENTITY[1]}",
        "-c",
        "commit.gpgsign=false",
    ]
    return subprocess.run(
        ["git", *env_identity, "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def branch_exists(root: Path, branch: str = RESULTS_BRANCH) -> bool:
    local = _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    remote = _git(
        root, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}", check=False
    )
    return local.returncode == 0 or remote.returncode == 0


def commit_result(
    root: Path,
    *,
    prereg_id: str,
    seq: int,
    partition: str,
    result_files: Mapping[str, bytes],
    hashes: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
    branch: str = RESULTS_BRANCH,
    push: bool = False,
) -> str:
    """Add one commit to ``branch`` holding the result files, ``hashes.json`` and
    the checkpoint line; returns the commit sha.  The branch is created
    (orphan) on first use."""
    root = Path(root)
    wt = Path(tempfile.mkdtemp(prefix="iap-results-"))
    try:
        if branch_exists(root, branch):
            start = f"origin/{branch}"
            if _git(root, "rev-parse", "--verify", "--quiet", start, check=False).returncode != 0:
                start = branch
            _git(root, "worktree", "add", "--detach", str(wt), start)
        else:
            _git(root, "worktree", "add", "--detach", str(wt), "HEAD")
            _git(wt, "checkout", "--orphan", branch)
            _git(wt, "rm", "-rf", "-q", ".", check=False)
            for child in list(wt.iterdir()):
                if child.name == ".git":
                    continue
                shutil.rmtree(child) if child.is_dir() else child.unlink()
            (wt / "README.md").write_text(
                "# results\n\nAppend-only registered-run results committed by the iap-runner "
                "identity (CR1). Never rewrite history on this branch.\n",
                encoding="utf-8",
            )
        dest = wt / "results" / slug(prereg_id) / f"{seq:04d}"
        if dest.exists():
            raise FileExistsError(f"{dest.relative_to(wt).as_posix()} exists: one result per run")
        dest.mkdir(parents=True)
        for name, data in result_files.items():
            (dest / name).write_bytes(data)
        (dest / "hashes.json").write_text(json.dumps(hashes, indent=1, sort_keys=True) + "\n")
        cp = wt / CHECKPOINTS_REL
        cp.parent.mkdir(parents=True, exist_ok=True)
        with cp.open("a", encoding="utf-8", newline="\n") as f:
            f.write(
                json.dumps({"partition": partition, "checkpoint": dict(checkpoint)}, sort_keys=True)
                + "\n"
            )
        _git(wt, "add", "-A")
        _git(
            wt,
            "commit",
            "-q",
            "-m",
            f"registered run {prereg_id} seq {seq} (partition {partition})",
        )
        sha = _git(wt, "rev-parse", "HEAD").stdout.strip()
        _git(wt, "checkout", "-q", "--detach")  # free the branch name for the update below
        _git(root, "branch", "-f", branch, sha)
        if push:
            _git(root, "push", "origin", f"{sha}:refs/heads/{branch}")
        return sha
    finally:
        _git(root, "worktree", "remove", "--force", str(wt), check=False)
        shutil.rmtree(wt, ignore_errors=True)


def read_result_hash(root: Path, sha: str, prereg_id: str, seq: int) -> str | None:
    """``hashes.json["result_sha256"]`` as committed at ``sha`` (property 5 check)."""
    rel = f"results/{slug(prereg_id)}/{seq:04d}/hashes.json"
    out = _git(root, "show", f"{sha}:{rel}", check=False)
    if out.returncode != 0:
        return None
    return json.loads(out.stdout).get("result_sha256")
