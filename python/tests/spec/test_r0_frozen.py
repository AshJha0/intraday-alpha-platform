"""PR R0 (v2.0 stage 1, session A): the confirmatory test specification is frozen.

``python/tests/spec/CONFIRMATORY_TEST_SPEC.md`` is the reviewer-approved R0
revision 4 (freeze decision 2026-10-24) committed byte for byte behind a path
header block.  This test strips that header block (everything up to and
including the ``END R0 FREEZE HEADER -->`` line) and asserts that the sha256 of
the remainder is the frozen hash.  A change to the frozen text is a new R0
revision and needs a new freeze decision; it is not a reason to edit the hash
here.  Nothing in this file reads data or imports the gate.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

SPEC = Path(__file__).resolve().parent / "CONFIRMATORY_TEST_SPEC.md"
FROZEN_SHA256 = "900d9e73e1aca92482ab24de384b9ac02c30d24321f17db0ffff02b8f947690e"
HEADER_START = b"<!-- R0 FREEZE HEADER"
HEADER_END = b"END R0 FREEZE HEADER -->\r\n"


def _split() -> tuple[bytes, bytes]:
    data = SPEC.read_bytes()
    assert data.startswith(HEADER_START), "the path header block must open the file"
    end = data.index(HEADER_END) + len(HEADER_END)
    return data[:end], data[end:]


def test_frozen_text_hash_is_the_reviewer_freeze_hash() -> None:
    _header, frozen = _split()
    assert hashlib.sha256(frozen).hexdigest() == FROZEN_SHA256


def test_frozen_bytes_are_not_normalised_by_git() -> None:
    # The freeze copy carries CRLF line endings; .gitattributes marks the file
    # -text so a checkout on any platform reproduces the frozen bytes.
    _header, frozen = _split()
    assert frozen.count(b"\r\n") == 252
    assert b"\n" not in frozen.replace(b"\r\n", b"")
    assert frozen.startswith(b"# R0: confirmatory inference test specification (DRAFT revision 4")


def test_header_block_names_hash_decision_and_revision_rule() -> None:
    header, _frozen = _split()
    text = header.decode("ascii")
    assert FROZEN_SHA256 in text
    assert re.search(r"approved for freeze by the outside reviewer on 2026-10-24", text)
    assert "NEW R0 revision" in text and "new reviewer freeze decision" in text
    assert "python/tests/spec/CONFIRMATORY_TEST_SPEC.md" in text
    # The header is the only text outside the frozen bytes.
    assert header.count(HEADER_START) == 1 and header.endswith(HEADER_END)
