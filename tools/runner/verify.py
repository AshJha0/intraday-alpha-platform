"""Pre-request verification for the registered runner (property 4: hash
verification precedes any read; property 2: the image digest is in the
prereg).

:func:`verify_registration` refuses (``RegistrationError``) unless, in order:

1. the prereg document is signed and its Ed25519 signature verifies under the
   repository's trust root at the checked-out commit;
2. it carries a ``registered_run`` block naming a partition (``a``/``b``/``c``),
   a ``dataset_hash_id``, the ``image_digest`` and the ``command`` to run;
3. the pinned reproduction image digest (``deployment/docker/IMAGE_DIGEST``,
   written by P1) equals the bound ``image_digest``;
4. the ``analysis_identity`` block verifies against the checkout and the
   process (:func:`iap.agents.analysis_identity.verify_identity`: scripts,
   code tree, configs, reference data, lock, installed packages,
   interpreter, and the bound commit is HEAD or an ancestor of HEAD, since
   the signed prereg itself is committed on top of the commit it binds while
   every bound hash must still match); the dataset hash id is one of the
   bound ``data_versions``.

Dataset hashes: when the block carries ``dataset_files`` (``name -> sha256``),
:func:`check_dataset_files` requires the served file set and hashes to equal
it exactly before the analysis runs.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from iap.agents import analysis_identity as ai

PARTITIONS = ("a", "b", "c")
REQUIRED_RUN_FIELDS = ("partition", "dataset_hash_id", "image_digest", "command")
IMAGE_DIGEST_REL = Path("deployment") / "docker" / "IMAGE_DIGEST"


class RegistrationError(ValueError):
    """The registration does not verify; nothing was requested or read."""


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def prereg_hash(doc: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical(doc).encode("ascii")).hexdigest()


def load_prereg(root: Path, rel: str) -> dict[str, Any]:
    p = Path(root) / rel
    if not p.is_file():
        raise RegistrationError(f"prereg {rel} is not in the checkout")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise RegistrationError(f"prereg {rel} is not JSON: {exc}") from exc


def read_image_digest(root: Path, rel: Path = IMAGE_DIGEST_REL) -> str:
    p = Path(root) / rel
    if not p.is_file():
        raise RegistrationError(f"{rel.as_posix()} missing: no pinned reproduction image digest")
    digest = p.read_text(encoding="utf-8").strip().split()[0] if p.read_text(encoding="utf-8").strip() else ""
    if not digest.startswith("sha256:") or len(digest) != 71:
        raise RegistrationError(f"{rel.as_posix()} does not hold a sha256 image digest")
    return digest


def registered_run(doc: Mapping[str, Any]) -> dict[str, Any]:
    run = doc.get("registered_run")
    if not isinstance(run, Mapping):
        raise RegistrationError("prereg has no registered_run block")
    missing = [k for k in REQUIRED_RUN_FIELDS if not run.get(k)]
    if missing:
        raise RegistrationError(f"registered_run lacks {missing}")
    if run["partition"] not in PARTITIONS:
        raise RegistrationError(f"registered_run.partition {run['partition']!r} is not a/b/c")
    if not isinstance(run["command"], list) or not all(isinstance(x, str) for x in run["command"]):
        raise RegistrationError("registered_run.command must be a list of strings")
    return dict(run)


def verify_registration(
    root: Path, doc: Mapping[str, Any], *, image_digest_rel: Path = IMAGE_DIGEST_REL
) -> dict[str, Any]:
    """Everything that must hold BEFORE the sealed read is requested.  Returns a
    record of what verified (hashes only, no paths)."""
    root = Path(root)
    try:
        pub = ai.verify_signed_document(root, doc)
    except (ai.SignatureError, ValueError) as exc:
        raise RegistrationError(f"signature: {exc}") from exc
    run = registered_run(doc)
    digest = read_image_digest(root, image_digest_rel)
    if digest != run["image_digest"]:
        raise RegistrationError("pinned image digest differs from the one bound by the prereg")
    bound = doc.get("analysis_identity")
    if not isinstance(bound, Mapping):
        raise RegistrationError("prereg binds no analysis_identity")
    try:
        ident = ai.verify_identity(
            root,
            bound,
            data_versions=list(bound.get("data_versions") or []),
            family=str(bound.get("family")),
            metrics=list(bound.get("metrics") or []),
            thresholds=dict(bound.get("thresholds") or {}),
        )
    except ai.IdentityError as exc:
        raise RegistrationError(f"analysis_identity: {exc}") from exc
    if run["dataset_hash_id"] not in (bound.get("data_versions") or []):
        raise RegistrationError("registered_run.dataset_hash_id is not among the bound data_versions")
    return {
        "prereg_id": str(doc.get("prereg_id")),
        "prereg_sha256": prereg_hash(doc),
        "signing_key": pub,
        "image_digest": digest,
        "commit": ident.get("commit"),
        "partition": run["partition"],
        "dataset_hash_id": run["dataset_hash_id"],
    }


def check_dataset_files(run: Mapping[str, Any], served: Mapping[str, str]) -> None:
    """The served ``name -> sha256`` map must equal the bound one when the prereg
    binds one; the file set is checked exactly (no extra, no missing file)."""
    bound = run.get("dataset_files")
    if bound is None:
        return
    if dict(bound) != dict(served):
        extra = sorted(set(served) - set(bound))
        missing = sorted(set(bound) - set(served))
        changed = sorted(n for n in set(bound) & set(served) if bound[n] != served[n])
        raise RegistrationError(
            f"served dataset differs from the bound dataset_files: extra {extra}, "
            f"missing {missing}, changed {changed}"
        )
