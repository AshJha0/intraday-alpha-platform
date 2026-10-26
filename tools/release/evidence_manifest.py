"""Release evidence manifest (v2.0 U14 / Z1, U0 stage 12): every claim the
release notes make, with its evidence class, the hashes of what supports it,
its prereg and its runner record.

    python tools/release/evidence_manifest.py build --claims <claims.json> --out <manifest.json>
    python tools/release/evidence_manifest.py check --manifest <manifest.json>
    python tools/release/evidence_manifest.py notes --manifest <manifest.json>

The claims file lists ``{"release": ..., "claims": [...]}``; each claim has
``id``, ``claim`` (the sentence the notes print), ``evidence_class`` (one of
the seven classes of U0 section 10) and the references its class requires:

=========================  ==================================================
class                      requires
=========================  ==================================================
independently confirmed    ``prereg`` (signed), ``runner_log``,
                           ``result_hashes`` (the runner's ``hashes.json``,
                           whose ``prereg_sha256`` is the prereg's), and
                           ``custody`` naming an approver other than the
                           developer (U0 section 2 item 6)
mechanically isolated      the same without ``custody``; the runner record's
                           own ``evidence_class`` must say so
rejected                   either the registered set above (a registered
                           single-shot rejection) or ``artefacts``
development-supported      ``artefacts`` (at least one file)
exploratory                ``artefacts``
data-ineligible            ``reason`` and ``artefacts``
pending                    ``reason``; no file is cited
=========================  ==================================================

Every cited file is hashed (sha256 of the bytes with line endings normalised
for text, raw bytes otherwise) and the prereg also by its canonical hash, the
one the runner and the sealed service key on.  ``check`` re-hashes every file
of a built manifest and lists each drift; ``notes`` prints the claims table,
negatives first (rejected, data-ineligible, pending, exploratory,
development-supported, then the registered classes).  The builder refuses
(exit 2, nothing written) on the first claim that does not meet its class;
it reads no data and never signs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

TOOLS = Path(__file__).resolve().parents[1]
REPO = TOOLS.parent
for _p in (str(TOOLS), str(REPO / "python" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from runner.verify import prereg_hash  # noqa: E402

# U0 section 10, in the order the release notes print them (negatives first).
CLASSES = (
    "rejected",
    "data-ineligible",
    "pending",
    "exploratory",
    "development-supported",
    "mechanically isolated",
    "independently confirmed",
)
REGISTERED = ("mechanically isolated", "independently confirmed")


class ManifestRefused(ValueError):
    """A claim does not carry what its evidence class requires."""


def file_sha256(path: Path) -> str:
    data = path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return hashlib.sha256(data).hexdigest()
    return hashlib.sha256(
        text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    ).hexdigest()


def _cite(root: Path, rel: Any, cid: str, what: str) -> dict[str, str]:
    if not isinstance(rel, str) or not rel:
        raise ManifestRefused(f"{cid}: {what} must be a repo-relative path")
    p = root / rel
    if not p.is_file():
        raise ManifestRefused(f"{cid}: {what} {rel} does not exist")
    return {"path": Path(rel).as_posix(), "sha256": file_sha256(p)}


def _registered(root: Path, c: Mapping[str, Any], cid: str, cls: str) -> dict[str, Any]:
    for k in ("prereg", "runner_log", "result_hashes"):
        if not c.get(k):
            raise ManifestRefused(f"{cid}: class '{cls}' needs {k}")
    prereg = _cite(root, c["prereg"], cid, "prereg")
    doc = json.loads((root / c["prereg"]).read_text(encoding="utf-8"))
    if doc.get("signed") is not True or not isinstance(doc.get("signature"), Mapping):
        raise ManifestRefused(f"{cid}: prereg {c['prereg']} is not signed")
    prereg["canonical_sha256"] = prereg_hash(doc)
    hashes = _cite(root, c["result_hashes"], cid, "result_hashes")
    rec = json.loads((root / c["result_hashes"]).read_text(encoding="utf-8"))
    if rec.get("prereg_sha256") != prereg["canonical_sha256"]:
        raise ManifestRefused(f"{cid}: runner record names another prereg than {c['prereg']}")
    # the runner writes "mechanically isolated" under sole custody; a registered
    # rejection carries the custody class its run had
    if cls != "independently confirmed" and rec.get("evidence_class") != "mechanically isolated":
        raise ManifestRefused(
            f"{cid}: runner record's class is {rec.get('evidence_class')!r}, not 'mechanically isolated'"
        )
    out = {
        "prereg": prereg,
        "runner_log": _cite(root, c["runner_log"], cid, "runner_log"),
        "result_hashes": hashes,
        "runner_record": {
            k: rec.get(k)
            for k in (
                "prereg_id",
                "seq",
                "result_sha256",
                "result_commit",
                "dataset_hash_id",
                "image_digest",
                "commit",
            )
            if k in rec
        },
    }
    if cls == "independently confirmed":
        cu = c.get("custody")
        if (
            not isinstance(cu, Mapping)
            or not cu.get("approver")
            or not cu.get("developer")
            or cu["approver"] == cu["developer"]
        ):
            raise ManifestRefused(
                f"{cid}: 'independently confirmed' needs custody with an approver other than the developer "
                "(under sole custody the class is 'mechanically isolated')"
            )
        out["custody"] = {"approver": cu["approver"], "developer": cu["developer"]}
    return out


def build_claim(root: Path, c: Mapping[str, Any]) -> dict[str, Any]:
    cid = str(c.get("id") or "")
    if not cid:
        raise ManifestRefused("a claim has no id")
    if not str(c.get("claim") or "").strip():
        raise ManifestRefused(f"{cid}: empty claim text")
    cls = c.get("evidence_class")
    if cls not in CLASSES:
        raise ManifestRefused(f"{cid}: evidence class {cls!r} is not one of {list(CLASSES)}")
    out: dict[str, Any] = {"id": cid, "claim": str(c["claim"]).strip(), "evidence_class": cls}
    arts = c.get("artefacts") or []
    if cls in REGISTERED or (cls == "rejected" and c.get("prereg")):
        out.update(_registered(root, c, cid, cls))
    elif cls == "pending":
        if arts or c.get("prereg") or c.get("runner_log"):
            raise ManifestRefused(f"{cid}: a pending claim cites no evidence")
    elif not arts:
        raise ManifestRefused(f"{cid}: class '{cls}' needs at least one artefact")
    if cls in ("pending", "data-ineligible"):
        if not str(c.get("reason") or "").strip():
            raise ManifestRefused(f"{cid}: class '{cls}' needs a reason")
    if c.get("reason"):
        out["reason"] = str(c["reason"]).strip()
    if arts:
        out["artefacts"] = [_cite(root, a, cid, "artefact") for a in arts]
    return out


def build(root: Path, claims_doc: Mapping[str, Any]) -> dict[str, Any]:
    claims = claims_doc.get("claims")
    if not isinstance(claims, list) or not claims:
        raise ManifestRefused("the claims file lists no claims")
    built = [build_claim(Path(root), c) for c in claims]
    ids = [c["id"] for c in built]
    if len(set(ids)) != len(ids):
        raise ManifestRefused(
            f"duplicate claim ids: {sorted({i for i in ids if ids.count(i) > 1})}"
        )
    built.sort(key=lambda c: (CLASSES.index(c["evidence_class"]), c["id"]))
    return {
        "x-version": 1,
        "kind": "release_evidence_manifest",
        "release": str(claims_doc.get("release") or ""),
        "classes": {k: sum(c["evidence_class"] == k for c in built) for k in CLASSES},
        "claims": built,
    }


def check(root: Path, manifest: Mapping[str, Any]) -> list[str]:
    """Every cited file's current hash against the manifest; empty when nothing drifted."""
    drift = []
    for c in manifest.get("claims", []):
        cites = [c[k] for k in ("prereg", "runner_log", "result_hashes") if k in c] + list(
            c.get("artefacts", [])
        )
        for ref in cites:
            p = Path(root) / ref["path"]
            if not p.is_file():
                drift.append(f"{c['id']}: {ref['path']} is missing")
            elif file_sha256(p) != ref["sha256"]:
                drift.append(f"{c['id']}: {ref['path']} changed since the manifest was built")
    return drift


def notes(manifest: Mapping[str, Any]) -> str:
    """The release notes' claims table, negatives first, one row per claim."""
    rows = ["| # | claim | evidence class | prereg | runner record |", "|---|---|---|---|---|"]
    for c in manifest["claims"]:
        pre = f"`{c['prereg']['path']}`" if "prereg" in c else "none"
        run = (
            f"seq {c['runner_record'].get('seq')}, result `{str(c['runner_record'].get('result_sha256'))[:12]}`"
            if "runner_record" in c
            else "none"
        )
        text = c["claim"].replace("|", "\\|") + (f" ({c['reason']})" if c.get("reason") else "")
        rows.append(f"| {c['id']} | {text} | {c['evidence_class']} | {pre} | {run} |")
    return "\n".join(rows) + "\n"


def _dump(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--root", type=Path, default=REPO)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--claims", required=True, type=Path)
    b.add_argument("--out", type=Path)
    for name in ("check", "notes"):
        s = sub.add_parser(name)
        s.add_argument("--manifest", required=True, type=Path)
    a = ap.parse_args(argv)
    if a.cmd == "build":
        try:
            m = build(a.root, json.loads(a.claims.read_text(encoding="utf-8")))
        except ManifestRefused as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2
        if a.out:
            a.out.parent.mkdir(parents=True, exist_ok=True)
            with a.out.open("w", encoding="ascii", newline="\n") as fh:
                fh.write(_dump(m))
        else:
            sys.stdout.write(_dump(m))
        return 0
    m = json.loads(a.manifest.read_text(encoding="utf-8"))
    if a.cmd == "notes":
        sys.stdout.write(notes(m))
        return 0
    drift = check(a.root, m)
    for d in drift:
        print(d)
    print("manifest matches the tree" if not drift else f"{len(drift)} drift(s)")
    return 1 if drift else 0


if __name__ == "__main__":
    raise SystemExit(main())
