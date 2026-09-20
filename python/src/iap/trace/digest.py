"""``TraceDigest`` — the replay-determinism digest of a trace stream.

Specification (every port reproduces it with one hash and the canonical
JSON rules of ``iap.contracts.versions.canonical_json``):

* For each trace, in emission order, the line is
  ``canonical_json(trace.to_dict())`` — keys sorted recursively, separators
  ``","`` / ``":"``, no whitespace, non-ASCII escaped as ``\\uXXXX``, ints
  as decimal, floats as the shortest round-trip repr (Python
  ``float.__repr__``: ``0.00042``, ``1e-05``, ``2.5``, ``1e+16``), enums as
  their wire values, ``null`` / ``true`` / ``false``.
* The bytes hashed are the ASCII encoding of that line followed by one
  ``"\\n"`` (0x0A).  Lines are concatenated with nothing else in between.
* ``hexdigest()`` is the lowercase SHA-256 hex of everything hashed so far.
  The digest of an empty stream is SHA-256 of zero bytes.

Because the line is exactly what :class:`~iap.trace.sinks.JsonlTraceSink`
writes, the digest of a live run equals the digest of its JSONL file
(:meth:`TraceDigest.of_jsonl`), and the same seed replays to the same
digest.  A change to any field of any trace changes the digest.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Union

from iap.contracts.types import DecisionTrace
from iap.contracts.versions import canonical_json

__all__ = ["TraceDigest", "trace_line"]


def trace_line(trace: DecisionTrace) -> str:
    """The canonical JSON line of ``trace`` (without the newline)."""
    return canonical_json(trace.to_dict())


class TraceDigest:
    """Streaming SHA-256 over canonical trace lines (see module doc)."""

    def __init__(self) -> None:
        self._hash = hashlib.sha256()
        self._count = 0

    @property
    def count(self) -> int:
        """Number of traces folded in so far."""
        return self._count

    def update(self, trace: DecisionTrace) -> "TraceDigest":
        """Fold one trace in."""
        self.update_line(trace_line(trace))
        return self

    def update_line(self, line: str) -> "TraceDigest":
        """Fold one canonical line in (the line must already be canonical:
        this is what a port that reads a JSONL file does)."""
        self._hash.update(line.encode("ascii"))
        self._hash.update(b"\n")
        self._count += 1
        return self

    def hexdigest(self) -> str:
        """Lowercase SHA-256 hex of the stream so far (non-destructive)."""
        return self._hash.hexdigest()

    @classmethod
    def of_jsonl(cls, path: Union[str, Path]) -> "TraceDigest":
        """The digest of a trace JSONL file.  Each line is re-canonicalised
        (parsed and re-serialised) so a pretty-printed file digests to the
        same value as the stream that produced it; a line that is not a
        valid trace document raises ``ValueError``."""
        digest = cls()
        with open(path, "r", encoding="ascii") as fh:
            for lineno, raw in enumerate(fh, 1):
                text = raw.strip()
                if not text:
                    continue
                try:
                    trace = DecisionTrace.from_dict(json.loads(text))
                except ValueError as exc:
                    raise ValueError(f"{path}:{lineno}: not a DecisionTrace ({exc})") from exc
                digest.update(trace)
        return digest
