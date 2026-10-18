"""Analysis identity bound by a signed pre-registration (v1.18 REL-02a, review R-I).

A signature that covers only a hypothesis lets the analysis change after it:
a different script, commit, dataset or threshold under the same signed text.
A prereg that carries ``analysis_identity`` binds, in the signed body:

* ``scripts``: repo-relative path -> sha256 of the LF-normalised source;
* ``code_tree_sha256``: one hash over every ``python/src/iap/**/*.py`` file
  (the library the scripts call; a changed helper changes it);
* ``commit``: the exact commit the identity was built at.  The runner needs it
  to be HEAD or an ancestor of HEAD (the prereg document itself lands in a
  later commit, so equality with HEAD is impossible); the tree hash is the
  hard check, the commit ties it to history;
* ``data_versions``: the ``dataset_version`` hashes the analysis may read;
* ``family``: the declared hypothesis family (what N counts);
* ``metrics`` and ``thresholds``: the statistics and the numbers that decide.

:func:`verify_identity` refuses on ANY mismatch and names every one.

Signer is not runner: code can enforce that the runner declares an actor id
different from the signature's maintainer and holds no signing key material
in its environment (:func:`require_independent_runner`).  It cannot prove who
sits at the keyboard; the private key lives outside the repository
(``docs/governance/TRUST_ROOTS.md``) and the owner signs on a machine that
does not run the analysis.  That residual is documented, not hidden.

Legacy preregs (R7, signed before v1.18) carry no block; ``legacy_ok=True``
accepts only those, and reports them as legacy.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from iap.agents.blackboard import canonical

BLOCK = "analysis_identity"
#: environment variables whose presence means this process can sign
SIGNING_ENV = ("IAP_MAINTAINER_KEY", "IAP_SIGNING_KEY", "IAP_AGENT_PRIVATE_KEY", "IAP_KEYFILE")
ACTOR_ENV = "IAP_RUN_ACTOR"


class SignatureError(ValueError):
    pass


class IdentityError(ValueError):
    """The analysis about to run is not the one the signed prereg bound."""


def verify_signed_document(root: Path, doc: Mapping[str, Any]) -> str:
    """Signed, Ed25519 signature valid under the trust root; returns the public key."""
    from iap.agents import signing, trust

    if doc.get("signed") is not True:
        raise SignatureError("prereg is not signed")
    sig = doc.get("signature")
    if not isinstance(sig, dict) or sig.get("scheme") != "ed25519":
        raise SignatureError("prereg has no ed25519 signature block")
    roots = trust.read_roots(root) or {}
    pub = (roots.get("maintainers") or {}).get(sig.get("maintainer"))
    if pub is None or pub != sig.get("public_key"):
        raise SignatureError("prereg signing key is not the trust-root maintainer key")
    body = {k: v for k, v in doc.items() if k != "signature"}
    msg = canonical(body).encode("ascii")
    if hashlib.sha256(msg).hexdigest() != sig.get("message_sha256"):
        raise SignatureError("prereg message sha256 mismatch (document changed after signing)")
    if not signing.verify_ed25519(pub, msg, sig.get("sig", "")):
        raise SignatureError("prereg Ed25519 signature INVALID")
    return pub


def _norm_sha(path: Path) -> str:
    text = path.read_bytes().decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def script_fingerprints(root: Path, scripts: Sequence[str]) -> dict[str, str]:
    out = {}
    for rel in sorted(scripts):
        p = Path(root) / rel
        if not p.is_file():
            raise IdentityError(f"bound script {rel} does not exist")
        out[rel] = _norm_sha(p)
    return out


def code_tree_sha256(root: Path) -> str:
    h = hashlib.sha256()
    base = Path(root) / "python" / "src" / "iap"
    for p in sorted(base.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        h.update(p.relative_to(base).as_posix().encode() + b"\n" + _norm_sha(p).encode() + b"\n")
    return h.hexdigest()


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )


def head_commit(root: Path) -> str | None:
    p = _git(root, "rev-parse", "HEAD")
    return p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else None


def build_identity(
    root: Path,
    *,
    scripts: Sequence[str],
    data_versions: Sequence[str],
    family: str,
    metrics: Sequence[str],
    thresholds: Mapping[str, Any],
    commit: str | None = None,
) -> dict[str, Any]:
    """The block to embed in a prereg draft before the owner signs it."""
    c = commit or head_commit(root)
    if not c:
        raise IdentityError("not a git checkout: the identity needs an exact commit")
    return {
        "scripts": script_fingerprints(root, scripts),
        "code_tree_sha256": code_tree_sha256(root),
        "commit": c,
        "data_versions": sorted(data_versions),
        "family": family,
        "metrics": sorted(metrics),
        "thresholds": dict(thresholds),
    }


def verify_identity(
    root: Path,
    bound: Mapping[str, Any] | None,
    *,
    data_versions: Sequence[str],
    family: str,
    metrics: Sequence[str],
    thresholds: Mapping[str, Any],
    legacy_ok: bool = False,
) -> dict[str, Any]:
    """Compare what is about to run (arguments, plus the files on disk) with the
    signed block; raise :class:`IdentityError` listing every mismatch."""
    if not bound:
        if legacy_ok:
            return {"legacy": True}
        raise IdentityError("the signed prereg binds no analysis_identity; refusing to run")
    bad: list[str] = []
    for rel, want in (bound.get("scripts") or {}).items():
        p = Path(root) / rel
        if not p.is_file():
            bad.append(f"script {rel} missing")
        elif _norm_sha(p) != want:
            bad.append(f"script {rel} changed since signing")
    if not bound.get("scripts"):
        bad.append("no script fingerprint bound")
    if code_tree_sha256(root) != bound.get("code_tree_sha256"):
        bad.append("python/src/iap code tree differs from the bound code_tree_sha256")
    commit = bound.get("commit")
    head = head_commit(root)
    if not commit or head is None:
        bad.append("exact commit unavailable")
    elif commit != head and _git(root, "merge-base", "--is-ancestor", commit, head).returncode != 0:
        bad.append(f"bound commit {commit[:12]} is not HEAD or an ancestor of HEAD")
    if sorted(data_versions) != sorted(bound.get("data_versions") or []):
        bad.append(f"data versions {sorted(data_versions)} != bound {bound.get('data_versions')}")
    if family != bound.get("family"):
        bad.append(f"family {family!r} != bound {bound.get('family')!r}")
    if sorted(metrics) != sorted(bound.get("metrics") or []):
        bad.append(f"metrics {sorted(metrics)} != bound {bound.get('metrics')}")
    if dict(thresholds) != dict(bound.get("thresholds") or {}):
        bad.append(f"thresholds {dict(thresholds)} != bound {bound.get('thresholds')}")
    if bad:
        raise IdentityError("analysis identity mismatch: " + "; ".join(bad))
    return {"legacy": False, "commit": commit, "scripts": sorted(bound["scripts"])}


def require_independent_runner(doc: Mapping[str, Any], env: Mapping[str, str] | None = None) -> str:
    """The process running the analysis must not be the signer.  Refuses when
    the declared actor (``IAP_RUN_ACTOR``) is missing or equals the signing
    maintainer, or when signing-key material is present in the environment.
    Returns the runner actor."""
    e = os.environ if env is None else env
    signer = (doc.get("signature") or {}).get("maintainer")
    actor = e.get(ACTOR_ENV)
    if not actor:
        raise IdentityError(f"{ACTOR_ENV} is not set: the runner must declare who it is")
    if signer and actor == signer:
        raise IdentityError(f"runner actor {actor!r} is the prereg signer; sign and run apart")
    held = [k for k in SIGNING_ENV if e.get(k)]
    if held:
        raise IdentityError(f"signing key material in the runner's environment: {held}")
    return actor
