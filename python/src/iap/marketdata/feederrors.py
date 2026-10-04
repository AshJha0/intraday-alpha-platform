"""Typed errors of the real-data readers (docs/REAL_DATA.md).

Every reader failure is one of these — never a bare ``struct.error`` /
``EOFError`` / ``IndexError`` and never a silent truncation.  All of them are
``ValueError`` (PLATFORM_CONVENTIONS.md §8) and carry the position of the
problem in the input: a byte ``offset`` for binary feeds, a 1-based ``line``
for text feeds.
"""

from __future__ import annotations


class FeedFormatError(ValueError):
    """Malformed vendor input. ``offset`` (bytes) or ``line`` locates it."""

    def __init__(self, message: str, *, offset: int | None = None, line: int | None = None):
        where = ""
        if offset is not None:
            where = f" at byte offset {offset}"
        elif line is not None:
            where = f" at line {line}"
        super().__init__(f"{message}{where}")
        self.offset = offset
        self.line = line


class FeedTruncatedError(FeedFormatError):
    """The input ends in the middle of a message / record."""


class IngestError(ValueError):
    """The ingest request cannot be satisfied (bad arguments, a symbol that
    is not in the file, a dataset directory that holds a different universe,
    an off-tick displayed price under a forced tick size)."""


class BookDivergenceError(ValueError):
    """The reconstructed book differs from the vendor's own book.

    ``report`` is the verification document, ``report["first_divergence"]``
    names the message, side and level.
    """

    def __init__(self, message: str, report: dict):
        super().__init__(message)
        self.report = report
