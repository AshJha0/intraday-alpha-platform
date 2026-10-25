"""Fixtures for the CR1 adversarial tests: a throwaway git repository that
plays the checked-out bound commit (trust root, prereg, image digest, one
bound script), a sealed partition with its service, and an in-memory
transport that drives the service through its own frame protocol."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from conftest import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(REPO_ROOT / "python" / "tests" / "isolation"))

from runner import redact, results, run, verify  # noqa: E402
from sealed_fixtures import keypair, load_tool, make_partition, sign_document  # noqa: E402

__all__ = ["IMAGE", "World", "git", "redact", "results", "run", "verify"]

IMAGE = "sha256:" + "cd" * 32


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=dev",
            "-c",
            "user.email=dev@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(cwd),
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def content_hash(files: dict[str, str]) -> str:
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(name.encode() + b"\n" + files[name].encode() + b"\n")
    return h.hexdigest()


class World:
    """Repo + partition + service + transport, with knobs for the adversarial cases."""

    def __init__(self, tmp_path: Path, partition: str = "b") -> None:
        self.ss = load_tool("sealed_service", "tools/sealed_service.py")
        self.partition = partition
        self.priv, self.pub = keypair()
        # -- the partition and its named dataset
        self.part_root = tmp_path / "sealed" / partition
        self.ddir, self.files = make_partition(self.part_root, partition, self.pub, "placeholder")
        self.dataset_hash_id = content_hash(self.files)
        ds = json.loads((self.part_root / "service" / "datasets.json").read_text())
        (self.part_root / "service" / "datasets.json").write_text(
            json.dumps({self.dataset_hash_id: ds["placeholder"]})
        )
        self.service = self.ss.SealedService(self.ss.ServiceConfig.load(self.part_root, partition))
        self.calls: list[dict[str, Any]] = []
        self.caller_account = "iap-runner"
        # -- the repository at the bound commit
        self.root = tmp_path / "repo"
        (self.root / "research" / "agents").mkdir(parents=True)
        (self.root / "research" / "preregs").mkdir(parents=True)
        (self.root / "deployment" / "docker").mkdir(parents=True)
        (self.root / "research" / "agents" / "trust_roots.json").write_text(
            json.dumps({"maintainers": {"test": self.pub}})
        )
        (self.root / "deployment" / "docker" / "IMAGE_DIGEST").write_text(IMAGE + "\n")
        self.script = self.root / "analysis.py"
        self.script.write_text(
            "import json, pathlib, sys\n"
            "d, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])\n"
            "n = sum(len(p.read_bytes()) for p in sorted(d.rglob('*')) if p.is_file())\n"
            "print('ROW', n)  # must never reach the log\n"
            "(out / 'result.json').write_text(json.dumps({'bytes': n, 'verdict': 'ITERATE'}))\n"
        )
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "bound commit")
        self.commit = git(self.root, "rev-parse", "HEAD")
        self.prereg_rel = "research/preregs/registered_run_synthetic.json"
        self.write_prereg()
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "signed prereg (binds the commit before it)")

    # -- prereg ----------------------------------------------------------------
    def body(self, **over: Any) -> dict[str, Any]:
        from iap.agents import analysis_identity as ai

        ident = ai.build_identity(
            self.root,
            scripts=["analysis.py"],
            data_versions=[self.dataset_hash_id],
            family="synthetic-registered-run",
            metrics=["bytes"],
            thresholds={"bytes_min": 1},
            commit=self.commit,
        )
        body = {
            "kind": "prereg",
            "prereg_id": self.prereg_rel,
            "x-version": 1,
            "analysis_identity": ident,
            "registered_run": {
                "partition": self.partition,
                "dataset_hash_id": self.dataset_hash_id,
                "dataset_files": dict(self.files),
                "image_digest": IMAGE,
                "command": [sys.executable, "-I", str(self.script)],
            },
        }
        body.update(over)
        return body

    def write_prereg(self, doc: dict[str, Any] | None = None, **over: Any) -> dict[str, Any]:
        doc = doc or sign_document(self.body(**over), self.priv, self.pub)
        (self.root / self.prereg_rel).write_text(json.dumps(doc, indent=1, sort_keys=True))
        return doc

    # -- transport ---------------------------------------------------------------
    def transport(self, partition: str, request):
        self.calls.append({"partition": partition, "op": request.get("op")})
        assert partition == self.partition, "request went to the wrong partition's pipe"
        req = io.BytesIO()
        self.ss.write_frame(req, json.dumps(request, sort_keys=True).encode())
        req.seek(0)
        out = io.BytesIO()
        caller = self.ss.Caller(account=self.caller_account, pid=1, image_sha256=None)
        self.ss.serve_connection(self.service, caller, req, out)
        out.seek(0)
        return self.ss.receive(out)

    def ops(self) -> list[str]:
        return [c["op"] for c in self.calls]
