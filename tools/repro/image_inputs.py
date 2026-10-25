"""The reproduction image's build inputs and their one hash (v2.0 stage 3 P1).

    python tools/repro/image_inputs.py            # list the input files
    python tools/repro/image_inputs.py --hash     # one sha256 over all of them

The inputs are ``deployment/docker/Dockerfile.repro`` itself plus every build-
context path its ``COPY`` lines name, read from the Dockerfile so the list
cannot drift from it. The hash is stored on the built image as the OCI label
``org.iap.repro.inputs_sha256``; ``.github/workflows/repro-image.yml`` rebuilds
and re-publishes the image only when the label of the image named by
``deployment/docker/IMAGE_DIGEST`` differs from this hash (an image build is not
bit-reproducible, so "same inputs" is decided here, not by rebuilding).
Files hash LF-normalised (a CRLF checkout is not a different image).
"""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = "deployment/docker/Dockerfile.repro"
LABEL = "org.iap.repro.inputs_sha256"
_COPY = re.compile(r"^COPY\s+(?!--from)(.+)$", re.M)


def input_files(root: Path = ROOT) -> list[str]:
    text = (root / DOCKERFILE).read_text(encoding="utf-8")
    rels = {DOCKERFILE}
    for m in _COPY.finditer(text):
        parts = [p for p in m.group(1).split() if not p.startswith("--")]
        rels.update(parts[:-1])  # the last token is the destination
    return sorted(rels)


def _norm_bytes(path: Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def inputs_hash(root: Path = ROOT) -> str:
    h = hashlib.sha256()
    for rel in input_files(root):
        p = root / rel
        if not p.is_file():
            raise FileNotFoundError(f"{DOCKERFILE} copies {rel}, which does not exist")
        h.update(rel.encode() + b"\n" + hashlib.sha256(_norm_bytes(p)).hexdigest().encode() + b"\n")
    return h.hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--hash", action="store_true", help="print the one sha256, not the list")
    a = ap.parse_args(argv)
    if a.hash:
        print(inputs_hash(a.root))
    else:
        print("\n".join(input_files(a.root)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
