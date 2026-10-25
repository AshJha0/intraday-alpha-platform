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

Identity versions (review finding 1, 2026-10-18).  A block without
``identity_version`` is **v1**: exactly the fields above, verified exactly as
when it was signed, so preregs signed under v1 stay verifiable history and are
never re-signed.  :func:`build_identity` now writes **v2**, which also binds
every other input that can change a result
(``docs/governance/ANALYSIS_IDENTITY.md`` lists them):

* ``code_tree_sha256`` over EVERY file of ``python/src/iap`` (package data
  too, not only ``*.py``): feature registry and spec code, label
  definitions, cost and fill models;
* ``configs_tree_sha256``: every file under ``configs/`` (instruments,
  venues, fee tiers, regulatory fees, venue latency, execution, risk, ...);
* ``reference_tree_sha256``: every file under ``data/reference/`` (the
  feature registry JSON);
* ``inputs``: any further file the study reads (a calibration, a venue file
  under ``research/``), path -> sha256, declared at bind time;
* ``deps_lock_sha256``: the hashed lock ``python/requirements-ci.txt``, and
  ``environment``: the installed version of every package the lock pins (a
  matching lock with a drifted environment is refused);
* ``interpreter``: implementation and exact ``major.minor.micro``;
* ``image_digest`` (v2.0 stage 3 P1, optional): the digest of the pinned
  reproduction image (``deployment/docker/IMAGE_DIGEST``, the image
  ``deployment/docker/Dockerfile.repro`` builds).  A block that carries it can
  only run inside that image: the runner declares the image it was started
  from in ``IAP_IMAGE_DIGEST`` (the registered-run workflow sets it from the
  same digest it pulls) and a missing or different value is a mismatch.  A
  block without it is verified as before, so earlier v2 preregs stay valid.

Paths.  Every path in a block is repo-relative with ``/`` separators, and
every check takes the checkout root as an argument, so the same block
verifies at ``C:\\Work\\...`` on the laptop and at ``/work`` inside the
container (:func:`verify_identity` and :func:`environment_mismatches` take
``root``; nothing reads the working directory).

Datasets are bound by ``data_versions``, which are content hashes
(``iap.marketdata.ingest.real_dataset_version``): a changed byte is a
different version, and :func:`dataset_version_of` re-hashes the files
against their manifest, so a tampered tree is refused before it is used.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from iap.agents.blackboard import canonical

BLOCK = "analysis_identity"
#: what :func:`build_identity` writes; a block without the field is version 1
IDENTITY_VERSION = 2
SUPPORTED_VERSIONS = (1, 2)
LOCK_REL = "python/requirements-ci.txt"
CODE_REL = "python/src/iap"
CONFIGS_REL = "configs"
REFERENCE_REL = "data/reference"
#: environment variables whose presence means this process can sign
SIGNING_ENV = ("IAP_MAINTAINER_KEY", "IAP_SIGNING_KEY", "IAP_AGENT_PRIVATE_KEY", "IAP_KEYFILE")
ACTOR_ENV = "IAP_RUN_ACTOR"
#: the digest of the reproduction image the running process was started from
IMAGE_DIGEST_ENV = "IAP_IMAGE_DIGEST"
IMAGE_DIGEST_REL = "deployment/docker/IMAGE_DIGEST"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


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


def _file_sha(path: Path) -> str:
    """Text hashes LF-normalised (checkout line endings do not matter); a file
    that is not UTF-8 hashes its raw bytes."""
    try:
        return _norm_sha(path)
    except UnicodeDecodeError:
        return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_sha256(root: Path, rel: str) -> str | None:
    """One hash over every file under ``root/rel`` (relative path and content;
    byte-code caches excluded); ``None`` when the directory does not exist."""
    base = Path(root) / rel
    if not base.is_dir():
        return None
    h = hashlib.sha256()
    for p in sorted(base.rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts or p.suffix in (".pyc", ".pyo"):
            continue
        h.update(p.relative_to(base).as_posix().encode() + b"\n" + _file_sha(p).encode() + b"\n")
    return h.hexdigest()


def lock_pins(root: Path) -> dict[str, str]:
    """``name -> version`` of every ``name==version`` line of the hashed lock."""
    p = Path(root) / LOCK_REL
    if not p.is_file():
        return {}
    out = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        if line[:1].isalnum() and "==" in line:
            name, ver = line.split("==", 1)
            out[name.strip().lower()] = ver.split()[0].strip()
    return out


def environment(root: Path) -> dict[str, str]:
    """Installed version of every package the lock pins (``"absent"`` when it
    is not installed)."""
    from importlib import metadata

    out = {}
    for name in sorted(lock_pins(root)):
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            out[name] = "absent"
    return out


def interpreter() -> str:
    v = sys.version_info
    return f"{sys.implementation.name}-{v.major}.{v.minor}.{v.micro}"


def read_image_digest(root: Path) -> str | None:
    """The one-line ``deployment/docker/IMAGE_DIGEST`` (``sha256:<64 hex>``), or
    ``None`` when the checkout has none; a malformed file is an error."""
    p = Path(root) / IMAGE_DIGEST_REL
    if not p.is_file():
        return None
    text = p.read_text(encoding="utf-8").strip()
    if not _DIGEST.match(text):
        raise IdentityError(f"{IMAGE_DIGEST_REL} is not one line sha256:<64 hex>: {text[:80]!r}")
    return text


def input_fingerprints(root: Path, inputs: Sequence[str]) -> dict[str, str]:
    out = {}
    for rel in sorted(inputs):
        p = Path(root) / rel
        if not p.is_file():
            raise IdentityError(f"bound input {rel} does not exist")
        out[rel] = _file_sha(p)
    return out


def dataset_version_of(dataset_dir: Path | str) -> str:
    """The content version of a dataset directory after re-hashing its files
    against the manifest (a tampered file is refused, never versioned)."""
    from iap.research.holdout_registry import HoldoutRefused, dataset_identity

    try:
        ident = dataset_identity(dataset_dir, rehash=True)
    except HoldoutRefused as exc:
        raise IdentityError(str(exc)) from exc
    if not ident.get("dataset_version"):
        raise IdentityError(f"{dataset_dir}: the manifest declares no dataset_version")
    return str(ident["dataset_version"])


def _environment_block(root: Path) -> dict[str, Any]:
    """The v2 fields computed from the checkout and the running process."""
    lock = Path(root) / LOCK_REL
    return {
        "code_tree_sha256": tree_sha256(root, CODE_REL),
        "configs_tree_sha256": tree_sha256(root, CONFIGS_REL),
        "reference_tree_sha256": tree_sha256(root, REFERENCE_REL),
        "deps_lock_sha256": _norm_sha(lock) if lock.is_file() else None,
        "environment": environment(root),
        "interpreter": interpreter(),
    }


def identity_version(bound: Mapping[str, Any]) -> int:
    v = bound.get("identity_version", 1)
    if v not in SUPPORTED_VERSIONS:
        raise IdentityError(f"unknown analysis identity version {v!r}")
    return int(v)


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
    inputs: Sequence[str] = (),
    image_digest: str | None = None,
) -> dict[str, Any]:
    """The (version 2) block to embed in a prereg draft before the owner signs it.

    ``image_digest`` pins the reproduction image the analysis must run in
    (``sha256:<64 hex>``); ``None`` binds no image."""
    c = commit or head_commit(root)
    if not c:
        raise IdentityError("not a git checkout: the identity needs an exact commit")
    if image_digest is not None and not _DIGEST.match(image_digest):
        raise IdentityError(f"image digest must be sha256:<64 hex>, got {image_digest!r}")
    block = {
        "identity_version": IDENTITY_VERSION,
        "scripts": script_fingerprints(root, scripts),
        "inputs": input_fingerprints(root, inputs),
        **_environment_block(root),
        "commit": c,
        "data_versions": sorted(data_versions),
        "family": family,
        "metrics": sorted(metrics),
        "thresholds": dict(thresholds),
    }
    if image_digest is not None:
        block["image_digest"] = image_digest
    return block


def verify_identity(
    root: Path,
    bound: Mapping[str, Any] | None,
    *,
    data_versions: Sequence[str],
    family: str,
    metrics: Sequence[str],
    thresholds: Mapping[str, Any],
    legacy_ok: bool = False,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Compare what is about to run (arguments, plus the files on disk and the
    process environment) with the signed block; raise :class:`IdentityError`
    listing every mismatch.  ``env`` defaults to ``os.environ``."""
    if not bound:
        if legacy_ok:
            return {"legacy": True}
        raise IdentityError("the signed prereg binds no analysis_identity; refusing to run")
    version = identity_version(bound)
    # the two-argument call is kept when no env is given: runner tests replace
    # environment_mismatches with a two-argument double
    bad = (
        environment_mismatches(root, bound)
        if env is None
        else environment_mismatches(root, bound, env=env)
    )
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
    return {
        "legacy": False,
        "identity_version": version,
        "commit": commit,
        "scripts": sorted(bound["scripts"]),
    }


def environment_mismatches(
    root: Path, bound: Mapping[str, Any], env: Mapping[str, str] | None = None
) -> list[str]:
    """Every difference between the checkout / process and ``bound`` that needs
    no runner declaration: scripts and code tree and, for v2, inputs,
    configs, reference data, lock, installed packages, interpreter and (when
    bound) the reproduction image digest declared in ``env``
    (:data:`IMAGE_DIGEST_ENV`; ``env`` defaults to ``os.environ``).
    :func:`verify_identity` and the holdout guard both use it (a prereg
    cannot authorise a holdout read from code it did not bind)."""
    version = identity_version(bound)
    bad: list[str] = []
    for rel, want in (bound.get("scripts") or {}).items():
        p = Path(root) / rel
        if not p.is_file():
            bad.append(f"script {rel} missing")
        elif _norm_sha(p) != want:
            bad.append(f"script {rel} changed since signing")
    if not bound.get("scripts"):
        bad.append("no script fingerprint bound")
    if version == 1:
        if code_tree_sha256(root) != bound.get("code_tree_sha256"):
            bad.append("python/src/iap code tree differs from the bound code_tree_sha256")
        return bad
    for rel, want in (bound.get("inputs") or {}).items():
        p = Path(root) / rel
        if not p.is_file():
            bad.append(f"input {rel} missing")
        elif _file_sha(p) != want:
            bad.append(f"input {rel} changed since signing")
    now = _environment_block(root)
    labels = {
        "code_tree_sha256": "python/src/iap code tree (code, features, labels, costs)",
        "configs_tree_sha256": "configs/ tree (instruments, venues, fees)",
        "reference_tree_sha256": "data/reference/ tree (feature registry)",
        "deps_lock_sha256": f"dependency lock {LOCK_REL}",
        "interpreter": "interpreter",
    }
    for key, what in labels.items():
        if now[key] != bound.get(key):
            bad.append(
                f"{what.split(' (')[0]} differs from the bound {key} ({what}: {bound.get(key)} -> {now[key]})"
            )
    want_env = bound.get("environment") or {}
    drift = sorted(
        f"{n} {want_env.get(n)} -> {now['environment'].get(n)}"
        for n in set(want_env) | set(now["environment"])
        if want_env.get(n) != now["environment"].get(n)
    )
    if drift:
        bad.append(f"installed packages differ from the bound environment: {drift}")
    want_image = bound.get("image_digest")
    if want_image:
        e = os.environ if env is None else env
        got = e.get(IMAGE_DIGEST_ENV)
        if not got:
            bad.append(
                f"the prereg binds reproduction image {want_image[:19]}... but {IMAGE_DIGEST_ENV} "
                "is not set: not running inside the pinned image"
            )
        elif got != want_image:
            bad.append(f"reproduction image {got} is not the bound {want_image}")
    return bad


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
