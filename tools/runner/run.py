#!/usr/bin/env python3
"""Registered run (CR1): verify, request the sealed read, run once, write the
result and its hashes, commit to the ``results`` branch as ``iap-runner``,
publish the checkpoint, leave a redacted log.

    python tools/runner/run.py run --prereg research/preregs/<x>.json [--push]
    python tools/runner/run.py whoami
    python tools/runner/run.py verify-result --prereg <x> --commit <sha> --seq <n>

Order of operations (U0 section 2), each step refusing before the next:

1. identity: the process runs as ``iap-runner`` (the self-hosted runner's
   service account); anything else exits 3 without a request;
2. :func:`runner.verify.verify_registration`: signature, ``registered_run``
   block, pinned image digest, ``analysis_identity`` (freeze hashes, bound
   commit = HEAD), dataset id bound;
3. one ``read`` request to the partition's sealed service (which applies its
   own checks, keeps the registration ledger and refuses a second read);
   the served files land in a private cache directory that exists only for
   this run and is removed afterwards;
4. served file hashes are re-computed and compared with the bound
   ``dataset_files`` when the prereg binds them;
5. the registered ``command`` runs ONCE with two positional arguments
   (the dataset directory, the output directory) and must write
   ``result.json``; its stdout and stderr are never copied into the log;
6. ``hashes.json`` (result file hashes, prereg hash, dataset id, seq, chain
   head, image digest, commit) is written; ``store_result`` records the
   result hash with the service; the result is committed to ``results``;
7. ``publish`` hands the commit sha to the service, which clears the pending
   checkpoint (until then every further read is refused);
8. the redacted log is written to ``--log`` (the only artifact the workflow
   uploads).

Exit codes: 0 done · 2 refused by the service or the run failed · 3 refused
before any request (identity, verification).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

TOOLS = Path(__file__).resolve().parents[1]
REPO = TOOLS.parent
for p in (str(TOOLS), str(REPO / "python" / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from runner import redact, results, sealed_client, verify  # noqa: E402

RUNNER_ACCOUNT = "iap-runner"
DEFAULT_CACHE_ROOT = Path(r"C:\iap-sealed\runner\cache")
RESULT_FILE = "result.json"


class RunRefused(RuntimeError):
    def __init__(self, code: int, msg: str) -> None:
        super().__init__(msg)
        self.code = code


def current_identity() -> str:
    for var in ("USERNAME", "USER", "LOGNAME"):
        v = os.environ.get(var, "").strip()
        if v:
            return v
    import getpass

    return getpass.getuser()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def registered_run(
    root: Path,
    prereg_rel: str,
    *,
    identity: str | None = None,
    transport=None,
    cache_root: Path | None = None,
    image_digest_rel: Path = verify.IMAGE_DIGEST_REL,
    push: bool = False,
    results_branch: str = results.RESULTS_BRANCH,
    command_timeout: float = 6 * 3600,
    log: redact.RedactingLog | None = None,
) -> dict[str, Any]:
    """The whole procedure; returns the hashes record.  Raises :class:`RunRefused`."""
    root = Path(root)
    log = log or redact.RedactingLog([str(root)])
    who = identity if identity is not None else current_identity()
    if who != RUNNER_ACCOUNT:
        log("refused: process identity is not the runner account")
        raise RunRefused(3, "the registered run executes only as the runner account")
    doc = verify.load_prereg(root, prereg_rel)
    try:
        rec = verify.verify_registration(root, doc, image_digest_rel=image_digest_rel)
    except verify.RegistrationError as exc:
        log(f"refused before any request: {exc}")
        raise RunRefused(3, str(exc)) from exc
    run = verify.registered_run(doc)
    log(
        f"verified prereg {rec['prereg_id']} sha256 {rec['prereg_sha256'][:16]} image {rec['image_digest'][7:23]}"
    )

    cache_root = Path(cache_root or DEFAULT_CACHE_ROOT)
    cache_root.mkdir(parents=True, exist_ok=True)
    cache = Path(tempfile.mkdtemp(prefix="run-", dir=cache_root))
    log.add_secret(str(cache))
    log.add_secret(str(cache_root))
    try:
        response, files = sealed_client.request(
            run["partition"],
            {"op": "read", "prereg": doc, "dataset_hash_id": run["dataset_hash_id"]},
            transport,
        )
        if not response.get("ok"):
            log(f"service refused the read: {response.get('error')}")
            raise RunRefused(
                2, f"service refused: {response.get('error')}: {response.get('detail', '')}"
            )
        seq = int(response["seq"])
        checkpoint = response["checkpoint"]
        log(
            f"sealed read served: seq {seq}, {len(files)} file(s), chain head {checkpoint['chain_head'][:16]}"
        )
        served = {h["file"]: h["sha256"] for h, _ in files}
        if served != dict(response["files"]):
            raise RunRefused(2, "served file set differs from the service's announced file map")
        for h, data in files:  # the client re-hashes what arrived
            if sha256_bytes(data) != h["sha256"]:
                raise RunRefused(2, f"served file {h['file']} does not match its hash")
        try:
            verify.check_dataset_files(run, served)
        except verify.RegistrationError as exc:
            log(f"dataset hashes differ from the bound ones; the analysis does not run: {exc}")
            raise RunRefused(2, str(exc)) from exc
        dataset_dir = cache / "dataset"
        for h, data in files:
            dest = dataset_dir / h["file"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        out_dir = cache / "out"
        out_dir.mkdir()
        argv = [*run["command"], str(dataset_dir), str(out_dir)]
        started = time.time()
        proc = subprocess.run(
            argv,
            cwd=str(root),
            capture_output=True,
            timeout=command_timeout,
            check=False,
            env={**os.environ, "PYTHONPATH": str(root / "python" / "src"), "PYTHONUTF8": "1"},
        )
        log(
            f"analysis exit {proc.returncode} after {time.time() - started:.1f}s "
            f"(stdout {len(proc.stdout)} bytes, stderr {len(proc.stderr)} bytes; not reproduced here)"
        )
        result_path = out_dir / RESULT_FILE
        if proc.returncode != 0 or not result_path.is_file():
            raise RunRefused(2, "the registered analysis failed or wrote no result.json")
        result_files = {
            p.relative_to(out_dir).as_posix(): p.read_bytes()
            for p in sorted(out_dir.rglob("*"))
            if p.is_file()
        }
        result_sha = sha256_bytes(result_files[RESULT_FILE])
        hashes = {
            "prereg_id": rec["prereg_id"],
            "prereg_sha256": rec["prereg_sha256"],
            "dataset_hash_id": run["dataset_hash_id"],
            "dataset_files": served,
            "seq": seq,
            "chain_head": checkpoint["chain_head"],
            "image_digest": rec["image_digest"],
            "commit": rec["commit"],
            "result_sha256": result_sha,
            "result_files": {n: sha256_bytes(b) for n, b in result_files.items()},
            "evidence_class": "mechanically isolated",
        }
        stored, _ = sealed_client.request(
            run["partition"],
            {
                "op": "store_result",
                "prereg_sha256": rec["prereg_sha256"],
                "result_sha256": result_sha,
            },
            transport,
        )
        if not stored.get("ok"):
            raise RunRefused(2, f"service refused store_result: {stored.get('error')}")
        sha = results.commit_result(
            root,
            prereg_id=rec["prereg_id"],
            seq=seq,
            partition=run["partition"],
            result_files=result_files,
            hashes=hashes,
            checkpoint=checkpoint,
            branch=results_branch,
            push=push,
        )
        log(f"result committed {sha[:12]} on {results_branch} (result sha256 {result_sha[:16]})")
        published, _ = sealed_client.request(
            run["partition"], {"op": "publish", "commit": sha, "checkpoint": checkpoint}, transport
        )
        if not published.get("ok"):
            raise RunRefused(2, f"service refused publish: {published.get('error')}")
        log(f"checkpoint seq {seq} published")
        hashes["result_commit"] = sha
        return hashes
    finally:
        shutil.rmtree(cache, ignore_errors=True)
        log("private cache removed")


def verify_result(root: Path, prereg_rel: str, commit: str, seq: int, transport=None) -> bool:
    """Property 5: the committed result hash equals the service's copy."""
    doc = verify.load_prereg(root, prereg_rel)
    run = verify.registered_run(doc)
    committed = results.read_result_hash(root, commit, str(doc.get("prereg_id")), seq)
    if committed is None:
        return False
    resp, _ = sealed_client.request(
        run["partition"],
        {
            "op": "verify_result",
            "prereg_sha256": verify.prereg_hash(doc),
            "result_sha256": committed,
        },
        transport,
    )
    return bool(resp.get("ok") and resp.get("match"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("whoami", help="print the process identity (must be iap-runner)")
    r = sub.add_parser("run", help="the registered run")
    r.add_argument("--prereg", required=True, help="repo-relative path of the signed prereg")
    r.add_argument("--root", type=Path, default=REPO)
    r.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    r.add_argument("--log", type=Path, default=REPO / "results-log.redacted.txt")
    r.add_argument("--push", action="store_true")
    r.add_argument("--results-branch", default=results.RESULTS_BRANCH)
    v = sub.add_parser(
        "verify-result", help="compare a committed result hash with the service's copy"
    )
    v.add_argument("--prereg", required=True)
    v.add_argument("--commit", required=True)
    v.add_argument("--seq", type=int, required=True)
    v.add_argument("--root", type=Path, default=REPO)
    a = ap.parse_args(argv)
    if a.cmd == "whoami":
        print(current_identity())
        return 0 if current_identity() == RUNNER_ACCOUNT else 3
    if a.cmd == "verify-result":
        ok = verify_result(a.root, a.prereg, a.commit, a.seq)
        print("result hash matches the service's copy" if ok else "MISMATCH or unknown")
        return 0 if ok else 1
    log = redact.RedactingLog([str(a.root), str(a.cache_root)])
    code = 0
    try:
        hashes = registered_run(
            a.root,
            a.prereg,
            cache_root=a.cache_root,
            push=a.push,
            results_branch=a.results_branch,
            log=log,
        )
        log(
            f"done: seq {hashes['seq']} result {hashes['result_sha256'][:16]} class {hashes['evidence_class']}"
        )
    except RunRefused as exc:
        code = exc.code
        log(f"exit {code}")
    a.log.parent.mkdir(parents=True, exist_ok=True)
    a.log.write_text(log.text(), encoding="utf-8")
    sys.stdout.write(log.text())
    return code


if __name__ == "__main__":
    raise SystemExit(main())
