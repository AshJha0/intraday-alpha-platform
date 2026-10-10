"""Agent request signatures (G4): Ed25519 by default, HMAC legacy-verify only.

A signed request covers ``{"agent", "op", "args_digest", "nonce"}`` in
canonical JSON.  The scheme is named in the stored ``auth`` record:

- ``"ed25519"`` (default, v1.10): the agent signs with its private key, held
  outside the repository; the broker and every verifier hold only the public
  key (``research/agents/agent_pubkeys.json``), so a verifier cannot forge.
- ``"hmac-sha256"``: the symmetric scheme of the first v1.10 draft.  Records
  without a ``scheme`` field are this scheme.  It can still be VERIFIED with
  the shared key (:func:`verify`), but nothing signs new requests with it.

Uses the ``cryptography`` package (Ed25519, raw 32-byte keys as hex).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from iap.agents.blackboard import canonical, digest

ED25519 = "ed25519"
HMAC_SHA256 = "hmac-sha256"
_RAW = serialization.Encoding.Raw


def message(agent: str, op: Any, args_digest: Any, nonce: Any) -> bytes:
    doc = {"agent": agent, "op": op, "args_digest": args_digest, "nonce": nonce}
    return canonical(doc).encode("ascii")


def generate_keypair() -> tuple[str, str]:
    """``(private_hex, public_hex)``, raw 32-byte Ed25519 keys."""
    priv = Ed25519PrivateKey.generate()
    priv_raw = priv.private_bytes(
        _RAW, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    pub_raw = priv.public_key().public_bytes(_RAW, serialization.PublicFormat.Raw)
    return priv_raw.hex(), pub_raw.hex()


def public_of(private_hex: str) -> str:
    priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_hex))
    return priv.public_key().public_bytes(_RAW, serialization.PublicFormat.Raw).hex()


def sign(private_hex: str, agent: str, op: str, args: Any) -> dict[str, str]:
    """The ``auth`` an agent passes with a request (fresh nonce each call)."""
    nonce = secrets.token_hex(16)
    priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_hex))
    sig = priv.sign(message(agent, op, digest(args), nonce))
    return {"scheme": ED25519, "nonce": nonce, "sig": sig.hex()}


def verify_ed25519(public_hex: str, msg: bytes, sig_hex: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex)).verify(
            bytes.fromhex(sig_hex), msg
        )
    except (InvalidSignature, ValueError):
        return False
    return True


def verify(
    auth: dict[str, Any],
    agent: str,
    pubkeys: dict[str, str],
    legacy_hmac_keys: dict[str, bytes] | None = None,
) -> str | None:
    """``None`` if the stored ``auth`` record is a valid signature by ``agent``,
    else the reason."""
    if auth.get("key_id") != agent:
        return "key_id is not the writing agent"
    msg = message(agent, auth.get("op"), auth.get("args_digest"), auth.get("nonce"))
    scheme = auth.get("scheme", HMAC_SHA256)
    if scheme == ED25519:
        pub = pubkeys.get(agent)
        if pub is None:
            return f"no public key for {agent}"
        return None if verify_ed25519(pub, msg, str(auth.get("sig", ""))) else "bad signature"
    if scheme == HMAC_SHA256:
        key = (legacy_hmac_keys or {}).get(agent)
        if key is None:
            return f"legacy hmac-sha256 entry by {agent}: no shared key to verify it"
        want = hmac.new(key, msg, hashlib.sha256).hexdigest()
        return None if hmac.compare_digest(want, str(auth.get("mac", ""))) else "bad signature"
    return f"unknown signature scheme {scheme!r}"
