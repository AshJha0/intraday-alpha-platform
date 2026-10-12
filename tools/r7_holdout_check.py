"""Pre-ingest check for one R7 holdout gz: bytes + sha256 match the SIGNED prereg,
free disk >= 25 GB.

Usage::

    python -I tools/r7_holdout_check.py <gz> <data_dir> [--repo ROOT] [--allow-draft]

The repo root defaults to ``<data_dir>/../..`` (data/real -> repo). By default the
signed ``research/r7/r7_confirm_prereg*.json`` (non-DRAFT, ``signed: true`` with a
``signature`` block) is required; exactly one must exist. ``--allow-draft`` falls
back to ``r7_confirm_prereg_DRAFT.json`` with a loud warning (never for the real
confirmatory look). Prints ``PRECHECK OK`` / ``PRECHECK FAIL`` and exits 0 / 1.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

MIN_FREE_GB = 25.0


class PrecheckError(Exception):
    pass


def _is_signed(doc: dict) -> bool:
    return doc.get("signed") is True and isinstance(doc.get("signature"), dict)


def find_prereg(repo: Path, allow_draft: bool = False) -> tuple[Path, dict]:
    r7 = repo / "research" / "r7"
    signed = []
    for p in sorted(r7.glob("r7_confirm_prereg*.json")):
        if "DRAFT" in p.name:
            continue
        doc = json.loads(p.read_text(encoding="utf-8"))
        if _is_signed(doc):
            signed.append((p, doc))
    if len(signed) > 1:
        names = ", ".join(p.name for p, _ in signed)
        raise PrecheckError(f"several signed preregs match: {names}")
    if signed:
        return signed[0]
    if not allow_draft:
        raise PrecheckError(
            f"no signed r7_confirm_prereg*.json in {r7} (use --allow-draft only for dry runs)"
        )
    draft = r7 / "r7_confirm_prereg_DRAFT.json"
    if not draft.exists():
        raise PrecheckError(f"no signed prereg and no draft at {draft}")
    print(f"WARNING: using UNSIGNED draft prereg {draft.name} (--allow-draft)", file=sys.stderr)
    return draft, json.loads(draft.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 24), b""):
            h.update(b)
    return h.hexdigest()


def check(
    gz: Path,
    data_dir: Path,
    repo: Path,
    allow_draft: bool = False,
    min_free_gb: float = MIN_FREE_GB,
) -> str:
    src, pre = find_prereg(repo, allow_draft)
    entry = next((f for f in pre["holdout_files"]["files"] if f["file"] == gz.name), None)
    if not entry:
        raise PrecheckError(f"{gz.name} not in {src.name}")
    if gz.stat().st_size != entry["bytes"]:
        raise PrecheckError(f"size mismatch {gz.name}")
    if _sha256(gz) != entry["sha256"]:
        raise PrecheckError(f"sha256 mismatch {gz.name}")
    free = shutil.disk_usage(data_dir).free / 1e9
    if free < min_free_gb:
        raise PrecheckError(f"free disk {free:.1f} GB < {min_free_gb:g}")
    return f"PRECHECK OK {gz.name} bytes+sha256 match ({src.name}), free {free:.1f} GB"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("gz", type=Path)
    ap.add_argument("data_dir", type=Path)
    ap.add_argument("--repo", type=Path, default=None)
    ap.add_argument("--allow-draft", action="store_true")
    a = ap.parse_args(argv)
    repo = a.repo or a.data_dir.resolve().parent.parent
    try:
        print(check(a.gz, a.data_dir, repo, a.allow_draft))
    except (PrecheckError, OSError, KeyError, ValueError) as e:
        print(f"PRECHECK FAIL {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
