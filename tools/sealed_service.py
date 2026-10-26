#!/usr/bin/env python3
"""The sealed-data service (v2.0 stage 2, D2; U0 section 2; v1.19 spec rev 9 D2).

One instance runs under EACH sealed identity (``iap-sealed-a`` / ``-b`` /
``-c``), owning one partition (``C:\\iap-sealed\\a`` / ``b`` / ``c``).  It is
the only way any other identity reaches sealed bytes: ``iap-runner`` cannot
list or open the partition, it asks this service, and the service decides.

Transport: a Windows named pipe (``\\\\.\\pipe\\iap-sealed-<partition>``).
Authentication is the operating system's: the service impersonates the
connected client just long enough to read the client token's account name
(``ImpersonateNamedPipeClient`` + ``GetUserNameW``), and reads the client
process id from the pipe (``GetNamedPipeClientProcessId``) to hash its
process image.  **No bearer token, no per-boot token file**: a client cannot
assert an identity, the kernel reports it.

Authorisation is service-side and in this order, every check audited:

1. the caller's account is in ``allowed_accounts`` (default ``iap-runner``);
2. the caller's process image hash is one of ``registered_images`` (when the
   list is non-empty; a hash that cannot be read refuses);
3. for ``read``, no checkpoint is pending (write-before-serve: a served read
   whose checkpoint the runner has not yet published blocks every further
   read until ``publish`` lands; a restart keeps the pending state; the
   pending registration's own ``store_result`` and ``publish`` still work);
4. ``read``: the request carries a SIGNED prereg (Ed25519 under the trust root
   copy pinned in the partition at install) whose ``registered_run`` names
   THIS partition and the requested ``dataset_hash_id``; the registration
   ledger has seen neither the prereg hash nor the prereg id (one run per
   registration; a new commit does not make a new registration); the dataset
   files on disk still match their recorded hashes (hash verification
   precedes any read; a mismatch is recorded as a failed registration and no
   retry is possible without a new signed registration); then the ledger
   entry, the audit record and the pending checkpoint are written BEFORE the
   first byte is served.

Checkpoint content rule (I2): ``seq``, ``chain_head``, ``utc``, ``prereg_id``
and ``dataset_hash_id`` only (the last two are public in the repository);
never a path, a credential, a host name or an account name.  The runner
commits each checkpoint to ``research/audit/CHECKPOINTS.jsonl`` (repository
anchor) and tells the service the commit through ``publish``.  ``verify``
checks the local audit chain, and ``verify-checkpoints`` checks a committed
checkpoint file against the content rule and the chain order (anyone, CI).

Ingestion (v2.0 stage 2 DD1): ``ingest`` is the only way bytes ENTER a
partition from another identity.  The caller (``iap-runner`` only, the same
account rule as every op) names a ``dataset_hash_id`` and streams the files
after the request frame; the service writes them under
``<root>/datasets/<hash_id>/``, re-hashes every file, checks the content hash
over the sorted ``name`` + sha256 lines equals the claimed id, records the
dataset exactly as ``add-dataset`` does (so a later ``read`` serves it), keeps
the caller's own row counts as metadata, and answers with hashes and counts
only.  A second ingest of a known id is refused (``dataset_exists``); a hash
mismatch removes the written files and is refused.  Nothing ingested is ever
returned by ``ingest``: the development identity learns the hashes and the
counts the ingestion job itself produced, never a value.

Results: ``store_result`` records the sha256 of the result file under the
registration; ``verify-result`` compares a result commit's hash with the
service's copy (property 5: a result commit that does not match is a
detectable tamper).

Backup (``tools/admin/backup_sealed.ps1`` / ``restore_sealed.ps1``): the offline
subcommands ``backup-manifest`` and ``verify-backup`` run AS the partition's
identity, never through the pipe, so the authorisation order above is
untouched.  They record and check the sha256 of every file and the audit chain
head; they print counts and hashes only.

Single-machine limit (``docs/governance/SEALED_DATA.md``): everything here
runs on one workstation whose administrator is the owner.  The controls stop
the development identity's processes, not the administrator; the evidence
class stays "mechanically isolated" (U0 section 2, property 6).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import io
import json
import os
import struct
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
#: The service may run from an all-users interpreter and a checkout outside the
#: owner's profile (``docs/governance/SEALED_DATA.md`` section 5, e.g. ``C:\\iap\\repo``):
#: the package root is this checkout's ``python/src``, or ``IAP_REPO`` when the
#: script was copied elsewhere.
_SRC = REPO / "python" / "src"
if not _SRC.is_dir() and os.environ.get("IAP_REPO"):
    _SRC = Path(os.environ["IAP_REPO"]) / "python" / "src"
sys.path.insert(0, str(_SRC))

SCHEMA = "iap.sealed_service/v1"
GENESIS = "0" * 64
DEFAULT_ACCOUNT = "iap-runner"
PIPE_PREFIX = r"\\.\pipe\iap-sealed-"
CHECKPOINT_FIELDS = ("seq", "chain_head", "utc", "prereg_id", "dataset_hash_id")
#: words that must never appear in a checkpoint (the content-rule test greps for them)
FORBIDDEN_CHECKPOINT_KEYS = frozenset(
    {"path", "dir", "file", "host", "hostname", "account", "user", "token", "key", "password"}
)
_FRAME = struct.Struct(">I")
MAX_FRAME = 1 << 30


def utc_now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def prereg_hash(doc: Mapping[str, Any]) -> str:
    """The registration key: sha256 of the canonical signed document."""
    return sha256_bytes(canonical(doc).encode("ascii"))


def content_hash(files: Mapping[str, str]) -> str:
    """The dataset id of a file set: sha256 over ``name\\nsha256\\n`` lines, names sorted."""
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(name.encode("utf-8") + b"\n" + files[name].encode("ascii") + b"\n")
    return h.hexdigest()


class Refused(PermissionError):
    """The request is refused; ``code`` is the audited reason."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------- identity --
@dataclass(frozen=True)
class Caller:
    """What the operating system says about the connected client."""

    account: str
    pid: int | None = None
    image_sha256: str | None = None


# ------------------------------------------------------------- audit chain --
class Audit:
    """Append-only, hash-chained JSON lines (``prev_sha256`` of the line before)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _last(self) -> bytes | None:
        if not self.path.is_file() or self.path.stat().st_size == 0:
            return None
        with self.path.open("rb") as f:
            f.seek(max(0, self.path.stat().st_size - (1 << 16)))
            lines = [ln for ln in f.read().split(b"\n") if ln.strip()]
        return lines[-1] if lines else None

    def head(self) -> str:
        last = self._last()
        return sha256_bytes(last) if last else GENESIS

    def append(self, record: Mapping[str, Any]) -> str:
        """Write one line; returns the new chain head (sha256 of that line)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {**record, "utc": record.get("utc") or utc_now(), "prev_sha256": self.head()},
            sort_keys=True,
        ).encode("utf-8")
        with self.path.open("ab") as f:
            f.write(line + b"\n")
            f.flush()
            os.fsync(f.fileno())
        return sha256_bytes(line)

    def verify(self) -> list[str]:
        problems: list[str] = []
        if not self.path.is_file():
            return problems
        prev: bytes | None = None
        for n, line in enumerate(self.path.read_bytes().split(b"\n"), 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                problems.append(f"line {n}: not JSON")
                prev = line
                continue
            want = sha256_bytes(prev) if prev is not None else GENESIS
            if rec.get("prev_sha256") != want:
                problems.append(f"line {n}: prev_sha256 does not match the line before it")
            prev = line
        return problems


# ------------------------------------------------------------------ ledger --
class Ledger:
    """Registrations keyed by prereg hash (and indexed by prereg id).  One JSON
    document rewritten atomically; the audit chain is the history."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.doc: dict[str, Any] = {"schema": SCHEMA, "seq": 0, "registrations": {}}
        if path.is_file():
            self.doc = json.loads(path.read_text(encoding="utf-8"))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.doc, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    @property
    def registrations(self) -> dict[str, dict[str, Any]]:
        return self.doc["registrations"]

    def by_id(self, prereg_id: str) -> dict[str, Any] | None:
        for r in self.registrations.values():
            if r["prereg_id"] == prereg_id:
                return r
        return None

    def next_seq(self) -> int:
        self.doc["seq"] = int(self.doc["seq"]) + 1
        return self.doc["seq"]


# ----------------------------------------------------------------- service --
@dataclass
class ServiceConfig:
    partition: str  # "a" | "b" | "c"
    root: Path  # the partition directory
    allowed_accounts: frozenset[str] = frozenset({DEFAULT_ACCOUNT})
    registered_images: frozenset[str] = frozenset()  # sha256 of allowed client images
    trust_roots: Mapping[str, str] = field(default_factory=dict)  # maintainer -> pubkey hex

    @property
    def state_dir(self) -> Path:
        return self.root / "service"

    @classmethod
    def load(cls, root: Path, partition: str) -> ServiceConfig:
        state = root / "service"
        cfg = json.loads((state / "config.json").read_text(encoding="utf-8"))
        roots = json.loads((state / "trust_roots.json").read_text(encoding="utf-8"))
        maint = (roots.get("maintainers") if isinstance(roots, Mapping) else None) or {}
        if not maint:
            raise SystemExit(f"{state / 'trust_roots.json'}: no maintainer key; refusing to start")
        return cls(
            partition=partition,
            root=root,
            allowed_accounts=frozenset(cfg.get("allowed_accounts") or [DEFAULT_ACCOUNT]),
            registered_images=frozenset(cfg.get("registered_images") or []),
            trust_roots=dict(maint),
        )


class SealedService:
    """The decision core.  Transport-free: :meth:`handle` takes a parsed request
    and the OS-reported caller, returns the response document and, for an
    allowed ``read``, an iterator of ``(file header, bytes)``."""

    def __init__(self, cfg: ServiceConfig, verify_signature: Callable | None = None) -> None:
        self.cfg = cfg
        s = cfg.state_dir
        self.audit = Audit(s / "audit.jsonl")
        self.ledger = Ledger(s / "ledger.json")
        self.pending_path = s / "checkpoint_pending.json"
        self.datasets_path = s / "datasets.json"
        self._verify_signature = verify_signature or self._ed25519
        self.reconcile()

    # -- startup -------------------------------------------------------------
    def reconcile(self) -> None:
        """Restart reconciliation: a pending checkpoint (service killed between
        write and publish) is re-announced and keeps refusing reads until the
        runner publishes it; a broken audit chain refuses to start."""
        bad = self.audit.verify()
        if bad:
            raise SystemExit("audit chain broken; refusing to serve: " + "; ".join(bad))
        if self.pending_path.is_file():
            cp = json.loads(self.pending_path.read_text(encoding="utf-8"))
            self.audit.append({"event": "restart_pending_checkpoint", "seq": cp["seq"]})
        else:
            self.audit.append({"event": "start"})

    # -- datasets ------------------------------------------------------------
    def datasets(self) -> dict[str, Any]:
        if not self.datasets_path.is_file():
            return {}
        return json.loads(self.datasets_path.read_text(encoding="utf-8"))

    def pending(self) -> dict[str, Any] | None:
        if self.pending_path.is_file():
            return json.loads(self.pending_path.read_text(encoding="utf-8"))
        return None

    # -- signature -----------------------------------------------------------
    def _ed25519(self, doc: Mapping[str, Any]) -> None:
        from iap.agents.signing import verify_ed25519

        if doc.get("signed") is not True:
            raise Refused("prereg_unsigned")
        sig = doc.get("signature")
        if not isinstance(sig, Mapping) or sig.get("scheme") != "ed25519":
            raise Refused("prereg_unsigned", "no ed25519 signature block")
        pub = self.cfg.trust_roots.get(str(sig.get("maintainer")))
        if pub is None or pub != sig.get("public_key"):
            raise Refused("prereg_untrusted_key")
        body = {k: v for k, v in doc.items() if k != "signature"}
        msg = canonical(body).encode("ascii")
        if sha256_bytes(msg) != sig.get("message_sha256") or not verify_ed25519(
            pub, msg, str(sig.get("sig", ""))
        ):
            raise Refused("prereg_signature_invalid")

    # -- the decision --------------------------------------------------------
    def _authenticate(self, caller: Caller, op: str) -> None:
        if caller.account not in self.cfg.allowed_accounts:
            raise Refused("caller_account", f"{caller.account!r} may not call this service")
        if self.cfg.registered_images:
            if caller.image_sha256 is None:
                raise Refused("caller_image_unknown", "client process image could not be read")
            if caller.image_sha256 not in self.cfg.registered_images:
                raise Refused("caller_image", "client process image is not a registered runner")
        if op == "read" and self.pending() is not None:
            raise Refused("checkpoint_pending", "publish the pending checkpoint first")

    def handle(
        self,
        request: Mapping[str, Any],
        caller: Caller,
        incoming: Iterable[tuple[Mapping[str, Any], bytes]] | None = None,
    ) -> tuple[dict[str, Any], Iterator[tuple[dict[str, Any], bytes]] | None]:
        op = str(request.get("op", ""))
        base = {"event": "request", "op": op, "account": caller.account, "pid": caller.pid}
        try:
            self._authenticate(caller, op)
            if op == "status":
                return self._status(), None
            if op == "read":
                return self._read(request, base)
            if op == "ingest":
                return self._ingest(request, base, incoming or ()), None
            if op == "publish":
                return self._publish(request, base), None
            if op == "store_result":
                return self._store_result(request, base), None
            if op == "verify_result":
                return self._verify_result(request), None
            raise Refused("unknown_op", op)
        except Refused as exc:
            self.audit.append({**base, "decision": "refused", "reason": exc.code})
            return {"ok": False, "error": exc.code, "detail": exc.detail}, None

    def _status(self) -> dict[str, Any]:
        cp = self.pending()
        return {
            "ok": True,
            "partition": self.cfg.partition,
            "seq": self.ledger.doc["seq"],
            "chain_head": self.audit.head(),
            "pending": cp,
            "registrations": len(self.ledger.registrations),
        }

    def _bound_run(self, doc: Mapping[str, Any]) -> Mapping[str, Any]:
        run = doc.get("registered_run")
        if not isinstance(run, Mapping):
            raise Refused("prereg_not_registered_run", "no registered_run block")
        return run

    def _read(self, request: Mapping[str, Any], base: dict[str, Any]):
        doc = request.get("prereg")
        if not isinstance(doc, Mapping):
            raise Refused("prereg_missing")
        self._verify_signature(doc)
        h = prereg_hash(doc)
        pid = str(doc.get("prereg_id") or "")
        base = {**base, "prereg_id": pid, "prereg_sha256": h}
        if not pid:
            raise Refused("prereg_id_missing")
        run = self._bound_run(doc)
        want = str(request.get("dataset_hash_id") or "")
        if run.get("partition") != self.cfg.partition:
            raise Refused("wrong_partition", f"prereg names partition {run.get('partition')!r}")
        if not want or run.get("dataset_hash_id") != want:
            raise Refused("dataset_not_named", "the prereg does not name the requested dataset")
        prior = self.ledger.registrations.get(h) or self.ledger.by_id(pid)
        if prior is not None:
            raise Refused(
                "already_registered",
                f"registration {prior['prereg_id']} was {prior['status']} (seq {prior['seq']}); "
                "a second read needs a new signed registration",
            )
        entry = self.datasets().get(want)
        if entry is None:
            raise Refused("dataset_unknown", "not a named dataset of this partition")
        ddir = self.cfg.root / entry["dir"]
        files: dict[str, str] = dict(entry["files"])
        # hash verification precedes any read; a mismatch is a FAILED registration (no retry)
        mismatch = [
            n
            for n, want_sha in sorted(files.items())
            if not (ddir / n).is_file() or sha256_file(ddir / n) != want_sha
        ]
        seq = self.ledger.next_seq()
        if mismatch:
            self.ledger.registrations[h] = {
                "prereg_id": pid,
                "dataset_hash_id": want,
                "status": "failed",
                "seq": seq,
                "utc": utc_now(),
            }
            self.ledger.save()
            self.audit.append(
                {**base, "decision": "failed", "reason": "dataset_hash_mismatch", "seq": seq}
            )
            raise Refused(
                "dataset_hash_mismatch", f"{len(mismatch)} file(s) differ from the record"
            )
        # write-before-serve: ledger, audit, pending checkpoint, then bytes
        self.ledger.registrations[h] = {
            "prereg_id": pid,
            "dataset_hash_id": want,
            "status": "served",
            "seq": seq,
            "utc": utc_now(),
        }
        self.ledger.save()
        head = self.audit.append({**base, "decision": "allowed", "seq": seq, "files": len(files)})
        checkpoint = {
            "seq": seq,
            "chain_head": head,
            "utc": utc_now(),
            "prereg_id": pid,
            "dataset_hash_id": want,
        }
        self.pending_path.write_text(json.dumps(checkpoint, sort_keys=True), encoding="utf-8")
        response = {
            "ok": True,
            "seq": seq,
            "prereg_sha256": h,
            "checkpoint": checkpoint,
            "files": files,
        }
        return response, self._stream(ddir, files)

    @staticmethod
    def _stream(ddir: Path, files: Mapping[str, str]) -> Iterator[tuple[dict[str, Any], bytes]]:
        for name in sorted(files):
            data = (ddir / name).read_bytes()
            yield {"file": name, "size": len(data), "sha256": files[name]}, data

    def _ingest(
        self,
        request: Mapping[str, Any],
        base: dict[str, Any],
        incoming: Iterable[tuple[Mapping[str, Any], bytes]],
    ) -> dict[str, Any]:
        """DD1: write a dataset INTO this partition (runner identity only; module docstring)."""
        want = str(request.get("dataset_hash_id") or "")
        base = {**base, "dataset_hash_id": want}
        if len(want) != 64 or any(c not in "0123456789abcdef" for c in want):
            raise Refused("dataset_hash_id_invalid", "need the 64-hex content hash")
        declared = request.get("files")
        if not isinstance(declared, Mapping) or not declared:
            raise Refused("files_missing", "the request must declare {name: sha256}")
        declared = {str(k): str(v) for k, v in declared.items()}
        if content_hash(declared) != want:
            raise Refused("dataset_hash_mismatch", "declared files do not hash to dataset_hash_id")
        if want in self.datasets():
            raise Refused("dataset_exists", "a dataset with this id is already recorded")
        counts = request.get("row_counts") or {}
        if not isinstance(counts, Mapping) or not all(
            isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in counts.values()
        ):
            raise Refused("row_counts_invalid", "row_counts must map file name to a count")
        ddir = self.cfg.root / "datasets" / want
        if ddir.exists():
            raise Refused("dataset_exists", "the dataset directory already exists")
        ddir.mkdir(parents=True)
        written: dict[str, str] = {}
        try:
            for header, data in incoming:
                name = str(header.get("file") or "")
                if not name or name in written or name not in declared:
                    raise Refused("file_unexpected", f"{name!r} is not a declared file")
                rel = Path(name)
                if rel.is_absolute() or ".." in rel.parts or not rel.parts:
                    raise Refused("file_name_invalid", name)
                sha = sha256_bytes(data)
                if sha != declared[name] or sha != header.get("sha256"):
                    raise Refused("file_hash_mismatch", name)
                dest = ddir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                written[name] = sha
            if set(written) != set(declared):
                raise Refused(
                    "files_incomplete", f"{len(written)} of {len(declared)} declared files arrived"
                )
            rec = _add_dataset(self.cfg.root, want, ddir)
            if rec["files"] != declared:
                raise Refused("dataset_hash_mismatch", "re-hash of the written files differs")
        except Refused:
            import shutil

            shutil.rmtree(ddir, ignore_errors=True)
            raise
        doc = json.loads(self.datasets_path.read_text(encoding="utf-8"))
        doc[want]["row_counts"] = {str(k): int(v) for k, v in counts.items()}
        doc[want]["ingested_utc"] = utc_now()
        self.datasets_path.write_text(json.dumps(doc, indent=1, sort_keys=True), encoding="utf-8")
        head = self.audit.append({**base, "decision": "ingested", "files": len(written)})
        return {
            "ok": True,
            "dataset_hash_id": want,
            "partition": self.cfg.partition,
            "files": dict(written),
            "row_counts": doc[want]["row_counts"],
            "chain_head": head,
        }

    def _publish(self, request: Mapping[str, Any], base: dict[str, Any]) -> dict[str, Any]:
        cp = self.pending()
        if cp is None:
            raise Refused("nothing_pending")
        commit = str(request.get("commit") or "")
        if len(commit) < 7 or not all(c in "0123456789abcdef" for c in commit):
            raise Refused("commit_invalid")
        if request.get("checkpoint") != cp:
            raise Refused("checkpoint_mismatch", "published content differs from the pending one")
        h = self._hash_for_seq(cp["seq"])
        self.ledger.registrations[h]["status"] = "published"
        self.ledger.registrations[h]["commit"] = commit
        self.ledger.save()
        self.audit.append({**base, "decision": "published", "seq": cp["seq"], "commit": commit})
        self.pending_path.unlink()
        return {"ok": True, "seq": cp["seq"]}

    def _hash_for_seq(self, seq: int) -> str:
        for h, r in self.ledger.registrations.items():
            if r["seq"] == seq:
                return h
        raise Refused("ledger_inconsistent", f"no registration for seq {seq}")

    def _store_result(self, request: Mapping[str, Any], base: dict[str, Any]) -> dict[str, Any]:
        h = str(request.get("prereg_sha256") or "")
        reg = self.ledger.registrations.get(h)
        if reg is None:
            raise Refused("unknown_registration")
        res = str(request.get("result_sha256") or "")
        if len(res) != 64:
            raise Refused("result_sha256_invalid")
        if reg.get("result_sha256") not in (None, res):
            raise Refused("result_already_stored", "a different result hash is on record")
        reg["result_sha256"] = res
        self.ledger.save()
        self.audit.append({**base, "decision": "result_stored", "seq": reg["seq"]})
        return {"ok": True, "seq": reg["seq"]}

    def _verify_result(self, request: Mapping[str, Any]) -> dict[str, Any]:
        h = str(request.get("prereg_sha256") or "")
        reg = self.ledger.registrations.get(h)
        if reg is None:
            raise Refused("unknown_registration")
        res = str(request.get("result_sha256") or "")
        return {"ok": True, "match": reg.get("result_sha256") == res, "seq": reg["seq"]}


# --------------------------------------------------------- checkpoint rules --
def checkpoint_problems(cp: Mapping[str, Any]) -> list[str]:
    """Content rule (I2): exactly the five public fields, nothing that names a
    path, host, account or credential."""
    out = []
    if set(cp) != set(CHECKPOINT_FIELDS):
        out.append(f"fields {sorted(cp)} != {sorted(CHECKPOINT_FIELDS)}")
    for k, v in cp.items():
        if k.lower() in FORBIDDEN_CHECKPOINT_KEYS:
            out.append(f"forbidden field {k}")
        s = str(v)
        if any(t in s for t in ("\\", ":/", "C:", "/home/", "iap-sealed-", "iap-runner", "@")):
            out.append(f"field {k} looks like a path, host or account: {s[:40]!r}")
    if not isinstance(cp.get("seq"), int) or cp.get("seq", 0) < 1:
        out.append("seq must be a positive integer")
    if len(str(cp.get("chain_head", ""))) != 64:
        out.append("chain_head must be a sha256")
    return out


def verify_checkpoints(path: Path) -> list[str]:
    """The committed ``research/audit/CHECKPOINTS.jsonl``: every line obeys the
    content rule and sequence numbers rise within each partition."""
    if not path.is_file():
        return []
    problems: list[str] = []
    last_seq: dict[str, int] = {}
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            problems.append(f"line {n}: not JSON")
            continue
        part = str(rec.get("partition", ""))
        cp = rec.get("checkpoint")
        if part not in ("a", "b", "c") or not isinstance(cp, Mapping):
            problems.append(f"line {n}: needs partition in a/b/c and a checkpoint object")
            continue
        problems += [f"line {n}: {p}" for p in checkpoint_problems(cp)]
        if cp.get("seq", 0) <= last_seq.get(part, 0):
            problems.append(f"line {n}: seq {cp.get('seq')} does not rise for partition {part}")
        last_seq[part] = int(cp.get("seq", 0))
    return problems


# ----------------------------------------------------------------- framing --
def write_frame(w: io.RawIOBase | Any, payload: bytes) -> None:
    w.write(_FRAME.pack(len(payload)) + payload)


def read_frame(r: io.RawIOBase | Any) -> bytes:
    head = _read_exact(r, _FRAME.size)
    (n,) = _FRAME.unpack(head)
    if n > MAX_FRAME:
        raise ValueError("frame too large")
    return _read_exact(r, n)


def _read_exact(r: Any, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = r.read(n - len(buf))
        if not chunk:
            raise EOFError("peer closed the pipe")
        buf += chunk
    return bytes(buf)


def serve_connection(service: SealedService, caller: Caller, rfile: Any, wfile: Any) -> None:
    """One request per connection: read the request frame, write the response
    frame, then (for an allowed read) header/bytes frame pairs and an end frame."""
    request = json.loads(read_frame(rfile).decode("utf-8"))
    incoming = list(_receive_files(rfile)) if request.get("op") == "ingest" else None
    response, stream = service.handle(request, caller, incoming)
    write_frame(wfile, json.dumps(response, sort_keys=True).encode("utf-8"))
    if stream is not None:
        for header, data in stream:
            write_frame(wfile, json.dumps(header, sort_keys=True).encode("utf-8"))
            write_frame(wfile, data)
        write_frame(wfile, b'{"end": true}')
    wfile.flush()


def _receive_files(rfile: Any) -> Iterator[tuple[dict[str, Any], bytes]]:
    """Header/bytes frame pairs up to the end frame (the ingest request's payload)."""
    while True:
        header = json.loads(read_frame(rfile).decode("utf-8"))
        if header.get("end"):
            return
        data = read_frame(rfile)
        if len(data) != int(header.get("size", -1)):
            raise ValueError(f"{header.get('file')}: size differs from the header")
        yield header, data


def send_files(wfile: Any, files: Iterable[tuple[str, bytes]]) -> None:
    """Client side of an ``ingest``: header/bytes frame pairs and the end frame,
    written right after the request frame."""
    for name, data in files:
        header = {"file": name, "size": len(data), "sha256": sha256_bytes(data)}
        write_frame(wfile, json.dumps(header, sort_keys=True).encode("utf-8"))
        write_frame(wfile, data)
    write_frame(wfile, b'{"end": true}')


def receive(rfile: Any) -> tuple[dict[str, Any], list[tuple[dict[str, Any], bytes]]]:
    """Client side of :func:`serve_connection`."""
    response = json.loads(read_frame(rfile).decode("utf-8"))
    files: list[tuple[dict[str, Any], bytes]] = []
    # a served read streams files after the response; an ingest response (which
    # carries "partition") lists hashes only and streams nothing
    if response.get("ok") and "files" in response and "partition" not in response:
        while True:
            header = json.loads(read_frame(rfile).decode("utf-8"))
            if header.get("end"):
                break
            data = read_frame(rfile)
            if len(data) != header["size"] or sha256_bytes(data) != header["sha256"]:
                raise ValueError(f"{header['file']}: bytes differ from the header")
            files.append((header, data))
    return response, files


# ------------------------------------------------------- Windows named pipe --
def pipe_name(partition: str) -> str:
    return PIPE_PREFIX + partition


def _win_caller(handle: int) -> Caller:  # pragma: no cover - needs Windows and a client
    """The kernel's answer about the connected client: account name via
    impersonation, process id via the pipe, image hash via a limited process query."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    if not adv.ImpersonateNamedPipeClient(wintypes.HANDLE(handle)):
        raise Refused("impersonation_failed", str(ctypes.get_last_error()))
    try:
        size = wintypes.DWORD(256)
        buf = ctypes.create_unicode_buffer(size.value)
        if not adv.GetUserNameW(buf, ctypes.byref(size)):
            raise Refused("username_unreadable", str(ctypes.get_last_error()))
        account = buf.value
    finally:
        adv.RevertToSelf()
    pid = wintypes.ULONG(0)
    k32.GetNamedPipeClientProcessId(wintypes.HANDLE(handle), ctypes.byref(pid))
    image: str | None = None
    proc = k32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if proc:
        try:
            n = wintypes.DWORD(32768)
            path = ctypes.create_unicode_buffer(n.value)
            if k32.QueryFullProcessImageNameW(proc, 0, path, ctypes.byref(n)):
                try:
                    image = sha256_file(Path(path.value))
                except OSError:
                    image = None
        finally:
            k32.CloseHandle(proc)
    return Caller(account=account, pid=pid.value or None, image_sha256=image)


def serve_pipe(service: SealedService, name: str, once: bool = False) -> None:  # pragma: no cover
    """Accept clients on the named pipe until interrupted (Windows only)."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateNamedPipeW.restype = wintypes.HANDLE
    PIPE_ACCESS_DUPLEX, PIPE_WAIT, PIPE_REJECT_REMOTE = 0x3, 0x0, 0x8
    while True:
        h = k32.CreateNamedPipeW(
            name, PIPE_ACCESS_DUPLEX, PIPE_WAIT | PIPE_REJECT_REMOTE, 1, 1 << 16, 1 << 16, 0, None
        )
        if h == wintypes.HANDLE(-1).value or not h:
            raise SystemExit(f"CreateNamedPipe failed: {ctypes.get_last_error()}")
        k32.ConnectNamedPipe(wintypes.HANDLE(h), None)
        try:
            try:
                caller = _win_caller(h)
            except Refused as exc:
                service.audit.append(
                    {"event": "connect", "decision": "refused", "reason": exc.code}
                )
                continue
            fd = msvcrt.open_osfhandle(h, os.O_RDWR | os.O_BINARY)
            with os.fdopen(fd, "r+b", buffering=0, closefd=False) as f:
                serve_connection(service, caller, f, f)
        except (EOFError, ValueError, OSError) as exc:
            service.audit.append(
                {"event": "connect", "decision": "error", "reason": type(exc).__name__}
            )
        finally:
            k32.FlushFileBuffers(wintypes.HANDLE(h))
            k32.DisconnectNamedPipe(wintypes.HANDLE(h))
            k32.CloseHandle(wintypes.HANDLE(h))
        if once:
            return


def request_over_pipe(
    name: str, request: Mapping[str, Any], files: Iterable[tuple[str, bytes]] | None = None
):  # pragma: no cover - Windows
    """Client: one request over the named pipe (``open`` on ``\\\\.\\pipe\\...`` is
    ``CreateFile``; the server reads our identity from the kernel).  ``files``
    are streamed after an ``ingest`` request."""
    with open(name, "r+b", buffering=0) as f:
        write_frame(f, json.dumps(request, sort_keys=True).encode("utf-8"))
        if request.get("op") == "ingest":
            send_files(f, files or ())
        return receive(f)


# --------------------------------------------------------------------- CLI --
def _add_dataset(root: Path, hash_id: str, directory: Path) -> dict[str, Any]:
    """Record a named dataset of this partition (run by the sealed identity after
    ingestion): the relative directory and the sha256 of every file."""
    rel = directory.resolve().relative_to(root.resolve())
    files = {
        p.relative_to(directory).as_posix(): sha256_file(p)
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }
    path = root / "service" / "datasets.json"
    doc = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    doc[hash_id] = {"dir": rel.as_posix(), "files": files}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1, sort_keys=True), encoding="utf-8")
    return doc[hash_id]


def install_state(root: Path, trust_roots: Path, *, allowed_accounts=(DEFAULT_ACCOUNT,)) -> Path:
    """Create ``<root>/service``, copy the trust roots and write ``config.json``
    (run AS the sealed identity: the partition ACL admits nobody else).  Existing
    files are kept; ``trust_roots.json`` is replaced only when its content differs."""
    state = root / "service"
    state.mkdir(parents=True, exist_ok=True)
    roots = json.loads(trust_roots.read_text(encoding="utf-8"))
    if not (roots.get("maintainers") if isinstance(roots, Mapping) else None):
        raise SystemExit(f"{trust_roots}: no maintainer key; refusing to install")
    dest = state / "trust_roots.json"
    data = trust_roots.read_bytes()
    if not dest.is_file() or dest.read_bytes() != data:
        dest.write_bytes(data)
    cfg = state / "config.json"
    if not cfg.is_file():
        cfg.write_text(
            json.dumps(
                {"allowed_accounts": list(allowed_accounts), "registered_images": []}, indent=1
            )
            + "\n",
            encoding="utf-8",
        )
    return state


def check_install(root: Path, partition: str) -> list[str]:
    """Problems with a partition's service state as THIS process sees it (run as the
    sealed identity): config, trust roots, the signing module import, audit chain."""
    problems: list[str] = []
    try:
        cfg = ServiceConfig.load(root, partition)
    except (OSError, ValueError, SystemExit) as exc:
        return [f"config: {exc}"]
    if DEFAULT_ACCOUNT not in cfg.allowed_accounts:
        problems.append(f"allowed_accounts {sorted(cfg.allowed_accounts)} lacks {DEFAULT_ACCOUNT}")
    try:
        import iap.agents.signing  # noqa: F401
    except Exception as exc:  # pragma: no cover - environment
        problems.append(f"iap.agents.signing not importable from {_SRC}: {exc}")
    problems += [f"audit: {p}" for p in Audit(cfg.state_dir / "audit.jsonl").verify()]
    try:
        probe = cfg.state_dir / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        problems.append(f"state dir not writable by this identity: {exc}")
    return problems


BACKUP_SCHEMA = "iap.sealed_backup/v1"


def backup_manifest(root: Path, partition: str) -> dict[str, Any]:
    """The per-partition backup record (run AS the partition's identity, offline):
    the sha256 of every file under ``root`` and the audit chain head.  Hashes and
    relative names only, never a byte of content; no timestamp, so the same tree
    always gives the same document (the owner's script stamps the backup set)."""
    files = {
        p.relative_to(root).as_posix(): sha256_file(p)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }
    audit = Audit(root / "service" / "audit.jsonl")
    return {
        "schema": BACKUP_SCHEMA,
        "partition": partition,
        "file_count": len(files),
        "files": files,
        "chain_head": audit.head() if audit.path.is_file() else None,
        "audit_problems": audit.verify(),
    }


def backup_problems(root: Path, manifest: Mapping[str, Any], partition: str) -> list[str]:
    """What differs between a restored (or copied) partition and its manifest:
    the audit chain must be intact and its head must equal the recorded head, and
    the file set and every sha256 must match.  Messages name files, never content."""
    if manifest.get("schema") != BACKUP_SCHEMA:
        return [f"manifest schema {manifest.get('schema')!r} != {BACKUP_SCHEMA}"]
    if manifest.get("partition") != partition:
        return [f"manifest is for partition {manifest.get('partition')!r}, not {partition!r}"]
    if manifest.get("audit_problems"):
        return ["the manifest records a broken audit chain at backup time; not a restore source"]
    now = backup_manifest(root, partition)
    problems = [f"audit: {p}" for p in now["audit_problems"]]
    if now["chain_head"] != manifest.get("chain_head"):
        problems.append(f"chain head {now['chain_head']} != manifest {manifest.get('chain_head')}")
    want: Mapping[str, str] = manifest.get("files") or {}
    have = now["files"]
    problems += [f"missing: {n}" for n in sorted(set(want) - set(have))]
    problems += [f"unexpected: {n}" for n in sorted(set(have) - set(want))]
    problems += [
        f"sha256 differs: {n}" for n in sorted(set(want) & set(have)) if want[n] != have[n]
    ]
    return problems


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the service for one partition (as its sealed identity)")
    s.add_argument("--partition", required=True, choices=("a", "b", "c"))
    s.add_argument("--root", type=Path, required=True, help=r"the partition, e.g. C:\iap-sealed\b")
    s.add_argument("--once", action="store_true", help="serve one connection and exit")
    v = sub.add_parser("verify", help="verify the local audit chain and ledger (sealed identity)")
    v.add_argument("--root", type=Path, required=True)
    c = sub.add_parser("verify-checkpoints", help="check a committed checkpoint file (anyone)")
    c.add_argument(
        "path", type=Path, nargs="?", default=REPO / "research" / "audit" / "CHECKPOINTS.jsonl"
    )
    d = sub.add_parser(
        "add-dataset", help="record a named dataset's files and hashes (sealed identity)"
    )
    d.add_argument("--root", type=Path, required=True)
    d.add_argument("--hash-id", required=True)
    d.add_argument("--dir", type=Path, required=True)
    i = sub.add_parser(
        "install",
        help="create <root>/service, copy trust roots, write config.json (sealed identity)",
    )
    i.add_argument("--root", type=Path, required=True)
    i.add_argument(
        "--trust-roots", type=Path, default=REPO / "research" / "agents" / "trust_roots.json"
    )
    k = sub.add_parser("check", help="verify a partition's service state as this identity")
    k.add_argument("--partition", required=True, choices=("a", "b", "c"))
    k.add_argument("--root", type=Path, required=True)
    g = sub.add_parser(
        "ping", help="send one status request over the pipe; exit 0 if the service answered at all"
    )
    g.add_argument("--partition", required=True, choices=("a", "b", "c"))
    m = sub.add_parser(
        "backup-manifest",
        help="write the partition's sha256-per-file + chain-head record (partition identity)",
    )
    m.add_argument("--partition", required=True, choices=("a", "b", "c", "runner"))
    m.add_argument("--root", type=Path, required=True)
    m.add_argument("--out", type=Path, required=True, help="outside the partition")
    vb = sub.add_parser(
        "verify-backup",
        help="check a copied or restored partition against its manifest (partition identity)",
    )
    vb.add_argument("--partition", required=True, choices=("a", "b", "c", "runner"))
    vb.add_argument("--root", type=Path, required=True)
    vb.add_argument("--manifest", type=Path, required=True)
    a = ap.parse_args(list(argv) if argv is not None else None)
    if a.cmd == "backup-manifest":
        if a.out.resolve().is_relative_to(a.root.resolve()):
            print("refused: --out must be outside the partition", file=sys.stderr)
            return 2
        doc = backup_manifest(a.root, a.partition)
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        # counts and the chain head only: the head is a hash, never content
        print(
            json.dumps(
                {
                    "partition": a.partition,
                    "files": doc["file_count"],
                    "chain_head": doc["chain_head"],
                    "audit_ok": not doc["audit_problems"],
                }
            )
        )
        return 1 if doc["audit_problems"] else 0
    if a.cmd == "verify-backup":
        manifest = json.loads(a.manifest.read_text(encoding="utf-8"))
        problems = backup_problems(a.root, manifest, a.partition)
        for p in problems:
            print(f"MISMATCH: {p}", file=sys.stderr)
        if problems:
            return 1
        print(f"partition {a.partition}: {manifest['file_count']} files and chain head match")
        return 0
    if a.cmd == "serve":
        if sys.platform != "win32":
            raise SystemExit("the named-pipe transport is Windows only (single-machine design)")
        cfg = ServiceConfig.load(a.root, a.partition)
        serve_pipe(SealedService(cfg), pipe_name(a.partition), once=a.once)
        return 0
    if a.cmd == "install":
        state = install_state(a.root, a.trust_roots)
        print(f"installed: {state}")
        return 0
    if a.cmd == "check":
        problems = check_install(a.root, a.partition)
        for p in problems:
            print(f"PROBLEM: {p}", file=sys.stderr)
        if problems:
            return 1
        print(f"partition {a.partition}: service state readable and writable by this identity")
        return 0
    if a.cmd == "ping":
        # Any answer proves the pipe is served: a caller other than iap-runner gets
        # the audited refusal `caller_account`, which is the expected reply for the owner.
        try:
            resp, _ = request_over_pipe(pipe_name(a.partition), {"op": "status"})
        except OSError as exc:
            print(f"no service on {pipe_name(a.partition)}: {exc}", file=sys.stderr)
            return 1
        print(json.dumps({"answered": True, "ok": resp.get("ok"), "error": resp.get("error")}))
        return 0
    if a.cmd == "verify":
        problems = Audit(a.root / "service" / "audit.jsonl").verify()
        for p in problems:
            print(f"BROKEN: {p}", file=sys.stderr)
        if problems:
            return 1
        print("audit chain intact")
        return 0
    if a.cmd == "verify-checkpoints":
        problems = verify_checkpoints(a.path)
        for p in problems:
            print(f"BAD: {p}", file=sys.stderr)
        if problems:
            return 1
        print(f"checkpoints ok: {a.path}")
        return 0
    if a.cmd == "add-dataset":
        rec = _add_dataset(a.root, a.hash_id, a.dir)
        print(json.dumps({"hash_id": a.hash_id, "files": len(rec["files"])}))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
