"""Verify every signed document in the repository against the pinned trust root.

Review finding 6 / trust point (outside reviewer, 2026-10-18).  One command
that enumerates, verifies, reports per file and fails loudly:

    PYTHONPATH=python/src python tools/verify_signatures.py [--root .] [--trust-ref origin/main] [--json]

What it checks (``docs/governance/TRUST_ROOT.md``):

1. **Trust root.** ``research/agents/trust_roots.json`` matches the pinned
   version (:data:`PINNED`: canonical-JSON sha256 and the maintainer key
   fingerprints).  A changed trust root fails until the pin is updated by the
   rotation procedure, in a reviewed commit.
2. **Signed JSON documents.** Every git-tracked ``*.json`` that is a JSON
   object with ``"signed": true`` or a ``signature`` object (preregs, the
   REL-08 rule, ...): Ed25519 over the canonical body under the trust-root
   maintainer key (:func:`iap.agents.analysis_identity.verify_signed_document`).
   A document with a ``signature`` block but ``signed`` not true fails.
3. **Agent key registry.** ``research/agents/agent_pubkeys.sig.json``: the
   maintainer signature over the agent public-key registry.
4. **Blackboards.** Every tracked ``blackboard.jsonl``: hash chain and the
   agents' Ed25519 request signatures; the main board also its git anchors.

Prints ``OK`` or ``FAIL <reason>`` per item, a summary, and exits 1 on any
failure (2 on a usage error).  Reads no data and no private key.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python" / "src"))

#: The pinned trust root (docs/governance/TRUST_ROOT.md).  Changing it is a
#: key rotation or revocation: follow that procedure, never edit it to make a
#: failure go away.
PINNED: dict[str, Any] = {
    "version": 1,
    "trust_roots_canonical_sha256": (
        "6fb5818c1303c81f74118e64302bb44916e618b755f1af3907720504a11283bb"
    ),
    "maintainers": {
        "ashjha0": {
            "public_key": "02a3e278cda3efac7dcd266fb8a3821a1415f0725a73f8b255aac84083c2106c",
            "fingerprint_sha256": (
                "0723075249ff61946e0416f56c681cc961fba94beb13913a2c1c0f398b41eb3f"
            ),
        }
    },
    "revoked": [],
}


def canonical_sha256(doc: Any) -> str:
    raw = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def fingerprint(public_key_hex: str) -> str:
    """sha256 of the raw 32-byte Ed25519 public key, hex."""
    return hashlib.sha256(bytes.fromhex(public_key_hex)).hexdigest()


def tracked(root: Path, pattern: str) -> list[str]:
    p = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", pattern],
        capture_output=True,
        check=False,
    )
    if p.returncode != 0:
        raise SystemExit(f"git ls-files failed in {root}: not a git checkout")
    return sorted(x for x in p.stdout.decode("utf-8").split("\0") if x)


def check_trust_root(root: Path, pinned: dict[str, Any] | None = None) -> list[str]:
    from iap.agents import trust

    pinned = pinned or PINNED

    roots = trust.read_roots(root)
    if not roots:
        return ["research/agents/trust_roots.json missing or empty"]
    bad = []
    got = canonical_sha256(roots)
    if got != pinned["trust_roots_canonical_sha256"]:
        bad.append(
            f"trust root changed: canonical sha256 {got} != pinned "
            f"{pinned['trust_roots_canonical_sha256']} (version {pinned['version']}); "
            "a rotation updates the pin in tools/verify_signatures.py and TRUST_ROOT.md"
        )
    have = roots.get("maintainers") or {}
    for name, want in pinned["maintainers"].items():
        pub = have.get(name)
        if pub != want["public_key"] or fingerprint(pub) != want["fingerprint_sha256"]:
            bad.append(f"maintainer {name}: key does not match the pinned fingerprint")
    for name in sorted(set(have) - set(pinned["maintainers"])):
        bad.append(f"maintainer {name} is in the trust root but not pinned")
    for fp in pinned["revoked"]:
        if any(fingerprint(pub) == fp for pub in have.values()):
            bad.append(f"revoked key {fp[:16]}... is still in the trust root")
    return bad


def signed_documents(root: Path) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for rel in tracked(root, "*.json"):
        p = root / rel
        if not p.is_file():
            continue
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(doc, dict) and (
            doc.get("signed") is True or isinstance(doc.get("signature"), dict)
        ):
            out.append((rel, doc))
    return out


def check_document(
    root: Path, doc: dict[str, Any], pinned: dict[str, Any] | None = None
) -> str | None:
    from iap.agents.analysis_identity import SignatureError, verify_signed_document

    pinned = pinned or PINNED

    try:
        pub = verify_signed_document(root, doc)
    except (SignatureError, ValueError, TypeError) as exc:
        return str(exc)
    if fingerprint(pub) in pinned["revoked"]:
        return "signed with a revoked key"
    return None


def check_registry(root: Path, ref: str | None) -> str | None:
    from iap.agents import trust

    try:
        trust.trusted_pubkeys(root, ref)
    except trust.TrustError as exc:
        return str(exc)
    return None


def check_board(root: Path, rel: str, ref: str | None) -> list[str]:
    from iap.agents import anchor, prereg_gate, trust
    from iap.agents.blackboard import Blackboard, BlackboardError
    from iap.agents.broker import board_id

    board = root / rel
    fork = board.parent / "FORK.json"
    if fork.is_file():
        return check_fork_board(board, fork)
    main = board.resolve() == prereg_gate.board_path(root).resolve()
    if main:
        report = anchor.verify(root, board, root / anchor.ANCHORS_RELPATH, ref=ref)
        problems = list(report["problems"])
    else:
        try:
            Blackboard(board).verify()
            problems = []
        except BlackboardError as exc:
            return [str(exc)]
    try:
        pubkeys = trust.trusted_pubkeys(root, ref)
    except trust.TrustError as exc:
        return [*problems, f"agent key registry not trusted: {exc}"]
    bb = Blackboard(board)
    if bb.entries():
        problems += bb.verify_signatures(pubkeys, None, board_id(root, board))
    return problems


def _sha_lf(p: Path) -> set[str]:
    raw = p.read_bytes()
    return {
        hashlib.sha256(raw).hexdigest(),
        hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest(),
    }


def check_fork_board(board: Path, fork: Path) -> list[str]:
    """An archived fork board (v1.16 item 11): its files match the hashes in
    ``FORK.json``, its chain is intact and its agents' signatures verify under
    the fork's own archived ``agent_pubkeys.json`` (pinned by FORK.json in a
    reviewed commit; that registry is not maintainer-signed)."""
    from iap.agents.blackboard import Blackboard, BlackboardError

    meta = json.loads(fork.read_text(encoding="utf-8"))
    problems = []
    for name, want in (meta.get("files") or {}).items():
        p = fork.parent / name
        if not p.is_file() or want not in _sha_lf(p):
            problems.append(f"{name} does not match its sha256 in FORK.json")
    bb = Blackboard(board)
    try:
        n = bb.verify()
    except BlackboardError as exc:
        return [*problems, str(exc)]
    if n != meta.get("entries"):
        problems.append(f"{n} entries, FORK.json records {meta.get('entries')}")
    kp = fork.parent / "agent_pubkeys.json"
    keys = json.loads(kp.read_text(encoding="utf-8")) if kp.is_file() else {}
    return problems + bb.verify_signatures(keys)


def run(root: Path, ref: str | None = None) -> list[dict[str, Any]]:
    """One result per item: ``{"item", "kind", "ok", "reasons"}``."""
    results: list[dict[str, Any]] = []

    def add(item: str, kind: str, reasons: list[str]) -> None:
        results.append({"item": item, "kind": kind, "ok": not reasons, "reasons": reasons})

    add("research/agents/trust_roots.json", "trust_root", check_trust_root(root))
    docs = signed_documents(root)
    for rel, doc in docs:
        reason = check_document(root, doc)
        add(rel, "signed_document", [reason] if reason else [])
    if not docs:
        add("(signed documents)", "signed_document", ["no signed document found"])
    reason = check_registry(root, ref)
    add("research/agents/agent_pubkeys.sig.json", "key_registry", [reason] if reason else [])
    for rel in tracked(root, "*blackboard.jsonl"):
        add(rel, "blackboard", check_board(root, rel, ref))
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument(
        "--trust-ref",
        default=None,
        help="read the agent trust roots from this git ref (CI: origin/main)",
    )
    ap.add_argument("--json", action="store_true", help="print the results as JSON")
    a = ap.parse_args(argv)
    root = a.root.resolve()
    results = run(root, a.trust_ref)
    if a.json:
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        for r in results:
            if r["ok"]:
                print(f"OK    {r['kind']:<16} {r['item']}")
            else:
                for reason in r["reasons"]:
                    print(f"FAIL  {r['kind']:<16} {r['item']}: {reason}")
    failed = [r for r in results if not r["ok"]]
    print(
        f"{len(results) - len(failed)}/{len(results)} verified; "
        f"{len(failed)} failed (trust root version {PINNED['version']})",
        file=sys.stderr,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
