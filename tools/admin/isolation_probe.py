#!/usr/bin/env python3
"""Identity x partition access probe (v2.0 stage 2, D2; U0 section 2).

Run AS one identity (the development user directly; the sealed and runner
accounts through ``python/tests/isolation/test_identity_partitions.py``'s
runas-style harness).  For every partition under ``--root`` it tries five
things and reports ``ok`` / ``denied`` / ``missing`` for each, as one JSON
document on stdout:

  read       open and read one byte of a file in the partition (the first
             file found, else the probe's own marker when the identity could
             write one)
  write      create and remove ``probe-<identity>.tmp``
  enumerate  list the directory
  report     the indirect route a research script takes: build a report path
             under the partition (``<partition>/reports/<name>.md``), create
             the parents and write it
  cache      the indirect route a cache takes: ``<partition>/cache/`` as a
             cache directory, create it and write a cache file

Nothing is read beyond one byte and nothing stays behind: every file the
probe could create is removed.  It never prints file contents or listings,
only outcomes (no sealed byte leaves the partition through this tool).
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

PARTITIONS = ("a", "b", "c", "runner")
OPERATIONS = ("read", "write", "enumerate", "report", "cache")


def identity() -> str:
    return os.environ.get("USERNAME") or getpass.getuser()


def _outcome(fn) -> str:
    try:
        fn()
        return "ok"
    except PermissionError:
        return "denied"
    except FileNotFoundError:
        return "missing"
    except OSError as exc:  # access denied surfaces as WinError 5 on some calls
        if getattr(exc, "winerror", None) in (5, 32, 1314):
            return "denied"
        return f"error:{type(exc).__name__}"


def _cleanup(p: Path) -> None:
    try:
        if p.is_file():
            p.unlink()
        elif p.is_dir():
            for child in p.iterdir():
                _cleanup(child)
            p.rmdir()
    except OSError:
        pass


def probe_partition(part: Path, who: str) -> dict[str, str]:
    out: dict[str, str] = {}
    marker = part / f"probe-{who}.tmp"

    def enumerate_() -> None:
        os.listdir(part)

    def write() -> None:
        try:
            marker.write_bytes(b"probe")
        finally:
            _cleanup(marker)

    def read() -> None:
        try:
            target = next((p for p in part.iterdir() if p.is_file()), None)
        except PermissionError:
            target = None
        if target is not None:
            with target.open("rb") as f:
                f.read(1)
            return
        # nothing to read yet (an empty or unlistable partition): the probe's own
        # marker is the read target; a denied write is a denied read as well
        try:
            marker.write_bytes(b"probe")
            with marker.open("rb") as f:
                f.read(1)
        finally:
            _cleanup(marker)

    def report() -> None:
        p = part / "reports" / f"probe-{who}.md"
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("# probe\n", encoding="utf-8")
        finally:
            _cleanup(p)
            _cleanup(p.parent)

    def cache() -> None:
        d = part / "cache"
        f = d / f"probe-{who}.bin"
        try:
            d.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"\0")
        finally:
            _cleanup(f)
            _cleanup(d)

    out["enumerate"] = _outcome(enumerate_)
    out["write"] = _outcome(write)
    out["read"] = _outcome(read)
    out["report"] = _outcome(report)
    out["cache"] = _outcome(cache)
    return out


def run(root: Path, partitions=PARTITIONS) -> dict:
    who = identity()
    return {
        "identity": who,
        "root": str(root),
        "partitions": {p: probe_partition(root / p, who) for p in partitions},
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=Path(r"C:\iap-sealed"))
    ap.add_argument("--partitions", default=",".join(PARTITIONS))
    ap.add_argument("--out", type=Path, default=None, help="also write the JSON here")
    a = ap.parse_args(argv)
    doc = run(a.root, tuple(x for x in a.partitions.split(",") if x))
    text = json.dumps(doc, indent=1, sort_keys=True)
    if a.out is not None:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(text, encoding="utf-8")
    sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
