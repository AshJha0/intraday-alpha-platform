#!/usr/bin/env python3
"""``iap-repro``: run a named runner by its ``analysis_identity`` inside the
pinned reproduction image (v2.0 stage 3 P1, ``deployment/docker/Dockerfile.repro``).

    iap-repro [--root /work] [--runner REL] [--code-at COMMIT] [--require-identity]
              [--actor NAME] [--list] PREREG [-- RUNNER_ARGS...]

``PREREG`` is a signed prereg or rule JSON, repo-relative to ``--root`` (the
mounted checkout; ``$IAP_REPO_ROOT`` or ``/work``). The entrypoint

1. loads the document and its ``analysis_identity``; when the document says it
   is signed, verifies the Ed25519 signature under the checkout's trust root;
2. resolves the runner: ``--runner REL`` (any repo-relative Python file; one
   outside the bound ``scripts`` is reported as such) or, by default, the one
   bound script whose file name starts with ``run_`` (ambiguous or none: exit 2);
3. with ``--code-at COMMIT``, exports from the checkout's history the code
   that runs (``python/src``, ``tools``, ``configs``, ``data/reference``,
   ``schemas``, the lock, every bound script and the runner) at that commit
   into a private directory and runs that, so a committed result is re-derived
   by the tree that produced it while the mounted checkout stays at HEAD;
4. compares what is about to run with the bound identity
   (:func:`iap.agents.analysis_identity.environment_mismatches`: scripts, code
   tree, inputs, configs, reference data, lock, installed packages,
   interpreter, image digest) and the commit ancestry, and prints every
   mismatch. With ``--require-identity`` any mismatch is a refusal (exit 2):
   the registered runner's mode. Without it the run proceeds and the list is
   written to ``$IAP_REPRO_RECORD`` when set (the re-derivation mode of
   ``docs/REPRODUCTION.md``, where the report says how the tree that ran
   differs from the bound one);
5. runs ``python3 <runner> RUNNER_ARGS`` with ``PYTHONPATH=<code>/python/src``,
   ``IAP_RUN_ACTOR`` set to ``--actor`` (default ``iap-repro``) and no signing
   key material in the environment, from ``<root>`` as the working directory.
   The runner's exit code is the entrypoint's.

The entrypoint is deliberately small and imports ``iap`` from the checkout
(or the export), never from a copy inside the image: the code that runs is
the bound commit's. It fetches nothing (run the container with ``--network none``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

SIGNING_ENV = ("IAP_MAINTAINER_KEY", "IAP_SIGNING_KEY", "IAP_AGENT_PRIVATE_KEY", "IAP_KEYFILE")
#: what --code-at exports besides the bound scripts and the runner
EXPORT_PATHS = (
    "python/src",
    "python/requirements-ci.txt",
    "tools",
    "configs",
    "data/reference",
    "schemas",
)


def _print(msg: str) -> None:
    print(f"[iap-repro] {msg}", flush=True)


def refuse(msg: str) -> int:
    print(f"[iap-repro] REFUSED: {msg}", file=sys.stderr, flush=True)
    return 2


def resolve_runner(identity: dict[str, Any], runner: str | None) -> tuple[str, bool]:
    """``(repo-relative runner, bound)``; raises ``ValueError`` when the default
    rule finds no single ``run_*.py`` among the bound scripts."""
    scripts = sorted(identity.get("scripts") or {})
    if runner:
        rel = Path(runner).as_posix()
        return rel, rel in scripts
    cands = [s for s in scripts if s.endswith(".py") and Path(s).name.startswith("run_")]
    if len(cands) != 1:
        raise ValueError(
            f"the identity binds {len(cands)} run_*.py scripts {cands}; pass --runner REL"
        )
    return cands[0], True


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )


def export_code(root: Path, commit: str, extra: list[str], dest: Path) -> str:
    """``git archive`` the code paths at ``commit`` into ``dest``; returns the
    full commit id.  Raises ``ValueError`` when the commit is unknown."""
    p = _git(root, "rev-parse", "--verify", f"{commit}^{{commit}}")
    if p.returncode != 0:
        raise ValueError(f"--code-at {commit}: not a commit of this checkout")
    full = p.stdout.strip()
    paths = []
    for rel in [*EXPORT_PATHS, *extra]:
        if _git(root, "cat-file", "-e", f"{full}:{rel}").returncode == 0:
            paths.append(rel)
    tar = dest / "code.tar"
    with tar.open("wb") as fh:
        q = subprocess.run(
            ["git", "-C", str(root), "archive", "--format=tar", full, *paths],
            stdout=fh,
            stderr=subprocess.PIPE,
            check=False,
        )
    if q.returncode != 0:
        raise ValueError(f"git archive failed: {q.stderr.decode(errors='replace')[:300]}")
    with tarfile.open(tar) as tf:
        tf.extractall(dest, filter="data")
    tar.unlink()
    return full


def identity_report(
    root: Path, code: Path, doc: dict[str, Any], env: dict[str, str], code_at: str | None
) -> dict[str, Any]:
    """Everything the entrypoint checked, as a record for the reproduction report."""
    from iap.agents import analysis_identity as ai

    bound = doc.get("analysis_identity") or {}
    head = code_at or ai.head_commit(root)
    # a checkout older than v2.0 stage 3 has no image-digest support: still checkable
    digest_env = getattr(ai, "IMAGE_DIGEST_ENV", "IAP_IMAGE_DIGEST")
    rep: dict[str, Any] = {
        "identity_version": ai.identity_version(bound) if bound else None,
        "bound_commit": bound.get("commit"),
        "head_commit": ai.head_commit(root),
        "code_at": code_at,
        "interpreter": ai.interpreter(),
        "image_digest_declared": env.get(digest_env),
        "image_digest_bound": bound.get("image_digest"),
        "mismatches": [],
        "signature": None,
    }
    if doc.get("signed") is True:
        try:
            ai.verify_signed_document(root, doc)
            rep["signature"] = "valid under the trust root"
        except ai.SignatureError as exc:
            rep["signature"] = f"INVALID: {exc}"
            rep["mismatches"].append(f"signature: {exc}")
    if not bound:
        rep["mismatches"].append("the document binds no analysis_identity")
        return rep
    try:
        rep["mismatches"] += ai.environment_mismatches(code, bound, env=env)
    except TypeError:  # the checkout's analysis_identity predates the env argument
        rep["mismatches"] += ai.environment_mismatches(code, bound)
    commit = bound.get("commit")
    if not commit or head is None:
        rep["mismatches"].append("exact commit unavailable")
    elif commit != head and _git(root, "merge-base", "--is-ancestor", commit, head).returncode:
        what = "the --code-at commit" if code_at else "HEAD"
        rep["mismatches"].append(f"bound commit {commit[:12]} is not {what} or an ancestor of it")
    return rep


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--root", default=os.environ.get("IAP_REPO_ROOT", "/work"))
    ap.add_argument(
        "--runner", help="repo-relative Python file to run instead of the bound run_*.py"
    )
    ap.add_argument(
        "--code-at",
        metavar="COMMIT",
        help="run the code (python/src, tools, configs, bound scripts, runner) as of this commit",
    )
    ap.add_argument(
        "--require-identity",
        action="store_true",
        help="refuse (exit 2) on any identity mismatch (the registered runner's mode)",
    )
    ap.add_argument("--actor", default="iap-repro", help="IAP_RUN_ACTOR for the runner")
    ap.add_argument("--list", action="store_true", help="print the identity check, run nothing")
    ap.add_argument("prereg", help="signed prereg / rule JSON, repo-relative")
    ap.add_argument("runner_args", nargs=argparse.REMAINDER, help="arguments after --")
    a = ap.parse_args(argv)

    root = Path(a.root).resolve()
    if not (root / "python" / "src" / "iap").is_dir():
        return refuse(f"{root} is not an IAP checkout (no python/src/iap); mount one at --root")
    prereg = root / a.prereg
    if not prereg.is_file():
        return refuse(f"{a.prereg} does not exist under {root}")
    doc = json.loads(prereg.read_text(encoding="utf-8"))
    env = {k: v for k, v in os.environ.items() if k not in SIGNING_ENV}
    held = [k for k in SIGNING_ENV if os.environ.get(k)]
    if held:
        return refuse(f"signing key material in the environment: {held}; a runner never signs")

    identity = doc.get("analysis_identity") or {}
    try:
        runner, bound = resolve_runner(identity, a.runner)
    except ValueError as exc:
        return refuse(str(exc))

    code = root
    code_at = None
    tmp: tempfile.TemporaryDirectory[str] | None = None
    if a.code_at:
        tmp = tempfile.TemporaryDirectory(prefix="iap-repro-")
        code = Path(tmp.name)
        try:
            code_at = export_code(
                root, a.code_at, [*sorted(identity.get("scripts") or {}), runner], code
            )
        except ValueError as exc:
            return refuse(str(exc))
        _print(f"code exported from {code_at} into {code}")
    # the entrypoint's own checks use the mounted checkout's iap; only the
    # runner subprocess gets the export on PYTHONPATH
    sys.path.insert(0, str(root / "python" / "src"))
    runner_path = code / runner if (code / runner).is_file() else root / runner
    if not runner_path.is_file():
        return refuse(f"runner {runner} does not exist under {code}")

    rep = identity_report(root, code, doc, env, code_at)
    rep.update(
        prereg=Path(a.prereg).as_posix(),
        prereg_sha256=hashlib.sha256(prereg.read_bytes()).hexdigest(),
        runner=runner,
        runner_bound=bound,
        runner_sha256=hashlib.sha256(runner_path.read_bytes().replace(b"\r\n", b"\n")).hexdigest(),
        actor=a.actor,
        require_identity=bool(a.require_identity),
    )
    _print(
        f"prereg {rep['prereg']} sha256 {rep['prereg_sha256'][:12]}; signature: {rep['signature']}"
    )
    _print(f"runner {runner} ({'bound' if bound else 'NOT among the bound scripts'})")
    _print(
        f"interpreter {rep['interpreter']}; head {rep['head_commit']}; "
        f"code at {code_at or 'HEAD'}; bound {rep['bound_commit']}"
    )
    for m in rep["mismatches"]:
        _print(f"identity mismatch: {m}")
    if not rep["mismatches"]:
        _print("identity: what is about to run is what was bound")
    record = os.environ.get("IAP_REPRO_RECORD")
    if record:
        Path(record).write_text(json.dumps(rep, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if a.require_identity and rep["mismatches"]:
        return refuse(f"{len(rep['mismatches'])} identity mismatch(es); not running {runner}")
    if a.list:
        return 0

    args = list(a.runner_args)
    if args and args[0] == "--":
        args = args[1:]
    run_env = dict(env)
    run_env["PYTHONPATH"] = str(code / "python" / "src")
    run_env["IAP_RUN_ACTOR"] = a.actor
    run_env.setdefault("PYTHONUTF8", "1")
    cmd = [sys.executable, str(runner_path), *args]
    _print("exec " + " ".join(cmd))
    try:
        return subprocess.run(cmd, cwd=str(root), env=run_env, check=False).returncode
    finally:
        if tmp is not None:
            tmp.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
