"""Trust roots for the agent public-key registry (G-P1-4, v1.14).

``research/agents/agent_pubkeys.json`` says which Ed25519 key speaks for
which agent.  Before v1.14 it was self-attesting: whoever could edit it could
register a key and then sign as that agent.  Now a NON-EMPTY registry is
trusted only when it carries a maintainer signature:

- ``research/agents/trust_roots.json``: ``{"maintainers": {id: public key
  hex}}`` - the maintainers' Ed25519 PUBLIC keys (private keys live outside
  the repository, ``python -m iap.agents.cli maintainer-keygen``);
- ``research/agents/agent_pubkeys.sig.json``: ``{"maintainer", "registry_digest",
  "sig"}`` - a maintainer's signature over the canonical digest of the
  registry (``python -m iap.agents.cli sign-registry``).

The trust roots a check uses should come from a ref the change under review
cannot edit: CI reads them from ``origin/main`` (``--trust-ref``), so a pull
request that adds both a maintainer key and a registry signed by it is not
trusted until the maintainer key itself has been reviewed onto main.  An
empty registry needs no signature (there is nothing to trust).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from iap.agents import signing
from iap.agents.blackboard import canonical, digest

PUBKEYS_RELPATH = Path("research") / "agents" / "agent_pubkeys.json"
TRUST_ROOTS_RELPATH = Path("research") / "agents" / "trust_roots.json"
REGISTRY_SIG_RELPATH = Path("research") / "agents" / "agent_pubkeys.sig.json"


class TrustError(ValueError):
    pass


def registry_message(pubkeys: dict[str, str]) -> bytes:
    doc = {"registry": "agent_pubkeys", "digest": digest(dict(pubkeys))}
    return canonical(doc).encode("ascii")


def sign_registry(private_hex: str, maintainer: str, pubkeys: dict[str, str]) -> dict[str, str]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_hex))
    return {
        "maintainer": maintainer,
        "registry_digest": digest(dict(pubkeys)),
        "sig": priv.sign(registry_message(pubkeys)).hex(),
    }


def verify_registry(pubkeys: dict[str, str], sig_doc: Any, roots: Any) -> list[str]:
    """Problems with the registry's maintainer signature; ``[]`` = trusted."""
    if not pubkeys:
        return []
    if not isinstance(roots, dict) or not isinstance(roots.get("maintainers"), dict):
        return [f"{TRUST_ROOTS_RELPATH} has no maintainers: a non-empty registry cannot be trusted"]
    maintainers = roots["maintainers"]
    if not maintainers:
        return [f"{TRUST_ROOTS_RELPATH} lists no maintainer: the registry cannot be trusted"]
    if not isinstance(sig_doc, dict):
        return [f"the registry is not signed ({REGISTRY_SIG_RELPATH} missing)"]
    who = sig_doc.get("maintainer")
    pub = maintainers.get(who)
    if pub is None:
        return [f"registry signed by {who!r}, who is not a trust root"]
    if sig_doc.get("registry_digest") != digest(dict(pubkeys)):
        return ["the registry changed after it was signed (re-sign with sign-registry)"]
    if not signing.verify_ed25519(pub, registry_message(pubkeys), str(sig_doc.get("sig", ""))):
        return [f"bad registry signature by {who!r}"]
    return []


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="ascii")) if path.is_file() else None


def read_roots(root: Path, ref: str | None = None) -> Any:
    """The trust roots from the working tree, or from git ``ref`` when given."""
    if ref is None:
        return _read_json(Path(root) / TRUST_ROOTS_RELPATH)
    out = subprocess.run(
        ["git", "-C", str(root), "show", f"{ref}:{TRUST_ROOTS_RELPATH.as_posix()}"],
        capture_output=True,
        check=False,
    )
    if out.returncode != 0:
        return None
    return json.loads(out.stdout.decode("ascii"))


def load_pubkeys(root: Path) -> dict[str, str]:
    data = _read_json(Path(root) / PUBKEYS_RELPATH)
    return dict(data) if isinstance(data, dict) else {}


def trusted_pubkeys(root: Path, ref: str | None = None) -> dict[str, str]:
    """The registry, if it is empty or signed by a trust root; else :class:`TrustError`."""
    root = Path(root)
    pubkeys = load_pubkeys(root)
    problems = verify_registry(
        pubkeys, _read_json(root / REGISTRY_SIG_RELPATH), read_roots(root, ref)
    )
    if problems:
        raise TrustError("; ".join(problems))
    return pubkeys
