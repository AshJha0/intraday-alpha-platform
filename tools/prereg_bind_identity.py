"""Write (and, for the owner, sign) the REL-02a ``analysis_identity`` of a prereg.

v1.18 REL-02a lets a signed prereg bind the analysis it authorises
(:mod:`iap.agents.analysis_identity`): script fingerprints, the
``python/src/iap`` tree hash, the exact commit, data versions, family,
metrics and thresholds; identity v2 also binds ``configs/``,
``data/reference/``, declared ``--inputs``, the dependency lock, the
installed packages, the interpreter and (v2.0 stage 3 P1) the digest of the
pinned reproduction image, taken from ``deployment/docker/IMAGE_DIGEST``
unless ``--image-digest`` / ``--no-image-digest`` says otherwise.  This tool builds that block for one protocol
document and writes the unsigned prereg JSON; ``sign`` is the separate step
the coordinator (owner delegation) runs on a machine holding the maintainer
key.  Neither step reads data or touches the ledger.

    # 1. bind (anyone; refuses on a dirty tree or scripts that differ from HEAD)
    python tools/prereg_bind_identity.py bind --protocol research/v118/NULL_PROTOCOL.md \\
        --study-config research/v118/known_null_config.json \\
        --out research/preregs/rel06_known_null_prereg.json
    # 2. sign (the coordinator only; re-verifies the identity first)
    python tools/prereg_bind_identity.py sign --prereg research/preregs/rel06_known_null_prereg.json

``--study-config`` takes the identity arguments from the study's own
``identity_args`` (REL-06: :func:`iap.research.known_null.identity_args`), so
the runner checks exactly what was signed.  Other studies pass ``--scripts``,
``--data-version``, ``--family``, ``--metric`` and ``--thresholds`` instead.

Refusals (exit code 2, nothing written): the working tree has uncommitted or
untracked changes (other than the prereg file itself); a bound script or the
protocol differs from its committed bytes at HEAD; the prereg is already
signed (re-binding would void the signature).  Never prints key material.

**Planted dry run (v1.19 R4, defect D-09).**  ``bind --planted-dry-run
<report.json>`` binds the report of a planted dry run
(:mod:`iap.agents.planted_dry_run`: the rule run on planted inputs that produce
every verdict value it can return, with ``confirmation_data_read: false``) as
the ``dry_run`` block (path, sha256, verdict summary).  ``sign`` REFUSES a prereg
without that block, with a report that changed since binding, or with a report
that fails to demonstrate a verdict value.  The REL-08 C1, REL-08c n = 0 and
A0 signatures of v1.18 would all have been refused by this check.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python" / "src"))

from iap.agents.analysis_identity import (  # noqa: E402
    IdentityError,
    build_identity,
    head_commit,
    read_image_digest,
    verify_identity,
)
from iap.agents.planted_dry_run import (  # noqa: E402
    DryRunRefused,
    build_dry_run_block,
    require_dry_run,
)

KEYFILE = Path("C:/Users/ashis/.iap_keys/maintainer.key")
MAINTAINER = "ashjha0"


class Refused(SystemExit):
    def __init__(self, msg: str) -> None:
        print(f"REFUSED: {msg}", file=sys.stderr)
        super().__init__(2)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)


def _norm(data: bytes) -> str:
    text = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def dirty_paths(root: Path, allow: set[str]) -> list[str]:
    """Modified, staged or untracked paths (repo-relative), minus ``allow``."""
    p = _git(root, "status", "--porcelain", "--untracked-files=all")
    if p.returncode != 0:
        raise Refused("not a git checkout (git status failed)")
    out = []
    for line in p.stdout.decode("utf-8").splitlines():
        path = line[3:].strip().strip('"')
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path not in allow:
            out.append(path)
    return out


def check_committed(root: Path, paths: list[str]) -> None:
    """Every path's working-tree bytes equal its bytes at HEAD (line endings
    normalised, as the fingerprints are)."""
    bad = []
    for rel in paths:
        f = root / rel
        if not f.is_file():
            bad.append(f"{rel} does not exist")
            continue
        p = _git(root, "show", f"HEAD:{rel}")
        if p.returncode != 0:
            bad.append(f"{rel} is not committed at HEAD")
        elif _norm(p.stdout) != _norm(f.read_bytes()):
            bad.append(f"{rel} differs from HEAD")
    if bad:
        raise Refused("; ".join(bad))


def _rel(root: Path, p: Path) -> str:
    return (p if p.is_absolute() else (root / p)).resolve().relative_to(root.resolve()).as_posix()


def identity_inputs(a: argparse.Namespace, root: Path) -> dict[str, Any]:
    if a.study_config:
        cfg_rel = _rel(root, Path(a.study_config))
        cfg = json.loads((root / cfg_rel).read_text(encoding="utf-8"))
        if cfg.get("kind") == "known_null_v3":
            from iap.research.known_null_v3 import identity_args_v3 as identity_args
        else:
            from iap.research.known_null import identity_args

        args = identity_args(cfg)
        if cfg_rel not in args["scripts"]:
            raise Refused(f"{cfg_rel} is not among the study's bound scripts {args['scripts']}")
        extra = list(getattr(a, "inputs", None) or [])
        return {**args, "inputs": extra} if extra else args
    missing = [
        n
        for n in ("scripts", "data_version", "family", "metric", "thresholds")
        if not getattr(a, n)
    ]
    if missing:
        raise Refused(
            f"without --study-config, pass {', '.join('--' + m.replace('_', '-') for m in missing)}"
        )
    return {
        "scripts": list(a.scripts),
        "data_versions": list(a.data_version),
        "family": a.family,
        "metrics": list(a.metric),
        "thresholds": json.loads(a.thresholds),
        "inputs": list(getattr(a, "inputs", None) or []),
    }


def cmd_bind(a: argparse.Namespace) -> int:
    root = Path(a.root).resolve()
    out_rel = _rel(root, Path(a.out))
    proto_rel = _rel(root, Path(a.protocol))
    if (root / out_rel).is_file():
        old = json.loads((root / out_rel).read_text(encoding="utf-8"))
        if old.get("signed") is True or "signature" in old:
            raise Refused(f"{out_rel} is already signed; re-binding would void the signature")
    dirty = dirty_paths(root, {out_rel})
    if dirty:
        raise Refused(f"working tree is dirty: {dirty[:10]}{' ...' if len(dirty) > 10 else ''}")
    inputs = identity_inputs(a, root)
    committed = [proto_rel, *inputs["scripts"], *inputs.get("inputs", [])]
    if a.planted_dry_run:
        committed.append(_rel(root, Path(a.planted_dry_run)))
    check_committed(root, committed)
    commit = head_commit(root)
    try:
        image = None if a.no_image_digest else (a.image_digest or read_image_digest(root))
        block = build_identity(root, commit=commit, image_digest=image, **inputs)
    except IdentityError as exc:
        raise Refused(str(exc)) from exc
    doc = {
        "x-version": 1,
        "kind": "analysis_prereg",
        "prereg_id": out_rel,
        "protocol": proto_rel,
        "protocol_sha256": _norm((root / proto_rel).read_bytes()),
        "status": "DRAFT: analysis identity bound, NOT signed",
        "signed": False,
        "bound_utc": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "analysis_identity": block,
    }
    if a.planted_dry_run:
        try:
            doc["dry_run"] = build_dry_run_block(root, _rel(root, Path(a.planted_dry_run)))
        except DryRunRefused as exc:
            raise Refused(str(exc)) from exc
    else:
        doc["status"] += "; NO planted dry run bound (sign will refuse)"
    if a.dry_run:
        print(json.dumps(doc, indent=2, sort_keys=True))
        return 0
    (root / out_rel).parent.mkdir(parents=True, exist_ok=True)
    with (root / out_rel).open("w", encoding="ascii", newline="\n") as fh:
        fh.write(json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=True) + "\n")
    print(
        f"wrote {out_rel}: commit {commit}, {len(block['scripts'])} scripts, "
        f"code tree {block['code_tree_sha256'][:12]}, image "
        f"{block.get('image_digest') or 'NOT bound'}; NOT signed"
    )
    return 0


def cmd_sign(a: argparse.Namespace) -> int:
    root = Path(a.root).resolve()
    rel = _rel(root, Path(a.prereg))
    doc = json.loads((root / rel).read_text(encoding="utf-8"))
    if doc.get("signed") is True or "signature" in doc:
        raise Refused(f"{rel} is already signed")
    block = doc.get("analysis_identity")
    if not block:
        raise Refused(f"{rel} carries no analysis_identity; run bind first")
    dirty = dirty_paths(root, {rel})
    if dirty:
        raise Refused(f"working tree is dirty: {dirty[:10]}")
    if _norm((root / doc["protocol"]).read_bytes()) != doc.get("protocol_sha256"):
        raise Refused(f"{doc['protocol']} changed since the identity was bound")
    try:
        dry = require_dry_run(root, doc)
    except DryRunRefused as exc:
        raise Refused(f"planted dry run (R4): {exc}") from exc
    try:
        verify_identity(
            root,
            block,
            data_versions=block["data_versions"],
            family=block["family"],
            metrics=block["metrics"],
            thresholds=block["thresholds"],
        )
    except IdentityError as exc:
        raise Refused(str(exc)) from exc
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from iap.agents import signing, trust
    from iap.agents.blackboard import canonical

    key = json.loads(Path(a.keyfile).read_text(encoding="ascii"))[a.maintainer]
    pub = (trust.read_roots(root) or {}).get("maintainers", {}).get(a.maintainer)
    if pub is None or signing.public_of(key) != pub:
        raise Refused("maintainer key does not match research/agents/trust_roots.json")
    doc.update(
        signed=True,
        status="SIGNED: the analysis identity is bound; edits void the signature",
        signed_utc=datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
    )
    msg = canonical(doc).encode("ascii")
    sig = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(key)).sign(msg).hex()
    if not signing.verify_ed25519(pub, msg, sig):
        raise Refused("signature did not verify")
    doc["signature"] = {
        "scheme": "ed25519",
        "maintainer": a.maintainer,
        "public_key": pub,
        "trust_root": "research/agents/trust_roots.json",
        "message": "iap.agents.blackboard.canonical(this document without 'signature')",
        "message_sha256": hashlib.sha256(msg).hexdigest(),
        "sig": sig,
    }
    with (root / rel).open("w", encoding="ascii", newline="\n") as fh:
        fh.write(json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=True) + "\n")
    print(
        f"signed {rel} as {a.maintainer} (planted dry run {dry['path']}, verdict values "
        f"{dry['verdict_values']} all demonstrated); commit it, then run the analysis from a "
        "non-signing actor"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--root", default=str(ROOT))
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bind", help="write the unsigned prereg with its analysis_identity")
    b.add_argument("--protocol", required=True, type=Path)
    b.add_argument("--out", required=True, type=Path)
    b.add_argument("--study-config", type=Path)
    b.add_argument("--scripts", nargs="+")
    b.add_argument("--data-version", nargs="+")
    b.add_argument("--family")
    b.add_argument("--metric", nargs="+")
    b.add_argument("--thresholds", help="JSON object")
    b.add_argument(
        "--inputs",
        nargs="+",
        help="further files the study reads outside python/src/iap, configs/ and "
        "data/reference/ (identity v2 binds their hashes)",
    )
    b.add_argument(
        "--planted-dry-run",
        type=Path,
        help="committed planted dry-run report (iap.agents.planted_dry_run) to bind as the "
        "dry_run block; without it, sign refuses",
    )
    b.add_argument(
        "--image-digest",
        help="reproduction image to bind (sha256:<64 hex>); default: the one line of "
        "deployment/docker/IMAGE_DIGEST when the checkout has it (v2.0 stage 3 P1)",
    )
    b.add_argument(
        "--no-image-digest",
        action="store_true",
        help="bind no image (the analysis may run outside the pinned image)",
    )
    b.add_argument("--dry-run", action="store_true", help="print the document, write nothing")
    s = sub.add_parser("sign", help="(coordinator only) verify the identity, then sign")
    s.add_argument("--prereg", required=True, type=Path)
    s.add_argument("--keyfile", type=Path, default=KEYFILE)
    s.add_argument("--maintainer", default=MAINTAINER)
    a = ap.parse_args(argv)
    return cmd_bind(a) if a.cmd == "bind" else cmd_sign(a)


if __name__ == "__main__":
    raise SystemExit(main())
