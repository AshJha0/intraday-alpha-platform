"""Shared fixtures for the D2 / CR1 tests: a signed prereg under a throwaway
trust root, a sealed partition in tmp_path with one named dataset, and a
service instance.  Everything synthetic; nothing under ``C:\\iap-sealed`` is
touched."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from conftest import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "python" / "src"))


def load_tool(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = mod  # dataclasses resolve the module through sys.modules
    spec.loader.exec_module(mod)
    return mod


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def keypair() -> tuple[str, str]:
    from iap.agents import signing

    return signing.generate_keypair()


def sign_document(body: dict[str, Any], priv_hex: str, pub_hex: str, maintainer: str = "test") -> dict:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    doc = {k: v for k, v in body.items() if k != "signature"}
    doc["signed"] = True
    msg = canonical(doc).encode("ascii")
    sig = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(priv_hex)).sign(msg).hex()
    doc["signature"] = {
        "scheme": "ed25519",
        "maintainer": maintainer,
        "public_key": pub_hex,
        "message": "canonical(this document without 'signature')",
        "message_sha256": hashlib.sha256(msg).hexdigest(),
        "sig": sig,
    }
    return doc


def make_prereg(
    priv: str,
    pub: str,
    *,
    prereg_id: str = "research/preregs/synthetic_registered_run.json",
    partition: str = "b",
    dataset_hash_id: str = "ds-synthetic-0001",
    command: list[str] | None = None,
    image_digest: str = "sha256:" + "ab" * 32,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "kind": "prereg",
        "prereg_id": prereg_id,
        "x-version": 1,
        "registered_run": {
            "partition": partition,
            "dataset_hash_id": dataset_hash_id,
            "image_digest": image_digest,
            "command": command
            or [
                "python",
                "-c",
                "import sys, json, pathlib; d=pathlib.Path(sys.argv[1]); "
                "out=pathlib.Path(sys.argv[2]); out.mkdir(parents=True, exist_ok=True); "
                "n=sum(len(p.read_bytes()) for p in d.rglob('*') if p.is_file()); "
                "(out/'result.json').write_text(json.dumps({'bytes': n, 'verdict': 'ITERATE'}))",
            ],
        },
    }
    if extra:
        body.update(extra)
    return sign_document(body, priv, pub)


def make_partition(root: Path, partition: str, pub: str, dataset_hash_id: str = "ds-synthetic-0001"):
    """A partition directory with the service state and one named dataset."""
    state = root / "service"
    state.mkdir(parents=True)
    (state / "trust_roots.json").write_text(json.dumps({"maintainers": {"test": pub}}))
    (state / "config.json").write_text(json.dumps({"allowed_accounts": ["iap-runner"]}))
    ddir = root / "datasets" / dataset_hash_id
    ddir.mkdir(parents=True)
    (ddir / "prices.csv").write_text("date,close\n2020-01-02,1.0\n2020-01-03,1.5\n")
    (ddir / "manifest.json").write_text(json.dumps({"rows": 2}))
    files = {
        p.relative_to(ddir).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(ddir.rglob("*"))
        if p.is_file()
    }
    (state / "datasets.json").write_text(
        json.dumps({dataset_hash_id: {"dir": ddir.relative_to(root).as_posix(), "files": files}})
    )
    return ddir, files


def make_service(tmp_path: Path, partition: str = "b"):
    ss = load_tool("sealed_service", "tools/sealed_service.py")
    priv, pub = keypair()
    root = tmp_path / "sealed" / partition
    ddir, files = make_partition(root, partition, pub)
    cfg = ss.ServiceConfig.load(root, partition)
    svc = ss.SealedService(cfg)
    return ss, svc, (priv, pub), ddir, files
