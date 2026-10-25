#!/usr/bin/env python3
"""Result artefacts kept outside the tree (v2.0 stage 11, S1).

``research/v118/RESULT_ARTEFACTS.json`` lists every result file over 1 MB
that was moved out of the repository to a GitHub release asset, with the
sha256 its result document quotes.  This tool:

    python tools/result_artefacts.py list
    python tools/result_artefacts.py check            # local copies (if any) match; exit 1 on a mismatch
    python tools/result_artefacts.py fetch <path>     # gh release download + sha256 check; exit 1 on mismatch

``fetch`` needs the GitHub CLI (``gh``) and network; ``check`` and ``list``
need neither.  A mismatch deletes nothing and prints both digests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "research" / "v118" / "RESULT_ARTEFACTS.json"
REPO = "AshJha0/intraday-alpha-platform"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def load(path: Path = MANIFEST) -> list[dict]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("x-version") != 1:
        raise ValueError(f"{path}: unsupported x-version {doc.get('x-version')!r}")
    out = []
    for e in doc["artefacts"]:
        for key in ("path", "sha256", "bytes", "release", "asset"):
            if key not in e:
                raise ValueError(f"{path}: artefact missing {key!r}: {e}")
        if not _HEX64.match(e["sha256"]):
            raise ValueError(f"{path}: {e['path']}: sha256 is not 64 lowercase hex")
        if not isinstance(e["bytes"], int) or e["bytes"] <= 0:
            raise ValueError(f"{path}: {e['path']}: bytes must be a positive int")
        out.append(e)
    return out


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check(entries: list[dict], root: Path = ROOT) -> int:
    rc = 0
    for e in entries:
        p = root / e["path"]
        if not p.is_file():
            print(f"absent   {e['path']} (release {e['release']} asset {e['asset']})")
            continue
        got = sha256_of(p)
        if got == e["sha256"] and p.stat().st_size == e["bytes"]:
            print(f"OK       {e['path']}")
        else:
            rc = 1
            print(
                f"MISMATCH {e['path']}: sha256 {got} ({p.stat().st_size} bytes) != manifest {e['sha256']} ({e['bytes']} bytes)"
            )
    return rc


def fetch(entries: list[dict], rel_path: str, root: Path = ROOT) -> int:
    match = [e for e in entries if e["path"] == rel_path]
    if not match:
        print(f"not in the manifest: {rel_path}")
        return 2
    e = match[0]
    dest = root / e["path"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "gh",
        "release",
        "download",
        e["release"],
        "--repo",
        REPO,
        "--pattern",
        e["asset"],
        "--dir",
        str(dest.parent),
        "--clobber",
    ]
    print(" ".join(cmd))
    subprocess.run(cmd, check=True)
    downloaded = dest.parent / e["asset"]
    if downloaded != dest:
        downloaded.replace(dest)
    return check([e], root)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("check")
    f = sub.add_parser("fetch")
    f.add_argument("path")
    a = ap.parse_args(argv)
    entries = load()
    if a.cmd == "list":
        for e in entries:
            print(
                f"{e['path']}  {e['bytes']} bytes  sha256 {e['sha256']}  -> release {e['release']} / {e['asset']}"
            )
        return 0
    if a.cmd == "check":
        return check(entries)
    return fetch(entries, a.path)


if __name__ == "__main__":
    sys.exit(main())
