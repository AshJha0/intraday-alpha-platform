"""E0 commit (v2.0 stage 5): the H1 economic specification is frozen.

``research/v119/H1_ALPHA_IMPROVEMENT_SPEC.md`` is E0 revision 6 committed byte
for byte behind a path header block.  This test strips the header (everything
up to and including the ``END E0 FREEZE HEADER -->`` line) and asserts that the
sha256 of the remainder is the frozen hash.  A change to the frozen text is a
new E0 revision with a new freeze decision; it is never a reason to edit the
hash here.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from conftest import REPO_ROOT

SPEC = REPO_ROOT / "research" / "v119" / "H1_ALPHA_IMPROVEMENT_SPEC.md"
FROZEN_SHA256 = "e1099e33464c797687297fea75ac34f7bbdab817181f8b66028837751ef0f8b1"
HEADER_START = b"<!-- E0 FREEZE HEADER"
HEADER_END = b"END E0 FREEZE HEADER -->\r\n"


def _split() -> tuple[bytes, bytes]:
    data = SPEC.read_bytes()
    assert data.startswith(HEADER_START), "the path header block must open the file"
    end = data.index(HEADER_END) + len(HEADER_END)
    return data[:end], data[end:]


def test_frozen_text_hash_is_the_e0_revision_6_hash() -> None:
    _header, frozen = _split()
    assert hashlib.sha256(frozen).hexdigest() == FROZEN_SHA256


def test_frozen_bytes_are_not_normalised_by_git() -> None:
    _header, frozen = _split()
    assert frozen.count(b"\r\n") == 527
    assert b"\n" not in frozen.replace(b"\r\n", b"")
    assert frozen.startswith(b"# H1 economic specification (E0, DRAFT revision 6")


def test_header_block_names_hash_path_and_revision_rule() -> None:
    header, _frozen = _split()
    text = header.decode("ascii")
    assert FROZEN_SHA256 in text
    assert "research/v119/H1_ALPHA_IMPROVEMENT_SPEC.md" in text
    assert "NEW E0 revision" in text and "new freeze decision" in text
    assert header.count(HEADER_START) == 1 and header.endswith(HEADER_END)
    attrs = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "research/v119/H1_ALPHA_IMPROVEMENT_SPEC.md -text" in attrs
